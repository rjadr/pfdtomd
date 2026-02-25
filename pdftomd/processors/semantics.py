from ..models import ParseResult, BlockType, LineBlock, TOCEntry
from ..utils.string_helpers import normalize_for_match, word_match_score
import re

class HeaderDetector:
    """Header detection ported to match pdf2md's DetectHeaders.js logic.

    Strategy (without TOC):
    1. Title-page heuristic: any PAGE containing the document's max font-size is a
       "title page". Items above the size threshold get H1/H2.
    2. Distinct-heights pass: collect every unique height > body-height across all
       pages, sort descending, assign H2/H3/H4/H5 in order. This mirrors pdf2md's
       approach and avoids fixed multipliers that miss section headers.
    3. Paragraph-height caps pass: items at body height, different font, ALL-CAPS
       get the next header level (e.g., "INTRODUCTION" as a same-size-but-allcaps header).
    4. Style Signature: same-height items whose font/color differs from body text
       and that look like headers (short, single-line, bold or colored).
    """
    def transform(self, result: ParseResult) -> ParseResult:
        base_h = result.globals.get('most_used_height', 10)
        max_h = result.globals.get('max_height', base_h)
        primary = result.globals.get('primary_style', {})
        body_font = primary.get('font', '')

        # --- Title-page heuristic (mirrors findPagesWithMaxHeight in pdf2md) ---
        # Any page that contains an item at max_height is a "title page".
        title_page_threshold = base_h + ((max_h - base_h) / 4)
        title_pages = set()
        for page in result.pages:
            for block in page.items:
                if isinstance(block, LineBlock):
                    if any(l.height == max_h for l in block.lines):
                        title_pages.add(page.index)

        for page in result.pages:
            if page.index not in title_pages:
                continue
            for block in page.items:
                if not isinstance(block, LineBlock) or block.toc_level is not None:
                    continue
                if block.block_type != BlockType.PARAGRAPH:
                    continue
                h = max(l.height for l in block.lines)
                txt = block.get_text().strip()
                if h >= title_page_threshold:
                    # Skip pure numbers — they're chapter/section numbers, not headings
                    if txt.isdigit():
                        continue
                    block.block_type = BlockType.H1 if h == max_h else BlockType.H2

        # --- Distinct-heights pass (mirrors pdf2md's no-TOC height ranking) ---
        # Collect unique rounded heights > base_h that aren't already assigned
        all_heights = set()
        for page in result.pages:
            for block in page.items:
                if isinstance(block, LineBlock):
                    for line in block.lines:
                        rh = round(line.height)
                        if rh > base_h:
                            all_heights.add(rh)
        # Remove max_h — already handled by title-page heuristic
        all_heights.discard(round(max_h))
        # Sort descending: largest height → H2, next → H3, etc.
        sorted_heights = sorted(all_heights, reverse=True)

        level_map = {}
        for i, h in enumerate(sorted_heights[:4]):  # Cap at H5
            level_map[h] = [BlockType.H2, BlockType.H3, BlockType.H4, BlockType.H5][i]

        if level_map:
            for page in result.pages:
                for block in page.items:
                    if not isinstance(block, LineBlock) or block.toc_level is not None:
                        continue
                    if block.block_type != BlockType.PARAGRAPH:
                        continue
                    h = round(max(l.height for l in block.lines))
                    if h in level_map:
                        txt = block.get_text().strip()
                        if not txt.isdigit():  # Skip pure numbers
                            block.block_type = level_map[h]

        # --- Paragraph-height ALL-CAPS headers (pdf2md's final pass) ---
        # Items at body height, different font, ALL-CAPS → next header level after assigned ones
        smallest_level = 1
        for page in result.pages:
            for block in page.items:
                if isinstance(block, LineBlock) and block.block_type != BlockType.PARAGRAPH:
                    lvl = _headline_level(block.block_type)
                    if lvl:
                        smallest_level = max(smallest_level, lvl)

        if smallest_level < 6:
            next_type = _headline_by_level(smallest_level + 1)
            for page in result.pages:
                prev_block = None
                for block in page.items:
                    if not isinstance(block, LineBlock):
                        prev_block = None
                        continue
                    if block.toc_level is None and block.block_type == BlockType.PARAGRAPH:
                        h = round(max(l.height for l in block.lines))
                        f = block.lines[0].font
                        txt = block.get_text().strip()
                        # Same height as body, different font, ALL-CAPS, preceded by
                        # a non-paragraph block or significant gap (not just inline styled text)
                        if (h == base_h and f != body_font and txt == txt.upper()
                                and txt and len(txt) < 100
                                and (prev_block is None or prev_block.block_type != BlockType.PARAGRAPH)):
                            block.block_type = next_type
                    prev_block = block

        # --- Style Signature pass (same-size headers via font/color deviation) ---
        for page in result.pages:
            for block in page.items:
                if not isinstance(block, LineBlock) or block.toc_level is not None:
                    continue
                if block.block_type != BlockType.PARAGRAPH:
                    continue
                h = max(l.height for l in block.lines)
                f = block.lines[0].font
                c = block.lines[0].color
                txt = block.get_text()

                if primary and round(h) == primary.get('height'):
                    font_differs = f != primary.get('font')
                    color_differs = c != primary.get('color')

                    if (font_differs or color_differs) and len(txt) < 150:
                        is_short_line = len(txt) < 80
                        has_bold_start = block.lines[0].words and block.lines[0].words[0].is_bold
                        is_single_line = len(block.lines) == 1

                        if color_differs and is_short_line and is_single_line:
                            block.block_type = BlockType.H4
                            continue
                        if font_differs and has_bold_start and is_short_line and is_single_line:
                            block.block_type = BlockType.H4
                            continue

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
    """Port of findHeadlineItems and detectPageMappingNumber from DetectTOC.js.
    Uses TOC entries to find actual headers and resolve page offsets.
    
    Improved handling for multi-line TOC items: the headline finder can match
    across multiple consecutive blocks if the TOC entry spans multiple lines.
    """
    def transform(self, result: ParseResult) -> ParseResult:
        if not result.toc_entries:
            return result

        # 1. Detect Page Offset
        # Find the first few TOC entries in the actual document to see the index-to-page gap
        mapping_offset = 0
        for entry in result.toc_entries[:3]:
            found_page_idx = self._find_text_in_doc(result, entry.text)
            if found_page_idx is not None:
                mapping_offset = found_page_idx - entry.page_num
                break
        
        result.globals['page_offset'] = mapping_offset

        # 2. Map TOC levels to Blocks
        for entry in result.toc_entries:
            target_page_idx = entry.page_num + mapping_offset
            if 0 <= target_page_idx < len(result.pages):
                page = result.pages[target_page_idx]
                target_norm = normalize_for_match(entry.text)
                
                blocks = [b for b in page.items if isinstance(b, LineBlock)]
                
                for i, block in enumerate(blocks):
                    block_norm = normalize_for_match(block.get_text())
                    
                    # Direct match
                    if target_norm in block_norm or word_match_score(entry.text, block.get_text()) > 0.7:
                        block.toc_level = entry.level
                        block.block_type = self._get_header_type(entry.level)
                        break
                    
                    # Multi-line TOC handling: try combining with next block(s)
                    # This handles cases where a TOC entry title spans multiple lines in the document
                    if i + 1 < len(blocks):
                        next_block = blocks[i + 1]
                        combined_text = block.get_text() + " " + next_block.get_text()
                        combined_norm = normalize_for_match(combined_text)
                        if target_norm in combined_norm or word_match_score(entry.text, combined_text) > 0.7:
                            # Mark first block as header
                            block.toc_level = entry.level
                            block.block_type = self._get_header_type(entry.level)
                            break
        
        return result

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
    _FN_START = re.compile(r'^\(?\d{1,3}\)?\s+\S')
    _FN_MATCH = re.compile(r'^\(?(\d{1,3})\)?\.?\s+(.+)')

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
