"""Execute a diff plan (services/diff_plan.py) on a page. Returns None when the plan does not
apply, so the caller falls back to the full re-typeset path in compiler.py."""
import dataclasses

import pymupdf as fitz

from services.compiler import layout_run, draw_pieces, compute_empirical_width_scale, looks_right_aligned
from services.diff_plan import plan_diff
from services.erase_service import safe_erase, remove_underline, draw_underline
from services.font_service import rgb
from services.style_runs import Run, old_char_styles, new_char_styles, style_of, _group


def try_diff_edit(doc, page_num, block, new_text, all_blocks, cache, ghost):
    if getattr(block, "char_tok", None) is None:
        return None
    page = doc.load_page(page_num)
    scale = compute_empirical_width_scale(page, doc, block, cache)
    new_styles = new_char_styles(block.text, old_char_styles(block), new_text)

    def runs(j1, j2):
        return _group(new_text[j1:j2], new_styles[j1:j2])

    def measure(j1, j2):
        w = 0.0
        for r in runs(j1, j2):
            pieces, _ = layout_run(page, doc, r, cache, width_scale=scale)
            w += sum(p.width for p in pieces)
        return w

    limit = max(block.container_max_x1 or block.bbox[2], block.bbox[2]) + 2.0
    plan = plan_diff(block, new_text, measure, limit, right_aligned=looks_right_aligned(block, all_blocks))
    if not plan.ok:
        return None                              # nothing has been modified yet

    missing = set()

    def draw(x, y, run, sc):
        st = dataclasses.replace(run.style, underline=False)   # underlines are moved as graphics, not redrawn
        pieces, fb = layout_run(page, doc, Run(run.text, st), cache, width_scale=sc)
        for p in pieces:
            if p.substituted:
                missing.update(c for c in p.text if not c.isspace())
        return draw_pieces(page, x, y, pieces, st, fb, cache)

    # 1. erase only what changed / has to move (fill stays transparent: cell shading is kept)
    for rect in plan.erase_rects:
        safe_erase(page, fitz.Rect(rect), fill_bg=False, ghost=ghost, cache=cache)

    # 2. move underlines: remove the exact old path, draw it again with the new extent and old colour
    for old, new, colour in plan.underlines:
        remove_underline(page, old)
        if new is not None:
            draw_underline(page, new, rgb(colour))

    # 3. new text
    for op in plan.ops:
        x = op.x
        for r in runs(op.j1, op.j2):
            x = draw(x, op.y, r, scale)

    # 4. moved characters keep their ORIGINAL baseline and spacing, only shifted by dx
    for tok, dx in plan.tail:
        if not tok.char.isspace():
            draw(tok.origin[0] + dx, tok.origin[1], Run(tok.char, style_of(tok)), 1.0)

    # 5. link annotations follow their text
    for lk in page.get_links():
        new = plan.remap_rect(tuple(lk["from"]))
        if new is not None:
            lk["from"] = fitz.Rect(new)
            page.update_link(lk)

    return {"substituted": "".join(sorted(missing))}
