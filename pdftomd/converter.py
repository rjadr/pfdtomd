import fitz
import re
from collections import Counter
from .models import Page, Word, LineItem, ParseResult, TableBlock
from .processor import Pipeline

URL_RE = re.compile(r'^(https?://|www\\.)\S+')

# Extraction flags: 
# - TEXT_DEHYPHENATE: Automatically joins hyphenated words across lines
# - TEXT_PRESERVE_WHITESPACE: Preserve whitespace
# - TEXT_PRESERVE_IMAGES: Preserve image placeholders
# Note: We explicitly do NOT include TEXT_PRESERVE_LIGATURES (decomposes ﬁ → fi)
EXTRACTION_FLAGS = (
    fitz.TEXT_PRESERVE_WHITESPACE | 
    fitz.TEXT_PRESERVE_IMAGES |
    fitz.TEXT_DEHYPHENATE  # Smart de-hyphenation built into PyMuPDF
)

# Optional: pandas for table rendering
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


def _detect_text_tables(raw_dict) -> list:
    """Detect borderless text tables from a PyMuPDF 'dict' extraction.

    Used as fallback when find_tables() returns nothing (tables without visible
    borders, common in academic PDFs from De Gruyter, Springer, etc.).

    Algorithm:
    1. Group spans by y-row.
    2. Find 'anchor rows': rows with 2+ well-separated effective columns.
       Effective columns = clusters of x-positions within CLUSTER_THRESH pt.
       Filters: min coverage > 100pt, smallest gap >= 10pt (blocks inline
       superscript footnotes), largest gap >= 30pt.
    3. Group consecutive anchor rows within ROW_GAP=50pt into table candidates.
    4. Use the first anchor row as the header to define column positions.
    5. Collect anchor rows (new entries) + nearby continuation rows (wrapped cells).
    6. Build GFM markdown table.

    Returns list of (markdown_str, bbox_tuple) pairs.
    """
    CLUSTER_THRESH = 12   # pt — merge x-positions within same column span
    ROW_GAP = 50          # pt — max y-gap between consecutive anchor rows
    MIN_COVERAGE = 100    # pt — minimum x-range for a valid anchor row

    def effective_xs(spans):
        """Merge close x-positions into column representatives (leftmost of each cluster)."""
        xs = sorted(set(round(s["x"]) for s in spans))
        merged = [[xs[0]]]
        for x in xs[1:]:
            if x - merged[-1][-1] <= CLUSTER_THRESH:
                merged[-1].append(x)
            else:
                merged.append([x])
        return [min(g) for g in merged]

    def is_anchor(spans):
        """Return True if the row looks like a real table row (not body-text + superscript)."""
        if not spans:
            return False
        coverage = max(s["x_end"] for s in spans) - min(s["x"] for s in spans)
        if coverage <= MIN_COVERAGE:
            return False
        eff = effective_xs(spans)
        if len(eff) < 2:
            return False
        gaps = sorted([eff[i + 1] - eff[i] for i in range(len(eff) - 1)])
        if len(eff) == 2:
            if gaps[0] <= 30:
                return False
            # Reject rows where col1 starts immediately after col0 ends (<20pt gap).
            # This filters body-text lines where an inline footnote/citation appears
            # right after the text (e.g., "...romantic look." + "3" at x_end+12pt).
            # Real 2-column tables have clear whitespace between columns.
            col1_threshold = eff[1] - 5
            col0_x_end = max((s["x_end"] for s in spans if s["x"] < col1_threshold), default=-1)
            col1_x_start = min((s["x"] for s in spans if s["x"] >= col1_threshold), default=99999)
            if col0_x_end >= 0 and (col1_x_start - col0_x_end) < 20:
                return False
            return True
        # 3+ effective columns: smallest gap >= 15pt filters justified body text where
        # word-level spans can sit 10-14pt apart due to justification spacing, while
        # real table columns always have wider start-to-start gaps (25pt+).
        # Cap at 5 columns: justified narrow-column prose (e.g., right column of a
        # 2-column paper) often produces 6-11 fake "columns" from word-level spans,
        # while real borderless tables in single-column documents rarely exceed 5.
        if len(eff) > 5:
            return False
        return gaps[0] >= 15 and gaps[-1] >= 30

    def assign_col(x, col_lefts):
        """Assign x to the rightmost column whose left edge is <= x (5pt tolerance)."""
        for i in range(len(col_lefts) - 1, -1, -1):
            if x >= col_lefts[i] - 5:
                return i
        return 0

    # 1. Collect spans grouped by rounded y-coordinate
    span_rows: dict = {}
    for block in raw_dict.get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            y_key = round(line["bbox"][1])
            if y_key not in span_rows:
                span_rows[y_key] = []
            for span in line["spans"]:
                text = span["text"].strip()
                if text:
                    span_rows[y_key].append({
                        "x": span["bbox"][0],
                        "x_end": span["bbox"][2],
                        "y_actual": line["bbox"][1],
                        "text": text,
                        "size": span["size"],
                    })

    sorted_ys = sorted(span_rows.keys())
    anchor_ys = [y for y in sorted_ys if is_anchor(span_rows[y])]
    if len(anchor_ys) < 2:
        return []

    # 2. Group anchor rows into table candidates (gap <= ROW_GAP)
    groups: list = []
    current = [anchor_ys[0]]
    for i in range(1, len(anchor_ys)):
        if anchor_ys[i] - anchor_ys[i - 1] <= ROW_GAP:
            current.append(anchor_ys[i])
        else:
            if len(current) >= 3:
                groups.append(current)
            current = [anchor_ys[i]]
    if len(current) >= 3:
        groups.append(current)
    if not groups:
        return []

    result = []
    for group in groups:
        # 3. Column positions come from the first anchor row (the header)
        col_lefts = effective_xs(span_rows[group[0]])
        n_cols = len(col_lefts)
        if n_cols < 2:
            continue

        # 4. All rows in range: anchor rows + continuation rows (wrapped cell text)
        max_anchor_y = group[-1]
        all_table_ys = [y for y in sorted_ys if group[0] <= y <= max_anchor_y + 20]
        group_set = set(group)

        # 5. Build rows: anchor rows start new entries; non-anchor rows extend the last
        rows: list = []
        for y in all_table_ys:
            spans = span_rows[y]
            cells = [""] * n_cols
            for s in sorted(spans, key=lambda s: s["x"]):
                ci = assign_col(s["x"], col_lefts)
                cells[ci] = (cells[ci] + " " + s["text"]).strip()

            if y in group_set:
                rows.append(cells)
            elif rows:
                for ci in range(n_cols):
                    if cells[ci]:
                        rows[-1][ci] = (rows[-1][ci] + " " + cells[ci]).strip()

        if not rows:
            continue

        # 6. Build GFM table
        headers = rows[0]
        if not any(h.strip() for h in headers):
            continue
        # Reject cross-column false positives: real table headers have short labels
        # (e.g., "Location"=8, "Relative frequency (%)"=22). Prose paragraph text
        # merged across layout columns produces cells of 40-100+ chars.
        if any(len(h) > 40 for h in headers):
            continue

        gfm = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * n_cols) + " |",
        ]
        for row in rows[1:]:
            cells = [c.strip().replace("|", "\\|") for c in row]
            while len(cells) < n_cols:
                cells.append("")
            gfm.append("| " + " | ".join(cells[:n_cols]) + " |")

        md = "\n".join(gfm)

        # 7. Bounding box for text masking
        all_spans_flat = [s for y in all_table_ys for s in span_rows[y]]
        bbox = (
            min(s["x"] for s in all_spans_flat) - 2,
            min(s["y_actual"] for s in span_rows[group[0]]) - 1,
            max(s["x_end"] for s in all_spans_flat) + 2,
            max(s["y_actual"] for s in span_rows[all_table_ys[-1]]) + 15,
        )
        result.append((md, bbox))

    return result


def _table_to_gfm(table) -> str:
    """Convert a PyMuPDF table to GFM markdown without pandas."""
    try:
        data = table.extract()
        if not data or len(data) < 1:
            return ""
        
        # First row is headers
        headers = [str(cell or "").strip() for cell in data[0]]
        if not any(headers):  # Skip empty tables
            return ""
        
        # Build GFM table
        lines = []
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        
        for row in data[1:]:
            cells = [str(cell or "").strip().replace("|", "\\|") for cell in row]
            # Pad if row has fewer cells
            while len(cells) < len(headers):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(headers)]) + " |")
        
        return "\n".join(lines)
    except Exception:
        return ""

def fix_encoding_bugs(word: str) -> str:
    """Handle non-standard font mapping errors that fitz flags can't catch.
    
    These are CID mapping errors common in Dutch governmental PDFs or older scans,
    not standard Unicode ligatures.
    """
    # Fix > that should be ft (common in Dutch words with broken font encoding)
    if '>' in word:
        word = re.sub(r'(\w)>', r'\1ft', word)
        word = re.sub(r'^>', 'ft', word)
    
    # Fix E that appears in place of ti (only in middle of lowercase words)
    # Pattern: lowercase + E + lowercase (e.g., "sElle" -> "stille")
    if 'E' in word and not word.isupper():
        word = re.sub(r'([a-z])E([a-z])', r'\1ti\2', word)
    
    # Pattern: E at start followed by "jd" specifically (e.g., "Ejd" -> "Tijd")
    # This is a very specific Dutch pattern - "tijd" is commonly broken this way
    if word.startswith('Ejd'):
        word = 'Tijd' + word[3:]
    
    return word

def _build_parse_result(pdf_path: str) -> ParseResult:
    """Extract text, tables and metadata from a PDF into a ParseResult."""
    doc = fitz.open(pdf_path)
    pages = []
    
    # Extract Metadata (Parity with Metadata.js)
    # First try standard metadata
    meta = doc.metadata
    parsed_meta = {
        "title": meta.get("title"),
        "author": meta.get("author"),
        "creator": meta.get("creator"),
        "producer": meta.get("producer")
    }
    
    # Deep search: Try XMP metadata for modern PDFs (like Node.js Metadata.js)
    # XMP often contains dc:title, xap:creatortool etc. that standard metadata misses
    try:
        xmp = doc.xref_get_key(-1, "Metadata")
        if xmp and xmp[0] == "stream":
            xmp_stream = doc.xref_stream(-1)
            if xmp_stream:
                xmp_text = xmp_stream.decode('utf-8', errors='ignore')
                # Extract dc:title if standard title is empty
                if not parsed_meta["title"]:
                    import re
                    title_match = re.search(r'<dc:title[^>]*>.*?<rdf:li[^>]*>([^<]+)</rdf:li>', xmp_text, re.DOTALL)
                    if title_match:
                        parsed_meta["title"] = title_match.group(1).strip()
                # Extract xap:CreatorTool if creator is empty
                if not parsed_meta["creator"]:
                    creator_match = re.search(r'<xap:CreatorTool>([^<]+)</xap:CreatorTool>', xmp_text)
                    if creator_match:
                        parsed_meta["creator"] = creator_match.group(1).strip()
    except Exception:
        pass  # XMP extraction is best-effort
    
    for i, page in enumerate(doc):
        # === TABLE DETECTION (Pro Feature #1) ===
        # Detect tables BEFORE text extraction so we can mask those regions
        table_blocks = []
        table_rects = []
        
        try:
            tables = page.find_tables()
            for table in tables:
                # Get table bounding box to exclude from text extraction
                table_rects.append(fitz.Rect(table.bbox))
                
                # Convert to GFM markdown
                if HAS_PANDAS:
                    try:
                        df = table.to_pandas()
                        # Clean up column names (pandas may auto-generate them)
                        if df.columns.tolist() == list(range(len(df.columns))):
                            # No headers - use first row as headers
                            df.columns = df.iloc[0]
                            df = df[1:]
                        md_table = df.to_markdown(index=False)
                    except Exception:
                        # Fallback: manual GFM table rendering
                        md_table = _table_to_gfm(table)
                else:
                    md_table = _table_to_gfm(table)
                
                if md_table:
                    table_blocks.append(TableBlock(
                        y=table.bbox[1],  # Top Y coordinate for sorting
                        markdown=md_table,
                        bbox=table.bbox
                    ))
        except Exception:
            pass  # Table detection is best-effort
        
        # === TEXT EXTRACTION with Multi-Column Sort (Pro Feature #5) ===
        # sort=True follows visual reading flow (columns) rather than strict Y order
        raw = page.get_text("dict", flags=EXTRACTION_FLAGS, sort=True)

        # === STRATEGY 2: Text-based table detection (borderless tables) ===
        # Fallback when find_tables() found nothing (no visible borders in PDF).
        # Detects tables by analysing span x/y alignment across consecutive rows.
        if not table_blocks:
            for md_table, bbox in _detect_text_tables(raw):
                table_rects.append(fitz.Rect(bbox))
                table_blocks.append(TableBlock(
                    y=bbox[1],
                    markdown=md_table,
                    bbox=tuple(bbox)
                ))

        all_lines = []
        for b in raw["blocks"]:
            if "lines" not in b: continue
            block_lines = []
            for l in b["lines"]:
                # === TABLE MASKING: Skip text inside detected tables ===
                line_rect = fitz.Rect(l["bbox"])
                in_table = any(line_rect.intersects(t_rect) for t_rect in table_rects)
                if in_table:
                    continue  # Don't extract text that's already in a table
                
                words = []
                last_x_end = None
                
                # Collect all spans first to find the primary baseline (most common Y)
                spans_data = []
                for s in l["spans"]:
                    spans_data.append({
                        'span': s,
                        'y': s["origin"][1],
                        'size': s["size"]
                    })
                
                # Find the primary Y (baseline) - the one used by most text
                if spans_data:
                    # Use the first span's Y as primary baseline reference
                    primary_y = spans_data[0]['y']
                    primary_size = spans_data[0]['size']

                for s in l["spans"]:
                    # Geometric Spacing (Port of LineConverter.js combineText)
                    # If the gap between spans is > 5 units, we treat it as a space
                    if last_x_end is not None and (s["bbox"][0] - last_x_end) > 5:
                        if words and not words[-1].text.endswith(" "):
                            words[-1].text += " "

                    font_name = s["font"].lower()
                    font_flags = s.get("flags", 0)
                    
                    # Bold/Italic detection using fitz flags for accuracy
                    # fitz flags: bit 0 = superscript, bit 1 = italic, bit 2 = serifed, 
                    #             bit 3 = monospaced, bit 4 = bold
                    is_bold = bool(font_flags & (1 << 4)) or "bold" in font_name
                    is_italic = bool(font_flags & (1 << 1)) or any(x in font_name for x in ["italic", "oblique"])
                    
                    # Superscript detection: JS uses Y comparison within line
                    # A span is superscript if its Y is significantly higher (smaller) than the primary baseline
                    # AND it's a number (for footnote links)
                    span_y = s["origin"][1]
                    is_super = False
                    text_content = s["text"].strip()
                    # Only mark as superscript if it's a number and positioned higher than baseline
                    if text_content.isdigit() and spans_data and span_y < primary_y - (primary_size * 0.3):
                        is_super = True
                    
                    raw_text_parts = s["text"].split(" ")
                    for sw in raw_text_parts:
                        clean_word = sw.strip()
                        if not clean_word: continue
                        
                        # Fix broken font encoding issues (CID mapping errors)
                        clean_word = fix_encoding_bugs(clean_word)
                        # Convert soft hyphens (U+00AD) to regular hyphens so the
                        # end-of-line de-hyphenation logic in the pipeline can handle them.
                        clean_word = clean_word.replace('\xad', '-')
                        
                        # Check if this specific word is a number for superscript
                        word_is_super = is_super and clean_word.isdigit()
                        
                        words.append(Word(
                            text=clean_word,
                            is_bold=is_bold,
                            is_italic=is_italic,
                            is_superscript=word_is_super,
                            is_link=bool(URL_RE.match(clean_word))
                        ))
                    last_x_end = s["bbox"][2]

                if not words: continue
                # Capture color and flags from first span for style signature detection
                first_span = l["spans"][0]
                # Use modal baseline y (most-common span origin y) instead of bbox[1].
                # bbox[1] is the TOP of the line bounding box, which gets inflated upward
                # when a line contains superscripts, causing false paragraph breaks.
                # The baseline y of the primary text is stable and unaffected by superscripts.
                all_span_ys = [s["origin"][1] for s in l["spans"]]
                line_y = Counter(all_span_ys).most_common(1)[0][0] if all_span_ys else l["bbox"][1]
                block_lines.append(LineItem(
                    x=l["bbox"][0], y=line_y,
                    width=l["bbox"][2]-l["bbox"][0],
                    height=max([s["size"] for s in l["spans"]]),
                    words=words,
                    font=first_span["font"],
                    color=first_span.get("color", 0),
                    flags=first_span.get("flags", 0)
                ))
            
            # Sort lines within this block geometrically (Top to Bottom, Left to Right)
            # Sorting per-block preserves multi-column structure since PyMuPDF groups
            # columns as separate blocks. Sorting the whole page would interleave columns.
            block_lines.sort(key=lambda ln: (round(ln.y), round(ln.x)))
            all_lines.extend(block_lines)
        
        # Combine LineItems and TableBlocks, then sort by Y position
        # This ensures tables appear in the correct document flow
        all_items = all_lines + table_blocks
        # Don't sort here - let the pipeline handle ordering (tables have .y attribute)
        
        pages.append(Page(index=i, items=all_items))

    return ParseResult(pages=pages, metadata=parsed_meta)


def convert(pdf_path: str, page_breaks: bool = False) -> str:
    """Convert a PDF to a single markdown string."""
    return Pipeline().run(_build_parse_result(pdf_path), page_breaks=page_breaks)


def convert_pages(pdf_path: str) -> list:
    """Convert a PDF and return one markdown string per page (0-indexed).

    Useful when per-page content is needed, e.g.:
        pages = convert_pages("paper.pdf")
        page_map = {(i + 1, ""): text for i, text in enumerate(pages)}
    """
    parse_result = _build_parse_result(pdf_path)
    pipeline = Pipeline()
    for proc in pipeline.processors:
        parse_result = proc.transform(parse_result)
    return pipeline.render_pages(parse_result)
