"""Why does editing this text look wrong?   python tools/diagnose.py file.pdf "30,000.00" ["35,000.00"]

For every span containing the search text it prints the font the PDF uses, whether that font
is embedded, whether the embedded copy can draw NEW characters (producer subsets usually
cannot), which installed font would stand in, and which characters of the new text would
have to come from a different font.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf as fitz
from services.font_service import find_system_font, split_style, base14, sys_fonts


def main(path, needle, new=None):
    new = new or needle
    doc = fitz.open(path)
    sys_fonts()
    found = 0
    for page in doc:
        fonts = {f[3].split("+", 1)[-1]: f for f in page.get_fonts(full=True)}
        for b in page.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    text = "".join(c["c"] for c in sp["chars"])
                    if needle not in text:
                        continue
                    found += 1
                    name, fl = sp["font"], sp["flags"]
                    print(f"\n--- page {page.number + 1}: {text!r}")
                    print(f"font      : {name}   size {sp['size']:.1f}   colour #{sp['color']:06x}")
                    print(f"flags     : {fl}  (serif={bool(fl & 4)} bold={bool(fl & 16)} italic={bool(fl & 2)})"
                          f"   parsed name -> {split_style(name)}")
                    f = fonts.get(name.split('+', 1)[-1])
                    emb = None
                    if f:
                        try:
                            _, ext, _, buf = doc.extract_font(f[0])
                            if buf and ext not in ("", "n/a"):
                                emb = fitz.Font(fontbuffer=buf)
                                print(f"embedded  : yes ({ext}, {len(buf)} bytes) -> maps text: "
                                      f"{any(emb.has_glyph(ord(c)) for c in 'aA0e')}")
                        except Exception as e:
                            print("embedded  : extract failed:", e)
                    if emb is None:
                        print("embedded  : NO (or unreadable)")
                    sysf = find_system_font(name, fl)
                    print("installed :", sysf or "none found -> falls back to base-14 "
                          f"'{base14(name, fl)}'  (put the .ttf in ./fonts)")
                    sf = fitz.Font(fontfile=sysf) if sysf else None
                    miss_emb = [c for c in new if not (emb and emb.has_glyph(ord(c)))]
                    miss_sys = [c for c in new if not (sf and sf.has_glyph(ord(c)))]
                    print(f"new text  : {new!r}")
                    print(f"  not drawable with embedded font : {''.join(miss_emb) or '-'}")
                    print(f"  not drawable with installed font: {''.join(miss_sys) or '-'}")
    if not found:
        print("text not found (scanned page? try a shorter piece of the text)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(*sys.argv[1:4])
