from __future__ import annotations

from io import BytesIO
from typing import Any, Callable
from xml.sax.saxutils import escape


CITY_NAMES = {
    "广州": "Guangzhou",
    "深圳": "Shenzhen",
    "成都": "Chengdu",
    "苏州": "Suzhou",
    "武汉": "Wuhan",
    "大连": "Dalian",
    "杭州": "Hangzhou",
    "无锡": "Wuxi",
    "南京": "Nanjing",
    "宁波": "Ningbo",
    "上海": "Shanghai",
}


def _safe_text(value: Any) -> str:
    text = str(value)
    for chinese, english in CITY_NAMES.items():
        text = text.replace(chinese, english)
    return text.encode("latin-1", "replace").decode("latin-1")


def _money(value: Any) -> str:
    return f"¥{float(value or 0):,.0f}"


def _flow(value: Any) -> str:
    amount = float(value or 0)
    if abs(amount) < 0.005:
        return "--"
    return f"{'+' if amount > 0 else '-'} {_money(abs(amount))}"


def _num(value: Any, decimals: int = 0) -> str:
    return f"{float(value or 0):,.{decimals}f}"


def _pct_points(value: Any) -> str:
    return f"{float(value or 0):.2f}%"


def build_round_report_pdf(
    company: dict[str, Any],
    round_no: int,
    report: dict[str, Any],
    rank: int | str,
    market_sections: list[dict[str, Any]],
) -> bytes:
    """Build a continuous official-style report from public result fields."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = "Courier"
    bold = "Courier-Bold"
    italic = "Courier-Oblique"
    bold_italic = "Courier-BoldOblique"
    chinese_font = "STSong-Light"
    if chinese_font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(chinese_font))

    ink = colors.HexColor("#292929")
    muted = colors.HexColor("#666666")
    line = colors.HexColor("#909090")
    hairline = colors.HexColor("#c1c1c1")
    watermark = colors.HexColor("#f1f1f1")
    page_width = A4[0]
    margin = 7.5 * mm
    content_width = page_width - 2 * margin

    body = ParagraphStyle(
        "OfficialBody", fontName=bold, fontSize=8.0, leading=10.1,
        textColor=ink, allowWidows=1, allowOrphans=1,
    )
    table_body = ParagraphStyle(
        "OfficialTable", parent=body, fontName=bold, fontSize=7.0, leading=8.3,
    )
    table_head = ParagraphStyle(
        "OfficialTableHead", parent=table_body, fontName=bold,
        textColor=muted,
    )
    note = ParagraphStyle(
        "OfficialNote", parent=body, fontName=bold_italic, fontSize=6.7,
        leading=10.0, leftIndent=8, firstLineIndent=-8, spaceAfter=8.0,
    )
    header_center = ParagraphStyle(
        "OfficialHeaderCenter", parent=body, fontName=bold, fontSize=7.8, leading=9.8,
        alignment=TA_LEFT,
    )
    header_right = ParagraphStyle(
        "OfficialHeaderRight", parent=body, fontName=bold, fontSize=7.4, leading=9.3,
        alignment=TA_RIGHT,
    )
    section_style = ParagraphStyle(
        "OfficialSection", parent=body, fontName=bold, fontSize=10.0,
        leading=11.8,
    )

    def p(value: Any, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(escape(_safe_text(value)), style)

    def rich(value: str, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(_safe_text(value), style)

    class Brand(Flowable):
        def __init__(self) -> None:
            super().__init__()
            self.width = 41 * mm
            self.height = 11 * mm

        def draw(self) -> None:
            import math

            center_x, center_y = 3.5 * mm, 5.8 * mm
            rays = (
                (colors.HexColor("#ef5350"), 0, 3.1),
                (colors.HexColor("#f6a623"), 45, 2.7),
                (colors.HexColor("#45b9a8"), 90, 3.1),
                (colors.HexColor("#62a6d8"), 135, 2.7),
            )
            self.canv.setLineWidth(1.15)
            for colour, angle, length in rays:
                radians = math.radians(angle)
                dx, dy = length * mm * math.cos(radians), length * mm * math.sin(radians)
                self.canv.setStrokeColor(colour)
                self.canv.line(center_x - dx, center_y - dy, center_x + dx, center_y + dy)
            self.canv.setFillColor(muted)
            self.canv.setFont(chinese_font, 6.4)
            self.canv.drawString(8 * mm, 4.6 * mm, "阿思丹")
            self.canv.setFillColor(ink)
            self.canv.setFont("Helvetica-Bold", 8.4)
            self.canv.drawString(18.5 * mm, 4.6 * mm, "ASEEDER")

    def section(title: str) -> Table:
        result = Table([[p(title, section_style)]], colWidths=[112 * mm], hAlign="LEFT")
        result.keepWithNext = True
        result.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (-1, 0), 0.65, line),
            ("LINEBELOW", (0, 0), (-1, 0), 0.65, line),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 2.3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8),
        ]))
        return result

    def make_table(
        data: list[list[Any]], widths_mm: list[float], *, header_rows: int = 1,
        alignments: dict[int, str] | None = None, font_size: float = 7.0,
        row_padding: float = 5.7, extra_style: list[tuple[Any, ...]] | None = None,
    ) -> Table:
        cooked: list[list[Any]] = []
        paragraph_alignments = {
            "LEFT": TA_LEFT,
            "CENTER": TA_CENTER,
            "RIGHT": TA_RIGHT,
        }
        column_alignments = alignments or {}
        for row_index, row in enumerate(data):
            cooked_row: list[Any] = []
            for column_index, cell in enumerate(row):
                if isinstance(cell, Flowable):
                    cooked_row.append(cell)
                    continue
                alignment_name = column_alignments.get(column_index, "LEFT")
                parent_style = table_head if row_index < header_rows else table_body
                cell_style = ParagraphStyle(
                    f"DynamicCell-{row_index}-{column_index}",
                    parent=parent_style,
                    fontName=bold,
                    fontSize=font_size,
                    leading=font_size + 1.3,
                    alignment=paragraph_alignments[alignment_name],
                )
                cooked_row.append(p(cell, cell_style))
            cooked.append(cooked_row)
        result = Table(
            cooked, colWidths=[width * mm for width in widths_mm], repeatRows=header_rows,
            hAlign="LEFT", splitByRow=1, splitInRow=1,
        )
        commands: list[tuple[Any, ...]] = [
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TEXTCOLOR", (0, 0), (-1, -1), ink),
            ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.45, line),
            ("LINEBELOW", (0, header_rows), (-1, -1), 0.22, hairline),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
            ("TOPPADDING", (0, 0), (-1, -1), row_padding),
            ("BOTTOMPADDING", (0, 0), (-1, -1), row_padding),
        ]
        if alignments:
            for column, alignment in alignments.items():
                commands.append(("ALIGN", (column, 0), (column, -1), alignment))
        if extra_style:
            commands.extend(extra_style)
        result.setStyle(TableStyle(commands))
        return result

    def notes(lines: list[str]) -> list[Paragraph]:
        return [Paragraph(f"• {escape(_safe_text(item))}", note) for item in lines]

    metrics = report.get("key_metrics", {})
    finance = report.get("finance", {})
    hr = report.get("human_resources", {})
    production = report.get("production", {})
    research = report.get("research", {})
    sales = report.get("sales", [])
    code = _safe_text(company.get("code", "-"))

    header = Table([
        [Brand(), rich(
            "<b>ASIA BUSINESS SIMULATION</b><br/>"
            f"Round {escape(str(round_no))} Report", header_center,
        ), rich(
            f"Team Number:&nbsp;&nbsp; <font size=10><b>{escape(code)}</b></font>",
            header_right,
        )],
    ], colWidths=[42 * mm, 109 * mm, 44 * mm], hAlign="LEFT")
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.65, line),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
    ]))

    key_metrics = make_table([
        ["Total Assets", "", "Debt", "", "Net Assets", "", "Rank"],
        [_money(metrics.get("total_assets")), "-", _money(metrics.get("debt")), "=", _money(metrics.get("net_assets")), "", rank],
        ["Sales Revenue", "", "Cost", "", "Net Profit", "", ""],
        [_money(metrics.get("sales_revenue")), "-", _money(metrics.get("cost")), "=", _money(metrics.get("net_profit")), "", ""],
    ], [46, 6, 43, 6, 46, 6, 42], alignments={0: "CENTER", 1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER", 5: "CENTER", 6: "CENTER"},
       extra_style=[
           ("LINEABOVE", (0, 2), (-1, 2), 0.32, hairline),
           ("TEXTCOLOR", (0, 0), (-1, 0), muted),
           ("TEXTCOLOR", (0, 2), (-1, 2), muted),
       ])

    project_bonus = float(finance.get("project_bonus", 0))
    stored_start_cash = float(finance.get("round_begins", 0))
    bonus_in_start = bool(finance.get("bonus_in_round_begins", False))
    start_cash = stored_start_cash - project_bonus if bonus_in_start else stored_start_cash
    start_debt = float(finance.get(
        "starting_debt",
        float(metrics.get("debt", 0)) - float(finance.get("loan_change", 0)) - float(finance.get("interest", 0)),
    ))
    running_cash = start_cash
    running_debt = start_debt
    finance_rows: list[list[Any]] = [["Items", "Cash Flow", "Cash", "Debt Change", "Debt"]]
    finance_rows.append(["Round begins", "--", _money(running_cash), "--", _money(running_debt)])
    finance_events = [
        ("Bank loan", float(finance.get("loan_change", 0)), float(finance.get("loan_change", 0))),
        ("Workers salary cost", -float(finance.get("worker_wages", finance.get("wages", 0))), 0.0),
        ("Engineers salary cost", -float(finance.get("engineer_wages", 0)), 0.0),
        ("Layoff compensation", -float(finance.get("layoff_cash", finance.get("layoff", 0))), float(finance.get("layoff_debt", 0))),
        ("Quit compensation", -float(finance.get("quit_penalty_cash", finance.get("quit_penalty", 0))), float(finance.get("quit_penalty_debt", 0))),
        ("Employee training cost", -float(finance.get("training", 0)), 0.0),
        ("Components material cost", -float(finance.get("component_material", finance.get("materials", 0))), 0.0),
        ("Components storage cost", -float(finance.get("component_storage", finance.get("storage", 0))), 0.0),
        ("Products material cost", -float(finance.get("product_material", 0)), 0.0),
        ("Products storage cost", -float(finance.get("product_storage", 0)), 0.0),
        ("Change sales agents cost", -float(finance.get("agents", 0)), 0.0),
        ("Marketing investment", -float(finance.get("marketing", 0)), 0.0),
        ("Quality investment", -float(finance.get("quality", 0)), 0.0),
        ("Management investment", -float(finance.get("management", 0)), 0.0),
        ("Sales revenue", float(metrics.get("sales_revenue", finance.get("sales_revenue", 0))), 0.0),
        ("Market report cost", -float(finance.get("market_reports", 0)), 0.0),
        ("Research investment", -float(finance.get("research", 0)), 0.0),
        ("Transportation cost", -float(finance.get("transport", 0)), 0.0),
        ("Debt interest", 0.0, float(finance.get("interest", 0))),
        ("Tax deduction", -float(finance.get("tax", 0)), 0.0),
        ("Project bonus", project_bonus, 0.0),
    ]
    for label, cash_change, debt_change in finance_events:
        running_cash += cash_change
        running_debt += debt_change
        finance_rows.append([label, _flow(cash_change), _money(running_cash), _flow(debt_change), _money(running_debt)])
    finance_rows.append([
        "Round ends", "--", _money(finance.get("round_ends", running_cash)), "--", _money(metrics.get("debt", running_debt)),
    ])

    hr_rows = hr.get("rows") or [
        {"employee": "Inexperienced Workers", "previous": hr.get("previous_workers", 0), "laid": max(0, -int(hr.get("worker_delta", 0))), "quitted": 0, "added": max(0, int(hr.get("worker_delta", 0))), "promoted": 0, "working": hr.get("workers", 0), "salary": hr.get("worker_salary", 0), "average": hr.get("average_worker_salary", 0)},
        {"employee": "Experienced Workers", "previous": 0, "laid": 0, "quitted": 0, "added": 0, "promoted": 0, "working": 0, "salary": hr.get("worker_salary", 0), "average": hr.get("average_worker_salary", 0)},
        {"employee": "Inexperienced Engineers", "previous": hr.get("previous_engineers", 0), "laid": max(0, -int(hr.get("engineer_delta", 0))), "quitted": 0, "added": max(0, int(hr.get("engineer_delta", 0))), "promoted": 0, "working": hr.get("engineers", 0), "salary": hr.get("engineer_salary", 0), "average": hr.get("average_engineer_salary", 0)},
        {"employee": "Experienced Engineers", "previous": 0, "laid": 0, "quitted": 0, "added": 0, "promoted": 0, "working": 0, "salary": hr.get("engineer_salary", 0), "average": hr.get("average_engineer_salary", 0)},
    ]
    human_rows: list[list[Any]] = [["Employees", "Previous", "Laid", "Quitted", "Added", "Promoted", "Working", "Salary", "Avg."]]
    for row in hr_rows:
        human_rows.append([
            row.get("employee", ""), row.get("previous", 0), row.get("laid", 0), row.get("quitted", 0),
            row.get("added", 0), row.get("promoted", 0), row.get("working", 0),
            _money(row.get("salary", 0)), _money(row.get("average", 0)),
        ])

    planned = int(production.get("planned", 0))
    produced = int(production.get("produced", 0))
    components_per_product = int(production.get("components_per_product", 7))
    components = int(production.get("components", produced * components_per_product))
    old_products = int(production.get("old_products", 0))
    old_components = int(production.get("old_components", 0))
    sold = int(production.get("sold", 0))
    surplus = int(production.get("surplus", max(0, old_products + produced - sold)))
    component_material_price = production.get(
        "component_material_unit_price", float(finance.get("component_material", 0)) / max(components, 1),
    )
    product_material_price = production.get(
        "product_material_unit_price", float(finance.get("product_material", 0)) / max(produced, 1),
    )
    component_storage_price = production.get(
        "component_storage_unit_price", float(finance.get("component_storage", 0)) / max(int(production.get("component_storage_increase", 0)), 1),
    )
    product_storage_price = production.get(
        "product_storage_unit_price", float(finance.get("product_storage", 0)) / max(int(production.get("product_storage_increase", 0)), 1),
    )

    management_table = make_table([
        ["Management", "Management Investment", "Management Index"],
        ["", _money(finance.get("management", 0)), _num(production.get("ma_index", 0), 2)],
    ], [55, 70, 70], alignments={0: "CENTER", 1: "CENTER", 2: "CENTER"})
    overview_table = make_table([
        ["Overview", "Plan", "Previous", "Produced", "Total", "Used/Sold", "Surplus"],
        ["Components", planned * components_per_product, old_components, components,
         old_components + components, production.get("component_used", components), production.get("component_surplus", 0)],
        ["Products", planned, old_products, produced, old_products + produced, sold, surplus],
    ], [35, 26, 26, 27, 27, 27, 27], alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 6: "RIGHT"})
    details_table = make_table([
        ["Details", "Productivity", "Employees", "Production", "Material Price", "Material Cost"],
        ["Components", _num(production.get("component_productivity", 0), 3), _num(hr.get("workers", 0)), components, _money(component_material_price), _money(finance.get("component_material", 0))],
        ["Products", _num(production.get("product_productivity", 0), 3), _num(hr.get("engineers", 0)), produced, _money(product_material_price), _money(finance.get("product_material", 0))],
    ], [35, 31, 28, 31, 33, 37], alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"})
    storage_table = make_table([
        ["Storage", "Capacity Before", "Capacity After", "Increment", "Unit Price", "Storage Cost"],
        ["Components", production.get("component_storage_before", 0), production.get("component_storage_after", 0), production.get("component_storage_increase", 0), _money(component_storage_price), _money(finance.get("component_storage", 0))],
        ["Products", production.get("product_storage_before", 0), production.get("product_storage_after", 0), production.get("product_storage_increase", 0), _money(product_storage_price), _money(finance.get("product_storage", 0))],
    ], [35, 32, 34, 27, 30, 37], alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"})
    quality_table = make_table([
        ["Quality", "Quality Investment", "Old Products", "New Products", "Product Quality Index"],
        ["", _money(production.get("quality_investment", finance.get("quality", 0))), old_products, produced, _num(production.get("qi_index", 0), 2)],
    ], [35, 43, 35, 36, 46], alignments={1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER"})
    research_table = make_table([
        ["Overview", "Previous", "Change", "After", "Accumulated Research Investment"],
        ["Patents", research.get("active_patents_this_round", 0), 1 if research.get("success") else 0, research.get("patents_after", 0), _money(research.get("accumulated_after", 0))],
    ], [35, 30, 28, 30, 72], alignments={1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER"})

    agent_rows: list[list[Any]] = [["Agents", "Previous", "Change", "After", "Change Cost", "Marketing Investment"]]
    sales_rows: list[list[Any]] = [["Market", "Competitive Power", "Sales Volume", "Market Share", "Price", "Sales"]]
    active_sales = [
        row for row in sales
        if int(row.get("agents", 0)) > 0
        or int(row.get("agents_previous", 0)) > 0
        or int(row.get("agent_change", 0)) != 0
        or float(row.get("marketing", 0)) > 0
        or int(row.get("sold", 0)) > 0
    ]
    for row in active_sales:
        agent_rows.append([
            row.get("city", ""), row.get("agents_previous", max(0, int(row.get("agents", 0)) - int(row.get("agent_change", 0)))),
            row.get("agent_change", 0), row.get("agents", 0), _money(row.get("agent_change_cost", 0)), _money(row.get("marketing", 0)),
        ])
        gross_sales = float(row.get("sold", 0)) * float(row.get("price", 0))
        sales_rows.append([
            row.get("city", ""), _pct_points(row.get("cpi", 0)), _num(row.get("sold", 0)),
            _pct_points(float(row.get("market_share", 0)) * 100), _money(row.get("price", 0)), _money(gross_sales),
        ])

    story: list[Flowable] = [
        header, Spacer(1, 3.2 * mm), section("Key Metrics"), key_metrics, Spacer(1, 2 * mm),
        *notes([
            "Net Profit = Sales Revenue - All Costs. The direct indicator of your achievement in this round.",
            "Net Assets = Total Assets - Debt. Your result till this round, used for ranking.",
        ]),
        Spacer(1, 3 * mm), section("Finance"),
        make_table(finance_rows, [54, 34, 37, 32, 38], font_size=6.6,
                   alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT"}, row_padding=4.7),
        Spacer(1, 3.5 * mm), section("Human Resources"),
        make_table(human_rows, [39, 19, 16, 18, 16, 19, 18, 24, 26], font_size=6.15,
                   alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 6: "RIGHT", 7: "RIGHT", 8: "RIGHT"}, row_padding=4.4),
        Spacer(1, 1.8 * mm),
        *notes([
            "Low-salary Effect: If your salary is relatively low, you cannot add as many employees as you planned to, and some employees may quit.",
            "Layoff Cost: When you lay off your employees, you must compensate them for one month's salary.",
            "Salary-reduction Penalty: When employees quit while you have reduction in salary, you must compensate them for two months' salary.",
            "Worker Promotion: eligible workers are ready to be promoted in the next round.",
            "Engineer Promotion: eligible engineers are ready to be promoted in the next round.",
            "Compensations are based on the salary of the current round.",
        ]),
        Spacer(1, 2 * mm), management_table, Spacer(1, 3.5 * mm), section("Production"), overview_table,
        Spacer(1, 1.8 * mm), details_table, Spacer(1, 1.4 * mm),
        *notes([
            "Productivity: This shows how many items one employee can produce in a round, affected by salary.",
            "Production: The actual total produced items. It is often limited by your plan, components, employees and available cash.",
        ]),
        Spacer(1, 1.8 * mm), storage_table, Spacer(1, 1.4 * mm),
        *notes(["Storage Cost: You only need to spend money on increasing your storage capacity."]),
        Spacer(1, 1.8 * mm), quality_table, Spacer(1, 1.4 * mm),
        *notes(["Product Quality Index = Quality Investment / (Old Products x 1.20 + New Products)."]),
        Spacer(1, 3.2 * mm), section("Research Investment"), research_table, Spacer(1, 1.4 * mm),
        *notes([
            "Accumulated Research Investment: If your research is not successful, your research investment is accumulated to the next round. If successful, you receive a patent and the accumulated investment is reset to 0.",
        ]),
        Spacer(1, 3.2 * mm), section("Sales"),
        make_table(agent_rows, [35, 29, 27, 27, 35, 42], alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"}),
        Spacer(1, 1.8 * mm),
        make_table(sales_rows, [35, 38, 29, 33, 28, 32], alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"}),
    ]

    for market in market_sections:
        summary_table = make_table([
            ["Population", "Penetration", "Market Size", "Total Sales Volume", "Avg. Price"],
            [_num(market.get("population", 0)), f"{float(market.get('penetration', 0)) * 100:.2f}%",
             _num(market.get("market_size", 0)), _num(market.get("total_volume", 0)), _money(market.get("average_price", 0))],
        ], [40, 35, 40, 43, 37], alignments={0: "CENTER", 1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER"})
        market_rows: list[list[Any]] = [[
            "Team", "Management Index", "Agents", "Marketing Investment",
            "Product Quality Index", "Price", "Sales Volume", "Market Share",
        ]]
        for row in market.get("rows", []):
            market_rows.append([
                row.get("code", ""), _num(row.get("ma_index", 0), 2), row.get("agents", 0),
                _money(row.get("marketing", 0)), _num(row.get("qi_index", 0), 2),
                _money(row.get("price", 0)), _num(row.get("sold", 0)),
                _pct_points(float(row.get("market_share", 0)) * 100),
            ])
        story.extend([
            Spacer(1, 3.4 * mm), section(f"Market Report - {market.get('city', '')}"), summary_table,
            Spacer(1, 1.8 * mm),
            make_table(market_rows, [16, 33, 18, 33, 34, 24, 21, 16], font_size=6.15,
                       alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 6: "RIGHT", 7: "RIGHT"}, row_padding=5.0),
        ])

    def measured_height(flowables: list[Flowable]) -> float:
        height = 0.0
        for flowable in flowables:
            before: Callable[[], float] | None = getattr(flowable, "getSpaceBefore", None)
            after: Callable[[], float] | None = getattr(flowable, "getSpaceAfter", None)
            if before:
                height += float(before())
            _, item_height = flowable.wrap(content_width, 50_000)
            height += item_height
            if after:
                height += float(after())
        return height

    content_height = measured_height(story)
    page_height = max(A4[1], min(13_900.0, content_height + 45 * mm))
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream, pagesize=(page_width, page_height), leftMargin=margin, rightMargin=margin,
        topMargin=7 * mm, bottomMargin=7 * mm,
        title=f"Round {round_no} Report - {code}", author="ABS Business Simulation",
        subject="Official-style round report", invariant=1,
    )

    def page_decorations(canvas: Any, document: Any) -> None:
        width, height = canvas._pagesize
        canvas.saveState()
        canvas.setFillColor(watermark)
        y = 115 * mm
        while y < height - 40 * mm:
            canvas.saveState()
            canvas.translate(width / 2, y)
            canvas.rotate(37)
            canvas.setFont(chinese_font, 18)
            canvas.drawRightString(-5 * mm, 0, "阿思丹")
            canvas.setFont("Helvetica-Bold", 31)
            canvas.drawString(1 * mm, 0, "ASEEDER")
            canvas.restoreState()
            y += 220 * mm
        canvas.restoreState()

    doc.build(story, onFirstPage=page_decorations, onLaterPages=page_decorations)
    return stream.getvalue()
