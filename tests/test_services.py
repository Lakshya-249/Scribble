"""Run with:  python -m pytest tests/test_services.py -q"""
import os, sys, pytest
import pymupdf as fitz
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from main import app
from services import sessions
from services import erase_service
from services.page_geometry import extract_page_geometry
from services.spatial_cluster import SpatialClusterer

PARA = ("The quick brown fox jumps over the lazy dog while the five boxing wizards jump "
        "quickly and the sphinx of black quartz judges my vow every single morning.")


def make_pdf(path, title, para=PARA, color=(0.8, 0, 0)):
    d = fitz.open(); p = d.new_page()
    p.insert_text((60, 80), title, fontsize=18, fontname="hebo", color=color)
    p.insert_textbox(fitz.Rect(60, 100, 400, 220), para, fontsize=11, fontname="helv", align=3)
    p.insert_text((60, 300), "Footer line that must never move", fontsize=11, fontname="helv")
    d.save(str(path)); return str(path)


def open_pdf(client, path, **kw):
    return client.post("/open", content=open(path, "rb").read(), **kw)


def block(client, start, page=0):
    return next(b for b in client.get(f"/text/{page}").json() if b["text"].startswith(start))


def edit(client, b, new, page=0):
    return client.post("/edit", json={"type": "line", "page": page, "old": b["text"], "bbox": b["bbox"], "new": new})


@pytest.fixture
def pdfs(tmp_path):
    return make_pdf(tmp_path / "a.pdf", "Alpha Heading"), make_pdf(tmp_path / "b.pdf", "Bravo Heading", "Second user text only.", (0, 0, .8))


# ---------------------------------------------------------------- spaces in the editor text
def words_pdf(path):
    d = fitz.open(); p = d.new_page()
    def row(y, text, gap_em, size=12):
        x = 50
        for w in text.split():       # one object per word, NO space glyphs
            p.insert_text((x, y), w, fontsize=size, fontname="helv"); x += fitz.get_text_length(w, "helv", size) + gap_em * size
    row(100, "normal gap between every word here", 0.25)
    row(130, "tight justified gap between words here", 0.10)
    row(160, "very tight condensed gap words here", 0.07)
    x = 50
    for ch in "PAY SLIP DETAILS":    # letter-spaced heading
        if ch == " ": x += 0.43 * 14; continue
        p.insert_text((x, 200), ch, fontsize=14, fontname="hebo"); x += fitz.get_text_length(ch, "hebo", 14) + 0.18 * 14
    p.insert_text((50, 240), "Name:  John Smith   ID 42", fontsize=12, fontname="helv")   # real double spaces
    d.save(str(path))


def test_word_gaps_are_detected_at_every_tightness(tmp_path):
    words_pdf(tmp_path / "w.pdf")
    texts = [b.text for b in SpatialClusterer().process(extract_page_geometry(str(tmp_path / "w.pdf"), 0))]
    assert "normal gap between every word here" in texts
    assert "tight justified gap between words here" in texts
    assert "very tight condensed gap words here" in texts
    assert "PAY SLIP DETAILS" in texts                     # tracking is not word spacing
    assert "Name:  John Smith   ID 42" in texts            # real spaces are never doubled


def test_payslip_sample_has_no_glued_words():
    here = os.path.join(os.path.dirname(__file__), "..", "temp", "sample1.pdf")
    if not os.path.exists(here):
        pytest.skip("sample1.pdf not present")
    texts = [b.text for b in SpatialClusterer().process(extract_page_geometry(here, 0))]
    assert any("56,000.00 (Indian Rupee" in t for t in texts)


# ---------------------------------------------------------------- multi-user
def test_two_users_never_see_each_other(pdfs):
    a, b = TestClient(app), TestClient(app)
    open_pdf(a, pdfs[0]); open_pdf(b, pdfs[1])
    assert a.cookies.get("sid") != b.cookies.get("sid")
    assert "Alpha Heading" in str(a.get("/text/0").json()) and "Alpha" not in str(b.get("/text/0").json())
    before_b = b.get("/text/0").json()
    assert edit(a, block(a, "The quick"), PARA.replace("quick", "very quick")).status_code == 200
    assert b.get("/text/0").json() == before_b                          # B untouched
    assert b.post("/undo").status_code == 400                           # B has nothing to undo
    assert a.post("/undo").status_code == 200                           # A does
    assert "very quick" not in str(a.get("/text/0").json())


def test_no_session_means_clear_error_not_crash():
    c = TestClient(app)
    for r in (c.get("/text/0"), c.get("/pageimg/0"), c.get("/save"), c.post("/undo"),
              c.post("/edit", json={"type": "line", "page": 0})):
        assert r.status_code == 400 and "open your PDF" in r.json()["error"]


def test_expired_session_is_cleaned_up(pdfs, monkeypatch):
    c = TestClient(app); open_pdf(c, pdfs[0])
    s = sessions._SESSIONS[c.cookies.get("sid")]
    folder = s.dir; assert os.path.isdir(folder)
    s.seen -= 10 ** 6
    sessions.reap_once()
    assert not os.path.exists(folder)
    assert c.get("/text/0").status_code == 400


def test_session_cap_returns_busy(pdfs, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_SESSIONS", len(sessions._SESSIONS))
    r = open_pdf(TestClient(app), pdfs[0])
    assert r.status_code == 503 and "busy" in r.json()["error"]


# ---------------------------------------------------------------- limits / security
def test_bad_uploads_and_limits(pdfs, monkeypatch):
    c = TestClient(app)
    r = c.post("/open", content=b"this is not a pdf"); assert r.status_code == 400 and "error" in r.json()
    monkeypatch.setattr(sessions, "MAX_UPLOAD", 100)
    assert open_pdf(c, pdfs[0]).status_code == 413


def test_password_pdf(tmp_path):
    d = fitz.open(); d.new_page().insert_text((50, 50), "secret", fontsize=12)
    path = tmp_path / "enc.pdf"; d.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    c = TestClient(app)
    assert open_pdf(c, path).status_code == 401
    assert open_pdf(c, path, params={"password": "wrong"}).status_code == 401
    assert open_pdf(c, path, params={"password": "pw"}).status_code == 200
    assert "secret" in str(c.get("/text/0").json())


def test_foreign_host_and_origin_rejected(pdfs):
    c = TestClient(app)
    assert c.get("/", headers={"host": "evil.example"}).status_code == 400
    r = c.post("/open", content=b"x", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_errors_use_the_error_key_the_frontend_reads(pdfs):
    c = TestClient(app); open_pdf(c, pdfs[0])
    r = c.post("/edit", json={"type": "nonsense", "page": 0})
    assert r.status_code == 400 and r.json()["error"]
    assert c.get("/text/99").json()["error"] == "Page not found"


# ---------------------------------------------------------------- editing
def test_edit_keeps_colour_rewraps_and_footer_stays(pdfs):
    c = TestClient(app); open_pdf(c, pdfs[0])
    b = block(c, "The quick")
    assert "\n" not in b["text"]                                        # soft wraps are spaces
    assert edit(c, b, b["text"].replace("quick brown", "very quick brown")).status_code == 200
    d = fitz.open("pdf", c.get("/save").content)
    text = d[0].get_text("text")
    assert "very quick brown" in text and "Footer line that must never move" in text
    cols = {s["text"].strip(): s["color"] for bl in d[0].get_text("dict")["blocks"] for l in bl.get("lines", []) for s in l["spans"]}
    assert cols["Alpha Heading"] == 0xCC0000                            # untouched heading keeps its colour


def test_edited_coloured_text_keeps_its_colour(tmp_path):
    d = fitz.open(); p = d.new_page()
    p.insert_text((60, 100), "Red warning text here", fontsize=14, fontname="helv", color=(0.8, 0, 0)); d.save(str(tmp_path / "r.pdf"))
    c = TestClient(app); open_pdf(c, tmp_path / "r.pdf")
    b = block(c, "Red warning"); assert edit(c, b, "Red warning text changed").status_code == 200
    spans = [s for bl in fitz.open("pdf", c.get("/save").content)[0].get_text("dict")["blocks"] for l in bl["lines"] for s in l["spans"]]
    assert any("changed" in s["text"] and s["color"] == 0xCC0000 for s in spans)


def test_unchanged_edit_adds_no_undo_step(pdfs):
    c = TestClient(app); open_pdf(c, pdfs[0])
    b = block(c, "The quick"); r = edit(c, b, b["text"])
    assert r.json()["unchanged"] is True and c.post("/undo").status_code == 400


def test_failed_edit_rolls_back(pdfs):
    c = TestClient(app); open_pdf(c, pdfs[0])
    b = block(c, "The quick")
    r = c.post("/edit", json={"type": "line", "page": 0, "old": "zzz", "bbox": [0, 0, 1, 1], "new": "x"})
    assert r.status_code == 500 and "not found" in r.json()["error"].lower()
    assert c.post("/undo").status_code == 400                           # rollback consumed its own snapshot
    assert block(c, "The quick")["text"] == b["text"]


def test_hidden_fallback_leaves_page_intact_and_editor_clean(pdfs, monkeypatch):
    monkeypatch.setattr(erase_service, "_redaction_is_safe", lambda *a, **k: False)   # force the clip path
    c = TestClient(app); open_pdf(c, pdfs[0])
    b = block(c, "The quick")
    assert edit(c, b, b["text"].replace("quick", "speedy")).status_code == 200
    texts = [t["text"] for t in c.get("/text/0").json()]
    assert not any("quick" in t for t in texts)                         # hidden old text is not offered again
    assert any("speedy" in t for t in texts) and "Footer line that must never move" in texts
    assert c.post("/undo").status_code == 200
    assert block(c, "The quick")["text"] == b["text"]                   # undo restores text AND ghost list


def test_cover_tool_and_add_text(pdfs):
    c = TestClient(app); open_pdf(c, pdfs[0])
    assert c.post("/edit", json={"type": "cover", "page": 0, "rect": [55, 285, 320, 305]}).status_code == 200
    assert "Footer" not in str(c.get("/text/0").json())
    r = c.post("/edit", json={"type": "add", "page": 0, "x": 60, "y": 400, "text": "Added \u00fcnicode", "size": 12,
                              "family": "serif", "bold": True, "italic": False, "color": 0x006600})
    assert r.status_code == 200 and "Added" in str(c.get("/text/0").json())


def test_rotated_and_cropped_pages_edit_in_place(tmp_path):
    for name, setup in (("crop", lambda p: p.set_cropbox(fitz.Rect(100, 100, 495, 500))), ("rot", lambda p: p.set_rotation(90))):
        d = fitz.open(); p = d.new_page()
        p.insert_text((120, 160), "First heading text", fontsize=16, fontname="helv"); p.insert_text((120, 200), "Second line here", fontsize=12, fontname="helv")
        setup(p); d.save(str(tmp_path / f"{name}.pdf"))
        c = TestClient(app); open_pdf(c, tmp_path / f"{name}.pdf")
        b = block(c, "First heading"); old = fitz.Rect(b["bbox"])
        assert edit(c, b, "NEW heading text").status_code == 200
        nb = block(c, "NEW heading"); assert abs(fitz.Rect(nb["bbox"]).y0 - old.y0) < 3   # did not jump elsewhere
