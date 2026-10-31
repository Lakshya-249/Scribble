"""List-marker geometry (bullets, 'a)', '1.', '(iv)' ...) and the visible justify edge.

Why this exists
---------------
A paragraph's flat text keeps only ONE blank between a marker and its body ("a) The Employee"),
but in the PDF the body starts at a tab-like column (here 205.7pt, 10pt right of the ')').
Re-typesetting "a) The ..." as one string collapses that gap while the wrapped lines still start
at the body column. The marker column and the body column have to be taken from the ORIGINAL GLYPHS.

Detection is purely geometric (no list of marker characters is needed for 'a)', '(iv)', '12.' ...):
  a short run of glyphs with no gap, then a gap wider than GAP_EM, then the body.
It is accepted when the body column lines up with another line of the same paragraph (hanging
indent), when it is a glyph bullet, or when the marker is a recognisable list number.

No PyMuPDF dependency: unit-testable with fake geometry.
"""
import unicodedata
from dataclasses import dataclass
from statistics import median
from typing import List, Optional

from services.spatial_cluster import BULLET_CHARS, _GLYPH_BULLETS, _is_list_number

_PUA_BULLETS = set("\uf0b7\uf0a7\uf0d8\uf076\uf0fc")   # Symbol / Wingdings bullets
GAP_EM = 0.8            # marker -> body gap (in em) that counts as a column, not a word space
BULLET_GAP_EM = 0.25    # ascii-ish bullets ('-', '*', '>', dashes) need at least this much gap
MAX_MARKER_CHARS = 6
ALIGN_TOL = 2.0         # pt: body column vs left edge of a sibling line


@dataclass(frozen=True)
class Marker:
    text: str        # marker glyphs, no blanks: "a)", "•", "(iv)"
    x: float         # origin x of the marker
    body_x: float    # origin x of the first body glyph
    glyph: bool = False   # unambiguous bullet symbol (never underlined)


def _invisible(ch: str) -> bool:
    return ch in "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad" or unicodedata.category(ch) in ("Cc", "Cf")


def _visible(line) -> list:
    toks = [t for t in line.tokens if not t.char.isspace() and not _invisible(t.char)
            and (t.bbox[2] - t.bbox[0]) > 0.3]
    toks.sort(key=lambda t: t.bbox[0])
    return toks


def _marker_of_line(toks: list, siblings_left: List[float]) -> Optional[Marker]:
    if len(toks) < 2:
        return None
    first = toks[0]
    size = max(float(first.font_size), 1.0)

    # glyph bullets: '•' ... and '-', '–' ... when followed by a gap
    if first.char in BULLET_CHARS or first.char in _PUA_BULLETS:
        body = next((t for t in toks[1:] if t.char not in BULLET_CHARS and t.char not in _PUA_BULLETS
                     and t.bbox[0] >= first.bbox[2] - 0.5
                     and abs(t.origin[1] - first.origin[1]) < size * 0.8), None)
        if body is None:
            return None
        gap = body.bbox[0] - first.bbox[2]
        if first.char in _GLYPH_BULLETS or first.char in _PUA_BULLETS or gap >= BULLET_GAP_EM * size:
            return Marker(first.char, float(first.origin[0]), float(body.origin[0]), glyph=True)
        return None

    group = [first]
    body = None
    for t in toks[1:]:
        if abs(t.origin[1] - first.origin[1]) >= size * 0.8:
            return None
        if t.bbox[0] - group[-1].bbox[2] > GAP_EM * size:
            body = t
            break
        group.append(t)
        if len(group) > MAX_MARKER_CHARS:
            return None
    if body is None:
        return None
    text = "".join(t.char for t in group)
    body_x = float(body.origin[0])
    aligned = any(abs(l - body.bbox[0]) <= ALIGN_TOL for l in siblings_left)
    if aligned or _is_list_number(text):
        return Marker(text, float(first.origin[0]), body_x)
    return None


def find_markers(block) -> List[Marker]:
    """Markers found at the start of any line of the block (one Marker per distinct text)."""
    vis = [_visible(ln) for ln in block.lines]
    lefts = [v[0].bbox[0] if v else None for v in vis]
    out: List[Marker] = []
    for k, toks in enumerate(vis):
        sib = [l for j, l in enumerate(lefts) if j != k and l is not None]
        m = _marker_of_line(toks, sib)
        if m is not None and all(o.text != m.text for o in out):
            out.append(m)
    return out


def match_marker(markers: List[Marker], text: str) -> Optional[Marker]:
    """The marker an output line starts with (blank, end of text or a glyph bullet must follow)."""
    best = None
    for m in markers:
        if not text.startswith(m.text):
            continue
        rest = text[len(m.text):]
        if m.glyph or not rest or rest[0].isspace() or _invisible(rest[0]):
            if best is None or len(m.text) > len(best.text):
                best = m
    return best


def visible_right_edge(block) -> Optional[float]:
    """Right edge of the last VISIBLE glyph of the justified lines. Line bboxes also contain the
    trailing blank glyph, which is ~one space (2-3pt) wider than what the eye sees as the margin."""
    rs = []
    for ln in block.lines[:-1]:
        v = _visible(ln)
        if v:
            rs.append(max(t.bbox[2] for t in v))
    if rs and max(rs) - min(rs) <= 3.0:
        return float(median(rs))
    return None


@dataclass
class Placement:
    marker: Optional[Marker]
    body_x: float     # where the body text starts
    just_w: float     # width the body is justified to (0 = do not justify)


def place_line(markers: List[Marker], edge: Optional[float], justified: bool,
               text: str, x: float, target_w: float, is_last: bool) -> Placement:
    m = match_marker(markers, text)
    bx = m.body_x if m else x
    if not justified or is_last:
        jw = 0.0
    elif edge is not None:
        jw = max(0.0, edge - bx)                       # measured from where the body REALLY starts
    else:
        jw = max(0.0, target_w - (bx - x))
    return Placement(m, bx, jw)


def split_marker(runs: list, n: int):
    """Split the first n characters off a list of Run objects -> (marker_runs, body_runs)."""
    from services.style_runs import Run
    head, rest, left = [], [], n
    for r in runs:
        if left <= 0:
            rest.append(r)
        elif len(r.text) <= left:
            head.append(r)
            left -= len(r.text)
        else:
            head.append(Run(r.text[:left], r.style))
            rest.append(Run(r.text[left:], r.style))
            left = 0
    return head, rest
