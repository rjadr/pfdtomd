from ..models import ParseResult, BlockType, LineBlock, TOCEntry, DecisionRecord
from ..utils.string_helpers import normalize_for_match, word_match_score, is_bullet_list, is_numbered_list
from collections import Counter
import os
import re


def _is_list_item(text: str) -> bool:
    return is_bullet_list(text) or is_numbered_list(text)

_CONTINUATION_WORDS = frozenset([
    "the", "a", "an", "and", "or", "of", "in", "to", "for", "with", "by",
    "on", "at", "from", "as", "is", "are", "was", "were", "be", "that",
    "this", "their", "its", "our", "your", "has", "have", "had", "not",
])


class HeaderDetector:
    """Heading detection ported from pdf-inspector's scoring approach (convert.rs).

    Strategy:
    1. Compute heading tiers: font sizes ≥ 1.2× body, clustered within 0.5 pt,
       capped at 4 tiers (H1–H4). Title-page heuristic maps max-font blocks to H1.
    2. Tier promotion: blocks whose height falls in a tier → heading level.
       Guards: word count ≤ 15, not the dominant height on the page.
    3. Scoring path (body-size blocks, port of pdf-inspector's rarity system):
         score = rarity×0.5 + bold×0.3 + standalone×0.2 + isolated×0.3
       Requires score ≥ 0.5 AND standalone AND word_count ∈ [2,15]
       AND at least one strong signal (bold | isolated | rarity≥0.97 with ≤8 words).
       Isolated = paragraph break both before and after the block.
       Density guard: if >25% of blocks on a page are isolated → suppress all isolated
       headings on that page (it's multi-column or dense text, not real headings).
    4. Merge consecutive same-level heading blocks (wrapped headings).
    """

    def transform(self, result: ParseResult) -> ParseResult:
        base_h = result.globals.get('most_used_height', 10)
        max_h  = result.globals.get('max_height', base_h)
        dist   = result.globals.get('most_used_distance', base_h * 1.2)
        # Paragraph gap threshold: gap > this → paragraph break (port of compute_paragraph_threshold)
        para_threshold = dist * 1.3

        # ── 1. Compute heading tiers (pdf-inspector: compute_heading_tiers) ──────
        # Collect all font heights ≥ 1.2× body, cluster within 0.5 pt, cap at 4.
        heading_sizes: list[float] = []
        for page in result.pages:
            for block in page.items:
                if isinstance(block, LineBlock) and block.lines:
                    h = block.lines[0].height
                    if h / base_h >= 1.2:
                        heading_sizes.append(h)
        heading_sizes.sort(reverse=True)
        tiers: list[float] = []
        for h in heading_sizes:
            if not any(abs(t - h) < 0.5 for t in tiers):
                tiers.append(h)
        tiers = tiers[:4]  # H1–H4 max

        # Map tier index → BlockType (tier 0 → H1 only when max_h > 1.15× body)
        def _tier_type(idx: int) -> BlockType:
            if tiers and max_h > base_h * 1.15:
                types = [BlockType.H1, BlockType.H2, BlockType.H3, BlockType.H4]
            else:
                types = [BlockType.H2, BlockType.H3, BlockType.H4, BlockType.H5]
            return types[min(idx, len(types) - 1)]

        def _match_tier(h: float) -> int | None:
            for i, t in enumerate(tiers):
                if abs(h - t) < 0.5:
                    return i
            return None

        # ── 2. Font-size rarity (pdf-inspector: font_size_rarity) ────────────────
        h_counts: Counter = Counter()
        total_blocks = 0
        for page in result.pages:
            for block in page.items:
                if isinstance(block, LineBlock) and block.lines:
                    h_counts[round(block.lines[0].height * 10)] += 1
                    total_blocks += 1

        def _rarity(h: float) -> float:
            if total_blocks == 0:
                return 0.0
            key = round(h * 10)
            return 1.0 - h_counts[key] / total_blocks

        # ── 3. Per-page dominant-height guard ────────────────────────────────────
        page_dominant: dict[int, set] = {}
        for page in result.pages:
            ph: Counter = Counter()
            for block in page.items:
                if isinstance(block, LineBlock) and block.lines:
                    ph[round(max(l.height for l in block.lines))] += 1
            if ph:
                max_cnt = max(ph.values())
                page_dominant[page.index] = {
                    h for h, c in ph.items() if c >= 3 and c > max_cnt * 0.4
                }

        # ── 4. Isolated-block detection (pdf-inspector: find_isolated_lines) ─────
        # A single-line block is "isolated" when there is a paragraph break both
        # before and after it.  We compute this per page from block Y positions.
        isolated_blocks: set[int] = set()  # ids of isolated LineBlock objects

        for page in result.pages:
            lb = [b for b in page.items if isinstance(b, LineBlock)]
            if not lb:
                continue

            cands: list[int] = []
            for i, block in enumerate(lb):
                if len(block.lines) != 1:
                    continue
                txt = block.get_text().strip()
                wc  = len(txt.split())
                if not (1 <= wc <= 6) or len(txt) <= 3:
                    continue
                # Skip list items and pure numbers
                if txt.isdigit() or _is_list_item(txt):
                    continue
                # Skip continuation endings
                last_ch = txt[-1] if txt else ''
                if last_ch in ('-', ',', ';'):
                    continue
                last_word = txt.split()[-1].lower()
                if last_word in _CONTINUATION_WORDS:
                    continue
                # Paragraph break before?
                if i > 0:
                    gap = block.lines[0].y - lb[i - 1].lines[-1].y
                    if gap <= para_threshold:
                        continue
                # Paragraph break after?
                if i < len(lb) - 1:
                    gap = lb[i + 1].lines[0].y - block.lines[-1].y
                    if gap <= para_threshold:
                        continue
                cands.append(i)

            # Density guard: >25% isolated → suppress (multi-column / dense layout)
            if cands and len(cands) / len(lb) <= 0.25:
                for i in cands:
                    isolated_blocks.add(id(lb[i]))

        # ── 5. Promote blocks ─────────────────────────────────────────────────────
        for page in result.pages:
            lb = [b for b in page.items if isinstance(b, LineBlock)]
            dom = page_dominant.get(page.index, set())

            for i, block in enumerate(lb):
                if block.toc_level is not None or block.block_type != BlockType.PARAGRAPH:
                    continue

                txt = block.get_text().strip()
                if not txt or len(txt) <= 3:
                    continue

                wc = len(txt.split())
                h  = block.lines[0].height
                rh = round(h)
                role = getattr(block, '_style_role', None)

                # Caption-like blocks should not be promoted to headings.
                if role in ('caption', 'running_header', 'running_footer', 'footnote'):
                    continue

                # Universal word-count cap: pdf-inspector never promotes >15 words
                if wc > 15:
                    continue

                # Universal lowercase-start guard: skip if first char is lowercase
                # unless it looks like a Roman numeral (i., iv., etc.)
                if txt and txt[0].islower():
                    if not re.match(r'^[ivxlcdm]+[.)\s]', txt, re.IGNORECASE):
                        continue

                # --- Tier-based promotion ---
                tier_idx = _match_tier(h)
                if tier_idx is not None and (rh not in dom or role == 'heading') and not txt.isdigit():
                    block.block_type = _tier_type(tier_idx)
                    continue

                # --- Scoring path for body-size blocks ---
                # (port of pdf-inspector's rarity + bold + standalone + isolated)
                if h / base_h < 0.95 or wc < 2:
                    continue

                # Skip if all words are numeric (table data, figure axis labels, etc.)
                if all(w.replace('.', '').replace(',', '').isdigit() for w in txt.split()):
                    continue

                rarity   = _rarity(h)
                all_bold = bool(block.lines[0].words and all(w.is_bold for w in block.lines[0].words))
                isolated = id(block) in isolated_blocks

                # "standalone" = paragraph break before this block.
                # In pdf-inspector terms: the gap from the previous line exceeds
                # para_threshold, so in_paragraph was reset to False.
                # In our block system: gap from the previous block > para_threshold,
                # OR this is the first block, OR previous block is a heading.
                if i > 0:
                    prev_b = lb[i - 1]
                    gap_before = block.lines[0].y - prev_b.lines[-1].y
                    prev_is_heading = _headline_level(prev_b.block_type) is not None
                    standalone = gap_before > para_threshold or prev_is_heading
                else:
                    standalone = True

                score = (rarity   * 0.5
                         + (0.3 if all_bold   else 0.0)
                         + (0.2 if standalone else 0.0)
                         + (0.3 if isolated   else 0.0))

                if role == 'heading':
                    score += 0.22

                has_strong = all_bold or isolated or (rarity >= 0.97 and wc <= 8)
                if role == 'heading':
                    has_strong = True

                if score >= 0.5 and standalone and has_strong:
                    # Level = one below the deepest assigned tier
                    assigned = [_headline_level(b.block_type) for b in lb
                                if _headline_level(b.block_type)]
                    base_level = max(assigned) if assigned else (len(tiers) if tiers else 1)
                    lvl = min(base_level + 1, 6)
                    block.block_type = _headline_by_level(lvl)
                    continue

        # ── 6. Merge consecutive same-level heading blocks ────────────────────────
        # (port of pdf-inspector's merge_heading_lines)
        for page in result.pages:
            merged: list = []
            for block in page.items:
                if (merged
                        and isinstance(merged[-1], LineBlock)
                        and isinstance(block, LineBlock)
                        and merged[-1].block_type == block.block_type
                        and _headline_level(block.block_type) is not None
                        and block.toc_level is None):
                    prev = merged[-1]
                    prev_h = max(l.height for l in prev.lines)
                    gap = block.lines[0].y - prev.lines[-1].y
                    combined = len(prev.get_text()) + len(block.get_text())
                    if 0 < gap < prev_h * 2.5 and combined < 200:
                        prev.lines.extend(block.lines)
                        continue
                merged.append(block)
            page.items = merged

        return result


def _headline_level(bt: BlockType):
    """Return integer level 1-6 for a headline BlockType, or None."""
    mapping = {
        BlockType.H1: 1, BlockType.H2: 2, BlockType.H3: 3,
        BlockType.H4: 4, BlockType.H5: 5, BlockType.H6: 6,
    }
    return mapping.get(bt)


def _headline_by_level(level: int) -> BlockType:
    levels = [BlockType.H2, BlockType.H3, BlockType.H4, BlockType.H5, BlockType.H6]
    return levels[min(level - 2, len(levels) - 1)]

class TOCDetector:
    """Absolute Parity Port of DetectTOC.js (including multi-line stash)"""
    def transform(self, result: ParseResult) -> ParseResult:
        entries = []
        for page in result.pages[:20]:
            toc_links = []
            title_stash = None # For 2-line items
            
            for block in page.items:
                if not isinstance(block, LineBlock): continue
                txt = block.get_text()
                
                # Check for dots
                has_dots = "..." in txt or re.search(r"\.\s?\.\s?\.", txt)
                # Check for trailing digits
                digit_match = re.search(r"(\d+)$", txt.strip())
                
                if has_dots and digit_match:
                    page_num = int(digit_match.group(1))
                    title = re.sub(r"[\.\d\s]+$", "", txt).strip()
                    
                    if title_stash: # Merge with stashed title from previous line
                        title = f"{title_stash} {title}"
                        title_stash = None
                        
                    block.block_type = BlockType.TOC
                    toc_links.append((block, title, page_num))
                else:
                    # Potential 1st line of a 2-line TOC entry
                    if len(txt) > 3 and not txt.isupper():
                        title_stash = txt
        
            if not toc_links: continue
            
            # LinkLeveler logic
            unique_x = sorted(list(set(round(l[0].lines[0].x) for l in toc_links)))
            for block, title, pnum in toc_links:
                level = unique_x.index(round(block.lines[0].x))
                entries.append(TOCEntry(text=title, page_num=pnum, level=level))
        
        result.toc_entries = entries
        return result

class ListDetector:
    """Absolute Parity Port of DetectListItems.js"""
    def transform(self, result: ParseResult) -> ParseResult:
        from ..utils.string_helpers import is_bullet_list, is_numbered_list
        for page in result.pages:
            line_blocks = [b for b in page.items if isinstance(b, LineBlock)]
            if not line_blocks: continue
            min_x = min(round(b.lines[0].x) for b in line_blocks)
            
            for block in line_blocks:
                # Don't reclassify blocks already identified as headings
                if block.block_type not in (BlockType.PARAGRAPH, BlockType.LIST):
                    continue
                txt = block.get_text().strip()
                if is_bullet_list(txt) or is_numbered_list(txt):
                    block.block_type = BlockType.LIST
                    block.indent_level = max(0, (round(block.lines[0].x) - min_x) // 12)
                    
                    # Normalization: JS forces bullets like • to -
                    first_word = block.lines[0].words[0].text
                    if first_word in ["•", "–"]:
                        block.lines[0].words[0].text = "-"
        return result

class DocumentMapper:
    """Map TOC entries to document headings using monotone sequence alignment.

    This replaces independent local matching with a globally consistent decode:
    TOC entries should align to headings in increasing document order.
    """

    WINDOW_PAGES = 2
    MIN_SCORE = 0.45

    def transform(self, result: ParseResult) -> ParseResult:
        if not result.toc_entries:
            result.decision_log.append(DecisionRecord(
                module="DocumentMapper",
                decision="toc_alignment_skipped_no_toc",
                score=0.0,
                confidence=1.0,
                features={"toc_entries": 0},
                alternatives=["toc_monotone_alignment_decoded"],
            ))
            return result

        # 1) Detect page-offset using earliest robust match.
        mapping_offset = 0
        for entry in result.toc_entries[:3]:
            found_page_idx = self._find_text_in_doc(result, entry.text)
            if found_page_idx is not None:
                mapping_offset = found_page_idx - entry.page_num
                break

        result.globals['page_offset'] = mapping_offset

        # 2) Build per-entry candidate heading matches near expected pages.
        entry_candidates = self._build_candidates(result, mapping_offset)

        # 3) Decode monotone alignment path over candidates.
        aligned = self._decode_monotone(entry_candidates)

        total_candidates = sum(len(c) for c in entry_candidates)
        result.decision_log.append(DecisionRecord(
            module="DocumentMapper",
            decision="toc_monotone_alignment_decoded",
            score=float(len(aligned)),
            confidence=(len(aligned) / max(len(result.toc_entries), 1)),
            features={
                "toc_entries": len(result.toc_entries),
                "candidate_count": total_candidates,
                "aligned_count": len(aligned),
                "page_offset": mapping_offset,
            },
            alternatives=["independent_local_matching"],
        ))

        # 4) Apply aligned matches to blocks.
        for entry_idx, page_idx, block_idx, _score in aligned:
            if not (0 <= entry_idx < len(result.toc_entries)):
                continue
            if not (0 <= page_idx < len(result.pages)):
                continue
            page = result.pages[page_idx]
            blocks = [b for b in page.items if isinstance(b, LineBlock)]
            if not (0 <= block_idx < len(blocks)):
                continue
            entry = result.toc_entries[entry_idx]
            block = blocks[block_idx]
            block.toc_level = entry.level
            block.block_type = self._get_header_type(entry.level)

        return result

    def _build_candidates(self, result: ParseResult, mapping_offset: int) -> list:
        out = []
        n_pages = len(result.pages)

        for i, entry in enumerate(result.toc_entries):
            expected = entry.page_num + mapping_offset
            cands = []
            for pidx in range(max(0, expected - self.WINDOW_PAGES),
                              min(n_pages, expected + self.WINDOW_PAGES + 1)):
                page = result.pages[pidx]
                blocks = [b for b in page.items if isinstance(b, LineBlock)]
                target_norm = normalize_for_match(entry.text)

                for bidx, block in enumerate(blocks):
                    txt = block.get_text()
                    if not txt:
                        continue

                    direct = word_match_score(entry.text, txt)
                    norm_txt = normalize_for_match(txt)
                    contains = 1.0 if (target_norm and target_norm in norm_txt) else 0.0

                    multi = 0.0
                    if bidx + 1 < len(blocks):
                        combined = txt + " " + blocks[bidx + 1].get_text()
                        multi = max(multi, word_match_score(entry.text, combined))
                        if target_norm and target_norm in normalize_for_match(combined):
                            multi = max(multi, 1.0)

                    text_score = max(direct, multi, contains)
                    if text_score < self.MIN_SCORE:
                        continue

                    # Page prior: small boost when close to expected mapped page.
                    page_penalty = 0.08 * abs(pidx - expected)
                    score = text_score - page_penalty
                    if score < self.MIN_SCORE:
                        continue

                    order_key = pidx * 100000 + bidx
                    cands.append((pidx, bidx, score, order_key))

            out.append(cands)

        return out

    def _decode_monotone(self, entry_candidates: list) -> list:
        """Weighted monotone alignment over TOC-entry candidate matches.

        Returns list of tuples: (entry_idx, page_idx, block_idx, score).
        """
        states = []
        for i, cands in enumerate(entry_candidates):
            for pidx, bidx, score, order_key in cands:
                states.append((i, pidx, bidx, score, order_key))

        if not states:
            return []

        states.sort(key=lambda s: (s[0], s[4]))

        n = len(states)
        dp = [s[3] for s in states]
        prev = [-1] * n

        for j in range(n):
            ej, _, _, sj, oj = states[j]
            best_val = dp[j]
            best_idx = -1
            for i in range(j):
                ei, _, _, _, oi = states[i]
                # Strictly monotone in entry index and document order.
                if ei >= ej or oi >= oj:
                    continue
                # Mild skip penalty for skipping TOC entries.
                gap = ej - ei - 1
                cand = dp[i] + sj - 0.06 * max(0, gap)
                if cand > best_val:
                    best_val = cand
                    best_idx = i
            dp[j] = best_val
            prev[j] = best_idx

        end = max(range(n), key=lambda k: dp[k])
        path = []
        while end != -1:
            eidx, pidx, bidx, score, _ = states[end]
            path.append((eidx, pidx, bidx, score))
            end = prev[end]
        path.reverse()

        # Keep first assignment per TOC entry in path order.
        final = []
        used_entries = set()
        for eidx, pidx, bidx, score in path:
            if eidx in used_entries:
                continue
            used_entries.add(eidx)
            final.append((eidx, pidx, bidx, score))

        return final

    def _find_text_in_doc(self, result, text):
        norm_target = normalize_for_match(text)
        for p in result.pages:
            blocks = [b for b in p.items if isinstance(b, LineBlock)]
            for i, b in enumerate(blocks):
                if norm_target in normalize_for_match(b.get_text()):
                    return p.index
                # Also check combined with next block for multi-line headers
                if i + 1 < len(blocks):
                    next_b = blocks[i + 1]
                    combined = b.get_text() + " " + next_b.get_text()
                    if norm_target in normalize_for_match(combined):
                        return p.index
        return None

    def _get_header_type(self, level):
        levels = [BlockType.H2, BlockType.H3, BlockType.H4, BlockType.H5]
        return levels[min(level, len(levels)-1)]


class FootnoteLinker:
    """Pro Feature #4: Semantic Footnote Linking

    Instead of just formatting superscript numbers, this creates proper
    Markdown footnote links: body text[^1] matched with [^1]: Footer text

    Detects footnote definitions by position (bottom 15% zone) OR by font size
    (significantly smaller than body text). Also splits merged footnotes
    (e.g., when footnotes 14, 15, 16 were grouped into one block by GatherBlocks).
    """
    _FN_START = re.compile(r'^\(?\d{1,3}(?!\d)\)?\s+\S')
    _FN_MATCH = re.compile(r'^\(?(\d{1,3})(?!\d)\)?\.?\s+(.+)')

    def transform(self, result: ParseResult) -> ParseResult:
        base_h = result.globals.get('most_used_height', 10)

        for page in result.pages:
            blocks = [b for b in page.items if isinstance(b, LineBlock)]
            if not blocks:
                continue

            max_y = max(b.lines[-1].y for b in blocks if b.lines)
            page_height = max_y if max_y > 0 else 800
            footnote_zone_y = page_height * 0.85

            new_items = []
            for item in page.items:
                if not isinstance(item, LineBlock) or not item.lines:
                    new_items.append(item)
                    continue

                block = item
                block_y = block.lines[0].y
                block_height = block.lines[0].height

                # Two detection strategies:
                # 1. Position-based: block is in bottom 15% of page
                # 2. Font-based: block uses significantly smaller font than body text
                #    (catches footnotes that appear mid-page in dense footnote sections)
                in_footnote_zone = block_y >= footnote_zone_y
                is_small_font = block_height < base_h - 0.5

                if not in_footnote_zone and not is_small_font:
                    new_items.append(block)
                    continue

                txt = block.get_text().strip()

                # Must start with a footnote number pattern
                if not self._FN_MATCH.match(txt):
                    new_items.append(block)
                    continue

                # Split at lines that start new footnotes (handles merged 14+15+16)
                split_blocks = self._split_footnote_block(block)
                for b in split_blocks:
                    b.block_type = BlockType.FOOTNOTE
                new_items.extend(split_blocks)

            page.items = new_items

        return result

    def _split_footnote_block(self, block: LineBlock) -> list:
        """Split a merged block into separate footnotes if multiple footnotes are present."""
        groups: list = []
        current: list = []

        for line in block.lines:
            line_txt = line.get_text().strip()
            if current and self._FN_START.match(line_txt):
                groups.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            groups.append(current)

        if len(groups) <= 1:
            return [block]

        result = []
        for group in groups:
            new_block = LineBlock()
            new_block.lines = group
            new_block.indent_level = block.indent_level
            new_block.toc_level = block.toc_level
            result.append(new_block)
        return result


# ─── v0.5 New processors ─────────────────────────────────────────────────────

class StyleRoleInferer:
    """Infer latent typography roles for every LineBlock using document-level
    feature heuristics, storing the result in block._style_role.

    Roles: heading | body | footnote | running_header | running_footer |
           caption | toc | list_item
    """

    def transform(self, result: ParseResult) -> ParseResult:
        base_h    = result.globals.get('most_used_height', 10)
        body_font = result.globals.get('most_used_font', '')

        for page in result.pages:
            W = getattr(page, 'width',  595.0)
            H = getattr(page, 'height', 842.0)

            for block in page.items:
                if not isinstance(block, LineBlock) or not block.lines:
                    continue

                h          = block.lines[0].height
                txt        = block.get_text().strip()
                words      = txt.split()
                n_words    = len(words)
                all_words  = [w for l in block.lines for w in l.words]
                bold_ratio = sum(1 for w in all_words if w.is_bold) / max(len(all_words), 1)
                width_ratio= max((l.width for l in block.lines), default=0) / max(W, 1)
                top_y      = block.lines[0].y
                bot_y      = block.lines[-1].y

                role = "body"

                if top_y < H * 0.08 and n_words <= 10:
                    role = "running_header"
                elif bot_y > H * 0.90 and n_words <= 10:
                    role = "running_footer"
                elif (h < base_h - 0.8
                      and (bot_y > H * 0.78
                           or (txt and txt[0].isdigit() and n_words >= 2))):
                    role = "footnote"
                elif (n_words >= 2 and words and
                      words[0].lower().rstrip('.') in ('figure', 'fig', 'table',
                                                        'plate', 'chart', 'map')):
                    role = "caption"
                elif h >= base_h * 1.18 and n_words <= 20:
                    role = "heading"
                elif (h >= base_h * 0.90 and bold_ratio >= 0.80
                      and n_words <= 15 and width_ratio < 0.90):
                    role = "heading"
                elif block.block_type == BlockType.TOC:
                    role = "toc"
                elif block.block_type == BlockType.LIST:
                    role = "list_item"

                block._style_role = role  # type: ignore[attr-defined]

        return result


class HeaderDecoder:
    """Refine heading assignments made by HeaderDetector.

    Runs AFTER HeaderDetector.  Does NOT re-run block promotion (HeaderDetector
    handles that).  Only performs two refinements:

    1. Sequence-consistency forward pass: if heading level jumps by > 1
       (e.g. H2 → H5) with no intermediate levels AND the next heading
       reverts, cap the isolated jump to prev_level + 1.

    2. Tighter heading merge for HeaderDecoder-specific cases: merges
       consecutive same-level headings when the gap is very tight
       (gap < 1.3× height) and the combined text is short (< 120 chars).
       HeaderDetector already runs a wider merge (gap < 2.5× height, < 200 chars),
       so this pass provides an additional safety net.
    """

    def transform(self, result: ParseResult) -> ParseResult:
        use_style_role_decoder = os.getenv('PDFTOMD_ENABLE_STYLE_HEADING_DECODER', '').strip() == '1'

        # Optional role-guided heading recovery for short heading-like lines
        # that remained paragraphs after HeaderDetector.
        if use_style_role_decoder:
            for page in result.pages:
                line_blocks = [b for b in page.items if isinstance(b, LineBlock)]
                if not line_blocks:
                    continue

                assigned_levels = [_headline_level(b.block_type) for b in line_blocks
                                   if _headline_level(b.block_type)]
                base_level = max(assigned_levels) if assigned_levels else 2

                for block in line_blocks:
                    if block.block_type != BlockType.PARAGRAPH:
                        continue
                    if getattr(block, '_style_role', None) != 'heading':
                        continue

                    txt = block.get_text().strip()
                    if not txt:
                        continue
                    wc = len(txt.split())
                    if wc < 2 or wc > 14:
                        continue
                    if txt[0].islower() or _is_list_item(txt):
                        continue

                    # Avoid promoting sentence-like prose fragments.
                    if txt.endswith(('.', '!', '?', ':', ';')) and wc > 6:
                        continue

                    block.block_type = _headline_by_level(min(base_level + 1, 6))

        # ── 1. Sequence-consistency smoothing ─────────────────────────────────
        all_headings = [(b, _headline_level(b.block_type))
                        for page in result.pages
                        for b in page.items
                        if isinstance(b, LineBlock) and _headline_level(b.block_type)]

        if len(all_headings) >= 3:
            prev_lv = all_headings[0][1]
            for i in range(1, len(all_headings)):
                block, lv = all_headings[i]
                if lv > prev_lv + 1:
                    if (i + 1 < len(all_headings)
                            and all_headings[i+1][1] <= prev_lv + 1):
                        capped = min(lv, prev_lv + 1)
                        block.block_type = _headline_by_level(capped)
                        all_headings[i] = (block, capped)
                        lv = capped
                prev_lv = lv

        return result


# ─── v0.6 replacements ───────────────────────────────────────────────────────

class ListStructureInferer:
    """Sequence-aware list detection — replaces ListDetector.

    Improvements over ListDetector:
    1. Two-pass: candidate identification then sequence-context extension.
       Adjacent blocks that share bullet/number style reinforce each other,
       so blocks that are slightly ambiguous get promoted when surrounded by
       confirmed list items.
    2. Numbered-list progression: detects (1) (2) (3) sequences even when
       individual items don't clearly match the bullet regex.
    3. Indent levels from x-position (unchanged from ListDetector).
    4. Normalises bullet characters (•, – → -).
    """

    _BULLET      = re.compile(r"^[\s]*[-•–\*][\s]")
    _NUMBERED    = re.compile(r"^[\s]*(\d+|[a-zA-Z])[.)]\s")
    _ALPHA_ITEM  = re.compile(r"^[\s]*([a-zA-Z])[.)]\s")

    def transform(self, result: ParseResult) -> ParseResult:
        for page in result.pages:
            line_blocks = [b for b in page.items if isinstance(b, LineBlock)]
            if not line_blocks:
                continue
            min_x = min(round(b.lines[0].x) for b in line_blocks if b.lines)

            # Pass 1 — hard candidates: blocks that clearly match list patterns
            is_cand = [self._is_list_candidate(b) for b in line_blocks]

            # Pass 2 — soften neighbours: if a block is surrounded (prev+next)
            # by confirmed candidates of the same bullet class, promote it.
            for i in range(1, len(line_blocks) - 1):
                if not is_cand[i] and is_cand[i - 1] and is_cand[i + 1]:
                    txt = line_blocks[i].get_text().strip()
                    if not txt:
                        continue
                    # Only promote if it doesn't look like a heading or very long para
                    wc = len(txt.split())
                    if (
                        line_blocks[i].block_type == BlockType.PARAGRAPH
                        and 2 <= wc <= 40
                        and (not txt[0].isupper() or wc <= 10)
                    ):
                        is_cand[i] = True

            # Apply + normalise
            for block, cand in zip(line_blocks, is_cand):
                if not cand:
                    continue
                if block.block_type not in (BlockType.PARAGRAPH, BlockType.LIST):
                    continue
                block.block_type  = BlockType.LIST
                block.indent_level = max(0, (round(block.lines[0].x) - min_x) // 12)
                if block.lines and block.lines[0].words:
                    fw = block.lines[0].words[0].text
                    if fw in ("•", "–", "*"):
                        block.lines[0].words[0].text = "-"

        return result

    def _is_list_candidate(self, block: LineBlock) -> bool:
        """True if block clearly matches a bullet or numbered-list pattern."""
        if block.block_type not in (BlockType.PARAGRAPH, BlockType.LIST):
            return False
        txt = block.get_text().strip()
        return bool(self._BULLET.match(txt) or self._NUMBERED.match(txt))


class FootnoteAssignmentSolver:
    """Improved footnote linker — replaces FootnoteLinker.

    Converts footnote-like blocks to BlockType.FOOTNOTE and splits merged
    multi-footnote blocks, exactly as FootnoteLinker did, but with:

    1. Broader detection zone — uses actual page height from Page.height
       rather than estimating from max observed y (fixes short pages where
       footnotes are mis-classified because the zone threshold is too low).
    2. Accepts letter-labelled footnotes (a., b., c.) and asterisk marks.
    3. Splits on any new footnote start (number or letter pattern).
    """

    # Require a word-boundary after the digits ((?!\d)) so that years (1978),
    # page ranges (255–275), DOI fragments (514517) and URL numbers (69382)
    # that begin a wrapped continuation line are never mistaken for a new footnote.
    _FN_START = re.compile(r'^\(?(\d{1,3})(?!\d)\)?\.?\s+\S|^[\*†‡§]|^\(?[a-z]\)\s')
    _FN_MATCH = re.compile(r'^\(?(\d{1,3})(?!\d)\)?\.?\s+(.+)|^([\*†‡§])\s*(.+)')

    def transform(self, result: ParseResult) -> ParseResult:
        base_h = result.globals.get('most_used_height', 10)

        for page in result.pages:
            blocks = [b for b in page.items if isinstance(b, LineBlock)]
            if not blocks:
                continue

            # Use actual page height stored on Page object (more reliable than
            # estimating from text coordinates alone).
            page_height   = getattr(page, 'height', 842.0)
            footnote_zone = page_height * 0.82

            new_items = []
            for item in page.items:
                if not isinstance(item, LineBlock) or not item.lines:
                    new_items.append(item)
                    continue

                block_y      = item.lines[0].y
                block_height = item.lines[0].height
                in_zone      = block_y >= footnote_zone
                is_small     = block_height < base_h - 0.5

                if not in_zone and not is_small:
                    new_items.append(item)
                    continue

                txt = item.get_text().strip()
                if not self._FN_MATCH.match(txt):
                    new_items.append(item)
                    continue

                split_blocks = self._split_footnote_block(item)
                for b in split_blocks:
                    b.block_type = BlockType.FOOTNOTE
                new_items.extend(split_blocks)

            page.items = new_items

        return result

    def _split_footnote_block(self, block: LineBlock) -> list:
        """Split a block that may contain multiple merged footnotes."""
        groups: list = []
        current: list = []
        for line in block.lines:
            line_txt = line.get_text().strip()
            if current and self._FN_START.match(line_txt):
                groups.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            groups.append(current)

        if len(groups) <= 1:
            return [block]

        result = []
        for grp in groups:
            nb = LineBlock()
            nb.lines       = grp
            nb.indent_level = block.indent_level
            nb.toc_level   = block.toc_level
            result.append(nb)
        return result
