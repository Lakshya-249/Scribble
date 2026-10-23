"""Launch-critical behaviour: edits really delete old text, uploads are bounded, history and page tools work."""
import io, os, sys, warnings
import pytest
import pymupdf as fitz
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from main import app
from services import sessions


def make_pdf(lines=("Account number 9876-5432-1000 belongs to Alice", "Salary: 30,000.00 per month",
                    "The quick brown fox jumps over the lazy dog"), pages=1):
    d = fitz.open()
    for n in range(pages):
        p = d.new_page()
        for k, t in enumerate(lines):
            p.insert_text((60, 100 + 30 * k), t if pages == 1 else f"{t} [p{n + 1}]", fontsize=12, fontname="helv")
    return d.tobytes()


@pytest.fixture
def c():
    cl = TestClient(app)
    yield cl
    cl.delete("/session")            # free the session slot: the server caps simultaneous users


def open_pdf(c, data, name="payslip.pdf"):
    r = c.post(f"/open?name={name}", content=data)
    assert r.status_code == 200, r.text
    return r.json()


def block(c, needle, page=0):
    return next(b for b in c.get(f"/text/{page}").json() if needle in b["text"])


def edit(c, b, new, page=0):
    return c.post("/edit", json=dict(type="line", page=page, block_id=b["id"], bbox=b["bbox"], old=b["text"], new=new))


def saved(c):
    return fitz.open("pdf", c.get("/save").content)


def words(doc, i=0):
    return [w[4] for w in doc[i].get_text("words", sort=True)]


# ---- the edit really replaces the text -------------------------------------------------
def test_edit_removes_old_text_from_the_file(c):
    open_pdf(c, make_pdf())
    assert edit(c, block(c, "Salary"), "Salary: 35,000.00 per month").status_code == 200
    raw = saved(c)[0].get_text("text")
    assert "30,000.00" not in raw and "3,000" not in raw
    assert "35,000.00" in " ".join(words(saved(c)))


def test_same_width_edit_leaves_neighbours_exactly_in_place(c):
    open_pdf(c, make_pdf())
    centre = lambda w: (w[0], (w[1] + w[3]) / 2)       # a stand-in font has a slightly different line box
    before = {w[4]: centre(w) for w in fitz.open("pdf", make_pdf())[0].get_text("words")}
    edit(c, block(c, "Salary"), "Salary: 35,000.00 per month")
    after = {w[4]: centre(w) for w in saved(c)[0].get_text("words")}
    for word in ("Salary:", "per", "month"):
        assert abs(after[word][0] - before[word][0]) < 0.8 and abs(after[word][1] - before[word][1]) < 1.0, word
    assert "35,000.00" in after and "30,000.00" not in after


def test_word_edit_keeps_words_whole_and_tail_spacing(c):
    open_pdf(c, make_pdf())
    before = {w[4]: w[:2] for w in fitz.open("pdf", make_pdf())[0].get_text("words")}
    edit(c, block(c, "quick"), "The slow green fox jumps over the lazy dog")
    doc = saved(c)
    after = {w[4]: w[:2] for w in doc[0].get_text("words")}
    assert "slow" in after and "green" in after                        # not "sl" + "ow"
    t = doc[0].get_text("text")
    assert "quick" not in t and "brown" not in t
    gap_before = before["dog"][0] - before["jumps"][0]
    assert abs((after["dog"][0] - after["jumps"][0]) - gap_before) < 1.0


def test_erase_box_removes_text(c):
    open_pdf(c, make_pdf())
    assert c.post("/edit", json=dict(type="cover", page=0, rect=[55, 85, 400, 105])).status_code == 200
    t = saved(c)[0].get_text("text")
    assert "9876" not in t and "Alice" not in t and "Salary" in t


# ---- upload limits -----------------------------------------------------------------------
def test_chunked_upload_cannot_bypass_size_limit(c, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_UPLOAD", 200_000)

    def gen():
        for _ in range(40):
            yield b"x" * 20_000                       # 800 KB, sent chunked: no Content-Length header
    r = c.post("/open", content=gen())
    assert r.status_code == 413


def test_rejects_non_pdf(c):
    r = c.post("/open", content=b"hello world")
    assert r.status_code == 400 and "not a readable PDF" in r.json()["error"]


def test_security_headers(c):
    r = c.get("/")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]


# ---- history -----------------------------------------------------------------------------
def test_undo_redo_roundtrip(c):
    open_pdf(c, make_pdf())
    r = edit(c, block(c, "Salary"), "Salary: 35,000.00 per month").json()
    assert r["can_undo"] and not r["can_redo"]
    u = c.post("/undo").json()
    assert u["can_redo"] and "30,000.00" in " ".join(words(saved(c)))
    rd = c.post("/redo").json()
    assert not rd["can_redo"] and "35,000.00" in " ".join(words(saved(c)))
    assert c.post("/redo").status_code == 400


def test_new_edit_clears_redo(c):
    open_pdf(c, make_pdf())
    edit(c, block(c, "Salary"), "Salary: 35,000.00 per month")
    c.post("/undo")
    r = edit(c, block(c, "quick"), "The slow green fox jumps over the lazy dog").json()
    assert not r["can_redo"]


# ---- search --------------------------------------------------------------------------------
def test_search(c):
    open_pdf(c, make_pdf(pages=3))
    r = c.get("/search", params={"q": "Salary"}).json()
    assert r["count"] == 3 and [h["page"] for h in r["hits"]] == [0, 1, 2]
    assert c.get("/search", params={"q": "zzzz"}).json()["count"] == 0


# ---- page tools ----------------------------------------------------------------------------
def page_tags(c):
    d = saved(c)
    return [d[i].get_text("text").split("[p")[1][0] for i in range(len(d))]


def test_page_move_delete_duplicate_rotate(c):
    open_pdf(c, make_pdf(pages=4))
    assert c.post("/pages", json=dict(op="move", page=0, to=2)).status_code == 200
    assert page_tags(c) == ["2", "3", "1", "4"]
    c.post("/pages", json=dict(op="move", page=3, to=0))
    assert page_tags(c) == ["4", "2", "3", "1"]
    r = c.post("/pages", json=dict(op="delete", page=1)).json()
    assert len(r["pages"]) == 3 and page_tags(c) == ["4", "3", "1"]
    r = c.post("/pages", json=dict(op="duplicate", page=0)).json()
    assert page_tags(c) == ["4", "4", "3", "1"]
    r = c.post("/pages", json=dict(op="rotate", page=0, deg=90)).json()
    assert round(r["pages"][0]["w"]) == 842 and saved(c)[0].rotation == 90
    assert c.post("/undo").status_code == 200 and saved(c)[0].rotation == 0


def test_cannot_delete_last_page(c):
    open_pdf(c, make_pdf())
    assert c.post("/pages", json=dict(op="delete", page=0)).status_code == 400


def test_edit_after_page_delete_uses_right_page(c):
    open_pdf(c, make_pdf(pages=3))
    c.post("/pages", json=dict(op="delete", page=0))
    b = block(c, "Salary", page=0)
    assert "[p2]" in b["text"]
    assert edit(c, b, "Salary: 35,000.00 per month [p2]").status_code == 200
    assert "35,000.00" in " ".join(words(saved(c), 0))


# ---- add text / image / highlight -----------------------------------------------------------
def test_add_text_multiline_and_style(c):
    open_pdf(c, make_pdf())
    r = c.post("/edit", json=dict(type="add", page=0, x=60, y=300, text="Hello\nWorld", family="serif", size=16,
                                  bold=True, italic=False, color=0xCC0000))
    assert r.status_code == 200, r.text
    t = saved(c)[0].get_text("text")
    assert "Hello" in t and "World" in t
    assert c.post("/edit", json=dict(type="add", page=0, x=1, y=1, text="   ")).status_code == 422


def png_bytes():
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 20), False)
    pix.set_rect(pix.irect, (220, 20, 20))
    return pix.tobytes("png")


@pytest.mark.parametrize("rotation", [0, 90])
def test_image_lands_where_clicked(c, rotation):
    d = fitz.open(); p = d.new_page(); p.set_rotation(rotation)
    open_pdf(c, d.tobytes())
    r = c.post("/image?page=0&x=100&y=200&w=80", content=png_bytes())
    assert r.status_code == 200, r.text
    pg = saved(c)[0]
    pix = pg.get_pixmap(dpi=72)                              # 1 px = 1 pt in the displayed orientation
    assert pix.pixel(140, 215)[:3] == (220, 20, 20)           # inside 100..180 x 200..240
    assert pix.pixel(60, 215)[:3] == (255, 255, 255)


def test_image_rejects_garbage(c):
    open_pdf(c, make_pdf())
    assert c.post("/image?page=0&x=1&y=1", content=b"not an image").status_code == 400


def test_highlight(c):
    open_pdf(c, make_pdf())
    assert c.post("/edit", json=dict(type="highlight", page=0, rect=[60, 88, 200, 104])).status_code == 200
    assert len(list(saved(c)[0].annots())) == 1


# ---- rendering ------------------------------------------------------------------------------
def test_page_image_scales_and_caches(c):
    open_pdf(c, make_pdf())
    a = c.get("/pageimg/0?s=1"); b = c.get("/pageimg/0?s=2")
    assert a.status_code == 200 and len(b.content) > len(a.content)
    assert a.headers["cache-control"].startswith("private")
    assert c.get("/pageimg/5").status_code == 404


# ---- session lifecycle ------------------------------------------------------------------------
def test_download_name_and_delete(c):
    open_pdf(c, make_pdf(), name="C:\\Users\\me\\My Payslip (Oct).pdf")
    cd = c.get("/save").headers["content-disposition"]
    assert "My Payslip (Oct)-edited.pdf" in cd and "C:" not in cd
    assert c.get("/ping").json() == {"alive": True}
    assert c.delete("/session").json() == {"deleted": True}
    assert c.get("/save").status_code == 400
