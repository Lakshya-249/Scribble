import os
import re
import hashlib
import unicodedata
import threading
import pymupdf as fitz

SYS = None
_SYS_LOCK = threading.Lock()


def new_cache() -> dict:
    """Font caches belong to ONE session/document to avoid leaking font bindings."""
    return {"fonts": {}, "emb": set(), "fname": {}, "same": {}, "synth": set()}


def reset_cache(cache: dict):
    cache["fonts"].clear()
    cache["emb"].clear()
    cache["fname"].clear()
    cache.setdefault("same", {}).clear()
    cache.setdefault("synth", set()).clear()


_DEFAULT_CACHE = new_cache()


def norm(n: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", n.split("+", 1)[-1].lower())
    n = re.sub(r"(psmt|mt)$", "", n)
    n = n.replace("bold", "b").replace("italic", "i").replace("oblique", "i")
    n = n.replace("regular", "").replace("roman", "")
    return re.sub(r"bd$", "b", n)


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAMILIES = {}          # family key -> [(bold, italic, weight, path)]

# (bold, italic, weight word) for the style part of a font name / file name
_STYLES = {
    "regular": (0, 0, "regular"), "roman": (0, 0, "regular"), "book": (0, 0, "regular"),
    "normal": (0, 0, "regular"), "r": (0, 0, "regular"),
    "medium": (0, 0, "medium"), "m": (0, 0, "medium"),
    "light": (0, 0, "light"), "l": (0, 0, "light"), "thin": (0, 0, "light"),
    "bold": (1, 0, "bold"), "b": (1, 0, "bold"), "bd": (1, 0, "bold"),
    "semibold": (1, 0, "bold"), "demibold": (1, 0, "bold"), "extrabold": (1, 0, "bold"),
    "black": (1, 0, "bold"), "heavy": (1, 0, "bold"),
    "italic": (0, 1, "regular"), "it": (0, 1, "regular"), "ital": (0, 1, "regular"),
    "oblique": (0, 1, "regular"), "i": (0, 1, "regular"), "ri": (0, 1, "regular"),
    "mi": (0, 1, "medium"), "li": (0, 1, "light"),
    "bolditalic": (1, 1, "bold"), "boldoblique": (1, 1, "bold"), "bi": (1, 1, "bold"),
    "bdit": (1, 1, "bold"),
}


def split_style(name: str):
    """'ABCDEF+Ubuntu-BoldItalic' -> ('ubuntu', bold, italic, weight).
    Understands Ubuntu-R / Ubuntu-BI, Arial-BoldMT, 'Open Sans Bold', ArialBold ..."""
    n = name.split("+", 1)[-1].strip()
    fam, tok = n, ""
    m = re.match(r"^(.*?)[-,_ ]+([A-Za-z]+)$", n)
    if m:
        t = re.sub(r"(psmt|mt)$", "", m.group(2).lower())
        if t in _STYLES:
            fam, tok = m.group(1), t
    else:
        m = re.match(r"^(.*?)(bolditalic|boldoblique|semibold|demibold|extrabold|bold|italic|oblique)(?:psmt|mt)?$", n, re.I)
        if m and m.group(1):
            fam, tok = m.group(1), m.group(2).lower()
    fam = re.sub(r"(psmt|mt)$", "", re.sub(r"[^a-z0-9]", "", fam.lower()))
    b, i, w = _STYLES.get(tok, (0, 0, "regular"))
    return fam, bool(b), bool(i), w


def sys_fonts():
    """norm(file stem) -> path. Also fills FAMILIES. Looks in ./fonts and $PDF_FONT_DIRS
    first, so a hosted copy can ship the fonts its users' PDFs need."""
    global SYS
    with _SYS_LOCK:
        if SYS is not None:
            return SYS
        SYS = {}
        home = os.path.expanduser("~")
        extra = [d for d in os.environ.get("PDF_FONT_DIRS", "").split(os.pathsep) if d]
        search_dirs = [os.path.join(HERE, "fonts")] + extra + [
            r"C:\Windows\Fonts",
            "/Library/Fonts",
            "/System/Library/Fonts",
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            home + "/.fonts",
            home + "/Library/Fonts",
            home + "/.local/share/fonts",
        ]
        for d in search_dirs:
            if not os.path.exists(d):
                continue
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith((".ttf", ".otf")):
                        stem, path = os.path.splitext(f)[0], os.path.join(root, f)
                        SYS.setdefault(norm(stem), path)
                        fam, b, i, w = split_style(stem)
                        FAMILIES.setdefault(fam, []).append((b, i, w, path))
    return SYS


# Commercial / unavailable font family -> free look-alikes, tried in order. These are
# metric-compatible (same letter widths), so a substituted edit keeps the layout.
ALIASES = {
    "arial": ["liberationsans", "arimo", "notosans"],
    "arialnarrow": ["liberationsansnarrow", "liberationsans"],
    "helvetica": ["liberationsans", "arimo", "notosans"],
    "helveticaneue": ["liberationsans", "notosans"],
    "timesnewroman": ["liberationserif", "tinos", "notoserif"],
    "times": ["liberationserif", "tinos", "notoserif"],
    "couriernew": ["liberationmono", "cousine", "dejavusansmono"],
    "courier": ["liberationmono", "cousine", "dejavusansmono"],
    "calibri": ["carlito", "notosans"],
    "cambria": ["caladea", "notoserif"],
    "georgia": ["notoserif", "dejavuserif", "liberationserif"],
    "verdana": ["dejavusans", "notosans"],
    "tahoma": ["dejavusans", "notosans"],
    "trebuchetms": ["notosans", "dejavusans"],
    "segoeui": ["notosans", "opensans"],
    "consolas": ["dejavusansmono", "liberationmono"],
    "lucidaconsole": ["dejavusansmono", "liberationmono"],
    "palatinolinotype": ["notoserif", "liberationserif"],
    "bookantiqua": ["notoserif", "liberationserif"],
}


def _best(cands, bold, ital, weight):
    return min(cands, key=lambda c: (c[0] != bold) * 4 + (c[1] != ital) * 4 + (c[2] != weight))


def find_system_font(name: str, flags: int = 0):
    """Best installed file for a PDF font name: exact file match first, then the same
    family in the closest weight/slant (Ubuntu-Medium -> Ubuntu-M, else Ubuntu-R), then a
    free look-alike (Arial -> Liberation Sans, Calibri -> Carlito ...)."""
    idx = sys_fonts()
    hit = idx.get(norm(name))
    if hit:
        return hit
    fam, bold, ital, weight = split_style(name)
    bold, ital = bold or bool(flags & 16), ital or bool(flags & 2)
    for f in [fam] + ALIASES.get(fam, []):
        cands = FAMILIES.get(f)
        if cands:
            return _best(cands, bold, ital, weight)[3]
    return None


def is_bold_file(path: str) -> bool:
    return split_style(os.path.splitext(os.path.basename(path))[0])[1]


_SANS_HINT = re.compile(
    r"ubuntu|arial|helvet|calibri|roboto|open ?sans|segoe|verdana|tahoma|lato|inter|poppins|"
    r"montserrat|nunito|noto ?sans|source ?sans|dejavu ?sans|liberation ?sans|sans|gothic|mukta|hind"
)
_SERIF_HINT = re.compile(r"times|serif|georgia|roman|garamond|palatino|minion|cambria|book ?antiqua|baskerville|lora|merriweather")


def base14(name: str, flags: int) -> str:
    n = name.lower()
    bold = bool(flags & 16) or any(k in n for k in ("bold", "black", "heavy"))
    ital = bool(flags & 2) or "italic" in n or "oblique" in n
    if flags & 8 or "cour" in n or "mono" in n:
        fam = ["cour", "cobo", "coit", "cobi"]
    elif "sans" not in n and (
        _SERIF_HINT.search(n) or (flags & 4 and not _SANS_HINT.search(n))
    ):
        fam = ["tiro", "tibo", "tiit", "tibi"]
    else:
        fam = ["helv", "hebo", "heit", "hebi"]
    return fam[int(bold) + 2 * int(ital)]


def font_chain(doc: fitz.Document, page: fitz.Page, fname: str, flags: int, cache: dict = None):
    cache = cache if cache is not None else _DEFAULT_CACHE
    key = (page.number, fname, flags)
    if key in cache["fonts"]:
        return cache["fonts"][key]

    chain = []
    want = norm(fname)

    # 1. Try extracting embedded font from PDF
    for f in page.get_fonts(full=True):
        if norm(f[3]) == want:
            try:
                _, ext, _, buf = doc.extract_font(f[0])
                if buf and ext not in ("", "n/a"):
                    fo = fitz.Font(fontbuffer=buf)
                    # Producer subsets (Chrome, wkhtmltopdf ...) often carry no Unicode
                    # cmap: has_glyph() is False for everything and the font can only be
                    # reached through its own glyph ids. Keep it only if it maps text.
                    if fo.buffer and any(fo.has_glyph(ord(c)) for c in "aA0e"):
                        chain.append((fo, None))
            except Exception:
                pass
            break

    # 2. Same family installed (or dropped into ./fonts), nearest weight / slant
    p = find_system_font(fname, flags)
    if p:
        try:
            with open(p, "rb") as fp:
                sbuf = fp.read()
            if sbuf:
                fo = fitz.Font(fontbuffer=sbuf)
                if fo.buffer:
                    chain.append((fo, None))
                    wants_bold = split_style(fname)[1] or bool(flags & 16)
                    if wants_bold and not is_bold_file(p):
                        cache.setdefault("synth", set()).add(id(fo))   # fake it when drawing
        except Exception:
            pass

    cache.setdefault("same", {})[key] = len(chain)     # entries that look like the original

    # 3. Base-14 fallback (guaranteed to render in PDF without needing insert_font)
    code = base14(fname, flags)
    chain.append((fitz.Font(code), code))

    # 4. Universal fallbacks
    for extra in ("notos", "cjk"):
        try:
            fo = fitz.Font(extra)
            if fo.buffer:
                chain.append((fo, None))
            else:
                chain.append((fo, extra))
        except Exception:
            pass

    cache["fonts"][key] = chain
    return chain


def register_font_name(page: fitz.Page, ent, fallback_code: str = "helv", cache: dict = None) -> str:
    cache = cache if cache is not None else _DEFAULT_CACHE
    fo, code = ent
    if code:
        return code

    buf = getattr(fo, "buffer", None)
    if not buf:
        return fallback_code

    name = cache["fname"].get(id(fo))
    if name is None:
        name = "PS" + hashlib.md5(buf).hexdigest()[:10]
        cache["fname"][id(fo)] = name

    # Check if the font resource name STILL exists on the page
    # (page.apply_redactions() purges unused embedded fonts from the page!)
    existing_ref_names = {f[4] for f in page.get_fonts()}
    if name not in existing_ref_names:
        try:
            page.insert_font(fontname=name, fontbuffer=buf)
            cache["emb"].add((page.number, name))
        except Exception:
            return fallback_code

    return name

def rgb(c: int):
    return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)


INVIS = set("\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad")


def invisible(ch: str) -> bool:
    return ch in INVIS or unicodedata.category(ch) in ("Cc", "Cf")
