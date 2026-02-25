from .models import ParseResult, LineBlock, BlockType, TableBlock
from .processors.cleaning import PageNumberDetector, RepetitiveElementRemover
from .processors.structure import StatsProcessor, GatherBlocks, VerticalToHorizontal, CodeBlockDetector
from .processors.semantics import HeaderDetector, ListDetector, TOCDetector, DocumentMapper, FootnoteLinker
import re

# Import ftfy for final text cleanup (more efficient to run once on final output)
try:
    import ftfy
    HAS_FTFY = True
except ImportError:
    HAS_FTFY = False

class Pipeline:
    def __init__(self):
        self.processors = [
            StatsProcessor(), 
            VerticalToHorizontal(), 
            PageNumberDetector(),
            RepetitiveElementRemover(), 
            GatherBlocks(), 
            CodeBlockDetector(),
            TOCDetector(),      
            DocumentMapper(),   
            HeaderDetector(),   
            ListDetector(),
            FootnoteLinker(),   # Semantic footnote linking (Pro Feature #4)
        ]

    def render_pages(self, result: ParseResult) -> list:
        """Return a list of per-page markdown strings (after pipeline processing)."""
        max_font = result.globals.get('max_height_font', '')
        pages_output = []

        for page in result.pages:
            page_blocks = []
            for block in page.items:
                # === TABLE BLOCKS: Render directly as GFM markdown ===
                if isinstance(block, TableBlock):
                    page_blocks.append(block.markdown)
                    continue
                    
                if not isinstance(block, LineBlock): continue
                
                # Check for Block-Level Continuity (Optimization)
                all_words = [w for l in block.lines for w in l.words]
                if not all_words: continue
                is_header_block = block.block_type in (
                    BlockType.H1, BlockType.H2, BlockType.H3,
                    BlockType.H4, BlockType.H5, BlockType.H6,
                )
                # Headers don't need bold/italic wrapping — the # prefix makes it clear.
                # For paragraphs, bold-wrap if the entire block is bold.
                is_block_bold = (not is_header_block) and all(
                    w.is_bold or l.font == max_font for l in block.lines for w in l.words
                )
                
                # For LIST blocks, check if first word is a bullet character to skip
                skip_first_word = False
                if block.block_type == BlockType.LIST and block.lines and block.lines[0].words:
                    first_word = block.lines[0].words[0].text
                    if first_word in ["-", "•", "–", "*"] or (len(first_word) <= 3 and first_word.rstrip(".):").isdigit()):
                        skip_first_word = True
                
                lines_text = []
                for line_idx, line in enumerate(block.lines):
                    line_str = ""
                    for i, word in enumerate(line.words):
                        # Skip the bullet character on first line of list items
                        if skip_first_word and line_idx == 0 and i == 0:
                            continue
                        
                        t = word.text
                        
                        # Superscript: only format as footnote link if it's a digit
                        if word.is_superscript and t.isdigit():
                            t = f"[^{t}]"
                        
                        # Apply word-level formatting if block-level isn't applicable.
                        # Headers get italic for emphasis (italicized words) but not bold
                        # (that would produce ugly "## **bold**" markers).
                        if not is_block_bold and not is_header_block:
                            if word.is_bold_italic: t = f"**_{t}_**"
                            elif word.is_bold or line.font == max_font: t = f"**{t}**"
                            elif word.is_italic: t = f"_{t}_"
                        elif is_header_block and word.is_italic and not word.is_bold:
                            t = f"_{t}_"
                        
                        if word.is_link: t = f"[{t}]({t if t.startswith('http') else 'http://'+t})"
                        
                        # JS Punctuation Rule: No space before punctuation
                        if i > 0 and t[0] not in ".,!?;:)]}":
                            line_str += " " + t
                        else:
                            line_str += t
                    # Merge adjacent same-format spans: **a** **b** → **a b**, _a_ _b_ → _a b_
                    line_str = re.sub(r'\*\* \*\*', ' ', line_str)
                    line_str = re.sub(r'_ _', ' ', line_str)
                    lines_text.append(line_str.strip())

                if block.block_type == BlockType.CODE:
                    content = "\n".join(lines_text)
                else:
                    # Join lines, de-hyphenating end-of-line hyphens.
                    # When a word is split with a hyphen at line end (e.g. "ambi-" / "tion"),
                    # join without the hyphen and without a space.
                    # Only applies when a lowercase letter precedes the hyphen AND the
                    # continuation starts with a lowercase letter (avoids compounds like X-Pro).
                    merged = []
                    for lt in lines_text:
                        if (merged
                                and merged[-1].endswith('-')
                                and len(merged[-1]) >= 2
                                and merged[-1][-2].islower()
                                and lt and lt[0].islower()):
                            merged[-1] = merged[-1][:-1] + lt
                        else:
                            merged.append(lt)
                    content = " ".join(merged)
                    # Merge bold/italic spans that cross line boundaries
                    content = re.sub(r'\*\* \*\*', ' ', content)
                    content = re.sub(r'_ _', ' ', content)
                if is_block_bold: content = f"**{content}**"

                # Prefix/Suffix
                prefix = block.block_type.value
                suffix = ""
                if block.block_type == BlockType.CODE:
                    prefix, suffix = "```\n", "\n```"
                elif block.block_type == BlockType.LIST:
                    prefix = (" " * (block.indent_level * 3)) + "- "
                elif block.block_type == BlockType.FOOTNOTE:
                    # The footnote number may be bold/italic (e.g., "**1**" or "_1_").
                    # Strip markdown formatting markers before extracting the number.
                    clean = re.sub(r'^[\*_\[\]\^]+(\d+)[\*_\[\]\^]*\s*', r'\1 ', content)
                    content = re.sub(r"^\(?\^?(\d+)\)?\s*", r"\1]: ", clean)
                    prefix = "[^"
                
                page_blocks.append(f"{prefix}{content}{suffix}")
            
            # Join blocks within a page with double newline (matching JS ToMarkdown)
            page_md = "\n\n".join(page_blocks)
            if HAS_FTFY:
                page_md = ftfy.fix_text(page_md)
            pages_output.append(page_md)

        return pages_output

    def run(self, result: ParseResult, page_breaks: bool = False) -> str:
        for proc in self.processors:
            result = proc.transform(result)

        pages = self.render_pages(result)

        # Join pages — optionally separated by horizontal rules
        page_sep = "\n\n---\n\n" if page_breaks else "\n"
        return page_sep.join(pages)
