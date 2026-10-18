import pymupdf as fitz  # Updated import to silence deprecation warning
import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any


# =====================================================================
# 1. GENERATE SYNTHETIC TEST PDF
# =====================================================================
def create_test_pdf(filename: str = "test_sample.pdf"):
    """Creates a sample PDF with headers, multi-line paragraphs, and 2 columns."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)  # Standard Letter size

    # Header
    page.insert_text(
        (50, 60),
        "Modular PDF Engine Test",
        fontsize=18,
        fontname="helv",
        color=(0.1, 0.1, 0.1),
    )

    # Paragraph 1 (Multi-line)
    p1_line1 = "This is the first line of an editable paragraph block in the document."
    p1_line2 = "PDF text is stored as discrete character fragments without structure."
    page.insert_text(
        (50, 100), p1_line1, fontsize=11, fontname="helv", color=(0, 0, 0)
    )
    page.insert_text(
        (50, 115), p1_line2, fontsize=11, fontname="helv", color=(0, 0, 0)
    )

    # Paragraph 2 (Separate paragraph after extra vertical gap)
    p2_text = "Here is a standalone paragraph separated by deliberate leading spacing."
    page.insert_text(
        (50, 155), p2_text, fontsize=11, fontname="helv", color=(0, 0, 0)
    )

    # Two-Column Section (Tests that lines with identical Y don't accidentally merge)
    # Column 1 (Left: X = 50)
    page.insert_text(
        (50, 220), "Column 1: Line Alpha", fontsize=10, fontname="helv"
    )
    page.insert_text(
        (50, 235), "Column 1: Line Beta", fontsize=10, fontname="helv"
    )

    # Column 2 (Right: X = 320, same vertical heights!)
    page.insert_text(
        (320, 220), "Column 2: Line Gamma", fontsize=10, fontname="helv"
    )
    page.insert_text(
        (320, 235), "Column 2: Line Delta", fontsize=10, fontname="helv"
    )

    doc.save(filename)
    doc.close()
    print(f"[+] Created test PDF: {filename}\n")


# =====================================================================
# 2. STEP 1: GEOMETRY PARSER (FIXED)
# =====================================================================
def extract_page_geometry(
    pdf_path: str, page_number: int = 0
) -> Dict[str, Any]:
    doc = fitz.open(pdf_path)
    page = doc.load_page(page_number)

    # Read page dimensions BEFORE closing the document
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)

    page_data = page.get_text("rawdict")

    extracted_blocks = []
    for block in page_data.get("blocks", []):
        if block.get("type") != 0:
            continue

        block_record = {"bbox": block["bbox"], "lines": []}
        for line in block.get("lines", []):
            line_record = {
                "bbox": line["bbox"],
                "wmode": line.get("wmode", 0),
                "dir": line.get("dir", (1.0, 0.0)),
                "spans": [],
            }
            for span in line.get("spans", []):
                span_record = {
                    "font": span.get("font"),
                    "size": span.get("size"),
                    "color": span.get("color"),
                    "flags": span.get("flags"),
                    "chars": [],
                }
                for char in span.get("chars", []):
                    span_record["chars"].append(
                        {
                            "char": char.get("c"),
                            "origin": char.get("origin"),
                            "bbox": char.get("bbox"),
                        }
                    )
                line_record["spans"].append(span_record)
            block_record["lines"].append(line_record)
        extracted_blocks.append(block_record)

    doc.close()

    return {
        "page_index": page_number,
        "page_width": page_width,
        "page_height": page_height,
        "blocks": extracted_blocks,
    }


# =====================================================================
# 3. STEP 2: SPATIAL CLUSTERER
# =====================================================================
@dataclass
class CharToken:
    char: str
    bbox: Tuple[float, float, float, float]
    origin: Tuple[float, float]
    font_name: str
    font_size: float
    flags: int


@dataclass
class TextLine:
    tokens: List[CharToken] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    baseline_y: float = 0.0
    dominant_font_size: float = 0.0
    text: str = ""

    def finalize(self):
        if not self.tokens:
            return
        self.tokens.sort(key=lambda t: t.bbox[0])

        x0 = min(t.bbox[0] for t in self.tokens)
        y0 = min(t.bbox[1] for t in self.tokens)
        x1 = max(t.bbox[2] for t in self.tokens)
        y1 = max(t.bbox[3] for t in self.tokens)
        self.bbox = (x0, y0, x1, y1)

        self.baseline_y = float(np.median([t.origin[1] for t in self.tokens]))
        self.dominant_font_size = float(
            np.median([t.font_size for t in self.tokens])
        )

        chars_out = []
        for i, t in enumerate(self.tokens):
            if i > 0:
                prev_t = self.tokens[i - 1]
                dx = t.bbox[0] - prev_t.bbox[2]
                if (
                    dx > (self.dominant_font_size * 0.25)
                    and not prev_t.char.isspace()
                ):
                    chars_out.append(" ")
            chars_out.append(t.char)
        self.text = "".join(chars_out)


@dataclass
class ParagraphBlock:
    lines: List[TextLine] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    text: str = ""

    def finalize(self):
        if not self.lines:
            return
        self.lines.sort(key=lambda ln: ln.baseline_y)

        x0 = min(ln.bbox[0] for ln in self.lines)
        y0 = min(ln.bbox[1] for ln in self.lines)
        x1 = max(ln.bbox[2] for ln in self.lines)
        y1 = max(ln.bbox[3] for ln in self.lines)
        self.bbox = (x0, y0, x1, y1)
        self.text = "\n".join(ln.text for ln in self.lines)


class SpatialClusterer:

    def __init__(
        self,
        line_y_tolerance_ratio: float = 0.35,
        para_lead_ratio: float = 1.6,
        max_char_h_gap_ratio: float = 2.5,
    ):
        self.line_y_tolerance_ratio = line_y_tolerance_ratio
        self.para_lead_ratio = para_lead_ratio
        self.max_char_h_gap_ratio = max_char_h_gap_ratio

    def cluster_characters_into_lines(
        self, tokens: List[CharToken]
    ) -> List[TextLine]:
        if not tokens:
            return []

        tokens.sort(key=lambda t: (t.origin[1], t.bbox[0]))

        lines: List[TextLine] = []
        for token in tokens:
            matched = False
            for line in reversed(lines[-8:] if len(lines) >= 8 else lines):
                allowed_y_jitter = (
                    line.dominant_font_size * self.line_y_tolerance_ratio
                )
                same_row = (
                    abs(token.origin[1] - line.baseline_y) <= allowed_y_jitter
                )

                if same_row:
                    closest_char_dist = min(
                        abs(token.bbox[0] - t.bbox[2]) for t in line.tokens
                    )
                    max_allowed_gap = (
                        line.dominant_font_size * self.max_char_h_gap_ratio
                    )

                    if closest_char_dist <= max_allowed_gap:
                        line.tokens.append(token)
                        line.baseline_y = float(
                            np.mean([t.origin[1] for t in line.tokens])
                        )
                        matched = True
                        break

            if not matched:
                new_line = TextLine(
                    tokens=[token],
                    baseline_y=token.origin[1],
                    dominant_font_size=token.font_size,
                )
                lines.append(new_line)

        for line in lines:
            line.finalize()
        return lines

    def cluster_lines_into_paragraphs(
        self, lines: List[TextLine]
    ) -> List[ParagraphBlock]:
        if not lines:
            return []

        # Group lines by approximate column band before clustering top-to-bottom
        # First sort by X coordinate band to keep separate columns decoupled
        lines.sort(key=lambda ln: (round(ln.bbox[0] / 50.0), ln.baseline_y))

        paragraphs: List[ParagraphBlock] = []
        for line in lines:
            if not paragraphs:
                paragraphs.append(ParagraphBlock(lines=[line]))
                continue

            current_para = paragraphs[-1]
            prev_line = current_para.lines[-1]

            delta_y = line.baseline_y - prev_line.baseline_y
            median_size = (
                line.dominant_font_size + prev_line.dominant_font_size
            ) / 2.0
            max_lead = median_size * self.para_lead_ratio

            # Horizontal continuity check
            prev_x0, _, prev_x1, _ = prev_line.bbox
            curr_x0, _, curr_x1, _ = line.bbox
            overlap_x = max(0.0, min(prev_x1, curr_x1) - max(prev_x0, curr_x0))
            has_spatial_continuity = (
                overlap_x > 0.0 or abs(curr_x0 - prev_x0) <= (median_size * 2.0)
            )

            font_size_continuity = (
                abs(line.dominant_font_size - prev_line.dominant_font_size)
                <= 1.5
            )

            if (
                (0.0 < delta_y <= max_lead)
                and has_spatial_continuity
                and font_size_continuity
            ):
                current_para.lines.append(line)
            else:
                paragraphs.append(ParagraphBlock(lines=[line]))

        for para in paragraphs:
            para.finalize()
        return paragraphs

    def process(
        self, raw_page_geometry: Dict[str, Any]
    ) -> List[ParagraphBlock]:
        tokens: List[CharToken] = []
        for block in raw_page_geometry.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    f_name = span.get("font", "Unknown")
                    f_size = float(span.get("size", 12.0))
                    f_flags = int(span.get("flags", 0))
                    for ch in span.get("chars", []):
                        tokens.append(
                            CharToken(
                                char=ch["char"],
                                bbox=tuple(ch["bbox"]),
                                origin=tuple(ch["origin"]),
                                font_name=f_name,
                                font_size=f_size,
                                flags=f_flags,
                            )
                        )

        lines = self.cluster_characters_into_lines(tokens)
        paragraphs = self.cluster_lines_into_paragraphs(lines)
        return paragraphs


# =====================================================================
# 4. EXECUTE PIPELINE
# =====================================================================
if __name__ == "__main__":
    pdf_filename = "test_sample.pdf"

    # 1. Build test sample
    create_test_pdf(pdf_filename)

    # 2. Extract geometry
    print("[*] Running Step 1: Geometry & Character Extraction...")
    geometry = extract_page_geometry(pdf_filename, page_number=0)

    total_chars = sum(
        len(span.get("chars", []))
        for b in geometry["blocks"]
        for l in b["lines"]
        for span in l["spans"]
    )
    print(f"    -> Extracted {total_chars} character tokens.")

    # 3. Cluster into paragraphs
    print("[*] Running Step 2: Spatial Clustering into Paragraphs...")
    clusterer = SpatialClusterer()
    paragraphs = clusterer.process(geometry)
    print(f"    -> Clustered into {len(paragraphs)} paragraph block(s).\n")

    # 4. Print results
    print("=" * 60)
    print("PARAGRAPH CLUSTER RESULTS")
    print("=" * 60)
    for i, p in enumerate(paragraphs, 1):
        x0, y0, x1, y1 = [round(c, 2) for c in p.bbox]
        print(f"\n[Block {i}] Lines: {len(p.lines)} | BBox: ({x0}, {y0}, {x1}, {y1})")
        print(f"Text:\n{p.text}")
        print("-" * 60)
