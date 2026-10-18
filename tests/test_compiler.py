import pymupdf as fitz
from typing import List, Tuple
from test_1 import extract_page_geometry, SpatialClusterer, ParagraphBlock
from test_reflow import TextReflowEngine, ReflowResult


class PDFCompiler:
    """
    Mutates PDF content streams by redacting targeted paragraph regions
    and injecting freshly reflowed typographical lines.
    """
    def __init__(self, src_pdf_path: str):
        self.doc = fitz.open(src_pdf_path)

    def replace_paragraph(
        self,
        page_num: int,
        orig_para: ParagraphBlock,
        reflow_res: ReflowResult,
        fontname: str = "helv",
        fontsize: float = 11.0,
        fontcolor: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    ):
        page = self.doc.load_page(page_num)

        # 1. Define redaction rectangle matching the original block bbox.
        # Slight padding (+0.5 pt) ensures sub-pixel anti-aliasing artifacts are eliminated.
        x0, y0, x1, y1 = orig_para.bbox
        redact_rect = fitz.Rect(x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)

        # Add redaction without any background color fill (clears vector text only)
        page.add_redact_annot(redact_rect, fill=False)
        page.apply_redactions()

        # 2. Re-inject reflowed lines at their calculated baseline origins
        for line in reflow_res.lines:
            page.insert_text(
                point=line.baseline_origin,
                text=line.text,
                fontname=fontname,
                fontsize=fontsize,
                color=fontcolor
            )

    def save(self, output_pdf_path: str):
        # Garbage collect and defragment mutated streams
        self.doc.save(
            output_pdf_path,
            garbage=4,        # Purge unused objects/streams
            deflate=True      # Recompress content streams
        )
        self.doc.close()


# =====================================================================
# END-TO-END MUTATION TEST
# =====================================================================
if __name__ == "__main__":
    src_file = "test_sample.pdf"
    out_file = "test_edited_output.pdf"

    print("[*] Running End-to-End Pipeline: Step 1 -> Step 2 -> Step 4 -> Step 5\n")

    # Step 1 & 2: Parse and Cluster
    geometry = extract_page_geometry(src_file, page_number=0)
    clusterer = SpatialClusterer()
    paragraphs = clusterer.process(geometry)

    # Target Block 2 (the multi-line paragraph we tested earlier)
    target_block = paragraphs[1]
    print(f"[Targeting Block 2 for Live In-Place Edit]")
    print(f"Original Text:\n{target_block.text}\n")

    # Step 4: Reflow with newly modified text
    engine = TextReflowEngine(font_name="helv", font_size=11.0, line_height=15.0)
    user_edited_text = (
        "This is the edited paragraph where the user typed a significantly "
        "longer amount of text into the PDF editor. Notice how the reflow "
        "engine calculates line widths against the original paragraph "
        "boundaries and wraps seamlessly."
    )

    # Initial baseline is the baseline of the first line of Block 2
    first_baseline_y = target_block.lines[0].baseline_y
    reflow_res = engine.reflow_paragraph(
        new_text=user_edited_text,
        original_bbox=target_block.bbox,
        initial_baseline_y=first_baseline_y
    )

    print(f"[+] Reflow complete: Generated {len(reflow_res.lines)} wrapped lines.")
    print(f"    Vertical expansion delta: +{reflow_res.height_delta:.2f} pt\n")

    # Step 5: Compile into updated PDF
    print(f"[*] Mutating PDF streams and writing to {out_file}...")
    compiler = PDFCompiler(src_file)
    compiler.replace_paragraph(
        page_num=0,
        orig_para=target_block,
        reflow_res=reflow_res,
        fontname="helv",
        fontsize=11.0,
        fontcolor=(0.0, 0.2, 0.8)  # Render in blue to visually spot the edit
    )
    compiler.save(out_file)
    print(f"[SUCCESS] Saved mutated PDF: {out_file}\n")

    # Verification: Re-parse the compiled PDF to prove the new text is in the PDF stream
    print("[*] Verifying compiled PDF text extraction...")
    verify_doc = fitz.open(out_file)
    extracted_text = verify_doc[0].get_text()
    print("=" * 60)
    print("NEW EXTRACTED PAGE TEXT:")
    print("=" * 60)
    print(extracted_text.strip())
    print("=" * 60)
