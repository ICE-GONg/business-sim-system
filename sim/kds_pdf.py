from __future__ import annotations

from io import BytesIO
import re
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape

from .defaults import DEFAULT_SETTINGS


# Deliberately mirror the player KDS page, never the admin settings dictionary.
PUBLIC_SETTING_KEYS = (
    "initial_cash", "component_workers", "component_hours", "product_engineers",
    "product_hours", "components_per_product", "worker_training_cost",
    "engineer_training_cost", "salary_min", "salary_max", "price_min",
    "price_max", "transport_cost", "agent_add_cost", "agent_remove_cost",
    "report_cost", "patent_factor",
)
PUBLIC_MARKET_FIELDS = (
    "city", "max_loan", "interest_rate", "worker_initial_salary",
    "engineer_initial_salary", "component_material", "product_material",
    "component_storage", "product_storage", "population", "penetration",
    "initial_avg_price",
)


def build_public_kds_pdf(
    settings: Mapping[str, Any], markets: Sequence[Mapping[str, Any]],
) -> bytes:
    """Export the same public KDS for players and administrators, without SQL."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import BaseDocTemplate, Flowable, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle

    public = {key: settings.get(key, DEFAULT_SETTINGS[key]) for key in PUBLIC_SETTING_KEYS}
    cities = sorted(
        ({key: row[key] for key in PUBLIC_MARKET_FIELDS} for row in markets),
        key=lambda row: str(row["city"]),
    )
    # ReportLab's standard Chinese CID font also works on the Linux deployment;
    # unlike a macOS font path, it does not depend on a particular host's fonts.
    font = "STSong-Light"
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    ink = colors.HexColor("#252525")
    muted = colors.HexColor("#646464")
    border = colors.HexColor("#b6b6b6")
    pale = colors.HexColor("#ededee")
    stream = BytesIO()
    doc = BaseDocTemplate(
        stream, pagesize=A4, leftMargin=8 * mm, rightMargin=8 * mm,
        topMargin=23 * mm, bottomMargin=12 * mm,
        title="公开 KDS - Key Data Sheet", author="Business Simulation",
        subject="公开比赛参数与城市比较", invariant=1,
    )
    body = ParagraphStyle(
        "KDSBody", fontName=font, fontSize=8, leading=12,
        textColor=ink, wordWrap="CJK",
    )
    cell = ParagraphStyle("KDSCell", parent=body, fontSize=7, leading=9, alignment=TA_CENTER)
    header = ParagraphStyle("KDSHeader", parent=cell, fontSize=6.3, leading=8, textColor=muted)
    note = ParagraphStyle("KDSNote", parent=body, fontSize=7.7, leading=12, textColor=muted)
    bullet = ParagraphStyle(
        "KDSBullet", parent=body, leftIndent=11, spaceAfter=9,
        bulletFontName="Helvetica", bulletFontSize=8,
    )
    note_bullet = ParagraphStyle("KDSNoteBullet", parent=bullet, fontSize=7.7, textColor=muted)

    def markup(value: Any, latin_font: str = "Courier") -> str:
        # Use the reference's typewriter styling, with a Chinese fallback for
        # arbitrary administrator-supplied city names. Escape all input first.
        pieces = re.split(r"([\x20-\x7e\xa0-\xff]+)", str(value))
        text = "".join(
            f'<font name="{latin_font}">{escape(piece)}</font>' if all(ord(c) <= 255 for c in piece)
            else escape(piece)
            for piece in pieces if piece
        )
        return text

    def p(value: Any, style: ParagraphStyle = body, latin_font: str = "Courier") -> Paragraph:
        return Paragraph(markup(value, latin_font), style)

    def number(value: Any) -> str:
        return f"{float(value):,.6f}".rstrip("0").rstrip(".")

    def money(value: Any) -> str:
        return f"¥{number(value)}"

    class SectionHeading(Flowable):
        """The two short rules used by the printed reference KDS."""

        def __init__(self, text: str):
            super().__init__()
            self.text = text
            self.width = doc.width
            self.height = 25
            self.keepWithNext = True

        def draw(self) -> None:
            self.canv.setStrokeColor(muted)
            self.canv.setLineWidth(0.65)
            self.canv.line(0, 24, 120 * mm, 24)
            self.canv.setFillColor(ink)
            self.canv.setFont("Courier-Bold", 11)
            self.canv.drawString(0, 10, self.text)
            self.canv.line(0, 5, min(120 * mm, pdfmetrics.stringWidth(self.text, "Courier-Bold", 11) + 8), 5)

    def item(label: str, detail: str = "", *, italic: bool = False) -> Paragraph:
        # A real text bullet rather than a raster screenshot: clean at any zoom.
        text = markup(label, "Courier-Oblique" if italic else "Courier-Bold")
        if detail:
            text += markup(detail)
        return Paragraph(text, note_bullet if italic else bullet, bulletText="•")

    story = [SectionHeading("Markets Details")]
    if cities:
        top_headers = [
            "Markets", "Initial Max\nLoan", "Interest\nRate", "Initial Salary", "",
            "Material Unit Cost", "", "Storage Unit Cost", "", "Population",
            "Initial\nPenetration", "Initial\nAvg. Price",
        ]
        sub_headers = ["", "", "", "Worker", "Engineer", "Component", "Product", "Component", "Product", "", "", ""]
        data = [
            [Paragraph(markup(value).replace("\n", "<br/>"), header) for value in top_headers],
            [p(value, header) for value in sub_headers],
        ]
        for row in cities:
            values = [
                row["city"], money(row["max_loan"]), f"{float(row['interest_rate']) * 100:.2f}%",
                money(row["worker_initial_salary"]), money(row["engineer_initial_salary"]),
                money(row["component_material"]), money(row["product_material"]),
                money(row["component_storage"]), money(row["product_storage"]), number(row["population"]),
                f"{float(row['penetration']) * 100:.2f}%", money(row["initial_avg_price"]),
            ]
            data.append([p(value, cell) for value in values])
        table = Table(
            data, colWidths=[width * mm for width in [18, 23, 13, 15, 15, 13, 13, 13, 13, 20, 18, 20]],
            repeatRows=2, hAlign="LEFT", splitInRow=1,
        )
        table.setStyle(TableStyle([
            *[("SPAN", (column, 0), (column, 1)) for column in (0, 1, 2, 9, 10, 11)],
            *[("SPAN", (column, 0), (column + 1, 0)) for column in (3, 5, 7)],
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#fafafa")),
            ("LINEBELOW", (0, 1), (-1, 1), 0.7, muted),
            ("LINEBELOW", (0, 2), (-1, -1), 0.35, border),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, border),
            ("LEFTPADDING", (0, 0), (-1, -1), 1),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, 1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, 1), 1.5),
            ("TOPPADDING", (0, 2), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 2), (-1, -1), 4),
        ]))
        story.append(table)
    else:
        story.append(p("No markets configured. 当前尚未配置城市。", body))
    story.extend([
        Spacer(1, 5 * mm),
        item("Maximum loans, salaries, prices and penetrations may change from round to round.", italic=True),
        item("Market penetration: the proportion of customers interested in buying your products.", italic=True),
        Spacer(1, 5 * mm),
    ])
    rules = [
        (f"1 Component = {number(public['component_workers'])} Inexperienced Workers + {number(public['component_hours'])} Hours + 1 Component Material", ""),
        (f"1 Product = {number(public['product_engineers'])} Inexperienced Engineers + {number(public['product_hours'])} Hours + {number(public['components_per_product'])} Components + 1 Product Material", ""),
        ("Experienced workers and engineers produce 10% more components and products per unit time compared to inexperienced workers and engineers.", ""),
        ("Training Cost: ", f"{money(public['worker_training_cost'])} / Worker, {money(public['engineer_training_cost'])} / Engineer"),
        ("Product Quality Index = ", "Quality Investment ÷ (Old Products × 1.20 + New Products)"),
        ("Management Index = ", "Management Investment / (Workers + Engineers)"),
        ("Salary Range: ", f"{money(public['salary_min'])} - {money(public['salary_max'])}"),
        ("Product Price Range: ", f"{money(public['price_min'])} - {money(public['price_max'])}"),
        ("Transportation Fee: ", f"{money(public['transport_cost'])} / Product sold outside the home market"),
        ("Add One Sales Agent: ", money(public["agent_add_cost"])),
        ("Remove One Sales Agent: ", money(public["agent_remove_cost"])),
        ("Order One Market Report: ", money(public["report_cost"])),
        ("Research and Development: ", "Unsuccessful research investment carries over to the next round; accumulated investment resets after success."),
        ("Patent: ", f"Multiplies Component Material cost and Product Material cost by {number(public['patent_factor'])} for each patent, effective from the next round."),
    ]
    story.append(KeepTogether([
        SectionHeading("Equations & Ranges & Prices"),
        Spacer(1, 2 * mm),
        *[item(label, detail) for label, detail in rules],
    ]))

    def page_frame(canvas: Canvas, document: BaseDocTemplate) -> None:
        canvas.saveState()
        width, height = A4
        # Match the reference's gray masthead, without copying its historical
        # competition title or claiming the PDF is issued by the organizer.
        canvas.setFillColor(pale)
        canvas.rect(0, height - 18 * mm, width, 18 * mm, stroke=0, fill=1)
        canvas.setStrokeColor(muted)
        canvas.setLineWidth(0.6)
        canvas.line(0, height - 18 * mm, width, height - 18 * mm)
        canvas.setFillColor(ink)
        canvas.setFont("Helvetica-Bold", 22)
        canvas.drawString(8 * mm, height - 12 * mm, "ABS")
        canvas.setFont("Courier-Bold", 11)
        canvas.drawString(37 * mm, height - 8 * mm, "Business Simulation")
        canvas.setFont("Courier", 8.5)
        canvas.drawString(37 * mm, height - 14 * mm, "Key Data Sheet")
        canvas.drawRightString(width - 8 * mm, height - 14 * mm, f"Initial Cash: {money(public['initial_cash'])}")
        canvas.setFont("Courier", 6.5)
        canvas.setFillColor(muted)
        canvas.drawString(8 * mm, 6 * mm, "PUBLIC KDS | Key Data Sheet")
        canvas.drawRightString(width - 8 * mm, 6 * mm, f"Page {document.page}")
        canvas.restoreState()

    # Identical saved public settings produce the same file from either role.
    doc.addPageTemplates(PageTemplate(
        id="public-kds",
        frames=[Frame(
            doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        )],
        onPage=page_frame,
    ))
    doc.build(story)
    return stream.getvalue()
