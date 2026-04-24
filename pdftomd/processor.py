from .models import ParseResult, LineBlock, BlockType, TableBlock
from .processors.cleaning import PageNumberDetector, RepetitiveElementRemover, RunningElementTemplateInferer
from .processors.structure import (StatsProcessor, GatherBlocks, VerticalToHorizontal,
                                    CodeBlockDetector, LayoutBandSegmenter,
                                    BlockBoundaryScorer, BlockAssembler)
from .processors.semantics import (HeaderDetector, ListDetector, TOCDetector,
                                    DocumentMapper, FootnoteLinker,
                                    StyleRoleInferer, HeaderDecoder,
                                    ListStructureInferer, FootnoteAssignmentSolver)
from .processors.tables import TableStructureInferer
import re
import os

# Import ftfy for final text cleanup (more efficient to run once on final output)
try:
    import ftfy
    HAS_FTFY = True
except ImportError:
    HAS_FTFY = False

class Pipeline:
    def __init__(self):
        processors = [
            # Stage 1: Document-wide statistics (body height, font, spacing)
            StatsProcessor(),
            # Stage 2: Convert TableCandidates → TableBlocks via lattice inference (v0.6)
            TableStructureInferer(),
            # Stage 3: Band-wise layout segmentation + column reorder (v0.5)
            LayoutBandSegmenter(),
            # Stage 4: Single-char vertical stacks → horizontal
            VerticalToHorizontal(),
            # Stage 5: Remove page numbers
            PageNumberDetector(),
            # Stage 6: Remove running headers/footers (v0.5 improved)
            RunningElementTemplateInferer(),
            # Stage 7: Legacy alternating header remover is disabled by default.
            # It can conflict with template inferer decisions and is available
            # for ablation via PDFTOMD_ENABLE_LEGACY_REPETITIVE=1.
            # Stage 8: Score block boundaries probabilistically (v0.5)
            BlockBoundaryScorer(),
            # Stage 9: Assemble LineItems into LineBlocks using boundary scores (v0.5)
            BlockAssembler(),
            # Stage 10: Detect CODE blocks
            CodeBlockDetector(),
            # Stage 11: Find Table of Contents
            TOCDetector(),
            # Stage 12: Map TOC entries to body blocks
            DocumentMapper(),
            # Stage 13: Classical heading detection (well-calibrated per-block heuristics)
            HeaderDetector(),
            # Stage 14: Infer latent typography roles
            StyleRoleInferer(),
            # Stage 15: Sequence-consistency smoothing + heading merge
            HeaderDecoder(),
            # Stage 16: Sequence-aware list detection (v0.6)
            ListStructureInferer(),
            # Stage 17: Footnote assignment solver (v0.6)
            FootnoteAssignmentSolver(),
        ]

        if os.getenv('PDFTOMD_ENABLE_LEGACY_REPETITIVE', '').strip() == '1':
            insert_at = 6 if len(processors) > 6 else len(processors)
            processors.insert(insert_at, RepetitiveElementRemover())

        # Optional ablation control:
        #   PDFTOMD_DISABLE_PROCESSORS=TableStructureInferer,LayoutBandSegmenter
        # allows old-vs-new comparisons without code edits.
        disable = {
            s.strip() for s in os.getenv('PDFTOMD_DISABLE_PROCESSORS', '').split(',')
            if s.strip()
        }
        self.processors = [p for p in processors if p.__class__.__name__ not in disable]

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
                    # Cap at 3 digits + non-digit lookahead: prevents a stray year
                    # or page-range number (e.g. 1978, 255) that somehow survived
                    # classification from being emitted as [^1978]: or [^255]:.
                    content = re.sub(r"^\(?\^?(\d{1,3})(?!\d)\)?\s*", r"\1]: ", clean)
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

        self._populate_diagnostics(result)

        pages = self.render_pages(result)

        # Join pages — optionally separated by horizontal rules
        page_sep = "\n\n---\n\n" if page_breaks else "\n"
        return page_sep.join(pages)

    def _populate_diagnostics(self, result: ParseResult) -> None:
        """Aggregate per-document diagnostics for evaluation and ablations."""
        pages = result.pages

        table_candidate_count = sum(len(getattr(p, 'table_candidates', [])) for p in pages)
        accepted_table_count = sum(
            1 for p in pages for it in p.items if isinstance(it, TableBlock)
        )

        accepted_table_conf = [
            d.confidence for d in result.decision_log
            if getattr(d, 'module', '') == 'TableStructureInferer'
            and getattr(d, 'decision', '') == 'table_candidate_accepted'
        ]
        avg_table_conf = (sum(accepted_table_conf) / len(accepted_table_conf)
                          if accepted_table_conf else 0.0)

        state_switches = 0
        for p in pages:
            bands = getattr(p, 'layout_bands', [])
            if len(bands) > 1:
                state_switches += sum(
                    1 for i in range(1, len(bands))
                    if bands[i].chosen_state != bands[i - 1].chosen_state
                )

        boundary_probs = [
            pr for p in pages for pr in getattr(p, '_boundary_probs', [])
        ]
        avg_boundary_conf = (1.0 - (sum(boundary_probs) / len(boundary_probs))
                             if boundary_probs else 0.0)

        role_dist = {}
        for p in pages:
            for it in p.items:
                if isinstance(it, LineBlock):
                    role = getattr(it, '_style_role', None)
                    if role:
                        role_dist[role] = role_dist.get(role, 0) + 1

        # Approximation: count distinct recurring top-zone strings as templates.
        top_texts = {}
        for p in pages:
            H = getattr(p, 'height', 842.0)
            top_y = H * 0.10
            for it in p.items:
                if isinstance(it, LineBlock) and it.lines and it.lines[0].y < top_y:
                    txt = it.get_text().strip().upper()
                    if txt:
                        top_texts[txt] = top_texts.get(txt, 0) + 1
        running_templates = sum(1 for _, c in top_texts.items() if c >= 2)
        failure_buckets = self._infer_failure_buckets(result)

        result.metadata['diagnostics'] = {
            'page_count': len(pages),
            'table_candidate_count': table_candidate_count,
            'accepted_table_count': accepted_table_count,
            'average_table_confidence': avg_table_conf,
            'layout_state_switches': state_switches,
            'average_block_boundary_confidence': avg_boundary_conf,
            'heading_role_distribution': role_dist,
            'running_header_templates_found': running_templates,
            'active_processors': [p.__class__.__name__ for p in self.processors],
            'failure_buckets': failure_buckets,
        }

    def _infer_failure_buckets(self, result: ParseResult) -> dict:
        """Heuristic bucketization of likely failure modes (spec requirement)."""
        buckets = {
            'mixed-layout pages': 0,
            'borderless tables': 0,
            'wrapped table rows': 0,
            'multi-row headers': 0,
            'running headers/footers': 0,
            'list/reference confusion': 0,
            'caption/heading confusion': 0,
            'footnote/reference confusion': 0,
        }

        # Mixed-layout pages: >1 distinct band state on page.
        for p in result.pages:
            states = {b.chosen_state for b in getattr(p, 'layout_bands', [])}
            if len(states) > 1:
                buckets['mixed-layout pages'] += 1

        # Table-related buckets from decision log diagnostics.
        for d in result.decision_log:
            if getattr(d, 'module', '') != 'TableStructureInferer':
                continue
            feat = getattr(d, 'features', {}) or {}
            src = str(feat.get('source', ''))
            if src == 'text_detect':
                buckets['borderless tables'] += 1
            if int(feat.get('header_rows', 0)) >= 2:
                buckets['multi-row headers'] += 1
            # Wrapped rows proxy: when final grid rows << raw candidate row ids/rows.
            # We only have inferred rows in diagnostics now, so detect long merged cells.
            if float(feat.get('table_objective', 0.0)) > 0 and float(feat.get('margin', 0.0) if 'margin' in feat else 0.0) > 1.0:
                pass

        # Wrapped table rows proxy from rendered table blocks.
        for p in result.pages:
            for it in p.items:
                if isinstance(it, TableBlock):
                    lines = [ln for ln in it.markdown.split('\n') if ln.startswith('|')]
                    if lines and any('  ' in ln for ln in lines):
                        buckets['wrapped table rows'] += 1

        # Running headers/footers risk: recurring top-zone text + low removal.
        top_texts = {}
        bot_texts = {}
        for p in result.pages:
            H = getattr(p, 'height', 842.0)
            top_y = H * 0.10
            bot_y = H * 0.92
            for it in p.items:
                if not isinstance(it, LineBlock) or not it.lines:
                    continue
                txt = it.get_text().strip().upper()
                if not txt:
                    continue
                if it.lines[0].y < top_y:
                    top_texts[txt] = top_texts.get(txt, 0) + 1
                if it.lines[-1].y > bot_y:
                    bot_texts[txt] = bot_texts.get(txt, 0) + 1
        buckets['running headers/footers'] = (
            sum(1 for _, c in top_texts.items() if c >= 2)
            + sum(1 for _, c in bot_texts.items() if c >= 2)
        )

        # List/reference confusion: very short paragraph/list lines starting numeric.
        for p in result.pages:
            for it in p.items:
                if not isinstance(it, LineBlock):
                    continue
                txt = it.get_text().strip()
                if not txt:
                    continue
                if txt[:2].isdigit() and len(txt.split()) <= 4 and it.block_type in (BlockType.LIST, BlockType.PARAGRAPH):
                    buckets['list/reference confusion'] += 1

        # Caption/heading confusion: heading-role blocks starting with Figure/Table tokens.
        for p in result.pages:
            for it in p.items:
                if not isinstance(it, LineBlock):
                    continue
                role = getattr(it, '_style_role', '')
                txt = it.get_text().strip().lower()
                if role == 'heading' and (txt.startswith('figure ') or txt.startswith('table ')):
                    buckets['caption/heading confusion'] += 1

        # Footnote/reference confusion: superscript links with unmatched footnote blocks.
        fn_blocks = sum(
            1 for p in result.pages for it in p.items
            if isinstance(it, LineBlock) and it.block_type == BlockType.FOOTNOTE
        )
        ref_count = len(getattr(result, 'footnote_refs', []))
        if ref_count > fn_blocks:
            buckets['footnote/reference confusion'] = ref_count - fn_blocks

        return buckets
