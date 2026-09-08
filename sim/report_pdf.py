from __future__ import annotations

from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape


CITY_NAMES = {
    "广州": "Guangzhou", "深圳": "Shenzhen", "成都": "Chengdu", "苏州": "Suzhou", "武汉": "Wuhan",
    "大连": "Dalian", "杭州": "Hangzhou", "无锡": "Wuxi", "南京": "Nanjing", "宁波": "Ningbo", "上海": "Shanghai",
}


def _safe_text(value: Any) -> str:
    text = str(value)
    for chinese, english in CITY_NAMES.items():
        text = text.replace(chinese, english)
    return text.encode("latin-1", "replace").decode("latin-1")


def _money(value: Any) -> str:
    return f"RMB {float(value or 0):,.0f}"


def _num(value: Any) -> str:
    return f"{float(value or 0):,.0f}"


def _pct_points(value: Any) -> str:
    return f"{float(value or 0):.2f}%"


def build_round_report_pdf(
    company: dict[str, Any],
    round_no: int,
    report: dict[str, Any],
    rank: int | str,
    market_sections: list[dict[str, Any]],
) -> bytes:
    """Create a compact official-style round report PDF."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_name = "Helvetica"

    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=13 * mm,
        bottomMargin=14 * mm,
        title=f"Round {round_no} Report - {company.get('code', '')}",
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("BodyCN", parent=styles["BodyText"], fontName=font_name, fontSize=7.2, leading=9.2, textColor=colors.HexColor("#333333"))
    small = ParagraphStyle("SmallCN", parent=normal, fontSize=6.2, leading=7.8, textColor=colors.HexColor("#666666"))
    title = ParagraphStyle("TitleCN", parent=styles["Title"], fontName=font_name, fontSize=13, leading=17, alignment=TA_CENTER, textColor=colors.HexColor("#1d5f2a"))
    section_style = ParagraphStyle("SectionCN", parent=styles["Heading2"], fontName=font_name, fontSize=9.5, leading=12, alignment=TA_LEFT, spaceBefore=7, spaceAfter=4, textColor=colors.HexColor("#222222"))

    def paragraph(value: Any, style: ParagraphStyle = normal) -> Paragraph:
        return Paragraph(escape(_safe_text(value)), style)

    def section(value: str) -> Paragraph:
        return Paragraph(escape(_safe_text(value)), section_style)

    def table(data: list[list[Any]], widths: list[float] | None = None, font_size: float = 6.6) -> Table:
        cooked = [[cell if isinstance(cell, Paragraph) else paragraph(cell, small) for cell in row] for row in data]
        result = Table(cooked, colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font_name),
                    ("FONTSIZE", (0, 0), (-1, -1), font_size),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf2ec")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#253127")),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor("#606860")),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#c8ccc8")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                ]
            )
        )
        return result

    metrics = report["key_metrics"]
    finance = report["finance"]
    hr = report["human_resources"]
    production = report["production"]
    research = report["research"]
    story: list[Any] = [
        Paragraph("ASEEDER Business Simulation", title),
        Paragraph(
            f"Team {escape(_safe_text(company.get('code', '')))} &nbsp;&nbsp; | &nbsp;&nbsp; Round {round_no} Report",
            ParagraphStyle("Subtitle", parent=normal, alignment=TA_CENTER, fontSize=8.5, leading=11),
        ),
        Spacer(1, 4),
        section("Key Metrics"),
        table(
            [
                ["Total Assets", "Debt", "Net Assets", "Rank"],
                [_money(metrics["total_assets"]), _money(metrics["debt"]), _money(metrics["net_assets"]), rank],
                ["Sales Revenue", "Total Cost", "Net Profit", "Home Market"],
                [_money(metrics["sales_revenue"]), _money(metrics["cost"]), _money(metrics["net_profit"]), company.get("home_city", "-")],
            ],
            [44 * mm, 44 * mm, 44 * mm, 45 * mm],
        ),
        paragraph("Net Profit = Sales Revenue - All Costs. Net Assets = Cash - Debt.", small),
        section("Finance"),
    ]
    finance_rows = [
        ["Items", "Cash Flow", "Debt Change"],
        ["Round begins", _money(finance.get("round_begins")), _money(finance.get("starting_debt", 0))],
        ["Bank loan", _money(finance.get("loan_change")), _money(finance.get("loan_change"))],
        ["Workers salary", _money(-float(finance.get("worker_wages", finance.get("wages", 0)))), "-"],
        ["Engineers salary", _money(-float(finance.get("engineer_wages", 0))), "-"],
        ["Layoff / training", _money(-float(finance.get("layoff", 0)) - float(finance.get("training", 0))), "-"],
        ["Components material", _money(-float(finance.get("component_material", 0))), "-"],
        ["Components storage", _money(-float(finance.get("component_storage", 0))), "-"],
        ["Products material", _money(-float(finance.get("product_material", 0))), "-"],
        ["Products storage", _money(-float(finance.get("product_storage", 0))), "-"],
        ["Change sales agents", _money(-float(finance.get("agents", 0))), "-"],
        ["Marketing investment", _money(-float(finance.get("marketing", 0))), "-"],
        ["Quality investment", _money(-float(finance.get("quality", 0))), "-"],
        ["Management investment", _money(-float(finance.get("management", 0))), "-"],
        ["Sales revenue", _money(metrics.get("sales_revenue", 0)), "-"],
        ["Research investment", _money(-float(finance.get("research", 0))), "-"],
        ["Market report", _money(-float(finance.get("market_reports", 0))), "-"],
        ["Debt interest", "-", _money(finance.get("interest", 0))],
        ["Tax", _money(-float(finance.get("tax", 0))), "-"],
        ["Round ends", _money(finance.get("round_ends", 0)), _money(metrics.get("debt", 0))],
    ]
    story.extend(
        [
            table(finance_rows, [88 * mm, 44 * mm, 45 * mm]),
            section("Human Resources"),
            table(
                [
                    ["Employees", "Previous", "Change", "Working", "Salary", "Average", "Multiplier"],
                    ["Workers", hr.get("previous_workers", 0), hr.get("worker_delta", 0), hr.get("workers", 0), _money(hr.get("worker_salary", 0)), _money(hr.get("average_worker_salary", 0)), f"{float(hr.get('worker_wage_multiplier', 0)):.2f}"],
                    ["Engineers", hr.get("previous_engineers", 0), hr.get("engineer_delta", 0), hr.get("engineers", 0), _money(hr.get("engineer_salary", 0)), _money(hr.get("average_engineer_salary", 0)), f"{float(hr.get('engineer_wage_multiplier', 0)):.2f}"],
                ],
                [35 * mm, 22 * mm, 20 * mm, 22 * mm, 26 * mm, 26 * mm, 26 * mm],
            ),
            section("Production"),
            table(
                [
                    ["Item", "Plan", "Previous", "Produced", "Total", "Used / Sold", "Surplus"],
                    ["Components", int(production.get("planned", 0)) * 7, 0, production.get("components", 0), production.get("components", 0), production.get("components", 0), 0],
                    ["Products", production.get("planned", 0), production.get("old_products", 0), production.get("produced", 0), int(production.get("old_products", 0)) + int(production.get("produced", 0)), production.get("sold", 0), production.get("surplus", 0)],
                ],
                [35 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm, 24 * mm],
            ),
            section("Research Investment"),
            table(
                [
                    ["Investment", "Probability", "Result", "Active Patents", "Effective From"],
                    [_money(research.get("investment", 0)), f"{float(research.get('probability', 0)) * 100:.1f}%", "Successful" if research.get("success") else "Not successful", research.get("patents_after", 0), research.get("effective_from_round") or "-"],
                ],
                [38 * mm, 32 * mm, 38 * mm, 32 * mm, 37 * mm],
            ),
            section("Sales"),
        ]
    )
    sales_rows = [["Market", "Agents", "CPI", "Sales Volume", "Market Share", "Price", "Sales"]]
    for item in report.get("sales", []):
        sales_rows.append([item.get("city", ""), item.get("agents", 0), _pct_points(item.get("cpi", 0)), _num(item.get("sold", 0)), _pct_points(float(item.get("market_share", 0)) * 100), _money(item.get("price", 0)), _money(float(item.get("sold", 0)) * float(item.get("price", 0)))])
    story.append(table(sales_rows, [26 * mm, 19 * mm, 21 * mm, 27 * mm, 28 * mm, 27 * mm, 29 * mm]))

    for market in market_sections:
        story.extend(
            [
                PageBreak(),
                section(f"Market Report - {market['city']}"),
                table(
                    [
                        ["Population", "Penetration", "Market Size", "Total Sales Volume", "Average Price"],
                        [_num(market.get("population", 0)), f"{float(market.get('penetration', 0)) * 100:.2f}%", _num(market.get("market_size", 0)), _num(market.get("total_volume", 0)), _money(market.get("average_price", 0))],
                    ],
                    [35 * mm, 35 * mm, 35 * mm, 36 * mm, 36 * mm],
                ),
                Spacer(1, 5),
            ]
        )
        rows = [["Team", "Management", "Agents", "Marketing", "Quality", "CPI", "Price", "Volume", "Share"]]
        for item in market.get("rows", []):
            rows.append([item.get("code", ""), f"{float(item.get('ma_index', 0)):,.2f}", item.get("agents", 0), _money(item.get("marketing", 0)), f"{float(item.get('qi_index', 0)):,.2f}", _pct_points(item.get("cpi", 0)), _money(item.get("price", 0)), _num(item.get("sold", 0)), _pct_points(float(item.get("market_share", 0)) * 100)])
        story.append(table(rows, [18 * mm, 25 * mm, 16 * mm, 25 * mm, 22 * mm, 18 * mm, 21 * mm, 17 * mm, 18 * mm], font_size=5.8))
        story.append(paragraph("Average Price = [sum(player price x volume) + base average x (market size - player total volume)] / market size", small))

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 6.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(14 * mm, 8 * mm, f"{company.get('code', '')} | Round {round_no}")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
