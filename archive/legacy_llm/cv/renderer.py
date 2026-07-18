import pathlib
from fpdf import FPDF

_FONT_PATH = str(pathlib.Path(__file__).parent / "DejaVuSans.ttf")


def render_cv_pdf(cv_text: str, role_title: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 20, 20)
    pdf.add_page()
    pdf.add_font("DejaVu", fname=_FONT_PATH)
    pdf.set_font("DejaVu", size=11)
    pdf.multi_cell(0, 6, cv_text)
    return bytes(pdf.output())
