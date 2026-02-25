# pdftomd

**pdftomd** is a Python port of the `opengovsg/pdf2md` library, enhanced with pro-level features that surpass the original. It uses geometric heuristics and statistical analysis of PDF coordinates to reconstruct structural Markdown.

## Features

### Core (Parity with JS original)
- **Heuristic Engine:** Uses document-wide font statistics to identify headers and paragraphs.
- **Smart Cleaning:** Mathematical detection of page number sequences and repetitive headers/footers.
- **Structural Integrity:** Detects nested lists via X-coordinate alignment and preserves Table of Contents formatting.
- **Fancy CLI:** Beautiful, colorful terminal progress bars for bulk conversion.

### Pro Features (Beyond the original)
- **Table Detection:** Detects bordered tables via PyMuPDF's `find_tables()` and borderless text tables via span-alignment analysis. Both are rendered as GitHub-Flavored Markdown (GFM) tables.
- **Smart De-hyphenation:** Uses `TEXT_DEHYPHENATE` flag for accurate hyphen removal across line breaks.
- **Semantic Footnotes:** Superscript numbers become clickable `[^1]` footnote links matched with `[^1]:` definitions.
- **Multi-Column Awareness:** Uses `sort=True` for proper reading flow in multi-column layouts.
- **Style Signature Detection:** Identifies headers by font/color deviation, not just size.
- **Running Header/Footer Removal:** Removes page numbers, single running headers, and alternating odd/even running headers.
- **Parallel Processing:** Convert thousands of PDFs with multiprocessing (`-w` flag).

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
```
