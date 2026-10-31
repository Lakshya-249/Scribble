import dataclasses
import re
import pymupdf as fitz
from typing import List, Tuple, Optional

from services.spatial_cluster import ParagraphBlock, BULLET_CHARS
from services.obj_reflow import ReflowResult
from services.markers import find_markers, visible_right_edge, place_line, split_marker
from services.erase_service import (safe_erase, erase_underline_rect, remove_underline, draw_underline,
                                    collect_drawings_below, remove_drawings_verified, redraw_drawings)
from services.font_service import (
    font_chain,
    register_font_name,
    base14,
    rgb,
    invisible,
    _DEFAULT_CACHE,
)
from services.style_runs import (
    Style, Run, DEFAULT_STYLE, old_char_styles, new_char_styles, line_runs, style_of,
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


def _bullet_cols(block: ParagraphBlock):
    """(bullet_x, text_x): ABSOLUTE x of the bullet glyph and of the first real letter after it, taken from
    the original glyphs (None if the block has no bullet). Invisible / zero-width glyphs between the two
    are ignored."""
    toks = [t for ln in block.lines for t in ln.tokens if not t.char.isspace() and not invisible(t.char)]
    bullets = [t for t in toks if t.char in BULLET_CHARS]
    if not bullets:
        return None
    bullet = min(bullets, key=lambda t: t.bbox[0])
    if bullet.bbox[0] > min(t.bbox[0] for t in toks) + 1.0:
        return None                                 # something sits left of the bullet: not a list marker
    size = max(float(bullet.font_size), 1.0)
    body = [t for t in toks
            if t.char not in BULLET_CHARS
            and t.bbox[0] >= bullet.bbox[2] - 0.5
            and (t.bbox[2] - t.bbox[0]) > 0.3
            and abs(t.origin[1] - bullet.origin[1]) < size * 0.8]
    if not body:
        return None
    return float(bullet.origin[0]), float(min(t.origin[0] for t in body))


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
    """Calibrates horizontal glyph advance widths against the original PDF tokens so edited
    paragraphs never shrink horizontally."""
    pdf_w_total = 0.0
    calc_w_total = 0.0
    for ln in block.lines:
        for tok in ln.tokens:
            if tok.char.isspace() or invisible(tok.char):
                continue
            tw = tok.bbox[2] - tok.bbox[0]
            if tw <= 0.6:
                continue
            chain = font_chain(doc, page, tok.font_name, tok.flags, cache=cache)
            ent = next((e for e in chain if e[0].has_glyph(ord(tok.char))), chain[0])
            cw = ent[0].text_length(tok.char, fontsize=tok.font_size)
            if cw > 0.6:
                pdf_w_total += tw
                calc_w_total += cw
    if calc_w_total <= 1.0:
        return 1.0
    ratio = pdf_w_total / calc_w_total
    return max(0.94, min(1.08, ratio))


def draw_pieces(page, x, y, pieces, st: Style, fallback_code, cache, extra_space_w: float = 0.0,
                original_underline_rects: Optional[List[tuple]] = None) -> float:
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
        # Match exact original underline thickness and offset if available
        ul_thick = max(0.45, st.size * 0.055)
        ul_offset = st.size * 0.11
        if original_underline_rects:
            # Take thickness and baseline offset from the first original underline rect
            urect = original_underline_rects[0]
            ul_thick = max(0.4, urect[3] - urect[1])
            # urect[1] is top of underline rect
            ul_offset = max(2.0, urect[1] - y)

        ul_y = y + ul_offset
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
                if oy0 >= target_y1 - 1.0 and overlap_x >= 0.35 * min(target_w, ow):
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

        # Marker -> body gap (bullets, 'a)', '1.', '(iv)' ...). Re-typesetting "a) text" with one blank would
        # collapse the original column, so marker and body go to the columns of the ORIGINAL glyphs.
        # Works for justified text too: only the body is justified, from where it really starts, to the
        # VISIBLE right margin.
        align = getattr(target_block, "alignment", "left")
        marks = find_markers(target_block) if align in ("left", "justify") else []
        edge = visible_right_edge(target_block) if is_justified else None

        for line in reflow_res.lines:
            runs, cursor = line_runs(line.text, new_text, new_styles, cursor, default)
            lx, ly = line.baseline_origin
            pl = place_line(marks, edge, is_justified, line.text, lx, line.target_width, line.is_last_in_para)
            if pl.marker is not None and runs:
                mk_runs, rest = split_marker(runs, len(pl.marker.text))
                if pl.marker.glyph:                      # a bullet symbol is never underlined
                    mk_runs = [Run(r.text, dataclasses.replace(r.style, underline=False)) for r in mk_runs]
                # drop every leading blank (it may sit in a later run when the marker is styled differently)
                while rest:
                    t = rest[0].text
                    k = 0
                    # blanks AND zero-width glyphs (U+200B after the bullet is common): str.lstrip() keeps
                    # U+200B, which hid the real space behind it and pushed the text right by one space
                    while k < len(t) and (t[k].isspace() or invisible(t[k])):
                        k += 1
                    if k < len(t):
                        rest[0] = Run(t[k:], rest[0].style)
                        break
                    rest.pop(0)
                plan.append(self._plan_line(page, pl.marker.x, ly, mk_runs, report, width_scale=width_scale))
                plan.append(self._plan_line(page, pl.body_x, ly, rest, report,
                                            width_scale=width_scale, justify_target_w=pl.just_w))
                continue
            plan.append(
                self._plan_line(page, lx, ly, runs, report,
                                width_scale=width_scale, justify_target_w=pl.just_w)
            )

        if (len(plan) == 1 and len(target_block.lines) == 1
                and is_numeric_cell(new_text) and is_numeric_cell(target_block.text)
                and looks_right_aligned(target_block, all_blocks)):
            plan[0][0] = target_x1 - plan[0][3]

        # ---- 2a. Vector art below the edit (table borders, shading, rules, underlines) must follow the text
        drawings_to_move = []
        if abs(height_delta) > 1.0:
            cand = collect_drawings_below(page, target_y1 - 1.0, target_x0, target_x1)
            if cand and remove_drawings_verified(page, cand):
                drawings_to_move = cand
        moved_rects = [fitz.Rect(d["rect"]) for d in drawings_to_move]

        def _moved(u):
            """this underline rect is one of the paths moved above (so don't treat it separately)"""
            return any(abs(r.x0 - u[0]) < 1.0 and abs(r.x1 - u[2]) < 1.0
                       and abs((r.y0 + r.y1) / 2 - (u[1] + u[3]) / 2) < 1.5 for r in moved_rects)

        # ---- 2. Erase old text AND old vector underlines
        # Erase old text AND all associated vector underlines
        safe_erase(page, target_block.bbox, fill_bg=False, ghost=ghost, cache=self.cache)
        for urect in getattr(target_block, "underline_rects", ()):
            erase_underline_rect(page, urect)

        # Blocks below are MOVED, not re-typeset: every glyph keeps its own x, spacing, font and underline
        # (re-typesetting collapsed bullet gaps and stretched underlines).
        moved_links = []
        if downstream:
            for lk in page.get_links():
                c = fitz.Rect(lk["from"])
                cx, cy = (c.x0 + c.x1) / 2.0, (c.y0 + c.y1) / 2.0
                if any(b.bbox[0] - 1 <= cx <= b.bbox[2] + 1 and b.bbox[1] - 1 <= cy <= b.bbox[3] + 1 for b in downstream):
                    moved_links.append(lk)
        for db in downstream:
            safe_erase(page, db.bbox, fill_bg=False, ghost=ghost, cache=self.cache)
            for ln in db.lines:
                for urect in getattr(ln, "underline_rects", ()):
                    if _moved(urect):
                        continue
                    remove_underline(page, urect)
                    erase_underline_rect(page, urect)   # also wipes anti-aliased remnants of the old line

        # target block's own underlines (its text is re-typeset, so they are redrawn below)
        for urect in getattr(target_block, "underline_rects", ()):
            erase_underline_rect(page, urect)

        # moved vector art goes down BEFORE any text is drawn (cell shading must not cover glyphs)
        if drawings_to_move:
            redraw_drawings(page, drawings_to_move, height_delta)

        # ---- 3. Draw planned runs and new underlines
        for x, y, planned, _, extra_space_w in plan:
            for run, pieces, fb in planned:
                x = draw_pieces(page, x, y, pieces, run.style, fb, self.cache, extra_space_w=extra_space_w)

        dy = height_delta
        for db in downstream:
            for ln in db.lines:
                items = []          # [x, y, text, style]; ligature halves ('f'+'l') share an origin: keep them together
                for tok in ln.tokens:
                    if tok.char.isspace() or invisible(tok.char):
                        continue
                    st = dataclasses.replace(style_of(tok), underline=False)
                    if (items and abs(tok.origin[0] - items[-1][0]) < 0.8 and abs(tok.origin[1] - items[-1][1]) < 0.5
                            and items[-1][3] == st):
                        items[-1][2] += tok.char
                    else:
                        items.append([tok.origin[0], tok.origin[1], tok.char, st])
                for ox, oy, txt, st in items:
                    pieces, fb = layout_run(page, self.doc, Run(txt, st), self.cache, width_scale=1.0)
                    draw_pieces(page, ox, oy + dy, pieces, st, fb, self.cache)
                cols = list(getattr(ln, "underline_colors", []))
                for k, (ux0, uy0, ux1, uy1) in enumerate(getattr(ln, "underline_rects", ())):
                    if _moved((ux0, uy0, ux1, uy1)):
                        continue
                    draw_underline(page, (ux0, uy0 + dy, ux1, uy1 + dy), rgb(cols[k] if k < len(cols) else 0))
        for lk in moved_links:
            r = fitz.Rect(lk["from"])
            lk["from"] = fitz.Rect(r.x0, r.y0 + dy, r.x1, r.y1 + dy)
            page.update_link(lk)

        report["substituted"] = "".join(sorted(report["substituted"]))
        return report
