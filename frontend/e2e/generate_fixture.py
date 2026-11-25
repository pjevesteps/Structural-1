"""Generate a deterministic 2-page architectural vector PDF for Playwright browser E2E tests."""
from pathlib import Path
import sys
import pymupdf


def generate_sample_plan(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()

    # --- Page 1: Elevation with 4 structural floor-to-floor levels + 1 non-floor distractor ---
    p1 = doc.new_page(width=800, height=560)
    p1.insert_text(pymupdf.Point(300, 510), "FRONT ELEVATION", fontsize=13)
    sh1 = p1.new_shape()
    elev_rows = [
        (430.0, "BASEMENT FLOOR FINISHED LINE", "0.00 m"),
        (378.0, "NATURAL GRADE LINE", "2.60 m"),                # distractor
        (360.0, "FIRST FLOOR FINISHED FLOOR LINE", "2.80 m"),
        (280.0, "SECOND FLOOR FINISHED FLOOR LINE", "5.80 m"),
        (200.0, "ROOF DECK FINISHED FLOOR LINE", "8.60 m"),
    ]
    for y_pt, label, elev_txt in elev_rows:
        sh1.draw_line(pymupdf.Point(150.0, y_pt), pymupdf.Point(650.0, y_pt))
        sh1.finish(color=(0.2, 0.2, 0.2), width=0.9)
        p1.insert_text(pymupdf.Point(24.0, y_pt - 4.0), label, fontsize=8.5)
        p1.insert_text(pymupdf.Point(662.0, y_pt - 4.0), elev_txt, fontsize=8.5)
    sh1.commit()

    # --- Page 2: Floor Plan (2x2 bay structural grid A/B/C x 1/2/3, 4x4m bays, 9 gray columns) ---
    # Scale: 30 pt = 1.0 m -> 8.0 m = 240 pt; 0.40 m column = 12 pt square
    pt_per_m = 30.0
    col_half = 0.40 * pt_per_m * 0.5  # 6.0 pt
    ax, ay = 180.0, 380.0  # Grid A-1 origin

    p2 = doc.new_page(width=760, height=520)
    sh2 = p2.new_shape()

    # Dashed/solid grid lines for visual reference + bubbles
    for lbl, xm in (("A", 0.0), ("B", 4.0), ("C", 8.0)):
        x = ax + xm * pt_per_m
        sh2.draw_line(pymupdf.Point(x, 85.0), pymupdf.Point(x, 425.0))
        sh2.finish(color=(0.45, 0.45, 0.45), width=0.6)
        p2.insert_text(pymupdf.Point(x - 4.0, 72.0), lbl, fontsize=11)

    for lbl, ym in (("1", 0.0), ("2", 4.0), ("3", 8.0)):
        y = ay - ym * pt_per_m
        sh2.draw_line(pymupdf.Point(110.0, y), pymupdf.Point(500.0, y))
        sh2.finish(color=(0.45, 0.45, 0.45), width=0.6)
        p2.insert_text(pymupdf.Point(88.0, y + 4.0), lbl, fontsize=11)

    # 9 gray-shaded 0.40m x 0.40m columns at all grid intersections
    for xm in (0.0, 4.0, 8.0):
        for ym in (0.0, 4.0, 8.0):
            cx = ax + xm * pt_per_m
            cy = ay - ym * pt_per_m
            sh2.draw_rect(pymupdf.Rect(cx - col_half, cy - col_half, cx + col_half, cy + col_half))
            sh2.finish(color=(0.1, 0.1, 0.1), fill=(0.55, 0.55, 0.55), width=0.6)
    sh2.commit()

    # Title + text distractors
    p2.insert_text(pymupdf.Point(240.0, 470.0), "FIRST FLOOR PLAN", fontsize=12)
    p2.insert_text(pymupdf.Point(225.0, 383.0), "4.00", fontsize=10)
    p2.insert_text(pymupdf.Point(220.0, 315.0), "LIVING AREA", fontsize=10)

    doc.save(out_path)
    doc.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / ".tmp-data" / "sample_plan.pdf"
    generate_sample_plan(target)
    print(f"Generated deterministic fixture PDF: {target}")
