import hashlib
from collections import Counter
from ..models import ParseResult, LineItem

class PageNumberDetector:
    """Deep Port of page-number-functions.js with proper sequence verification."""
    def transform(self, result: ParseResult) -> ParseResult:
        if not result.pages:
            return result
            
        # Get page height from globals or first page (more flexible than hardcoded A4)
        page_height = result.globals.get('page_height', 842)  # Default to A4 if not set
        top_zone = page_height / 6
        bottom_zone = page_height * 5 / 6
        
        page_map = {}  # page_index -> list of (number, LineItem)
        # Search ranges (Top 1/6 and Bottom 1/6)
        for page in result.pages:
            potential_nums = []
            for item in page.items:
                if isinstance(item, LineItem):
                    if item.y < top_zone or item.y > bottom_zone:
                        txt = item.get_text().strip()
                        if txt.isdigit():
                            potential_nums.append((int(txt), item))
            if potential_nums:
                page_map[page.index] = potential_nums

        # Find sequence to detect offset - need at least 3 consecutive matches for confidence
        # This prevents false positives from random digits in headers/footers
        offset = None
        best_sequence_length = 0
        sorted_keys = sorted(page_map.keys())
        
        for start_idx in range(len(sorted_keys)):
            idx1 = sorted_keys[start_idx]
            for n1, _ in page_map[idx1]:
                # Try this number as the starting point
                candidate_offset = n1 - idx1
                sequence_length = 1
                
                # Check how many consecutive pages match this offset
                for j in range(start_idx + 1, len(sorted_keys)):
                    idx2 = sorted_keys[j]
                    expected_num = idx2 + candidate_offset
                    if any(n == expected_num for n, _ in page_map[idx2]):
                        sequence_length += 1
                    else:
                        break
                
                # Require at least 3 matches (or 2 if document is short) for confidence
                min_required = min(3, len(sorted_keys))
                if sequence_length >= min_required and sequence_length > best_sequence_length:
                    best_sequence_length = sequence_length
                    offset = candidate_offset

        if offset is not None:
            for page in result.pages:
                expected = page.index + offset
                page.items = [i for i in page.items if not (
                    isinstance(i, LineItem) and
                    i.get_text().strip() == str(expected) and
                    (i.y < top_zone or i.y > bottom_zone)
                )]
        return result

class RepetitiveElementRemover:
    """Remove headers/footers that repeat across pages.

    Two passes:
    1. Classic pass (threshold 60%): removes items identical on first/last position
       across most pages — catches simple single running headers.
    2. Zone pass (threshold 30%, min 2 pages): removes short items in the top/bottom
       zone that appear on multiple pages — catches ALTERNATING running headers
       (e.g., author on odd pages, chapter title on even pages, each ~50% of pages).
    """
    def transform(self, result: ParseResult) -> ParseResult:
        if len(result.pages) < 2:
            return result

        n_pages = len(result.pages)

        # --- Pass 1: classic first/last item removal (60% threshold) ---
        # Only consider LineItem (not TableBlock, which lacks get_text())
        if n_pages >= 3:
            hashes = Counter()
            for page in result.pages:
                line_items = [i for i in page.items if isinstance(i, LineItem)]
                if line_items:
                    hashes[hashlib.md5(line_items[0].get_text().encode()).hexdigest()] += 1
                    hashes[hashlib.md5(line_items[-1].get_text().encode()).hexdigest()] += 1
            threshold = n_pages * 0.6
            to_strip = {h for h, c in hashes.items() if c >= threshold}
            for page in result.pages:
                line_items = [i for i in page.items if isinstance(i, LineItem)]
                if line_items and hashlib.md5(line_items[0].get_text().encode()).hexdigest() in to_strip:
                    page.items.remove(line_items[0])
                line_items = [i for i in page.items if isinstance(i, LineItem)]
                if line_items and hashlib.md5(line_items[-1].get_text().encode()).hexdigest() in to_strip:
                    page.items.remove(line_items[-1])

        # --- Pass 2: zone-based alternating header removal (30% threshold, min 2) ---
        # Items in the top/bottom zone (running headers/footers) that appear on
        # multiple pages should be removed even if they alternate (odd/even pages).
        from ..models import LineItem as LI
        page_height = 842  # fallback
        # Use a simple heuristic: top zone = first ~6% of page, bottom = last ~6%
        # These narrow zones capture only genuine running-header-area items.
        # (PageNumberDetector already cleared digits; what remains are text headers.)
        top_threshold = 50   # y < 50 in baseline coords ≈ top running header area
        bottom_threshold_frac = 0.92  # y > 92% of page

        # Collect zone item texts and which pages they appear on
        zone_text_pages: dict = {}  # normalized_text → set of page indices
        for page in result.pages:
            for item in page.items:
                if not isinstance(item, LI):
                    continue
                txt = item.get_text().strip()
                if not txt or len(txt) < 2:
                    continue
                # Normalize: strip Unicode spaces (em-space, en-space, etc.)
                norm = ''.join(c for c in txt if not (0x2000 <= ord(c) <= 0x200B or c in '\t\xa0'))
                norm = norm.strip()
                if not norm or norm.isdigit():
                    continue
                if item.y < top_threshold:
                    zone_text_pages.setdefault(norm, set()).add(page.index)

        zone_threshold = max(2, n_pages * 0.3)
        texts_to_remove = {
            norm for norm, pages in zone_text_pages.items()
            if len(pages) >= zone_threshold
        }

        if texts_to_remove:
            for page in result.pages:
                new_items = []
                for item in page.items:
                    if isinstance(item, LI) and item.y < top_threshold:
                        norm = ''.join(
                            c for c in item.get_text().strip()
                            if not (0x2000 <= ord(c) <= 0x200B or c in '\t\xa0')
                        ).strip()
                        if norm in texts_to_remove:
                            continue
                    new_items.append(item)
                page.items = new_items

        return result
