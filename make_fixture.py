"""Create a tiny test 'manga' PDF at inputs/ch_001.pdf so you can run the
pipeline immediately - no real (copyrighted) manhwa needed. Three pages, each
with two drawn panels and placeholder 'bubble' text.
"""
import fitz  # PyMuPDF

from config import INPUTS_DIR


def main() -> None:
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(1, 4):
        page = doc.new_page(width=800, height=1200)
        page.draw_rect(fitz.Rect(40, 40, 760, 580), color=(0, 0, 0), width=2)
        page.draw_rect(fitz.Rect(40, 620, 760, 1160), color=(0, 0, 0), width=2)
        page.insert_text((80, 120), f"Page {i} - top panel", fontsize=28)
        page.insert_text((80, 170), "Placeholder bubble text.", fontsize=18)
        page.insert_text((80, 700), f"Page {i} - bottom panel", fontsize=28)
        page.insert_text((80, 750), "More placeholder dialogue.", fontsize=18)
    out = INPUTS_DIR / "ch_001.pdf"
    doc.save(str(out))
    doc.close()
    print(f"Wrote test fixture: {out}")


if __name__ == "__main__":
    main()
