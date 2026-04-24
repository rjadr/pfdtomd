"""processors/tables.py — TableStructureInferer

Converts raw TableCandidate objects (stored on Page.table_candidates during
PDF extraction) into properly structured TableBlock objects via latent lattice
inference — replacing the old "extract–threshold–commit" pattern with:

  extract → candidate with raw data → infer grid → render

Two inference paths:
  find_tables  — raw cell matrix from PyMuPDF → phantom-col removal,
                 wrapped-row merging, multi-row header detection → GFM
  text_detect  — raw span positions → 1-D x/y anchor clustering → grid
                 cell assignment → wrapped-row merging → GFM

Per IMPROVEMENTS.md steps 2–12.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..models import (DecisionRecord, GridHypothesis, LineBlock, ParseResult,
                      TableBlock, TableCandidate)


# ─── helpers ────────────────────────────────────────────────────────────────

def _clean(cell: object) -> str:
    """Coerce a raw cell value to a clean string."""
    if cell is None:
        return ""
    s = str(cell).strip().replace("\n", " ")
    return "" if s.lower() in ("nan", "none") else s


def _cluster_1d(vals: List[float], thresh: float) -> List[List[float]]:
    """1-D single-link clustering. Returns clusters in position order."""
    if not vals:
        return []
    sv = sorted(vals)
    clusters: List[List[float]] = [[sv[0]]]
    for v in sv[1:]:
        if v - clusters[-1][-1] <= thresh:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return clusters


def _leftmost_anchors(vals: List[float], thresh: float) -> List[float]:
    """Return the leftmost (minimum) member of each 1-D cluster as the anchor."""
    return [min(c) for c in _cluster_1d(vals, thresh)]


def _remove_phantom_cols(rows: List[List[str]]) -> List[List[str]]:
    """Remove columns that are entirely empty across all rows."""
    if not rows:
        return rows
    n = len(rows[0])
    keep = [ci for ci in range(n) if any(
        rows[ri][ci] if ci < len(rows[ri]) else "" for ri in range(len(rows))
    )]
    if not keep or len(keep) == n:
        return rows
    return [[row[ci] for ci in keep if ci < len(row)] for row in rows]


def _merge_continuation(rows: List[List[str]]) -> List[List[str]]:
    """Merge wrapped continuation rows into their predecessor.

    A row is a continuation when:
      • its first cell is empty, AND
      • at least one other cell has content, AND
      • there is a preceding row to extend.

    This handles cell text that wraps to a new PDF line.
    """
    merged: List[List[str]] = []
    for row in rows:
        row = [c.strip() if c else "" for c in row]
        is_cont = (not row[0]) and any(row[1:])
        if is_cont and merged:
            for ci, val in enumerate(row):
                if val and ci < len(merged[-1]):
                    prev = merged[-1][ci]
                    merged[-1][ci] = (prev + " " + val).strip() if prev else val
        else:
            merged.append(list(row))
    return merged


def _detect_header_count(rows: List[List[str]]) -> int:
    """Return number of header rows (1 or 2).

    A second header row is inferred when row[1] has <20% numeric cells and
    row[2] has >40% numeric cells (header annotation before data rows).
    """
    if len(rows) < 3:
        return 1

    def _num_ratio(row: List[str]) -> float:
        filled = [c for c in row if c]
        if not filled:
            return 0.0
        nums = sum(
            1 for c in filled
            if c.replace(".", "").replace(",", "").replace("-", "").isdigit()
        )
        return nums / len(filled)

    r1 = _num_ratio(rows[1])
    r2 = _num_ratio(rows[2]) if len(rows) > 2 else 0.0
    if r1 < 0.20 and r2 > 0.40:
        return 2
    return 1


def _render_gfm(rows: List[List[str]], n_header: int = 1) -> str:
    """Render rows as a GFM pipe table."""
    if not rows:
        return ""
    n_cols = max(len(r) for r in rows)
    if n_cols == 0:
        return ""
    padded = [(list(r) + [""] * (n_cols - len(r)))[:n_cols] for r in rows]
    # Fill all-empty header with generic names
    if not any(padded[0]):
        padded[0] = [f"Col {i + 1}" for i in range(n_cols)]
    lines: List[str] = []
    for idx, row in enumerate(padded):
        escaped = [c.replace("|", "\\|") for c in row]
        lines.append("| " + " | ".join(escaped) + " |")
        if idx == n_header - 1:
            lines.append("| " + " | ".join("---" for _ in range(n_cols)) + " |")
    return "\n".join(lines)


# ─── main class ─────────────────────────────────────────────────────────────

class TableStructureInferer:
    """Convert Page.table_candidates into TableBlock objects using grid inference.

    Runs early in the pipeline (right after StatsProcessor), before block
    assembly, so that TableBlocks are correctly positioned in page.items when
    downstream processors see the page.
    """

    # Clustering / assignment tolerances
    X_THRESH: float = 12.0   # pt — merge x-positions this close into one column
    Y_THRESH: float =  5.0   # pt — merge y-positions this close into one row
    COL_TOL:  float = 18.0   # pt — tolerance for assigning a span to nearest column

    # Quality gates
    MIN_DENSITY:  float = 0.28  # min fraction of grid cells with content
    MIN_GFM_ROWS: int   = 2     # need at least separator + 1 data row

    # Objective terms (IMPROVEMENTS.md Step 5)
    LAMBDA_C: float = 0.03  # column-anchor complexity penalty
    LAMBDA_T: float = 0.02  # row-anchor complexity penalty
    LAMBDA_M: float = 0.06  # merge complexity penalty
    LAMBDA_E: float = 0.90  # implausible empty-cell penalty
    LAMBDA_O: float = 0.30  # overlap/conflict penalty
    ACCEPT_MARGIN: float = 0.05  # require prose-vs-table separation

    def transform(self, result: ParseResult) -> ParseResult:
        for page in result.pages:
            candidates: List[TableCandidate] = getattr(page, "table_candidates", [])
            if not candidates:
                continue

            new_blocks: List[TableBlock] = []
            for cand in candidates:
                block = self._process(cand)
                if block is not None:
                    new_blocks.append(block)
                    diag = getattr(block, "_table_diag", {})
                    result.decision_log.append(DecisionRecord(
                        module="TableStructureInferer",
                        decision="table_candidate_accepted",
                        score=float(diag.get("margin", 1.0)),
                        confidence=float(diag.get("confidence", 0.80)),
                        features={
                            "page_index": page.index,
                            "source": getattr(cand, "source", ""),
                            "bbox": list(getattr(cand, "bbox", (0, 0, 0, 0))),
                            "table_objective": float(diag.get("table_objective", 0.0)),
                            "prose_objective": float(diag.get("prose_objective", 0.0)),
                            "grid_rows": int(diag.get("grid_rows", 0)),
                            "grid_cols": int(diag.get("grid_cols", 0)),
                            "header_rows": int(diag.get("header_rows", 0)),
                        },
                        alternatives=["reject_as_non_table"],
                    ))
                else:
                    result.decision_log.append(DecisionRecord(
                        module="TableStructureInferer",
                        decision="table_candidate_rejected",
                        score=0.0,
                        confidence=0.65,
                        features={
                            "page_index": page.index,
                            "source": getattr(cand, "source", ""),
                            "bbox": list(getattr(cand, "bbox", (0, 0, 0, 0))),
                        },
                        alternatives=["accept_as_table"],
                    ))

            if not new_blocks:
                continue

            # Remove any old TableBlocks (stale from converter, shouldn't exist
            # in new flow but keep as safety net) and re-insert the infered ones.
            self._merge_into_items(page, new_blocks)

        return result

    # ── dispatch ─────────────────────────────────────────────────────────────

    def _process(self, cand: TableCandidate) -> Optional[TableBlock]:
        if cand.source == "find_tables":
            return self._from_find_tables(cand)
        return self._from_span_lattice(cand)

    # ── PyMuPDF find_tables path ──────────────────────────────────────────────

    def _from_find_tables(self, cand: TableCandidate) -> Optional[TableBlock]:
        """Process a raw cell matrix from PyMuPDF table.extract()."""
        if not cand.raw_rows:
            return None

        rows: List[List[str]] = [list(r) for r in cand.raw_rows]

        # Skip leading all-empty rows (phantom rows from spanning header cells)
        while rows and not any(_clean(c) for c in rows[0]):
            rows.pop(0)
        if len(rows) < 2:
            return None

        # Uniform column count + clean
        n_cols = max(len(r) for r in rows)
        rows = [
            [_clean(c) for c in (r + [None] * max(0, n_cols - len(r)))]
            for r in rows
        ]

        # Remove phantom (all-empty) columns
        rows = _remove_phantom_cols(rows)
        if not rows or not rows[0]:
            return None
        n_cols = len(rows[0])

        # Merge wrapped continuation rows
        rows = _merge_continuation(rows)
        if len(rows) < 2:
            return None

        # Gate: reject if header cells abnormally long (prose mis-detection).
        # Use a generous per-cell limit and require >40% of cells to be long
        # before rejecting — a single long header cell is still a valid table.
        headers = rows[0]
        if not any(headers):
            return None
        long_cells = sum(1 for h in headers if len(h) > 150)
        if len(headers) > 1 and long_cells > len(headers) * 0.4:
            return None

        n_header = _detect_header_count(rows)

        # Objective-based acceptance: table hypothesis must beat prose/layout alt.
        table_h = self._build_hypothesis(
            x_anchors=list(range(n_cols)),
            y_anchors=list(range(len(rows))),
            rows=rows,
            header_rows=n_header,
            overlap_cost=0.0,
            merge_count=max(0, len(cand.raw_rows) - len(rows)),
        )
        prose_obj = self._prose_objective(rows)
        margin = prose_obj - table_h.objective
        if margin <= self.ACCEPT_MARGIN:
            return None

        md = _render_gfm(rows, n_header)
        if not md or md.count("\n") < self.MIN_GFM_ROWS:
            return None

        block = TableBlock(y=cand.bbox[1], markdown=md, bbox=cand.bbox)
        block._table_diag = {
            "table_objective": table_h.objective,
            "prose_objective": prose_obj,
            "margin": margin,
            "confidence": self._confidence_from_margin(margin),
            "grid_rows": len(rows),
            "grid_cols": n_cols,
            "header_rows": n_header,
        }
        return block

    # ── text-detect / span lattice path ──────────────────────────────────────

    def _from_span_lattice(self, cand: TableCandidate) -> Optional[TableBlock]:
        """Infer table grid from raw span xy-positions via 1-D anchor clustering."""
        spans = cand.raw_spans
        if len(spans) < 4:
            return None

        # 1. Column anchors: cluster x-positions
        x_anchors = _leftmost_anchors([s["x"] for s in spans], self.X_THRESH)
        if len(x_anchors) < 2:
            return None

        # 2. Row anchors: cluster y-positions
        y_vals = [s.get("y", s.get("y_actual", 0.0)) for s in spans]
        y_anchors = _leftmost_anchors(y_vals, self.Y_THRESH)
        if len(y_anchors) < 2:
            return None

        n_cols = len(x_anchors)
        n_rows = len(y_anchors)

        # 3. Assign spans to grid cells
        grid: List[List[List[str]]] = [
            [[] for _ in range(n_cols)] for _ in range(n_rows)
        ]
        for span in spans:
            sy = span.get("y", span.get("y_actual", 0.0))
            ri = self._nearest_y(sy, y_anchors)
            ci = self._assign_col(span["x"], x_anchors)
            if 0 <= ri < n_rows and 0 <= ci < n_cols:
                text = _clean(span.get("text", ""))
                if text:
                    grid[ri][ci].append(text)

        # 4. Collapse to text rows
        rows: List[List[str]] = [
            [" ".join(grid[r][c]) for c in range(n_cols)]
            for r in range(n_rows)
        ]

        # Skip leading empty rows
        while rows and not any(rows[0]):
            rows.pop(0)
        if len(rows) < 2:
            return None

        # Merge wrapped continuation rows
        rows = _merge_continuation(rows)
        if len(rows) < 2:
            return None

        # Density gate: reject sparse grids (likely not a table)
        total  = len(rows) * n_cols
        filled = sum(1 for r in rows for c in r if c)
        if filled / max(total, 1) < self.MIN_DENSITY:
            return None

        # Simple overlap conflict estimate: heavily populated cells indicate
        # ambiguous assignments and should increase objective.
        overlap_conflicts = sum(
            1 for r in range(n_rows) for c in range(n_cols)
            if len(grid[r][c]) > 3
        ) / max(total, 1)

        # Gate: reject if header too long (prose mis-detection)
        if any(len(h) > 80 for h in rows[0]):
            return None

        table_h = self._build_hypothesis(
            x_anchors=x_anchors,
            y_anchors=y_anchors,
            rows=rows,
            header_rows=1,
            overlap_cost=overlap_conflicts,
            merge_count=0,
        )
        prose_obj = self._prose_objective(rows)
        margin = prose_obj - table_h.objective
        if margin <= self.ACCEPT_MARGIN:
            return None

        md = _render_gfm(rows, 1)
        if not md or md.count("\n") < self.MIN_GFM_ROWS:
            return None

        block = TableBlock(y=cand.bbox[1], markdown=md, bbox=cand.bbox)
        block._table_diag = {
            "table_objective": table_h.objective,
            "prose_objective": prose_obj,
            "margin": margin,
            "confidence": self._confidence_from_margin(margin),
            "grid_rows": len(rows),
            "grid_cols": n_cols,
            "header_rows": 1,
        }
        return block

    def _build_hypothesis(self,
                          x_anchors: List[float],
                          y_anchors: List[float],
                          rows: List[List[str]],
                          header_rows: int,
                          overlap_cost: float,
                          merge_count: int) -> GridHypothesis:
        """Build a scored grid hypothesis using penalized objective terms."""
        n_rows = len(rows)
        n_cols = max((len(r) for r in rows), default=0)
        total = max(1, n_rows * max(1, n_cols))

        # Implausible empty cells: empty cells inside otherwise filled rows.
        implausible_empty = 0
        for r in rows:
            filled = sum(1 for c in r if c)
            if 0 < filled < len(r):
                implausible_empty += sum(1 for c in r if not c)
        empty_cost = implausible_empty / total

        objective = (
            self.LAMBDA_C * len(x_anchors)
            + self.LAMBDA_T * len(y_anchors)
            + self.LAMBDA_M * float(merge_count)
            + self.LAMBDA_E * empty_cost
            + self.LAMBDA_O * overlap_cost
        )

        return GridHypothesis(
            x_anchors=x_anchors,
            y_anchors=y_anchors,
            header_row_count=header_rows,
            objective=objective,
            diagnostics={
                "implausible_empty_ratio": empty_cost,
                "overlap_cost": overlap_cost,
                "merge_count": merge_count,
            },
        )

    def _prose_objective(self, rows: List[List[str]]) -> float:
        """Alternative explanation score: lower means table-like, higher prose-like.

        We intentionally set prose objective higher when rows look grid-regular
        (many short cells per row), and lower when each row is sentence-like.
        """
        if not rows:
            return 0.0

        cell_counts = [sum(1 for c in r if c) for r in rows]
        avg_cells = sum(cell_counts) / max(len(cell_counts), 1)
        sentence_like = 0
        for r in rows:
            text = " ".join(c for c in r if c)
            if len(text.split()) >= 14 and ("." in text or "," in text):
                sentence_like += 1
        sentence_ratio = sentence_like / max(len(rows), 1)

        # More cells/row -> more table-like -> increase prose objective.
        # More sentence-like rows -> more prose-like -> decrease prose objective.
        return 1.6 + 0.35 * avg_cells - 0.6 * sentence_ratio

    @staticmethod
    def _confidence_from_margin(margin: float) -> float:
        if margin <= 0:
            return 0.05
        if margin >= 2.0:
            return 0.98
        # Linearized sigmoid-like confidence for lightweight diagnostics.
        return max(0.05, min(0.98, 0.35 + 0.3 * margin))

    # ── geometry helpers ──────────────────────────────────────────────────────

    def _nearest_y(self, y: float, anchors: List[float]) -> int:
        """Index of the nearest y-anchor (unconstrained — always returns valid index)."""
        best_i, best_d = 0, float("inf")
        for i, a in enumerate(anchors):
            d = abs(y - a)
            if d < best_d:
                best_d, best_i = d, i
        return best_i

    def _assign_col(self, x: float, anchors: List[float]) -> int:
        """Rightmost anchor whose position is ≤ x + tolerance."""
        for i in range(len(anchors) - 1, -1, -1):
            if x >= anchors[i] - 5:
                return i
        return 0

    # ── item merging ──────────────────────────────────────────────────────────

    def _merge_into_items(self, page: Any, new_blocks: List[TableBlock]) -> None:
        """Replace stale TableBlocks with new ones, preserving y-order."""
        non_table = [it for it in page.items if not isinstance(it, TableBlock)]
        tb_sorted = sorted(new_blocks, key=lambda t: t.y)
        tb_idx    = 0
        merged: List[Any] = []

        for item in non_table:
            if hasattr(item, "y"):
                item_y = item.y
            elif isinstance(item, LineBlock) and item.lines:
                item_y = item.lines[0].y
            else:
                item_y = 0.0

            while tb_idx < len(tb_sorted) and tb_sorted[tb_idx].y <= item_y:
                merged.append(tb_sorted[tb_idx])
                tb_idx += 1
            merged.append(item)

        merged.extend(tb_sorted[tb_idx:])
        page.items = merged
