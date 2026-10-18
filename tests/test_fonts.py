"""Font-name parsing and family matching (needs pymupdf only to import the module)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import font_service as fs


def test_split_style_variants():
    assert fs.split_style("ABCDEF+Ubuntu-Bold") == ("ubuntu", True, False, "bold")
    assert fs.split_style("Ubuntu-R") == ("ubuntu", False, False, "regular")
    assert fs.split_style("Ubuntu-BI") == ("ubuntu", True, True, "bold")
    assert fs.split_style("Arial-BoldMT") == ("arial", True, False, "bold")
    assert fs.split_style("ArialMT") == ("arial", False, False, "regular")
    assert fs.split_style("Open Sans Bold") == ("opensans", True, False, "bold")
    assert fs.split_style("Helvetica Neue")[0] == "helveticaneue"     # 'Neue' is not a style
    assert fs.split_style("Calibri")[0] == "calibri"                  # trailing 'i' is not italic


def test_family_matching_picks_nearest_weight(tmp_path, monkeypatch):
    for n in ("Ubuntu-R", "Ubuntu-B", "Ubuntu-RI", "Ubuntu-M"):
        (tmp_path / f"{n}.ttf").write_bytes(b"")
    monkeypatch.setenv("PDF_FONT_DIRS", str(tmp_path))
    monkeypatch.setattr(fs, "SYS", None)
    monkeypatch.setattr(fs, "FAMILIES", {})
    pick = lambda name, fl=0: os.path.basename(fs.find_system_font(name, fl))
    assert pick("ABCDEF+Ubuntu-Regular") == "Ubuntu-R.ttf"   # the old exact matcher missed this
    assert pick("Ubuntu-Bold") == "Ubuntu-B.ttf"
    assert pick("Ubuntu-Medium") == "Ubuntu-M.ttf"
    assert pick("Ubuntu", 16) == "Ubuntu-B.ttf"               # bold only known from the flags
    assert pick("Ubuntu-Italic") == "Ubuntu-RI.ttf"
    assert fs.find_system_font("NoSuchFont-Bold") is None


def test_serif_flag_is_not_trusted_for_known_sans_fonts():
    assert fs.base14("Ubuntu-Bold", 4 | 16) == "hebo"     # flag says serif, name says Ubuntu
    assert fs.base14("Cambria", 0) == "tiro"
    assert fs.base14("Unknown-Font", 4) == "tiro"         # unknown name: the flag decides


def test_alias_table_uses_free_lookalikes(tmp_path, monkeypatch):
    for n in ("LiberationSans-Regular", "LiberationSans-Bold", "Carlito-Regular", "LiberationSerif-Regular"):
        (tmp_path / f"{n}.ttf").write_bytes(b"")
    monkeypatch.setenv("PDF_FONT_DIRS", str(tmp_path))
    monkeypatch.setattr(fs, "SYS", None)
    monkeypatch.setattr(fs, "FAMILIES", {})
    pick = lambda name, fl=0: os.path.basename(fs.find_system_font(name, fl))
    assert pick("ABCDEF+Arial-BoldMT") == "LiberationSans-Bold.ttf"
    assert pick("ArialMT") == "LiberationSans-Regular.ttf"
    assert pick("Calibri") == "Carlito-Regular.ttf"
    assert pick("TimesNewRomanPSMT") == "LiberationSerif-Regular.ttf"
    assert pick("Helvetica", 16) == "LiberationSans-Bold.ttf"
    assert "caladea" in fs.ALIASES["cambria"]


def test_bold_file_detection():
    assert fs.is_bold_file("/x/Inter-Bold.ttf") and not fs.is_bold_file("/x/Inter-Regular.ttf")
