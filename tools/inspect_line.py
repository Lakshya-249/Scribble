"""Print the exact glyph positions of text lines, to compare an original PDF with an edited one.

    python tools/inspect_line.py original.pdf "Sal's TED talk from 2015"
    python tools/inspect_line.py edited.pdf   "more about"      (save the edited PDF from the app first)

For every line containing the text it prints, per glyph: char, pen-origin x, right edge, font.
Look at the x of the bullet and of the first letter: in the edited PDF they should equal the original's.
"""
import sys
import pymupdf as fitz


def main(path, needle):
    doc = fitz.open(path)
    hits = 0
    for page in doc:
        for b in page.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                chars = [c for sp in ln["spans"] for c in sp["chars"]]
                text = "".join(c["c"] for c in chars)
                if needle not in text:
                    continue
                hits += 1
                print(f"\n--- page {page.number + 1}  line y={ln['bbox'][1]:.1f}-{ln['bbox'][3]:.1f}  text={text!r}")
                for sp in ln["spans"]:
                    print(f"  span font={sp['font']} size={sp['size']:.1f}")
                    for c in sp["chars"][:8]:
                        print(f"    {c['c']!r:8} (U+{ord(c['c'][0]):04X})  origin_x={c['origin'][0]:.2f}  x1={c['bbox'][2]:.2f}")
                    if len(sp["chars"]) > 8:
                        print("    ...")
    if not hits:
        print("text not found")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
