from ..models import ParseResult, LineItem, Word, LineBlock, BlockType, TableBlock
from collections import Counter

class StatsProcessor:
    """Port of CalculateGlobalStats.js with Style Signature detection.

    Style Signatures identify the most common (height, font, color) combination
    which represents body text. Headers can then be detected by deviation from
    this signature even when they have the same font size.

    mostUsedDistance is measured from ACTUAL inter-line distances between
    consecutive body-text lines (like pdf2md), not inferred from font height.
    This avoids incorrect distance thresholds for documents with unusual leading.
    """
    def transform(self, result: ParseResult) -> ParseResult:
        line_items = [i for p in result.pages for i in p.items if isinstance(i, LineItem)]

        if not line_items: return result

        heights = [round(i.height) for i in line_items]
        x_coords = [round(i.x) for i in line_items]
        y_coords = [round(i.y) for i in line_items]

        common_height = Counter(heights).most_common(1)[0][0]
        result.globals['most_used_height'] = common_height
        result.globals['max_height'] = max(heights)

        # Compute mostUsedDistance from ACTUAL inter-line distances (like pdf2md's approach).
        # Only measure distances between consecutive body-text lines (same height as body).
        # Reset tracker on each non-body line to avoid measuring heading→body distances.
        dist_counter = Counter()
        for page in result.pages:
            last_body = None
            for item in page.items:
                if not isinstance(item, LineItem):
                    last_body = None
                    continue
                if round(item.height) == common_height and item.get_text().strip():
                    if last_body is not None:
                        d = round(item.y - last_body.y)
                        if 0 < d < common_height * 4:  # Reasonable range only
                            dist_counter[d] += 1
                    last_body = item
                else:
                    last_body = None  # Reset on non-body lines

        if dist_counter:
            result.globals['most_used_distance'] = dist_counter.most_common(1)[0][0]
        else:
            result.globals['most_used_distance'] = common_height * 1.2

        # Detect the document's left margin
        if x_coords:
            result.globals['min_x'] = min(x_coords)
        # Store page height for PageNumberDetector (estimate from max y + some margin)
        if y_coords:
            result.globals['page_height'] = max(y_coords) + 100

        # Style Signature Detection: find the most common (height, font, color) tuple
        # This represents the document's body text style
        signatures = [(round(i.height), i.font, i.color) for i in line_items]
        if signatures:
            primary_sig = Counter(signatures).most_common(1)[0][0]
            result.globals['primary_style'] = {
                'height': primary_sig[0],
                'font': primary_sig[1],
                'color': primary_sig[2]
            }
            # Also store most_used_font for backward compatibility
            result.globals['most_used_font'] = primary_sig[1]

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
                        # Determine allowed distance based on context
                        allowed = dist + 1

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

                        if last_item.x > min_x and item.x > min_x:
                            # Indented elements often have greater spacing (JS: mostUsedDistance * 1.5)
                            allowed = dist * 1.5

                        # LIST merge rule: lists can have slightly larger spacing for continuation
                        if is_list_context and item.x > min_x:
                            allowed = dist * 1.75

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

                        should_flush = distance > allowed or height_change_flush
                else:
                    should_flush = False

                if should_flush and curr.lines:
                    blocks.append(curr)
                    curr = LineBlock()
                
                curr.lines.append(item)
                last_item = item
                
            if curr.lines: blocks.append(curr)

            # Re-merge TableBlocks (preserved from input) with the new LineBlocks,
            # sorted by Y position so tables appear at the correct reading position.
            table_blocks = [i for i in page.items if isinstance(i, TableBlock)]
            all_items = blocks + table_blocks
            all_items.sort(key=lambda b: b.lines[0].y if isinstance(b, LineBlock) and b.lines else b.y)
            page.items = all_items
        return result
