import re
import pymupdf as fitz
from typing import List, Tuple, Optional

from services.spatial_cluster import ParagraphBlock
from services.obj_reflow import ReflowResult
from services.erase_service import safe_erase
from services.font_service import (
    font_chain,
    register_font_name,
    base14,
    rgb,
    invisible,
    _DEFAULT_CACHE,
)
from services.style_runs import (
    Style, Run, DEFAULT_STYLE, old_char_styles, new_char_styles, line_runs,
)

# "₹35,000.00", "(1,200.50)", "12.5%", "- 400"  -> a number cell
NUMERIC_CELL = re.compile(r"^[\s(+\-\u2212]*(?:[^\w\s(+\-\u2212]{1,3}\s*)?\d[\d,.\s]*\)?\s*%?\s*(?:Cr|Dr|CR|DR)?$")


def is_numeric_cell(text: str) -> bool:
    t = text.strip()
    return bool(t) and "\n" not in t and bool(NUMERIC_CELL.match(t))


def looks_right_aligned(block: ParagraphBlock, others: List[ParagraphBlock], tol: float = 1.5) -> bool:
    """A number column is right-aligned when other numeric cells share this cell's RIGHT edge
    while their LEFT edges differ. Then an edited number must keep its right edge fixed."""
    x0, _, x1, _ = block.bbox
    for o in others:
        if o is block or not is_numeric_cell(o.text):
            continue
        ox0, _, ox1, _ = o.bbox
        if abs(ox1 - x1) <= tol and abs(ox0 - x0) > tol:
            return True
    return False


# ----------------------------------------------------------------------------- layout
class Piece:
    __slots__ = ("ent", "text", "width", "substituted")

    def __init__(self, ent, text, width, substituted):
        self.ent, self.text, self.width, self.substituted = ent, text, width, substituted


def layout_run(page, doc, run: Run, cache) -> Tuple[List[Piece], str]:
    """Split a styled run into pieces that each use ONE font of the fallback chain.
    Returns (pieces, fallback_code). Resolving the chain here, BEFORE anything is erased,
    matters: apply_redactions can purge the page's embedded fonts."""
    st = run.style
    chain = font_chain(doc, page, st.font, st.flags, cache=cache)
    same = cache.get("same", {}).get((page.number, st.font, st.flags), 0)
    pieces: List[Piece] = []
    for ch in run.text:
        if invisible(ch):
            continue
        idx = next((i for i, e in enumerate(chain) if e[0].has_glyph(ord(ch))), None)
        if idx is None:
            idx = next((i for i, e in enumerate(chain) if e[0].has_glyph(63)), len(chain) - 1)
        ent = chain[idx]
        w = ent[0].text_length(ch, fontsize=st.size)
        sub = idx >= same            # not drawn with the original font / same family
        if pieces and pieces[-1].ent is ent:
            pieces[-1].text += ch
            pieces[-1].width += w
        else:
            pieces.append(Piece(ent, ch, w, sub))
    return pieces, base14(st.font, st.flags)


def draw_pieces(page, x, y, pieces, st: Style, fallback_code, cache) -> float:
    color = rgb(st.color)
    for pc in pieces:
        res_name = register_font_name(page, pc.ent, fallback_code=fallback_code, cache=cache)
        try:
            if id(pc.ent[0]) in cache.get("synth", ()):
                # only the regular weight is installed: thicken the glyphs (fill + thin stroke)
                page.insert_text((x, y), pc.text, fontname=res_name, fontsize=st.size,
                                 color=color, render_mode=2, border_width=0.03)
            else:
                page.insert_text((x, y), pc.text, fontname=res_name, fontsize=st.size, color=color)
        except Exception:
            page.insert_text((x, y), pc.text, fontname=fallback_code, fontsize=st.size, color=color)
        x += pc.width
    return x


def draw_text_with_glyph_fallback(page, doc, origin, text, fname, flags, fontsize, color,
                                  cache: Optional[dict] = None):
    """Backwards-compatible single-style entry point. Returns the x where drawing ended."""
    cache = cache if cache is not None else _DEFAULT_CACHE
    col = (round(color[0] * 255) << 16) | (round(color[1] * 255) << 8) | round(color[2] * 255)
    st = Style(fname, flags, fontsize, col)
    pieces, fb = layout_run(page, doc, Run(text, st), cache)
    return draw_pieces(page, origin[0], origin[1], pieces, st, fb, cache)


class PDFCompiler:
    def __init__(self, doc: fitz.Document, cache: Optional[dict] = None):
        self.doc = doc
        self.cache = cache if cache is not None else _DEFAULT_CACHE

    def _plan_line(self, page, x, y, runs: List[Run], report: dict):
        """-> (x, y, [(run, pieces, fallback_code)], total_width)"""
        planned, total = [], 0.0
        for r in runs:
            pieces, fb = layout_run(page, self.doc, r, self.cache)
            planned.append((r, pieces, fb))
            total += sum(p.width for p in pieces)
            for p in pieces:
                if p.substituted:
                    report["substituted"].update(c for c in p.text if not c.isspace())
        return [x, y, planned, total]

    def apply_edit_and_cascade(
        self,
        page_num: int,
        target_block: ParagraphBlock,
        reflow_res: ReflowResult,
        all_blocks: List[ParagraphBlock],
        new_text: Optional[str] = None,
        ghost: Optional[list] = None,
    ) -> dict:
        page = self.doc.load_page(page_num)
        height_delta = reflow_res.height_delta
        report = {"substituted": set()}

        target_x0, target_y0, target_x1, target_y1 = target_block.bbox
        downstream = []
        if abs(height_delta) > 0.5:
            for b in all_blocks:
                if b == target_block:
                    continue
                ox0, oy0, ox1, oy1 = b.bbox
                overlap_x = max(0.0, min(target_x1, ox1) - max(target_x0, ox0))
                if oy0 >= target_y1 and overlap_x > 0:
                    downstream.append(b)

        # ---- 1. PLAN everything first (fonts are resolved while the page is still intact)
        if new_text is None:
            new_text = "\n".join(ln.text for ln in reflow_res.lines)
        old_styles = old_char_styles(target_block)
        new_styles = new_char_styles(target_block.text, old_styles, new_text)
        default = old_styles[0] if old_styles else DEFAULT_STYLE

        plan = []
        cursor = 0
        for line in reflow_res.lines:
            runs, cursor = line_runs(line.text, new_text, new_styles, cursor, default)
            plan.append(self._plan_line(page, line.baseline_origin[0], line.baseline_origin[1], runs, report))

        # numeric cell in a right-aligned column: keep the RIGHT edge where it was
        if (len(plan) == 1 and len(target_block.lines) == 1
                and is_numeric_cell(new_text) and is_numeric_cell(target_block.text)
                and looks_right_aligned(target_block, all_blocks)):
            plan[0][0] = target_x1 - plan[0][3]

        for db in downstream:
            ds = old_char_styles(db)
            dcur = 0
            for line in db.lines:
                runs, dcur = line_runs(line.text, db.text, ds, dcur, ds[0] if ds else DEFAULT_STYLE)
                orig_x = line.tokens[0].origin[0] if line.tokens else db.bbox[0]
                plan.append(self._plan_line(page, orig_x, line.baseline_y + height_delta, runs, report))

        # ---- 2. erase
        safe_erase(page, target_block.bbox, fill_bg=False, ghost=ghost)
        for db in downstream:
            safe_erase(page, db.bbox, fill_bg=False, ghost=ghost)

        # ---- 3. draw
        for x, y, planned, _ in plan:
            for run, pieces, fb in planned:
                x = draw_pieces(page, x, y, pieces, run.style, fb, self.cache)

        report["substituted"] = "".join(sorted(report["substituted"]))
        return report
