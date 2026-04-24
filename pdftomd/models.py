from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict, Tuple
from enum import Enum

class BlockType(Enum):
    H1 = "# "
    H2 = "## "
    H3 = "### "
    H4 = "#### "
    H5 = "##### "
    H6 = "###### "
    PARAGRAPH = ""
    LIST = "- "
    CODE = "```"
    QUOTE = "> "
    TOC = "  - "
    FOOTNOTE = "(^"
    TABLE = ""  # Tables are rendered directly as GFM markdown

@dataclass
class Word:
    text: str
    is_bold: bool = False
    is_italic: bool = False
    is_superscript: bool = False
    is_link: bool = False
    
    @property
    def is_bold_italic(self) -> bool:
        return self.is_bold and self.is_italic

@dataclass
class LineItem:
    x: float
    y: float
    width: float
    height: float
    words: List[Word] = field(default_factory=list)
    font: str = ""
    color: int = 0      # PyMuPDF color integer for style signature detection
    flags: int = 0      # Font flags (bold/italic/monospace etc)
    
    def get_text(self) -> str:
        return " ".join([w.text for w in self.words])

@dataclass
class LineBlock:
    lines: List[LineItem] = field(default_factory=list)
    block_type: BlockType = BlockType.PARAGRAPH
    indent_level: int = 0
    toc_level: Optional[int] = None 
    
    def get_text(self) -> str:
        return " ".join([line.get_text() for line in self.lines])

@dataclass
class TableBlock:
    """A detected table rendered as GFM markdown."""
    y: float  # Vertical position for sorting with other blocks
    markdown: str  # Pre-rendered GFM table
    bbox: tuple = (0, 0, 0, 0)  # Bounding box to mask text extraction

@dataclass
class Page:
    index: int
    items: List[Any] = field(default_factory=list)
    width: float = 595.0
    height: float = 842.0
    span_rows: List[Any] = field(default_factory=list)    # List[SpanRow], populated by converter
    layout_bands: List[Any] = field(default_factory=list) # List[LayoutBand], set by LayoutBandSegmenter
    table_candidates: List[Any] = field(default_factory=list)  # List[TableCandidate], set by converter

@dataclass
class TOCEntry:
    text: str
    page_num: int
    level: int

@dataclass
class FootnoteRef:
    """A superscript footnote reference in the body text."""
    number: str
    page_index: int
    line_index: int  # For tracking position

@dataclass
class ParseResult:
    pages: List[Page]
    metadata: Dict[str, Any] = field(default_factory=dict)
    globals: Dict[str, Any] = field(default_factory=dict)
    toc_entries: List[TOCEntry] = field(default_factory=list)
    footnote_refs: List[FootnoteRef] = field(default_factory=list)  # Semantic footnotes
    span_atoms: List[Any] = field(default_factory=list)             # List[SpanAtom]
    span_rows: List[Any] = field(default_factory=list)              # Flattened page SpanRows
    layout_bands: List[Any] = field(default_factory=list)           # Flattened page LayoutBands
    region_candidates: List[Any] = field(default_factory=list)      # List[RegionCandidate]
    decision_log: List[Any] = field(default_factory=list)           # List[DecisionRecord]


# ─── New structured inference primitives (v0.5) ─────────────────────────────

class StyleRole(Enum):
    """Latent typography role inferred by StyleRoleInferer."""
    BODY          = "body"
    HEADING_1     = "heading_1"
    HEADING_2     = "heading_2"
    HEADING_3     = "heading_3"
    HEADING_4     = "heading_4"
    CAPTION       = "caption"
    FOOTNOTE      = "footnote"
    CODE          = "code"
    RUNNING_HEADER = "running_header"
    RUNNING_FOOTER = "running_footer"
    TOC           = "toc"
    LIST_ITEM     = "list_item"
    UNKNOWN       = "unknown"


@dataclass
class SpanAtom:
    """Raw PDF span primitive from PyMuPDF — pre-LineItem geometry."""
    x0: float
    y0: float
    x1: float
    y1: float
    baseline_y: float
    text: str
    font: str
    size: float
    color: int
    flags: int
    block_id: int
    line_id: int


@dataclass
class SupportInterval:
    """Merged x-support interval within a span row."""
    x0: float
    x1: float


@dataclass
class SpanRow:
    """Rowized support abstraction — one visual line's x-intervals."""
    y: float
    intervals: List[SupportInterval] = field(default_factory=list)
    band_id: Optional[str] = None  # Set by LayoutBandSegmenter


@dataclass
class LayoutBand:
    """Vertical page band with inferred layout state."""
    y0: float
    y1: float
    chosen_state: str = "single"
    state_scores: Dict[str, float] = field(default_factory=dict)
    features: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BlockBoundaryEvidence:
    """Probabilistic evidence for/against a block boundary between two lines."""
    same_block_prob: float
    features: Dict[str, float] = field(default_factory=dict)


@dataclass
class RegionCandidate:
    """Generic scored region candidate (column gutter, table zone, etc.)."""
    bbox: Tuple[float, float, float, float]
    kind: str
    score: float
    features: Dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass
class TableCandidate:
    """Candidate table region before grid hypothesis fitting."""
    bbox: Tuple[float, float, float, float]
    source: str = "text_detect"                          # "find_tables" | "text_detect"
    raw_rows: List[List[Any]] = field(default_factory=list)   # from PyMuPDF table.extract()
    raw_spans: List[Dict[str, Any]] = field(default_factory=list)  # span dicts for lattice path
    row_ids: List[int] = field(default_factory=list)
    span_ids: List[int] = field(default_factory=list)
    x_anchor_candidates: List[float] = field(default_factory=list)
    y_anchor_candidates: List[float] = field(default_factory=list)
    features: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class GridHypothesis:
    """Best-fit table grid: column/row anchors + cell assignments."""
    x_anchors: List[float] = field(default_factory=list)
    y_anchors: List[float] = field(default_factory=list)
    assignments: Dict = field(default_factory=dict)
    merge_map: Dict = field(default_factory=dict)
    header_row_count: int = 1
    objective: float = 0.0
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionRecord:
    """Audit record for a significant pipeline decision."""
    module: str
    decision: str
    score: float
    confidence: float
    features: Dict[str, Any] = field(default_factory=dict)
    alternatives: List[str] = field(default_factory=list)
