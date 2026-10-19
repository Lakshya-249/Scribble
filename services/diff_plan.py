"""Plan an edit as a *minimal diff* of the original glyphs.

Why: re-typesetting a whole line from a pen position throws away every position the PDF
had (bullet gaps, tabs, table columns, kerning), and forces erasing borders/underlines
around it. Here unchanged characters are never touched. Only the changed span is erased
and drawn; characters to its right are moved only while they belong to the same run
(a run ends at any gap wider than BREAK_EM, so table cells and bullet markers stay put).

This module has no PyMuPDF dependency so it can be unit-tested with fake geometry.
"""
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Tuple

TOL = 0.7        # pt. A width change smaller than this is treated as "same width": nothing else moves.
BREAK_EM = 0.9   # a gap wider than this many em separates independent runs (cells, bullet marker)


@dataclass
class Op:
    line: int
    i1: int
    i2: int
    j1: int
    j2: int
    old_x0: float
    old_x1: float
    old_w: float
    new_w: float
    y: float
    erase: Optional[Tuple[float, float, float, float]]
    run: int = 0
    after: int = 0           # index (in the line's real-token list) of the first token after this op
    delta: float = 0.0
    x: float = 0.0           # final x where the new text is drawn


@dataclass
class DiffPlan:
    ok: bool = True
    reason: str = ""
    ops: List[Op] = field(default_factory=list)
    tail: List[tuple] = field(default_factory=list)          # (token, dx) real tokens that must move
    erase_rects: List[tuple] = field(default_factory=list)
    underlines: List[tuple] = field(default_factory=list)    # (old_rect, new_rect | None, colour_int)
    _ctx: Dict[int, dict] = field(default_factory=dict, repr=False)

    def remap_rect(self, rect) -> Optional[tuple]:
        """New rect for something anchored to text (a link annotation), or None if unchanged."""
        x0, y0, x1, y1 = rect
        cy = (y0 + y1) / 2.0
        for ctx in self._ctx.values():
            ly0, ly1 = ctx["y"]
            if ly0 - 1.0 <= cy <= ly1 + 1.0:
                nx0, nx1 = _shift(ctx, x0, x1)
                if abs(nx0 - x0) > 0.05 or abs(nx1 - x1) > 0.05:
                    return (nx0, y0, nx1, y1)
        return None


def _fail(reason: str) -> DiffPlan:
    return DiffPlan(ok=False, reason=reason)


def _shift(ctx: dict, x0: float, x1: float) -> Tuple[float, float]:
    """Where the horizontal range [x0, x1] of the OLD line ends up after the planned ops."""
    toks, rid, ops = ctx["toks"], ctx["rid"], ctx["ops"]
    first = next((t for t in toks if t.bbox[2] > x0 + 0.5 and t.bbox[0] < x1), None)
    r = rid[id(first)] if first is not None else None
    s0 = sum(o.delta for o in ops if o.run == r and o.old_x1 < x0 - 0.5)
    inside = sum(o.delta for o in ops if o.run == r and o.old_x0 >= x0 - 0.5 and o.old_x1 <= x1 + 0.6)
    nx0, nx1 = x0 + s0, x1 + s0 + inside
    # An underline stops at the last visible glyph, but the line often ends with a blank glyph.
    # Text typed after those blanks continues the underlined run, so the underline grows to cover it.
    x1e = x1
    for t in toks:
        if t.bbox[0] >= x1 - 0.5:
            if not t.char.isspace():
                break
            x1e = max(x1e, t.bbox[2])
    for o in ops:
        if o.run == r and x1 + 0.6 < o.old_x0 <= x1e + 0.6 and o.new_w > 0:
            nx1 = max(nx1, o.x + o.new_w)
    return nx0, nx1


def _merge(rects: List[tuple]) -> List[tuple]:
    out: List[list] = []
    for r in sorted(rects):
        if out and r[0] <= out[-1][2] + 0.6 and abs(r[1] - out[-1][1]) < 3.0:
            out[-1] = [min(out[-1][0], r[0]), min(out[-1][1], r[1]), max(out[-1][2], r[2]), max(out[-1][3], r[3])]
        else:
            out.append(list(r))
    return [tuple(r) for r in out]


def plan_diff(block, new_text: str, measure: Callable[[int, int], float], limit_x: float,
              right_aligned: bool = False) -> DiffPlan:
    """measure(j1, j2) -> rendered width (pt) of new_text[j1:j2] in the styles it will be drawn with."""
    old = block.text
    ct, cl = getattr(block, "char_tok", None), getattr(block, "char_line", None)
    if not ct or not cl or len(ct) != len(old) or len(cl) != len(old):
        return _fail("no-char-map")

    n_old = len(old)

    def is_trailing_blank_delete(c):
        # editors usually trim the blank glyph at the end of a line; that is not a real edit
        tag, i1, i2, j1, j2 = c
        return (j1 == j2 and old[i1:i2].isspace()
                and (i2 >= n_old or cl[i2] != cl[i2 - 1]))

    codes = [c for c in SequenceMatcher(None, old, new_text, autojunk=False).get_opcodes()
             if c[0] != "equal" and not is_trailing_blank_delete(c)]
    plan = DiffPlan()
    if not codes:
        return plan

    # A line break was typed or removed. The diff path only draws glyphs on the existing line
    # positions, so a "\n" would be silently dropped and the new text would land on the same line.
    # Let the reflow engine (compiler fallback) lay out the new line structure instead.
    if any("\n" in new_text[j1:j2] or "\n" in old[i1:i2] for _, i1, i2, j1, j2 in codes):
        return _fail("line-break-changed")

    def real_before(i, line):
        k = i - 1
        while k >= 0 and cl[k] == line:
            if ct[k] is not None:
                return k
            k -= 1
        return None

    def real_from(i, line):
        k = i
        while k < n_old and cl[k] == line:
            if ct[k] is not None:
                return k
            k += 1
        return None

    # ---- per-line token lists and run ids (a run = tokens with no big gap between them)
    ctxs: Dict[int, dict] = {}

    def ctx_of(li):
        if li in ctxs:
            return ctxs[li]
        ln = block.lines[li]
        toks = [t for t in ln.char_tok if t is not None]
        rid, r, prev = {}, 0, None
        for t in toks:
            if prev is not None and (t.bbox[0] - prev.bbox[2]) > BREAK_EM * max(t.font_size, 1.0):
                r += 1
            rid[id(t)] = r
            prev = t
        ctxs[li] = {"toks": toks, "rid": rid, "idx": {id(t): k for k, t in enumerate(toks)},
                    "ops": [], "y": (ln.bbox[1], ln.bbox[3]), "line": ln}
        return ctxs[li]

    # ---- one Op per changed region
    for tag, i1, i2, j1, j2 in codes:
        if i2 > i1:
            ls = {cl[k] for k in range(i1, i2)}
        elif i1 > 0 and cl[i1 - 1] != -1:
            ls = {cl[i1 - 1]}
        elif i1 < n_old and cl[i1] != -1:
            ls = {cl[i1]}
        else:
            ls = set()
        if len(ls) != 1 or -1 in ls:
            return _fail("edit-crosses-lines")
        li = next(iter(ls))
        ctx = ctx_of(li)

        pk, nk = real_before(i1, li), real_from(i2, li)
        p = ct[pk] if pk is not None else None
        n = ct[nk] if nk is not None else None
        span = [ct[k] for k in range(i1, i2) if ct[k] is not None]

        if span:
            ox0, ox1 = min(t.bbox[0] for t in span), max(t.bbox[2] for t in span)
            y = span[0].origin[1]
            erase = (ox0, min(t.bbox[1] for t in span), ox1, max(t.bbox[3] for t in span))
            old_w = ox1 - ox0
            run = ctx["rid"][id(span[0])]
            after = ctx["idx"][id(span[-1])] + 1
        else:
            if p is None and n is None:
                return _fail("no-anchor")
            erase = None
            y = (p or n).origin[1]
            if i2 > i1:                                   # only inferred spaces were deleted
                ox0 = p.bbox[2] if p else n.bbox[0]
                ox1 = n.bbox[0] if (p and n) else ox0
                old_w = max(0.0, ox1 - ox0)
            else:                                         # pure insertion
                if p is not None and pk == i1 - 1:
                    ox0 = p.bbox[2]
                elif n is not None and nk == i1:
                    ox0 = n.bbox[0]
                else:
                    ox0 = p.bbox[2] if p else n.bbox[0]
                ox1, old_w = ox0, 0.0
            run = ctx["rid"][id(p)] if p is not None else ctx["rid"][id(n)]
            after = ctx["idx"][id(n)] if n is not None else len(ctx["toks"])

        new_w = measure(j1, j2) if j2 > j1 else 0.0
        delta = new_w - old_w
        if abs(delta) < TOL:
            delta = 0.0
        op = Op(li, i1, i2, j1, j2, ox0, ox1, old_w, new_w, y, erase, run, after, delta)
        ctx["ops"].append(op)
        plan.ops.append(op)

    # ---- final x of each op (earlier ops in the same run push it)
    carry: Dict[tuple, float] = {}
    for op in plan.ops:
        k = (op.line, op.run)
        op.x = op.old_x0 + carry.get(k, 0.0)
        carry[k] = carry.get(k, 0.0) + op.delta

    # ---- tokens that must move
    for li, ctx in ctxs.items():
        changed = {id(ct[k]) for op in ctx["ops"] for k in range(op.i1, op.i2) if ct[k] is not None}
        group: List[tuple] = []
        for q, t in enumerate(ctx["toks"]):
            dx = 0.0
            if id(t) not in changed:
                dx = sum(o.delta for o in ctx["ops"] if o.run == ctx["rid"][id(t)] and o.after <= q)
            if abs(dx) >= TOL and id(t) not in changed:
                plan.tail.append((t, dx))
                group.append(t.bbox)
            elif group:
                plan.erase_rects.append((min(b[0] for b in group), min(b[1] for b in group),
                                         max(b[2] for b in group), max(b[3] for b in group)))
                group = []
        if group:
            plan.erase_rects.append((min(b[0] for b in group), min(b[1] for b in group),
                                     max(b[2] for b in group), max(b[3] for b in group)))
    plan.erase_rects += [op.erase for op in plan.ops if op.erase is not None]
    plan.erase_rects = _merge(plan.erase_rects)

    # ---- cases where moving things is not enough: let the reflow engine handle it
    moved = any(op.delta != 0.0 for op in plan.ops)
    if moved:
        if right_aligned or getattr(block, "alignment", "left") in ("center", "right", "justify"):
            return _fail("needs-realign")
        if len(block.lines) > 1:
            return _fail("needs-reflow")
        right = max([t.bbox[2] + dx for t, dx in plan.tail] + [op.x + op.new_w for op in plan.ops])
        if right > limit_x:
            return _fail("overflow")

    # ---- underlines that sit under changed text follow it
    for li, ctx in ctxs.items():
        ln = ctx["line"]
        cols = list(getattr(ln, "underline_colors", []))
        for k, (ux0, uy0, ux1, uy1) in enumerate(ln.underline_rects):
            nx0, nx1 = _shift(ctx, ux0, ux1)
            if abs(nx0 - ux0) > 0.05 or abs(nx1 - ux1) > 0.05:
                new = (nx0, uy0, nx1, uy1) if nx1 - nx0 > 0.5 else None
                plan.underlines.append(((ux0, uy0, ux1, uy1), new, cols[k] if k < len(cols) else 0))

    plan._ctx = ctxs
    return plan
