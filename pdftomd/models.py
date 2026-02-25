from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict
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
