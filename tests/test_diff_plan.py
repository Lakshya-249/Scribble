"""Pure tests for services/diff_plan.py (no PyMuPDF needed): python tests/test_diff_plan.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services.spatial_cluster import SpatialClusterer
from services.diff_plan import plan_diff, TOL

SIZE, W, SP = 12.0, 5.5, 3.0


def chars(text, x, y):
    out = []
    for c in text:
        w = SP if c == " " else W
        out.append({"char": c, "origin": (x, y), "bbox": (x, y - 9.5, x + w, y + 2.5)})
        x += w
    return out, x


def page(parts, y=100.0, underline=None):
    """parts: [(text, x)] on one baseline -> raw geometry like extract_page_geometry()."""
    spans, end = [], 0
    for text, x in parts:
        cs, end = chars(text, x, y)
        spans.append({"font": "Lato", "size": SIZE, "flags": 0, "color": 0, "chars": cs})
    segs = [(underline[0], y + 1.2, underline[1], y + 1.8, 0)] if underline else []
    return {"blocks": [{"bbox": (0, 0, 1, 1), "lines": [{"bbox": (0, 0, 1, 1), "spans": spans}]}],
            "h_segments": segs, "v_barriers": [], "page_width": 612.0}


def measure_for(new):
    return lambda j1, j2: sum(SP if c == " " else W for c in new[j1:j2])


def plan(block, new, **kw):
    return plan_diff(block, new, measure_for(new), 600.0, **kw)


def test_bullet_link_append():
    # bullet at 147, text at 172 (gap 18 < 1.65em so they cluster into ONE block, like your PDF)
    text = "Sal's TED talk from 2011"
    cs, end = chars(text, 172, 100)
    blocks = SpatialClusterer().process(page([("\u25cf", 147), (text, 172)], underline=(172, end)))
    assert len(blocks) == 1, [b.text for b in blocks]
    b = blocks[0]
    assert b.text.startswith("\u25cf "), b.text
    new = b.text + " pink"
    p = plan(b, new)
    assert p.ok, p.reason
    assert p.erase_rects == [] and p.tail == []                 # nothing erased, nothing moved
    assert len(p.ops) == 1 and abs(p.ops[0].x - end) < 0.01     # new text starts where the old ended
    (old, newr, _), = p.underlines
    assert abs(newr[2] - (end + p.ops[0].new_w)) < 0.01 and abs(newr[0] - 172) < 0.01   # underline grows, same start
    print("bullet+link append: ok  (bullet and text x untouched)")


def test_same_width_digit_swap():
    cs, end = chars("30,000.00", 400, 200)
    b = SpatialClusterer().process(page([("30,000.00", 400)], y=200.0))[0]
    p = plan(b, "35,000.00", right_aligned=True)
    assert p.ok and p.tail == [] and len(p.erase_rects) == 1
    r = p.erase_rects[0]
    assert r[2] - r[0] < 7, r                                    # only ONE glyph erased
    print("digit swap: ok  (erase rect %.1fpt wide)" % (r[2] - r[0]))


def test_table_runs_do_not_drag():
    # two cells 14pt apart (> 0.9em, < 1.65em): same block, different runs
    b = SpatialClusterer().process(page([("Name", 100), ("Amount", 100 + 4 * W + 14)]))[0]
    new = b.text.replace("Name", "Names")
    p = plan(b, new)
    assert p.ok and p.tail == [], p.reason                       # second cell is NOT moved
    print("table cells: ok  (neighbour cell stays put)")


def test_midline_insert_shifts_same_run_only():
    b = SpatialClusterer().process(page([("Hello world", 100)]))[0]
    new = "Hello there world"
    p = plan(b, new)
    assert p.ok, p.reason
    d = p.ops[0].delta
    op = p.ops[0]
    assert abs(d - measure_for(new)(op.j1, op.j2)) < 0.01 and len(p.tail) == 5   # only "world" moves
    assert all(abs(dx - d) < 0.01 for _, dx in p.tail)
    print("mid-line insert: ok  (tail of %d glyphs moved by %.1fpt)" % (len(p.tail), d))


def test_fallbacks():
    b = SpatialClusterer().process(page([("Hello world", 100)]))[0]
    assert not plan(b, "Hello big world", right_aligned=True).ok        # right aligned + width change
    assert not plan_diff(b, "Hello big world", measure_for("Hello big world"), 150.0).ok   # overflow
    print("fallbacks: ok")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ALL PASSED")
