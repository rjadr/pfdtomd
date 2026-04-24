# pdftomd

**pdftomd** is a Python PDF-to-Markdown converter built on PyMuPDF. It uses a multi-stage heuristic pipeline — geometric layout analysis, document-wide font statistics, probabilistic block boundary scoring, and semantic role inference — to reconstruct high-quality Markdown from text-based PDFs.

## Features

### Core (Parity with JS original)
- **Heuristic Engine:** Uses document-wide font statistics to identify headers and paragraphs.
- **Smart Cleaning:** Mathematical detection of page number sequences and repetitive headers/footers.
- **Structural Integrity:** Detects nested lists via X-coordinate alignment and preserves Table of Contents formatting.
- **Fancy CLI:** Beautiful, colorful terminal progress bars for bulk conversion.

### Pro Features (Beyond the original)
- **Table Detection:** Detects bordered tables via PyMuPDF's `find_tables()` and borderless text tables via span-alignment analysis. Both are rendered as GitHub-Flavored Markdown (GFM) tables. Phantom columns and wrapped continuation rows are merged automatically.
- **Smart De-hyphenation:** Uses `TEXT_DEHYPHENATE` flag for accurate hyphen removal across line breaks.
- **Semantic Footnotes:** Superscript numbers become clickable `[^1]` footnote links matched with `[^1]:` definitions.
- **Multi-Column Awareness:** Band-wise histogram layout segmentation detects column gutters and re-sorts lines into correct left→right reading order before any downstream processing.
- **Style Signature Detection:** Identifies headers by font/color deviation, not just size. `StyleRoleInferer` assigns latent typography roles; `HeaderDecoder` smooths sequence consistency and merges wrapped heading lines.
- **Running Header/Footer Removal:** Fuzzy position + style + text clustering removes page numbers, single running headers, and alternating odd/even running headers across the document.
- **Sequence-Aware Lists:** `ListStructureInferer` tracks list-marker sequences to correctly assign nested levels.
- **Parallel Processing:** Convert thousands of PDFs with multiprocessing (`-w` flag).
- **Ablation Controls:** Selectively disable pipeline stages without editing code via `PDFTOMD_DISABLE_PROCESSORS=Stage1,Stage2`.

## Installation

```bash
# Clone the repo and install
pip install .

# Optional: pandas for better table formatting
pip install pandas tabulate
```

## Usage

### CLI
```bash
# Convert a single PDF
pdftomd paper.pdf -o result.md

# Convert a folder of PDFs recursively
pdftomd ./library -o ./output -r

# Bulk convert with 8 parallel workers
pdftomd ./library -o ./output -r -w 8

# Insert horizontal rules between pages
pdftomd paper.pdf -o result.md --page-breaks
```

### Library
```python
import pdftomd

# Basic conversion
md = pdftomd.convert("my_document.pdf")
print(md)

# With page break separators
md = pdftomd.convert("my_document.pdf", page_breaks=True)

# Per-page content (returns list[str], one string per page)
pages = pdftomd.convert_pages("my_document.pdf")
page_map = {(i + 1, ""): text for i, text in enumerate(pages)}
```

---

## Changelog

### v0.6.0

**Pipeline rewrite — 17-stage architecture** (was 10 stages in v0.2)

#### New processors
- **`LayoutBandSegmenter`** (`structure.py`) — Replaces the naive inline column-gap split. Builds a horizontal-projection occupancy histogram across each page to detect gutter valleys, then re-sorts `LineItem`s into correct column reading order (left column top→bottom, then right) before any downstream stage sees them. Falls back gracefully for single-column pages (no gutter detected → no reorder).
- **`BlockBoundaryScorer`** (`structure.py`) — Probabilistic block boundary scoring. Assigns a float confidence score to each candidate line break based on vertical gap, font-size change, indent shift, and list-marker presence. Replaces the hard gap-threshold approach.
- **`BlockAssembler`** (`structure.py`) — Assembles `LineItem`s into `LineBlock`s using the scored boundaries from `BlockBoundaryScorer`. Cleanly separates the scoring logic from the grouping logic.
- **`TableStructureInferer`** (`processors/tables.py`, new file) — Dedicated table detection stage. Runs `page.find_tables()` (PyMuPDF ≥1.23) as primary strategy and a span-alignment borderless-table detector as fallback. Extracts tables before the line-grouping stages, removes their spans from the normal pipeline, and injects pre-rendered GFM blocks. Handles: phantom columns (entirely-None columns across all rows), continuation rows (wrapped cell text merged back into the preceding row), and multi-row headers.
- **`RunningElementTemplateInferer`** (`cleaning.py`) — Replaces `RepetitiveElementRemover`. Clusters candidate lines by `(zone, page-parity, x-bucket, style-signature, normalised-text)` to catch alternating odd/even running headers (each appearing on only ~50% of pages), previously invisible to the 67%-threshold approach. Configurable zone fractions (top 10%, bottom 8% of page height).
- **`StyleRoleInferer`** (`semantics.py`) — Infers latent typography roles (heading tier, caption, pull-quote) from font-size percentile ranks and color deviations across the document, independent of absolute font sizes. Provides a second opinion to `HeaderDetector`.
- **`HeaderDecoder`** (`semantics.py`) — Sequence-consistency smoother. After `StyleRoleInferer` assigns roles, `HeaderDecoder` resolves conflicts between heuristic and style-role assignments, then merges consecutive same-level heading lines that are separated by normal line-wrap spacing (gap < 2.5× font height, combined length < 200 chars).
- **`ListStructureInferer`** (`semantics.py`) — Sequence-aware list detection. Tracks bullet/number marker sequences across `LineBlock`s to assign correct nesting levels, fixing cases where the original `ListDetector` produced flat lists from visually indented content.
- **`FootnoteAssignmentSolver`** (`semantics.py`) — Improved footnote matching. Resolves ambiguous inline footnote numbers against page-bottom note text using a bipartite assignment approach, reducing orphaned `[^N]` links in documents with dense footnote clusters.

#### Improvements to existing code
- **`_table_to_gfm()`** — Phantom column removal (empty columns stripped before GFM rendering); continuation row merging (rows with an empty anchor cell are appended to the previous row's cells).
- **`_detect_text_tables()`** — Column positions now derived from the union of all anchor rows (not just the first), catching tables where only data rows define a column. Added validation guards to reject false positives (empty headers, header cells > 40 chars).
- **Multi-column fallback** — Legacy `sort=True` page-level reorder is retained as a fallback behind `PDFTOMD_ENABLE_LEGACY_COLUMN_REORDER` env var for ablation comparisons.
- **Ablation env vars** — `PDFTOMD_DISABLE_PROCESSORS=Name1,Name2` lets you remove any stage by class name without editing code. `PDFTOMD_ENABLE_LEGACY_REPETITIVE=1` re-enables the old `RepetitiveElementRemover` alongside the new template inferer.

#### New data models (`models.py`)
`StyleRole`, `SpanAtom`, `SupportInterval`, `SpanRow`, `LayoutBand`, `BlockBoundaryEvidence`, `RegionCandidate`, `TableCandidate`, `GridHypothesis`, `DecisionRecord`

---

### v0.2.0
- Initial release, inspired by `opengovsg/pdf2md`
- `[^N]` semantic footnote linking
- `convert()` / `convert_pages()` public API
- Typer CLI with Rich progress bars, `--page-breaks` flag, `-w` parallel workers
