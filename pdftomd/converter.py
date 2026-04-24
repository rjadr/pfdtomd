import fitz
import re
import math
import os
from collections import Counter, defaultdict
from statistics import median
from .models import (Page, Word, LineItem, ParseResult, TableBlock,
                     SpanAtom, SpanRow, SupportInterval, TableCandidate,
                     RegionCandidate)
from .processor import Pipeline

# Keep legacy page-level reorder enabled by default for benchmark stability.
# It can be disabled for ablation with PDFTOMD_DISABLE_LEGACY_COLUMN_REORDER=1.
ENABLE_LEGACY_COLUMN_REORDER = os.getenv('PDFTOMD_DISABLE_LEGACY_COLUMN_REORDER', '').strip() != '1'

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
        if not spans:
            return []
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
        # Cap at 25 columns (pdf-inspector's limit).
        # For 6+ columns require a stronger gap signal to avoid false positives
        # from justified body text (word gaps ~15pt, real column gaps 25pt+).
        if len(eff) > 25:
            return False
        if len(eff) > 5:
            return gaps[0] >= 20 and gaps[-1] >= 30
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
        # 3. Column positions from the union of ALL anchor rows.
        # Using only the first row misses columns that appear only in data rows
        # (e.g., tables with multi-row headers where col 0 has no header label).
        all_anchor_spans = []
        for ay in group:
            all_anchor_spans.extend(span_rows[ay])
        col_lefts = effective_xs(all_anchor_spans)
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

        # Validation guards: reject obvious false positives before storing candidate
        rows_0 = rows[0]
        if not any(h.strip() for h in rows_0):
            continue
        if any(len(h) > 40 for h in rows_0):
            continue

        # 6. Collect all raw spans in the candidate region
        all_spans_flat = [s for y in all_table_ys for s in span_rows.get(y, [])]
        if not all_spans_flat:
            continue

        # 7. Bounding box for text masking
        bbox = (
            min(s["x"] for s in all_spans_flat) - 2,
            min(s["y_actual"] for s in span_rows[group[0]]) - 1,
            max(s["x_end"] for s in all_spans_flat) + 2,
            max(s["y_actual"] for s in all_spans_flat) + 15,
        )
        result.append(TableCandidate(
            bbox=tuple(bbox),
            source="text_detect",
            raw_spans=all_spans_flat,
        ))

    return result


def _table_to_gfm(table) -> str:
    """Convert a PyMuPDF table to GFM markdown.

    Handles two common PyMuPDF artefacts:

    1. **Phantom columns**: Columns that are entirely None/empty across all rows.
       PyMuPDF sometimes creates these when a table header cell spans multiple
       visual columns.  We remove them entirely.

    2. **Continuation rows**: Rows where the anchor column (col 0) is empty but
       other cells have text.  These are wrapped cell text or multi-row headers
       that belong to the previous row.  We merge them in.

    These two fixes collapse a 50-row table-with-continuation-rows into the
    correct 8-row clean table that matches the ground truth.
    """
    try:
        data = table.extract()
        if not data:
            return ""

        n_cols = len(data[0])

        def _clean(cell) -> str:
            if cell is None:
                return ""
            s = str(cell).strip().replace("\n", " ")
            return "" if s.lower() == "nan" else s

        # Step 1: Clean every cell.
        cleaned = [[_clean(c) for c in (row + [None] * max(0, n_cols - len(row)))]
                   for row in data]

        # Step 2: Remove phantom columns — entirely empty across all rows.
        keep_cols = [ci for ci in range(n_cols)
                     if any(cleaned[ri][ci] for ri in range(len(cleaned)))]
        if not keep_cols:
            return ""
        cleaned = [[row[ci] for ci in keep_cols] for row in cleaned]
        n_cols = len(keep_cols)

        # Step 3: Merge continuation rows.
        # A row is a continuation when col-0 is empty AND at least one cell has
        # content.  Merge each non-empty cell into the corresponding cell of the
        # most-recent non-continuation row (appending with a space).
        merged: list[list[str]] = []
        for row in cleaned:
            is_cont = not row[0] and any(row)
            if is_cont and merged:
                for ci, val in enumerate(row):
                    if val:
                        prev = merged[-1][ci]
                        merged[-1][ci] = (prev + " " + val).strip() if prev else val
            else:
                merged.append(list(row))

        if not merged:
            return ""

        # Skip leading all-empty rows (phantom rows PyMuPDF adds before the
        # real header when a table uses merged/spanning header cells).
        while merged and not any(merged[0]):
            merged.pop(0)

        if not merged:
            return ""

        headers = merged[0]
        if not any(headers):
            return ""

        # Sanity check: reject tables whose header cells are extremely long —
        # those are false-positive detections where body prose was mis-classified
        # as a table header.
        if any(len(h) > 80 for h in headers):
            return ""

        lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * n_cols) + " |",
        ]
        for row in merged[1:]:
            cells = [c.replace("|", "\\|") for c in row]
            while len(cells) < n_cols:
                cells.append("")
            lines.append("| " + " | ".join(cells[:n_cols]) + " |")

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

def _otsu_threshold(values: list, bins: int = 32):
    """Compute Otsu's threshold for binary splitting of a list of floats.
    Returns the threshold value or None if distribution is degenerate."""
    if not values:
        return None
    vmin, vmax = min(values), max(values)
    if vmax - vmin < 1e-6:
        return None

    hist = [0] * bins
    for v in values:
        idx = min(bins - 1, int((v - vmin) / (vmax - vmin) * bins))
        hist[idx] += 1

    total = sum(hist)
    sum_total = sum(i * h for i, h in enumerate(hist))

    sum_b = 0.0
    w_b = 0
    best_var = -1.0
    best_idx = None

    for i, h in enumerate(hist):
        w_b += h
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * h
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > best_var:
            best_var = var_between
            best_idx = i

    if best_idx is None:
        return None
    return vmin + (best_idx + 0.5) * (vmax - vmin) / bins


def _group_spans_into_rows(spans: list, W: float) -> list:
    """Group spans into rows by baseline y, then merge adjacent x-intervals.

    Each span dict must have keys: x, x_end, y, text, size.
    Returns list of {"y": float, "intervals": list[[x0, x1]]} dicts.
    """
    spans = [s for s in spans if s["text"].strip()]
    if not spans:
        return []

    sizes = sorted(s.get("size", 10.0) for s in spans)
    med_size = sizes[len(sizes) // 2]
    eps_y = max(2.0, min(4.0, 0.25 * med_size))
    merge_gap = min(6.0, 0.012 * W)

    sorted_spans = sorted(spans, key=lambda s: (s["y"], s["x"]))
    rows: list = []

    for s in sorted_spans:
        if not rows or abs(s["y"] - rows[-1]["y_ref"]) > eps_y:
            rows.append({"y_ref": s["y"], "spans": [s]})
        else:
            rows[-1]["spans"].append(s)
            all_ys = [t["y"] for t in rows[-1]["spans"]]
            rows[-1]["y_ref"] = sum(all_ys) / len(all_ys)

    result = []
    for row in rows:
        items = sorted(row["spans"], key=lambda s: s["x"])
        intervals: list = []
        for s in items:
            x0, x1 = s["x"], s["x_end"]
            if not intervals:
                intervals.append([x0, x1])
            elif x0 - intervals[-1][1] <= merge_gap:
                intervals[-1][1] = max(intervals[-1][1], x1)
            else:
                intervals.append([x0, x1])
        result.append({"y": row["y_ref"], "intervals": intervals})
    return result


def _persistent_anchor_count(centers_by_row: dict, W: float, n_rows: int) -> int:
    """Count x-position clusters that persist across at least 18% of rows."""
    if not centers_by_row:
        return 0

    eps = 0.03 * W
    min_support = max(3, math.ceil(0.18 * n_rows))

    pts = sorted(
        (c, row_idx)
        for row_idx, centers in centers_by_row.items()
        for c in centers
    )

    clusters: list = []
    for c, row_idx in pts:
        if not clusters or c - clusters[-1]["right"] > eps:
            clusters.append({"right": c, "rows": {row_idx}})
        else:
            clusters[-1]["right"] = c
            clusters[-1]["rows"].add(row_idx)

    return sum(1 for cl in clusters if len(cl["rows"]) >= min_support)


def _validate_gutter(spans: list, W: float, g: float) -> dict:
    """Validate whether a histogram-proposed column gutter separates real prose
    columns or is a false positive caused by a borderless table.

    For each row that has text on both sides of the gutter, compute:
      - N_eff  : effective number of occupied x-components (Herfindahl inverse)
      - rho    : fill ratio (occupied / hull width)
      - p_narrow: fraction of intervals narrower than the page-adaptive threshold
      - K_persist: count of x-anchors recurring across many rows

    Real 2-column prose → N_eff ≈ 2, rho ≈ 0.85, p_narrow low, K_persist ≈ 2.
    Borderless table    → N_eff > 4, rho < 0.70, p_narrow high, K_persist > 3.

    Returns dict with is_table_like, score, and diagnostic fields.
    """
    rows = _group_spans_into_rows(spans, W)
    if not rows:
        return {"is_table_like": False, "score": 0.0, "n_cross": 0}

    # Page-adaptive narrow/wide threshold via Otsu on log(interval_width / W)
    all_log_widths = [
        math.log(max(b - a, 1e-3) / W)
        for row in rows
        for a, b in row["intervals"]
    ]
    otsu_t = _otsu_threshold(all_log_widths) or math.log(0.085)

    gamma = 6.0
    min_side_occ = 0.015 * W
    cross_rows = []
    centers_by_row: dict = {}

    for idx, row in enumerate(rows):
        ivals = row["intervals"]

        # Discard rows where any interval touches the gutter clearance band
        if any(not (b < g - gamma or a > g + gamma) for a, b in ivals):
            continue

        left  = [(a, b) for a, b in ivals if b < g - gamma]
        right = [(a, b) for a, b in ivals if a > g + gamma]

        if sum(b - a for a, b in left) < min_side_occ:
            continue
        if sum(b - a for a, b in right) < min_side_occ:
            continue

        parts  = left + right
        widths = [b - a for a, b in parts]
        total  = sum(widths)
        hull   = max(b for _, b in parts) - min(a for a, _ in parts)
        sum_sq = sum(w * w for w in widths)

        neff     = (total * total) / sum_sq if sum_sq > 0 else 1.0
        rho      = total / hull if hull > 0 else 1.0
        p_narrow = sum(1 for w in widths if math.log(max(w, 1e-3) / W) <= otsu_t) / len(widths)

        centers_by_row[idx] = [(a + b) / 2.0 for a, b in parts]
        cross_rows.append({"neff": neff, "rho": rho, "p_narrow": p_narrow})

    n_cross = len(cross_rows)
    if n_cross < 3:
        return {"is_table_like": False, "score": 0.0, "n_cross": n_cross}

    med_neff = median(r["neff"]     for r in cross_rows)
    med_rho  = median(r["rho"]      for r in cross_rows)
    med_pn   = median(r["p_narrow"] for r in cross_rows)
    k_persist = _persistent_anchor_count(centers_by_row, W, n_cross)

    def clip(x: float) -> float:
        return max(0.0, min(1.0, x))

    s1 = clip((med_neff - 2.35) / 1.65)
    s2 = clip((0.78 - med_rho)  / 0.18)
    s3 = clip((med_pn  - 0.50)  / 0.30)
    s4 = clip((k_persist - 2.0) / 3.0)

    score = 0.35 * s1 + 0.20 * s2 + 0.20 * s3 + 0.25 * s4

    is_table_like = (score >= 0.58) or (med_neff >= 2.9 and k_persist >= 4)

    return {
        "is_table_like": is_table_like,
        "score": score,
        "n_cross": n_cross,
        "med_neff": med_neff,
        "med_rho": med_rho,
        "med_p_narrow": med_pn,
        "k_persist": k_persist,
    }


def _detect_columns(blocks: list, page_width: float) -> list[tuple[float, float]]:
    """Detect multi-column layout.

    Phase 1 — Histogram proposal:
        Build a 2-pt-wide bin occupancy histogram from non-spanning blocks
        (blocks narrower than 50 % of page width). Find empty valleys to
        propose candidate column gutters.

    Phase 2 — Span-row validation:
        For each candidate gutter, examine rows that have text on both sides.
        Compute row-support topology metrics (N_eff, fill ratio, p_narrow,
        K_persist) to distinguish real prose columns from borderless tables.
        Reject any gutter that looks table-like.

    Returns a list of (x_min, x_max) column regions, or [] for single-column
    pages (or when all gutters were rejected as table-like).
    """
    W = page_width
    BIN_W = 2.0
    WIDE_THRESHOLD = W * 0.5
    n_bins = int(W / BIN_W) + 1
    hist = [0] * n_bins

    for b in blocks:
        if "lines" not in b:
            continue
        bx0, _, bx1, _ = b["bbox"]
        if bx1 - bx0 > WIDE_THRESHOLD:
            continue
        left  = max(0, int(bx0 / BIN_W))
        right = min(n_bins, int(bx1 / BIN_W) + 1)
        for bi in range(left, right):
            hist[bi] += 1

    max_count = max(hist) if hist else 0
    if max_count == 0:
        return []

    noise  = max_count * 0.15
    margin = W * 0.05

    valleys: list = []
    start = None
    for i, v in enumerate(hist):
        if v <= noise:
            if start is None:
                start = i
        else:
            if start is not None:
                valleys.append((start, i))
                start = None
    if start is not None:
        valleys.append((start, n_bins))

    candidate_gutters: list[float] = [
        ((s + e) / 2) * BIN_W
        for s, e in valleys
        if (e - s) * BIN_W >= 8.0 and margin < ((s + e) / 2) * BIN_W < (W - margin)
    ]

    if not candidate_gutters:
        return []

    # --- Phase 2: validate each gutter against span-row topology ---
    # Extract flat span list from all text blocks
    raw_spans: list = []
    for b in blocks:
        if "lines" not in b:
            continue
        for line in b["lines"]:
            for s in line["spans"]:
                text = s.get("text", "").strip()
                if text:
                    raw_spans.append({
                        "x":     s["bbox"][0],
                        "x_end": s["bbox"][2],
                        "y":     s["origin"][1],
                        "text":  text,
                        "size":  s.get("size", 10.0),
                    })

    valid_gutters: list[float] = []
    for g in candidate_gutters:
        verdict = _validate_gutter(raw_spans, W, g)
        if not verdict["is_table_like"]:
            valid_gutters.append(g)

    if not valid_gutters:
        return []

    boundaries = [0.0] + valid_gutters + [W]
    columns = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]

    # Drop columns narrower than 10 % of page (margin artefacts)
    min_col_w = W * 0.10
    columns = [(x0, x1) for x0, x1 in columns if (x1 - x0) >= min_col_w]

    if len(columns) < 2 or len(columns) > 3:
        return []

    # Content-balance check: each column must contain at least 10 % of the
    # non-spanning blocks so empty margin-artefact columns are dropped.
    non_spanning = [
        b for b in blocks
        if "lines" in b and (b["bbox"][2] - b["bbox"][0]) <= W * 0.6
    ]
    if not non_spanning:
        return []

    min_per_col = max(1, len(non_spanning) * 0.10)
    col_counts  = [0] * len(columns)
    for b in non_spanning:
        mid_x = (b["bbox"][0] + b["bbox"][2]) / 2
        for k, (x0, x1) in enumerate(columns):
            if x0 <= mid_x < x1:
                col_counts[k] += 1
                break

    columns = [col for col, cnt in zip(columns, col_counts) if cnt >= min_per_col]

    if len(columns) < 2 or len(columns) > 3:
        return []

    return columns


def _reorder_for_columns(
    lines: list, columns: list[tuple[float, float]], page_width: float
) -> list:
    """Reorder LineItems into reading order: left column top→bottom, then right.

    Spanning items (width > 60 % of page) are re-inserted at their original
    y-position so titles and full-width paragraphs stay in place.
    """
    WIDE_THRESHOLD = page_width * 0.6
    spanning = [l for l in lines if l.width > WIDE_THRESHOLD]
    col_lines = [l for l in lines if l not in spanning]

    buckets: list[list] = [[] for _ in columns]
    for line in col_lines:
        mid_x = line.x + line.width / 2
        col_idx = 0
        for k, (x_min, x_max) in enumerate(columns):
            if x_min <= mid_x < x_max:
                col_idx = k
                break
        buckets[col_idx].append(line)

    for bucket in buckets:
        bucket.sort(key=lambda l: (l.y, l.x))

    ordered: list = []
    for bucket in buckets:
        ordered.extend(bucket)

    if not spanning:
        return ordered

    # Insert spanning items at their y-position
    spanning_sorted = sorted(spanning, key=lambda l: (l.y, l.x))
    result: list = []
    si = 0
    for line in ordered:
        while si < len(spanning_sorted) and spanning_sorted[si].y <= line.y:
            result.append(spanning_sorted[si])
            si += 1
        result.append(line)
    result.extend(spanning_sorted[si:])
    return result


def _build_parse_result(pdf_path: str) -> ParseResult:
    """Extract text, tables and metadata from a PDF into a ParseResult."""
    doc = fitz.open(pdf_path)
    pages = []
    all_span_atoms = []
    all_region_candidates = []
    
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
        # Detect tables BEFORE text extraction so we can mask those regions.
        # Candidates are stored on the Page and converted to TableBlocks by
        # TableStructureInferer (pipeline stage 2) — lattice inference happens there.
        table_rects = []
        page_table_candidates: list = []

        try:
            tables = page.find_tables()
            for table in tables:
                # Always mark bbox for text masking (prevents re-processing header rows)
                table_rects.append(fitz.Rect(table.bbox))
                # Store raw cell matrix for lattice inferer.
                # Only include as a candidate if PyMuPDF returned ≥2 rows;
                # 1-row detections are header-only (bordered header above unbordered
                # body) — let _detect_text_tables capture the full region instead.
                raw_rows: list = []
                try:
                    raw_rows = table.extract() or []
                except Exception:
                    pass
                if len(raw_rows) >= 2:
                    page_table_candidates.append(TableCandidate(
                        bbox=table.bbox,
                        source="find_tables",
                        raw_rows=raw_rows,
                    ))
        except Exception:
            pass  # Table detection is best-effort
        
        # === TEXT EXTRACTION with Multi-Column Sort (Pro Feature #5) ===
        # sort=True follows visual reading flow (columns) rather than strict Y order
        raw = page.get_text("dict", flags=EXTRACTION_FLAGS, sort=True)

        # Collect span-level primitives for structured inference diagnostics.
        for block_id, b in enumerate(raw.get("blocks", [])):
            if "lines" not in b:
                continue
            for line_id, l in enumerate(b["lines"]):
                for s in l.get("spans", []):
                    txt = (s.get("text") or "").strip()
                    if not txt:
                        continue
                    bbox = s.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    origin = s.get("origin", (bbox[0], bbox[1]))
                    all_span_atoms.append(SpanAtom(
                        x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3],
                        baseline_y=origin[1],
                        text=txt,
                        font=s.get("font", ""),
                        size=s.get("size", 0.0),
                        color=s.get("color", 0),
                        flags=s.get("flags", 0),
                        block_id=block_id,
                        line_id=line_id,
                    ))

        # === COLUMN LAYOUT DETECTION (v0.3.0) ===
        page_width = page.rect.width
        page_columns = _detect_columns(raw["blocks"], page_width)
        is_multicolumn = len(page_columns) >= 2

        # === STRATEGY 2: Text-based table detection (borderless / partial tables) ===
        # Run on non-multicolumn pages, OR on pages where find_tables found at
        # least one bordered table (even 1-row) — in the latter case the page IS a
        # table page; the false-multicolumn detection comes from the table structure
        # itself, so we still want text-detect for the unbordered body rows.
        found_any_bordered = bool(table_rects)   # any find_tables hits (incl. 1-row)
        if not is_multicolumn or found_any_bordered:
            existing_rects = [fitz.Rect(c.bbox) for c in page_table_candidates]
            for cand in _detect_text_tables(raw):
                cand_rect = fitz.Rect(cand.bbox)
                if not any(cand_rect.intersects(er) for er in existing_rects):
                    table_rects.append(fitz.Rect(cand.bbox))
                    page_table_candidates.append(cand)

        # Region candidates for later diagnostics and debugging.
        for cand in page_table_candidates:
            all_region_candidates.append(RegionCandidate(
                bbox=tuple(cand.bbox),
                kind="table_candidate",
                score=float(getattr(cand, "score", 0.0)),
                features={"page_index": i, "source": getattr(cand, "source", "")},
                source=getattr(cand, "source", ""),
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
        
        # === MULTI-COLUMN REORDER (legacy compatibility fallback) ===
        # Default path keeps ordering decisions in LayoutBandSegmenter.
        if is_multicolumn and ENABLE_LEGACY_COLUMN_REORDER:
            all_lines = _reorder_for_columns(all_lines, page_columns, page_width)

        # === BUILD SpanRows for LayoutBandSegmenter ===
        # Pre-compute the merged interval rows from all text blocks so the
        # pipeline's LayoutBandSegmenter has raw geometry without re-parsing.
        raw_spans_for_rows = []
        for b in raw["blocks"]:
            if "lines" not in b:
                continue
            for l in b["lines"]:
                for s in l["spans"]:
                    text = s.get("text", "").strip()
                    if text:
                        raw_spans_for_rows.append({
                            "x":    s["bbox"][0],
                            "x_end": s["bbox"][2],
                            "y":    s["origin"][1],
                            "text": text,
                            "size": s.get("size", 10.0),
                        })
        row_dicts = _group_spans_into_rows(raw_spans_for_rows, page_width)
        page_span_rows = [
            SpanRow(y=rd["y"], intervals=[SupportInterval(x0=iv[0], x1=iv[1]) for iv in rd["intervals"]])
            for rd in row_dicts
        ]

        # TableBlocks are NOT added here — TableStructureInferer (pipeline stage 2)
        # converts page_table_candidates into TableBlocks and inserts them by y-position.
        all_items = all_lines

        page_obj = Page(
            index=i,
            items=all_items,
            width=page_width,
            height=page.rect.height,
            span_rows=page_span_rows,
            table_candidates=page_table_candidates,
        )
        # Column proposals are attached for deferred fallback use by
        # LayoutBandSegmenter when no confident multi-column bands are decoded.
        page_obj.column_proposals = page_columns
        page_obj.is_multicolumn_candidate = is_multicolumn
        pages.append(page_obj)

    return ParseResult(
        pages=pages,
        metadata=parsed_meta,
        span_atoms=all_span_atoms,
        span_rows=[sr for p in pages for sr in getattr(p, "span_rows", [])],
        region_candidates=all_region_candidates,
    )


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
