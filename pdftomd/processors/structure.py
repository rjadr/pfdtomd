from ..models import (ParseResult, LineItem, Word, LineBlock, BlockType, TableBlock,
                      SpanRow, SupportInterval, LayoutBand, BlockBoundaryEvidence,
                      DecisionRecord)
from collections import Counter
from statistics import median as _median

class StatsProcessor:
    """Document-wide statistics with richer empirical distributions.

    Per IMPROVEMENTS.md: upgrade from point estimates to full distributions so
    that HeaderDetector and BlockBoundaryScorer can make posterior-style decisions.

    New globals added (v0.6):
      height_histogram    — Counter(rounded_height → count) across all LineItems
      font_freq           — Counter(font_name → count) for body-font posterior
      style_freq          — Counter((height, font, color) → count)
      left_margin_modes   — per-page most common x (for indent baseline)
      para_threshold      — 1.5 × most_used_distance (block gap threshold)
      max_height_font     — font of the tallest text (used for title/H1 detection)
    """

    def transform(self, result: ParseResult) -> ParseResult:
        line_items = [i for p in result.pages for i in p.items if isinstance(i, LineItem)]

        if not line_items:
            return result

        heights  = [round(i.height) for i in line_items]
        x_coords = [round(i.x)      for i in line_items]
        y_coords = [round(i.y)      for i in line_items]

        height_ctr = Counter(heights)
        common_height = height_ctr.most_common(1)[0][0]
        result.globals['most_used_height'] = common_height
        result.globals['max_height']       = max(heights)
        result.globals['height_histogram'] = dict(height_ctr)

        # Font frequency distribution
        font_ctr = Counter(i.font for i in line_items)
        result.globals['font_freq'] = dict(font_ctr)

        # Style signature frequency (height, font, color)
        sig_ctr = Counter((round(i.height), i.font, i.color) for i in line_items)
        result.globals['style_freq'] = dict(sig_ctr)

        # Most-used distance from actual inter-body-line gaps
        dist_counter: Counter = Counter()
        for page in result.pages:
            last_body = None
            for item in page.items:
                if not isinstance(item, LineItem):
                    last_body = None
                    continue
                if round(item.height) == common_height and item.get_text().strip():
                    if last_body is not None:
                        d = round(item.y - last_body.y)
                        if 0 < d < common_height * 4:
                            dist_counter[d] += 1
                    last_body = item
                else:
                    last_body = None

        if dist_counter:
            mud = dist_counter.most_common(1)[0][0]
        else:
            mud = common_height * 1.2
        result.globals['most_used_distance'] = mud
        result.globals['para_threshold']     = mud * 1.5

        # Left-margin mode per page (for indent baseline in ListStructureInferer)
        left_margin_modes: dict = {}
        for page in result.pages:
            xs = [round(i.x) for i in page.items if isinstance(i, LineItem)]
            if xs:
                left_margin_modes[page.index] = Counter(xs).most_common(1)[0][0]
        result.globals['left_margin_modes'] = left_margin_modes

        # Global left margin
        if x_coords:
            result.globals['min_x'] = min(x_coords)

        # Page height estimate (actual heights are on each Page object; this is
        # a legacy fallback used by PageNumberDetector when .height isn't set)
        if y_coords:
            result.globals['page_height'] = max(y_coords) + 100

        # Style signature detection (primary = most common style = body text)
        if sig_ctr:
            primary_sig = sig_ctr.most_common(1)[0][0]
            result.globals['primary_style'] = {
                'height': primary_sig[0],
                'font':   primary_sig[1],
                'color':  primary_sig[2],
            }
            result.globals['most_used_font'] = primary_sig[1]

        # Font of the tallest text (used for H1 / title detection in HeaderDetector)
        max_h = max(heights)
        tall_items = [i for i in line_items if round(i.height) == max_h]
        if tall_items:
            result.globals['max_height_font'] = Counter(
                i.font for i in tall_items
            ).most_common(1)[0][0]

        return result

class VerticalToHorizontal:
    def transform(self, result: ParseResult) -> ParseResult:
        for page in result.pages:
            new_items, stack = [], []
            for item in page.items:
                if isinstance(item, LineItem) and len(item.get_text().strip()) == 1:
                    if not stack or abs(item.x - stack[-1].x) < 3:
                        stack.append(item)
                    else:
                        new_items.extend(self._flush(stack)); stack = [item]
                else:
                    new_items.extend(self._flush(stack)); stack = []; new_items.append(item)
            new_items.extend(self._flush(stack))
            page.items = new_items
        return result

    def _flush(self, stack):
        if len(stack) < 4: return stack
        txt = "".join([i.get_text().strip() for i in stack])
        return [LineItem(x=stack[0].x, y=stack[0].y, width=10, height=stack[0].height, 
                         words=[Word(text=txt)], font=stack[0].font)]

class CodeBlockDetector:
    """Port of DetectCodeQuoteBlocks.js - detects CODE blocks.
    
    Note: The original JS version relies on pdfjs grouping columns separately.
    With PyMuPDF, multi-column PDFs can have columns that appear "indented"
    relative to the leftmost column. We use a very conservative approach:
    
    1. Block must be significantly indented (> 30 units from page min_x)
    2. Block must use a smaller font than body text, OR
    3. Block must use a monospace font
    
    This prevents marking entire columns as code in multi-column layouts.
    """
    def transform(self, result: ParseResult) -> ParseResult:
        base_h = result.globals.get('most_used_height', 10)
        page_height = result.globals.get('page_height', 842)
        top_zone = page_height / 8  # Top 12.5% of page — running headers live here

        # Common monospace font name patterns
        mono_patterns = ['mono', 'courier', 'consolas', 'menlo', 'dejavu', 'source code', 'fira code']

        for page in result.pages:
            # Calculate minX for THIS page from the blocks
            page_x_coords = []
            for block in page.items:
                if isinstance(block, LineBlock):
                    for line in block.lines:
                        page_x_coords.append(round(line.x))

            if not page_x_coords:
                continue
            min_x = min(page_x_coords)

            for block in page.items:
                if not isinstance(block, LineBlock): continue
                if block.block_type != BlockType.PARAGRAPH: continue  # Already typed

                if not block.lines:
                    continue

                # Never mark top-zone items as code — they're running headers/footers,
                # not indented code blocks. CodeBlockDetector is not meant for headers.
                if block.lines[0].y < top_zone:
                    continue

                # Check if ALL lines are significantly indented (at least 30 units)
                indent_threshold = min_x + 30
                is_indented = all(round(l.x) > indent_threshold for l in block.lines)
                if not is_indented:
                    continue

                # Check if font is smaller than body text
                is_small_font = all(l.height < base_h - 1 for l in block.lines)

                # Check if font is monospace
                is_monospace = False
                for line in block.lines:
                    font_lower = line.font.lower()
                    if any(pattern in font_lower for pattern in mono_patterns):
                        is_monospace = True
                        break

                # Only mark as code if indented AND (smaller font OR monospace)
                if is_indented and (is_small_font or is_monospace):
                    block.block_type = BlockType.CODE

        return result

class GatherBlocks:
    """Port of GatherBlocks.js - groups LineItems into LineBlocks based on vertical distance.
    
    Coordinate Systems:
    - JS (pdfjs): Y=0 at bottom, Y increases upward. lastItem.y - item.y > 0 means item is below.
    - Python (fitz): Y=0 at top, Y increases downward. item.y - last_item.y > 0 means item is below.
    
    The JS bigDistance() checks:
    1. distance < 0 - mostUsedDistance/2 → negative distance (item above last) triggers flush
    2. distance > allowedDistance → large gap triggers flush
    
    For Python (fitz), we need to invert the logic:
    - Normal flow: item.y > last_item.y (positive distance)
    - Negative distance (item.y < last_item.y) means item jumped up → flush
    """
    def transform(self, result: ParseResult) -> ParseResult:
        dist = result.globals.get('most_used_distance', 12)
        min_x = result.globals.get('min_x', 0)
        body_h = result.globals.get('most_used_height', 12)

        for page in result.pages:
            blocks, curr = [], LineBlock()
            if not page.items: continue

            last_item = None
            for item in page.items:
                if not isinstance(item, LineItem): continue

                if last_item:
                    # Calculate distance: positive means item is below last (normal flow in fitz)
                    # In fitz: Y increases downward, so item.y - last_item.y > 0 is normal
                    distance = item.y - last_item.y

                    # Check for "negative distance" - item jumped up significantly
                    # This matches JS: distance < 0 - mostUsedDistance / 2
                    if distance < -(dist / 2):
                        should_flush = True
                    else:
                        # Height-change flush: if the new line has a notably different font size
                        # than the current block's first line, flush. This separates run-in
                        # headings (e.g., bold 11pt title followed by 9.8pt body) and also
                        # prevents the author name from merging with the title on title pages.
                        # Threshold 0.9pt: catches 11→9.8 (diff 1.2) and 17→12 (diff 5) while
                        # ignoring trivial size noise within the same text style.
                        height_change_flush = False
                        if curr.lines:
                            block_ref_h = curr.lines[0].height
                            if abs(item.height - block_ref_h) > 0.9:
                                height_change_flush = True

                        # Determine allowed distance based on context.
                        # For large-font text (e.g. a 14pt decorative journal font used as body
                        # text), inter-line spacing scales with the font size. If both the
                        # current block and the incoming line are larger than body text, use a
                        # proportional allowed distance so consecutive lines in that font are not
                        # split into individual blocks.  The multiplier 1.8 covers generous
                        # leading (up to ~1.8× font height) while the dist*2.5 cap prevents
                        # merging across genuinely large vertical gaps.
                        block_h = curr.lines[0].height if curr.lines else body_h
                        if (not height_change_flush
                                and item.height > body_h + 1
                                and block_h > body_h + 1):
                            allowed = min(max(dist + 1, item.height * 1.8), dist * 2.5)
                        else:
                            # Check if current block looks like a list (starts with bullet/number)
                            is_list_context = False
                            if curr.lines:
                                first_text = curr.lines[0].get_text().strip()
                                is_list_context = (
                                    first_text.startswith(('-', '•', '–', '*')) or
                                    (len(first_text) > 1 and first_text[0].isdigit() and
                                     (first_text[1] in '.):' or (len(first_text) > 2 and first_text[1].isdigit() and first_text[2] in '.):'))
                                    )
                                )

                            allowed = dist + 1
                            if last_item.x > min_x and item.x > min_x:
                                # Indented elements often have greater spacing (JS: mostUsedDistance * 1.5)
                                allowed = dist * 1.5
                            # LIST merge rule: lists can have slightly larger spacing for continuation
                            if is_list_context and item.x > min_x:
                                allowed = dist * 1.75

                        # Bold-transition flush: isolates single-line all-bold blocks
                        # (e.g. "**Types of Sources**") so HeaderDetector's style-signature
                        # pass can promote them. Without this they merge with the following
                        # paragraph since they share the same font size.
                        bold_flush = False
                        if (not height_change_flush and curr.lines
                                and len(curr.lines) == 1):
                            curr_line = curr.lines[0]
                            curr_all_bold = (curr_line.words
                                             and all(w.is_bold for w in curr_line.words))
                            item_starts_non_bold = item.words and not item.words[0].is_bold
                            curr_short = len(curr_line.get_text().strip()) < 120
                            if curr_all_bold and item_starts_non_bold and curr_short:
                                bold_flush = True

                        should_flush = distance > allowed or height_change_flush or bold_flush
                else:
                    should_flush = False

                if should_flush and curr.lines:
                    blocks.append(curr)
                    curr = LineBlock()
                
                curr.lines.append(item)
                last_item = item
                
            if curr.lines: blocks.append(curr)

            # Re-merge TableBlocks with the new LineBlocks.
            # Preserve the order that items were processed in (which for multi-column
            # pages is column-major, established by _reorder_for_columns).  Only
            # insert TableBlocks at the right Y position; do NOT re-sort LineBlocks,
            # as that would undo the column reading order.
            table_blocks = [i for i in page.items if isinstance(i, TableBlock)]
            if not table_blocks:
                page.items = blocks
            else:
                # Insert each TableBlock before the first LineBlock whose start-Y
                # exceeds the table's Y, so tables appear at the correct position
                # in the flow without disrupting column order.
                merged: list = []
                tb_sorted = sorted(table_blocks, key=lambda t: t.y)
                tb_idx = 0
                for block in blocks:
                    block_y = block.lines[0].y if block.lines else float('inf')
                    while tb_idx < len(tb_sorted) and tb_sorted[tb_idx].y <= block_y:
                        merged.append(tb_sorted[tb_idx])
                        tb_idx += 1
                    merged.append(block)
                merged.extend(tb_sorted[tb_idx:])
                page.items = merged
        return result


# ─── v0.5 New processors ─────────────────────────────────────────────────────

class LayoutBandSegmenter:
    """Segment each page into vertical bands and infer local layout state.

    Replaces the page-global column detection with band-local detection via
    a Viterbi HMM over per-row x-interval features.  This correctly handles
    mixed-layout pages (single-column title → two-column body → footnotes).

    States:  single | two_col | three_col | table | footnote | figure_gap

    After state inference, applies column reordering only inside two_col /
    three_col bands, leaving single-column and footnote bands untouched.
    """

    STATES          = ["single", "two_col", "three_col", "table", "footnote", "figure_gap"]
    TRANSITION_COST = 1.8   # Log-space penalty for any state change between rows
    WIDE_FRAC       = 0.60  # Fraction of page width → "spanning" line

    def transform(self, result: ParseResult) -> ParseResult:
        for page in result.pages:
            span_rows = getattr(page, 'span_rows', [])
            if not span_rows:
                continue
            W = getattr(page, 'width',  595.0)
            H = getattr(page, 'height', 842.0)

            row_features = [self._row_features(sr, W, H) for sr in span_rows]
            states       = self._viterbi(row_features)

            for sr, st in zip(span_rows, states):
                sr.band_id = st

            bands = self._make_bands(span_rows, states)
            page.layout_bands = bands
            result.layout_bands.extend(bands)

            switches = sum(
                1 for i in range(1, len(states)) if states[i] != states[i - 1]
            )
            result.decision_log.append(DecisionRecord(
                module="LayoutBandSegmenter",
                decision="page_layout_states_decoded",
                score=float(len(bands)),
                confidence=0.75,
                features={
                    "page_index": page.index,
                    "row_count": len(span_rows),
                    "band_count": len(bands),
                    "state_switches": switches,
                },
                alternatives=["single_state_only"],
            ))

            multi_bands = [b for b in bands if b.chosen_state in ("two_col", "three_col")]
            if multi_bands:
                self._reorder_by_bands(page, bands, W)

        return result

    def _row_features(self, sr: SpanRow, W: float, H: float) -> dict:
        ivals = sr.intervals
        if not ivals:
            return {"y": sr.y, "n": 0, "neff": 0.0, "fill": 0.0,
                    "rho": 0.0, "balance": 0.0, "y_frac": sr.y / max(H, 1)}
        widths = [iv.x1 - iv.x0 for iv in ivals]
        total  = sum(widths)
        hull   = (ivals[-1].x1 - ivals[0].x0) if len(ivals) > 1 else (widths[0] if widths else 1.0)
        n      = len(ivals)
        sum_sq = sum(w * w for w in widths)
        neff   = (total * total) / sum_sq if sum_sq > 0 else 1.0
        rho    = total / hull  if hull > 0 else 0.0
        fill   = total / W     if W    > 0 else 0.0
        mid        = W / 2
        left_fill  = sum(min(iv.x1, mid) - max(iv.x0, 0)
                         for iv in ivals if iv.x1 > 0 and iv.x0 < mid)
        right_fill = sum(min(iv.x1, W)   - max(iv.x0, mid)
                         for iv in ivals if iv.x1 > mid and iv.x0 < W)
        balance    = (2 * min(left_fill, right_fill) / max(max(left_fill, right_fill), 1)
                      if left_fill + right_fill > 0 else 0.0)
        return {"y": sr.y, "n": n, "neff": neff, "fill": fill,
                "rho": rho, "balance": balance, "y_frac": sr.y / max(H, 1)}

    def _emit(self, f: dict, state: str) -> float:
        fill    = f["fill"]
        neff    = f["neff"]
        n       = f["n"]
        balance = f["balance"]
        y_frac  = f["y_frac"]
        if fill < 0.03:
            return 3.0 if state == "figure_gap" else -5.0
        if state == "figure_gap":
            return 3.0 if fill < 0.08 else (-1.0 if fill < 0.15 else -6.0)
        if state == "single":
            return -(neff - 1.0) ** 2 * 1.5 - max(0.0, 0.28 - fill) * 8.0
        if state == "two_col":
            score  = -(neff - 2.0) ** 2 * 2.0
            score -= max(0.0, abs(fill - 0.62) - 0.28) * 3.5
            score -= max(0.0, 0.55 - balance) * 3.0
            score -= 3.0 if n < 2 else 0.0
            return score
        if state == "three_col":
            score  = -(neff - 3.0) ** 2 * 2.0
            score -= max(0.0, abs(fill - 0.65) - 0.22) * 3.5
            score -= 4.0 if n < 3 else 0.0
            return score
        if state == "table":
            return -max(0.0, 2.8 - neff) * 2.0 + (1.5 if n >= 3 else 0.0)
        if state == "footnote":
            return max(0.0, y_frac - 0.80) * 6.0 - max(0.0, abs(fill - 0.40) - 0.38) * 3.0
        return 0.0

    def _viterbi(self, row_features: list) -> list:
        if not row_features:
            return []
        states, n_states, n_rows = self.STATES, len(self.STATES), len(row_features)
        INF = float('-inf')
        dp = [[INF] * n_states for _ in range(n_rows)]
        bp = [[-1]  * n_states for _ in range(n_rows)]
        for j, s in enumerate(states):
            dp[0][j] = self._emit(row_features[0], s)
        for i in range(1, n_rows):
            emit = [self._emit(row_features[i], s) for s in states]
            for j in range(n_states):
                best_v, best_k = INF, 0
                for k in range(n_states):
                    val = dp[i-1][k] + (0.0 if k == j else -self.TRANSITION_COST)
                    if val > best_v:
                        best_v, best_k = val, k
                dp[i][j] = best_v + emit[j]
                bp[i][j] = best_k
        out     = [0] * n_rows
        out[-1] = max(range(n_states), key=lambda j: dp[-1][j])
        for i in range(n_rows - 2, -1, -1):
            out[i] = bp[i+1][out[i+1]]
        return [states[j] for j in out]

    def _make_bands(self, span_rows: list, states: list) -> list:
        if not span_rows or not states:
            return []
        bands = []
        cur_state, cur_y0, cur_y1 = states[0], span_rows[0].y, span_rows[0].y
        for i in range(1, len(span_rows)):
            if states[i] != cur_state:
                bands.append(LayoutBand(y0=cur_y0, y1=cur_y1, chosen_state=cur_state))
                cur_state, cur_y0 = states[i], span_rows[i].y
            cur_y1 = span_rows[i].y
        bands.append(LayoutBand(y0=cur_y0, y1=cur_y1, chosen_state=cur_state))
        return bands

    def _find_gutters(self, span_rows: list, W: float, n_gutters: int) -> list:
        if not span_rows:
            return []
        BIN    = 2.0
        n_bins = int(W / BIN) + 1
        hist   = [0] * n_bins
        for sr in span_rows:
            for iv in sr.intervals:
                left  = max(0,      int(iv.x0 / BIN))
                right = min(n_bins, int(iv.x1 / BIN) + 1)
                for b in range(left, right):
                    hist[b] += 1
        max_c = max(hist) if hist else 0
        if max_c == 0:
            return []
        noise  = max_c * 0.15
        margin = W * 0.06
        valleys, start = [], None
        for i, v in enumerate(hist):
            if v <= noise:
                if start is None: start = i
            else:
                if start is not None: valleys.append((start, i)); start = None
        if start is not None:
            valleys.append((start, n_bins))
        gutters = []
        for s, e in valleys:
            w_pts = (e - s) * BIN
            mid   = ((s + e) / 2) * BIN
            if w_pts >= 6.0 and margin < mid < W - margin:
                gutters.append((w_pts, mid))
        gutters.sort(reverse=True)
        return sorted([mid for _, mid in gutters[:n_gutters]])

    def _reorder_by_bands(self, page, bands: list, W: float):
        line_items  = [it for it in page.items if isinstance(it, LineItem)]
        table_items = [it for it in page.items if isinstance(it, TableBlock)]
        if not line_items:
            return
        WIDE      = W * self.WIDE_FRAC
        new_items = []
        processed: set = set()

        for band in sorted(bands, key=lambda b: b.y0):
            band_lines = [l for l in line_items
                          if band.y0 - 5 <= l.y <= band.y1 + 5
                          and id(l) not in processed]
            if not band_lines:
                continue

            if band.chosen_state in ("two_col", "three_col"):
                n_cols    = 2 if band.chosen_state == "two_col" else 3
                spanning  = [l for l in band_lines if l.width > WIDE]
                col_lines = [l for l in band_lines if l not in spanning]
                band_srows = [sr for sr in getattr(page, 'span_rows', [])
                               if band.y0 - 5 <= sr.y <= band.y1 + 5]
                gutters    = self._find_gutters(band_srows, W, n_cols - 1)
                if not gutters:
                    # No clear gutter: use graph ordering with single-column bias.
                    new_items.extend(self._graph_order(band_lines, "single", None))
                    for l in band_lines:
                        processed.add(id(l))
                    continue
                boundaries = [0.0] + gutters + [W]

                buckets: list = [[] for _ in range(n_cols)]
                for l in col_lines:
                    mid_x   = l.x + l.width / 2
                    col_idx = n_cols - 1
                    for k in range(n_cols - 1):
                        if mid_x < boundaries[k + 1]:
                            col_idx = k
                            break
                    buckets[col_idx].append(l)
                ordered = self._graph_order(col_lines, band.chosen_state, boundaries)
                if spanning:
                    # Preserve column-major order; spanning items go before/after the block
                    col_min_y = min((l.y for l in ordered), default=float('inf'))
                    col_max_y = max((l.y for l in ordered), default=float('-inf'))
                    before = sorted([l for l in spanning if l.y <= col_min_y], key=lambda l: l.y)
                    after  = sorted([l for l in spanning if l.y >  col_max_y], key=lambda l: l.y)
                    mid    = sorted([l for l in spanning
                                     if col_min_y < l.y <= col_max_y], key=lambda l: l.y)
                    new_items.extend(before + mid)
                    new_items.extend(ordered)
                    new_items.extend(after)
                else:
                    new_items.extend(ordered)
            else:
                new_items.extend(self._graph_order(band_lines, band.chosen_state, None))

            for l in band_lines:
                processed.add(id(l))

        for l in line_items:
            if id(l) not in processed:
                new_items.append(l)

        if not table_items:
            page.items = new_items
        else:
            merged    = []
            tb_sorted = sorted(table_items, key=lambda t: t.y)
            tb_idx    = 0
            for item in new_items:
                while tb_idx < len(tb_sorted) and tb_sorted[tb_idx].y <= item.y:
                    merged.append(tb_sorted[tb_idx]); tb_idx += 1
                merged.append(item)
            merged.extend(tb_sorted[tb_idx:])
            page.items = merged

    def _column_index(self, line: LineItem, boundaries: list | None) -> int:
        if not boundaries or len(boundaries) < 2:
            return 0
        mid_x = line.x + line.width / 2
        for i in range(len(boundaries) - 1):
            if boundaries[i] <= mid_x < boundaries[i + 1]:
                return i
        return len(boundaries) - 2

    def _reorder_by_column_proposals(self, page, columns: list, W: float):
        line_items = [it for it in page.items if isinstance(it, LineItem)]
        table_items = [it for it in page.items if isinstance(it, TableBlock)]
        if not line_items:
            return

        wide_threshold = W * self.WIDE_FRAC
        spanning = [l for l in line_items if l.width > wide_threshold]
        col_lines = [l for l in line_items if l not in spanning]

        boundaries = [columns[0][0]] + [c[1] for c in columns]
        ordered = self._graph_order(col_lines, "two_col" if len(columns) == 2 else "three_col", boundaries)

        if spanning:
            min_y = min((l.y for l in ordered), default=float('inf'))
            max_y = max((l.y for l in ordered), default=float('-inf'))
            before = sorted([l for l in spanning if l.y <= min_y], key=lambda l: l.y)
            mid = sorted([l for l in spanning if min_y < l.y <= max_y], key=lambda l: l.y)
            after = sorted([l for l in spanning if l.y > max_y], key=lambda l: l.y)
            new_items = before + mid + ordered + after
        else:
            new_items = ordered

        if not table_items:
            page.items = new_items
        else:
            merged = []
            tb_sorted = sorted(table_items, key=lambda t: t.y)
            tb_idx = 0
            for item in new_items:
                while tb_idx < len(tb_sorted) and tb_sorted[tb_idx].y <= item.y:
                    merged.append(tb_sorted[tb_idx])
                    tb_idx += 1
                merged.append(item)
            merged.extend(tb_sorted[tb_idx:])
            page.items = merged

    def _graph_order(self, lines: list, state: str, boundaries: list | None) -> list:
        """Graph-based reading-order decode inside a local layout band.

        Constructs precedence edges and performs a weighted DAG linearization using
        a Kahn-style topological traversal with deterministic tie-breaking.
        """
        if len(lines) <= 1:
            return list(lines)

        n = len(lines)
        indeg = [0] * n
        out_w = [0.0] * n
        adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]

        for i in range(n):
            li = lines[i]
            ci = self._column_index(li, boundaries)
            for j in range(n):
                if i == j:
                    continue
                lj = lines[j]
                dy = lj.y - li.y
                cj = self._column_index(lj, boundaries)

                add_edge = False
                weight = 0.0

                if state in ("two_col", "three_col") and boundaries is not None:
                    # Column-major precedence: left columns before right columns.
                    if ci < cj:
                        add_edge = True
                        weight = 1.8 + max(0.0, min(0.6, (ci - cj) * -0.2))
                    elif ci == cj and dy >= -2:
                        add_edge = True
                        weight = 1.0 + max(0.0, 1.0 - min(abs(dy), 80) / 80)
                else:
                    # Single-column precedence: top-to-bottom with light x tie-break.
                    if dy > 2 or (abs(dy) <= 2 and li.x <= lj.x):
                        add_edge = True
                        weight = 1.0 + max(0.0, 1.0 - min(max(dy, 0), 100) / 100)

                if add_edge:
                    adj[i].append((j, weight))

        for i in range(n):
            for j, w in adj[i]:
                indeg[j] += 1
                out_w[i] += w

        remaining = set(range(n))
        ordered_idx: list[int] = []

        while remaining:
            zeros = [k for k in remaining if indeg[k] == 0]
            if not zeros:
                # Cycle fallback: pick by geometric order to ensure progress.
                zeros = list(remaining)

            if state in ("two_col", "three_col") and boundaries is not None:
                zeros.sort(key=lambda k: (
                    self._column_index(lines[k], boundaries),
                    lines[k].y,
                    lines[k].x,
                    -out_w[k],
                ))
            else:
                zeros.sort(key=lambda k: (lines[k].y, lines[k].x, -out_w[k]))

            chosen = zeros[0]
            ordered_idx.append(chosen)
            remaining.remove(chosen)
            for nxt, _w in adj[chosen]:
                indeg[nxt] = max(0, indeg[nxt] - 1)

        return [lines[i] for i in ordered_idx]


class BlockBoundaryScorer:
    """Compute P(block boundary) for each adjacent pair of LineItems.

    Scores are stored in page._boundary_probs for BlockAssembler to consume.
    Features: gap_ratio, height_change, bold_transition, continuation,
    style_mismatch, hyphen_continuation.
    """

    def transform(self, result: ParseResult) -> ParseResult:
        dist = result.globals.get('most_used_distance', 12)
        for page in result.pages:
            lines = [it for it in page.items if isinstance(it, LineItem)]
            if len(lines) < 2:
                page._boundary_probs = []
                continue
            probs = [self._boundary_prob(lines[i], lines[i+1], dist, result.globals)
                     for i in range(len(lines) - 1)]
            page._boundary_probs = probs
            avg_conf = 1.0 - (sum(probs) / len(probs)) if probs else 0.0
            result.decision_log.append(DecisionRecord(
                module="BlockBoundaryScorer",
                decision="boundary_probabilities_computed",
                score=float(sum(probs)) if probs else 0.0,
                confidence=float(avg_conf),
                features={
                    "page_index": page.index,
                    "line_count": len(lines),
                    "edge_count": len(probs),
                    "avg_boundary_prob": (sum(probs) / len(probs)) if probs else 0.0,
                },
                alternatives=["fixed_gap_threshold"],
            ))
        return result

    def _boundary_prob(self, a: LineItem, b: LineItem, dist: float, globs: dict) -> float:
        gap     = b.y - a.y
        body_h  = globs.get('most_used_height', 12)

        if gap < -(dist / 2):
            return 1.0

        gap_ratio = gap / max(dist, 1.0)

        height_change = abs(b.height - a.height) > 0.9

        a_text      = a.get_text().strip()
        b_text      = b.get_text().strip()
        hyphen_cont = (a_text.endswith('-') and len(a_text) >= 2
                       and a_text[-2].islower() and b_text and b_text[0].islower())
        lower_cont  = bool(b_text and b_text[0].islower())
        punct_end   = bool(a_text and a_text[-1] in '.!?')
        continuation= hyphen_cont or (not punct_end and lower_cont)

        a_all_bold  = bool(a.words and all(w.is_bold for w in a.words))
        b_non_bold  = bool(b.words and not b.words[0].is_bold)
        bold_trans  = (a_all_bold and b_non_bold
                       and len(a_text) < 120 and len(a_text.split()) <= 15)

        style_mismatch = (a.font != b.font) or (a.color != b.color)

        block_h = a.height
        if block_h > body_h + 1 and b.height > body_h + 1 and not height_change:
            allowed_ratio = min(block_h * 1.8, dist * 2.5) / max(dist, 1.0)
        else:
            allowed_ratio = 1.05

        if hyphen_cont:
            return 0.05
        if gap_ratio <= 0.7 and not height_change and not bold_trans:
            return 0.08
        if continuation and gap_ratio <= 1.3 and not height_change:
            return 0.18

        if height_change:
            base = 0.85
        elif gap_ratio > allowed_ratio * 1.4:
            base = 0.88
        elif gap_ratio > allowed_ratio:
            base = 0.62
        elif gap_ratio > 0.9:
            base = 0.28
        else:
            base = 0.10

        if bold_trans:
            base = max(base, 0.80)
        if style_mismatch and gap_ratio > 0.5:
            base = min(1.0, base + 0.12)
        if continuation:
            base = min(base, 0.30)

        return base


class BlockAssembler:
    """Assemble LineItems into LineBlocks using boundary probabilities from BlockBoundaryScorer.

    For the separable DP objective, the optimal greedy threshold is p > 0.5.
    """

    THRESHOLD = 0.50

    def transform(self, result: ParseResult) -> ParseResult:
        dist = result.globals.get('most_used_distance', 12)

        for page in result.pages:
            line_items  = [it for it in page.items if isinstance(it, LineItem)]
            table_items = [it for it in page.items if isinstance(it, TableBlock)]

            if not line_items:
                if table_items:
                    page.items = table_items
                continue

            probs  = getattr(page, '_boundary_probs', [])
            blocks: list   = []
            curr: LineBlock = LineBlock()

            for i, item in enumerate(line_items):
                if i == 0:
                    curr.lines.append(item)
                    continue
                if i - 1 < len(probs):
                    prob = probs[i - 1]
                else:
                    prev = line_items[i - 1]
                    gap  = item.y - prev.y
                    prob = 0.85 if gap > dist * 1.5 else (0.1 if gap < dist else 0.35)

                if prob >= self.THRESHOLD:
                    if curr.lines:
                        blocks.append(curr)
                    curr = LineBlock()
                curr.lines.append(item)

            if curr.lines:
                blocks.append(curr)

            if not table_items:
                page.items = blocks
            else:
                merged    = []
                tb_sorted = sorted(table_items, key=lambda t: t.y)
                tb_idx    = 0
                for block in blocks:
                    block_y = block.lines[0].y if block.lines else float('inf')
                    while tb_idx < len(tb_sorted) and tb_sorted[tb_idx].y <= block_y:
                        merged.append(tb_sorted[tb_idx]); tb_idx += 1
                    merged.append(block)
                merged.extend(tb_sorted[tb_idx:])
                page.items = merged

        return result
