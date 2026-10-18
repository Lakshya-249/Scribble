"""Pure-python tests: no PDF needed."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.spatial_cluster import CharToken, TextLine, ParagraphBlock
from services.style_runs import old_char_styles, new_char_styles, line_runs, Style


def tok(ch, x, font="Ubuntu-Bold", flags=16, size=10.0, color=0):
    return CharToken(ch, (x, 0, x + 5, 10), (x, 8), font, size, flags, color)


def block_from(text, **kw):
    ln = TextLine(tokens=[tok(c, i * 5, **kw) for i, c in enumerate(text)])
    ln.finalize()
    b = ParagraphBlock(lines=[ln])
    b.finalize()
    return b


def test_edit_digit_keeps_bold_font_and_rupee():
    b = block_from("\u20b930,000.00")
    old = old_char_styles(b)
    assert len(old) == len(b.text)
    new = "\u20b935,000.00"
    runs, _ = line_runs(new, new, new_char_styles(b.text, old, new), 0)
    assert [r.text for r in runs] == [new]                 # ONE run: same style everywhere
    assert runs[0].style.font == "Ubuntu-Bold" and runs[0].style.flags == 16


def test_mixed_styles_survive_an_edit_elsewhere():
    ln = TextLine(tokens=[tok("N", 0, "Sans", 0), tok("e", 5, "Sans", 0), tok("t", 10, "Sans", 0),
                          tok("9", 20), tok("9", 25)])
    ln.finalize()
    b = ParagraphBlock(lines=[ln]); b.finalize()
    old = old_char_styles(b)
    new = "Net 95"                                          # edit the bold part only
    runs, _ = line_runs(new, new, new_char_styles(b.text, old, new), 0)
    assert [(r.text, r.style.font) for r in runs] == [("Net ", "Sans"), ("95", "Ubuntu-Bold")]


def test_inserted_text_inherits_neighbour_style():
    b = block_from("100")
    old = old_char_styles(b)
    new = "1,000"
    st = new_char_styles(b.text, old, new)
    assert len(st) == 5 and set(st) == {Style("Ubuntu-Bold", 16, 10.0, 0)}


def test_deleting_everything_and_empty_old():
    b = block_from("abc")
    assert new_char_styles(b.text, old_char_styles(b), "") == []
    assert len(new_char_styles("", [], "xy")) == 2


def test_line_slicing_of_a_wrapped_paragraph():
    old = [Style("A", 0, 10.0)] * 3 + [Style("B", 16, 10.0)] * 3
    runs, cur = line_runs("aaa", "aaa bbb", old + [Style("B", 16, 10.0)], 0)
    assert runs[0].style.font == "A" and cur == 3
    runs, cur = line_runs("bbb", "aaa bbb", old + [Style("B", 16, 10.0)], cur)
    assert runs[0].style.font == "B" and cur == 7


def test_ghost_counting_keeps_new_text_drawn_over_hidden_old_text():
    from services.erase_service import unghost
    chars = [("T", 10, 10), ("T", 10, 10), ("q", 20, 10), ("x", 30, 10)]   # old T, new T, old q, x
    left = unghost(chars, [["T", 10, 10], ["q", 20, 10]], lambda t: t)
    assert [c[0] for c in left] == ["T", "x"]
