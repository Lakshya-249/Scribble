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

NUMERIC_CELL = re.compile(r"^[\s(+\-\u2212]*(?:[^\w\s(+\-\u2212]{1,3}\s*)?\d[\d,.\s]*\)?\s*%?\s*(?:Cr|Dr|CR|DR)?$")


def is_numeric_cell(text: str) -> bool:
    t = text.strip()
    return bool(t) and "\n" not in t and bool(NUMERIC_CELL.match(t))


def looks_right_aligned(block: ParagraphBlock, others: List[ParagraphBlock], tol: float = 1.5) -> bool:
    x0, _, x1, _ = block.bbox
    for o in others:
        if o is block or not is_numeric_cell(o.text):
            continue
        ox0, _, ox1, _ = o.bbox
        if abs(ox1 - x1) <= tol and abs(ox0 - x0) > tol:
            return True
    return False


class Piece:
    __slots__ = ("ent", "text", "width", "substituted")

    def __init__(self, ent, text, width, substituted):
        self.ent, self.text, self.width, self.substituted = ent, text, width, substituted


def layout_run(page, doc, run: Run, cache, width_scale: float = 1.0) -> Tuple[List[Piece], str]:
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
        w = ent[0].text_length(ch, fontsize=st.size) * width_scale
        sub = idx >= same
        if pieces and pieces[-1].ent is ent and ch != " " and not pieces[-1].text.endswith(" "):
            pieces[-1].text += ch
            pieces[-1].width += w
        else:
            pieces.append(Piece(ent, ch, w, sub))
    return pieces, base14(st.font, st.flags)


def compute_empirical_width_scale(page, doc, block: ParagraphBlock, cache: dict) -> float:
    """Compare the original PDF glyph advance widths in `block` against our font_chain's
    measured widths. If the PDF used custom tracking or a slightly wider/narrower font metric,
    return a calibration factor (clamped to [0.92, 1.12]) so the paragraph does not shrink."""
    pdf_w_total = 0.0
    calc_w_total = 0.0
    for ln in block.lines:
        for tok in ln.tokens:
            if tok.char.isspace() or invisible(tok.char):
                continue
            tw = tok.bbox[2] - tok.bbox[0]
            if tw <= 0.1:
                continue
            chain = font_chain(doc, page, tok.font_name, tok.flags, cache=cache)
            ent = next((e for e in chain if e[0].has_glyph(ord(tok.char))), chain[0])
            cw = ent[0].text_length(tok.char, fontsize=tok.font_size)
            if cw > 0.1:
                pdf_w_total += tw
                calc_w_total += cw
    if calc_w_total <= 1.0:
        return 1.0
    ratio = pdf_w_total / calc_w_total
    return max(0.92, min(1.12, ratio))


def draw_pieces(page, x, y, pieces, st: Style, fallback_code, cache, extra_space_w: float = 0.0) -> float:
    color = rgb(st.color)
    start_x = x
    for pc in pieces:
        res_name = register_font_name(page, pc.ent, fallback_code=fallback_code, cache=cache)
        if pc.text != " ":
            try:
                if id(pc.ent[0]) in cache.get("synth", ()):
                    page.insert_text((x, y), pc.text, fontname=res_name, fontsize=st.size,
                                     color=color, render_mode=2, border_width=0.03)
                else:
                    page.insert_text((x, y), pc.text, fontname=res_name, fontsize=st.size, color=color)
            except Exception:
                page.insert_text((x, y), pc.text, fontname=fallback_code, fontsize=st.size, color=color)
        x += pc.width
        if pc.text == " ":
            x += extra_space_w

    if st.underline and x > start_x:
        ul_y = y + st.size * 0.11
        ul_thick = max(0.45, st.size * 0.055)
        page.draw_line(fitz.Point(start_x, ul_y), fitz.Point(x, ul_y), color=color, width=ul_thick)

    return x


def draw_text_with_glyph_fallback(page, doc, origin, text, fname, flags, fontsize, color,
                                  cache: Optional[dict] = None):
    cache = cache if cache is not None else _DEFAULT_CACHE
    col = (round(color[0] * 255) << 16) | (round(color[1] * 255) << 8) | round(color[2] * 255)
    st = Style(fname, flags, fontsize, col, False)
    pieces, fb = layout_run(page, doc, Run(text, st), cache)
    return draw_pieces(page, origin[0], origin[1], pieces, st, fb, cache)


class PDFCompiler:
    def __init__(self, doc: fitz.Document, cache: Optional[dict] = None):
        self.doc = doc
        self.cache = cache if cache is not None else _DEFAULT_CACHE

    def _plan_line(
        self,
        page,
        x: float,
        y: float,
        runs: List[Run],
        report: dict,
        width_scale: float = 1.0,
        justify_target_w: float = 0.0,
    ):
        """-> [x, y, [(run, pieces, fallback_code)], total_width, extra_space_w]"""
        planned, total = [], 0.0
        space_count = 0
        for r in runs:
            pieces, fb = layout_run(page, self.doc, r, self.cache, width_scale=width_scale)
            planned.append((r, pieces, fb))
            for p in pieces:
                total += p.width
                if p.text == " ":
                    space_count += 1
                if p.substituted:
                    report["substituted"].update(c for c in p.text if not c.isspace())

        extra_space_w = 0.0
        if justify_target_w > total and space_count > 0:
            diff = justify_target_w - total
            # Stretch spaces up to a reasonable typographic limit so justified lines stay flush-right
            if diff <= max(35.0, justify_target_w * 0.22):
                extra_space_w = diff / space_count
                total += diff

        return [x, y, planned, total, extra_space_w]

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

        width_scale = compute_empirical_width_scale(page, self.doc, target_block, self.cache)

        target_x0, target_y0, target_x1, target_y1 = target_block.bbox
        target_w = max(1.0, target_x1 - target_x0)
        downstream = []
        if abs(height_delta) > 1.0:
            for b in all_blocks:
                if b == target_block:
                    continue
                ox0, oy0, ox1, oy1 = b.bbox
                ow = max(1.0, ox1 - ox0)
                overlap_x = max(0.0, min(target_x1, ox1) - max(target_x0, ox0))
                # Only cascade blocks in the SAME column (significant horizontal overlap)
                if oy0 >= target_y1 - 1.0 and overlap_x >= 0.4 * min(target_w, ow):
                    downstream.append(b)

        # ---- 1. PLAN everything first (before redacting/erasing embedded fonts)
        if new_text is None:
            new_text = "\n".join(ln.text for ln in reflow_res.lines)
        old_styles = old_char_styles(target_block)
        new_styles = new_char_styles(target_block.text, old_styles, new_text)
        default = old_styles[0] if old_styles else DEFAULT_STYLE

        plan = []
        cursor = 0
        is_justified = getattr(target_block, "alignment", "left") == "justify"
        for line in reflow_res.lines:
            runs, cursor = line_runs(line.text, new_text, new_styles, cursor, default)
            just_w = line.target_width if (is_justified and not line.is_last_in_para) else 0.0
            plan.append(
                self._plan_line(
                    page,
                    line.baseline_origin[0],
                    line.baseline_origin[1],
                    runs,
                    report,
                    width_scale=width_scale,
                    justify_target_w=just_w,
                )
            )

        # Numeric cell in a right-aligned column: keep the RIGHT edge where it was
        if (len(plan) == 1 and len(target_block.lines) == 1
                and is_numeric_cell(new_text) and is_numeric_cell(target_block.text)
                and looks_right_aligned(target_block, all_blocks)):
            plan[0][0] = target_x1 - plan[0][3]

        for db in downstream:
            ds = old_char_styles(db)
            dcur = 0
            db_scale = compute_empirical_width_scale(page, self.doc, db, self.cache)
            db_just = getattr(db, "alignment", "left") == "justify"
            for idx_l, line in enumerate(db.lines):
                runs, dcur = line_runs(line.text, db.text, ds, dcur, ds[0] if ds else DEFAULT_STYLE)
                orig_x = line.tokens[0].origin[0] if line.tokens else db.bbox[0]
                is_last_l = (idx_l == len(db.lines) - 1)
                just_w = (db.bbox[2] - orig_x) if (db_just and not is_last_l) else 0.0
                plan.append(
                    self._plan_line(
                        page,
                        orig_x,
                        line.baseline_y + height_delta,
                        runs,
                        report,
                        width_scale=db_scale,
                        justify_target_w=just_w,
                    )
                )

        # ---- 2. Erase text and any vector underlines attached to target/downstream blocks
        safe_erase(page, target_block.bbox, fill_bg=False, ghost=ghost)
        for urect in getattr(target_block, "underline_rects", ()):
            safe_erase(page, urect, fill_bg=True, ghost=None)

        for db in downstream:
            safe_erase(page, db.bbox, fill_bg=False, ghost=ghost)
            for urect in getattr(db, "underline_rects", ()):
                safe_erase(page, urect, fill_bg=True, ghost=None)

        # ---- 3. Draw planned runs and new underlines
        for x, y, planned, _, extra_space_w in plan:
            for run, pieces, fb in planned:
                x = draw_pieces(page, x, y, pieces, run.style, fb, self.cache, extra_space_w=extra_space_w)

        report["substituted"] = "".join(sorted(report["substituted"]))
        return report
