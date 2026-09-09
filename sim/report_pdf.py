from __future__ import annotations

from io import BytesIO
from typing import Any
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
    return f"RMB {float(value or 0):,.0f}"


def _flow(value: Any) -> str:
    amount = float(value or 0)
    if abs(amount) < 0.005:
        return "--"
    return f"{'+' if amount > 0 else '-'} RMB {abs(amount):,.0f}"


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
    """Build a clear, complete official-style competition round report."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_name = "Helvetica"
    font_bold = "Helvetica-Bold"
    ink = colors.HexColor("#282c2f")
    muted = colors.HexColor("#62686d")
    line = colors.HexColor("#8f9598")
    pale = colors.HexColor("#f3f5f4")
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=13 * mm,
        leftMargin=13 * mm,
        topMargin=16 * mm,
        bottomMargin=14 * mm,
        title=f"Round {round_no} Report - {company.get('code', '')}",
        author="ASEEDER Business Simulation",
        subject="Official Round Report",
    )
    base_styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ReportBody", parent=base_styles["BodyText"], fontName=font_name,
        fontSize=7.2, leading=9.1, textColor=ink,
    )
    small = ParagraphStyle(
        "ReportSmall", parent=body, fontSize=6.25, leading=7.7, textColor=muted,
    )
    tiny = ParagraphStyle("ReportTiny", parent=small, fontSize=5.55, leading=6.7)
    label = ParagraphStyle("ReportLabel", parent=small, fontName=font_bold, textColor=ink)
    section_style = ParagraphStyle(
        "ReportSection", parent=base_styles["Heading2"], fontName=font_bold,
        fontSize=9.2, leading=11, alignment=TA_LEFT, spaceBefore=6,
        spaceAfter=3, textColor=ink,
    )

    def paragraph(value: Any, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(escape(_safe_text(value)), style)

    def rich(value: str, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(_safe_text(value), style)

    def section(value: str) -> Table:
        result = Table([[Paragraph(escape(_safe_text(value)), section_style)]], colWidths=[184 * mm], hAlign="LEFT")
        result.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (0, 0), 0.9, ink),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 0),
            ("TOPPADDING", (0, 0), (0, 0), 0),
            ("BOTTOMPADDING", (0, 0), (0, 0), 1.5),
        ]))
        return result

    def table(
        data: list[list[Any]], widths: list[float], *, font_size: float = 6.35,
        header_rows: int = 1, alignments: dict[int, str] | None = None,
    ) -> Table:
        cooked: list[list[Any]] = []
        for row_index, row in enumerate(data):
            cooked.append([
                cell if isinstance(cell, Paragraph) else paragraph(cell, label if row_index < header_rows else small)
                for cell in row
            ])
        result = Table(cooked, colWidths=widths, repeatRows=header_rows, hAlign="LEFT")
        commands: list[tuple[Any, ...]] = [
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), font_size),
            ("BACKGROUND", (0, 0), (-1, header_rows - 1), pale),
            ("TEXTCOLOR", (0, 0), (-1, -1), ink),
            ("LINEABOVE", (0, 0), (-1, 0), 0.55, line),
            ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.55, line),
            ("LINEBELOW", (0, header_rows), (-1, -1), 0.22, colors.HexColor("#cfd3d1")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.15),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.15),
        ]
        if alignments:
            for column, alignment in alignments.items():
                commands.append(("ALIGN", (column, 0), (column, -1), alignment))
        result.setStyle(TableStyle(commands))
        return result

    def notes(lines: list[str]) -> list[Paragraph]:
        return [rich(f"&#8226; {escape(_safe_text(item))}", tiny) for item in lines]

    metrics = report.get("key_metrics", {})
    finance = report.get("finance", {})
    hr = report.get("human_resources", {})
    production = report.get("production", {})
    research = report.get("research", {})
    sales = report.get("sales", [])

    code = _safe_text(company.get("code", "-"))
    header_left = rich(
        '<font color="#e94d48" size="16">*</font> <font color="#282c2f"><b>ASEEDER</b></font>',
        ParagraphStyle("Brand", parent=body, fontSize=10, leading=13),
    )
    header_center = rich(
        f"<b>{escape(_safe_text(company.get('name') or 'Business Simulation'))}</b><br/>Round {round_no} Report",
        ParagraphStyle("HeaderCenter", parent=small, alignment=TA_LEFT, leading=8.2),
    )
    header_right = rich(
        f"Team Number:<br/><font size=12><b>{escape(code)}</b></font>",
        ParagraphStyle("HeaderRight", parent=small, alignment=TA_RIGHT, leading=9),
    )
    header = Table([[header_left, header_center, header_right]], colWidths=[42 * mm, 104 * mm, 38 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, line),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    key_metrics = table([
        ["Total Assets", "Debt", "Net Assets", "Rank"],
        [_money(metrics.get("total_assets")), _money(metrics.get("debt")), _money(metrics.get("net_assets")), rank],
        ["Sales Revenue", "Cost", "Net Profit", "Home Market"],
        [_money(metrics.get("sales_revenue")), _money(metrics.get("cost")), _money(metrics.get("net_profit")), company.get("home_city") or "-"],
    ], [46 * mm] * 4, alignments={0: "CENTER", 1: "CENTER", 2: "CENTER", 3: "CENTER"})
    key_metrics.setStyle(TableStyle([
        ("BACKGROUND", (0, 2), (-1, 2), pale),
        ("FONTNAME", (0, 2), (-1, 2), font_bold),
        ("LINEABOVE", (0, 2), (-1, 2), 0.55, line),
    ]))

    start_cash = float(finance.get("round_begins", 0))
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
        ("Layoff compensation", -float(finance.get("layoff", 0)), 0.0),
        ("Low-salary quit compensation", -float(finance.get("quit_penalty", 0)), 0.0),
        ("Employee training cost", -float(finance.get("training", 0)), 0.0),
        ("Components material cost", -float(finance.get("component_material", finance.get("materials", 0))), 0.0),
        ("Components storage cost", -float(finance.get("component_storage", finance.get("storage", 0))), 0.0),
        ("Products material cost", -float(finance.get("product_material", 0)), 0.0),
        ("Products storage cost", -float(finance.get("product_storage", 0)), 0.0),
        ("Change sales agents", -float(finance.get("agents", 0)), 0.0),
        ("Marketing investment", -float(finance.get("marketing", 0)), 0.0),
        ("Quality investment", -float(finance.get("quality", 0)), 0.0),
        ("Management investment", -float(finance.get("management", 0)), 0.0),
        ("Sales revenue", float(metrics.get("sales_revenue", finance.get("sales_revenue", 0))), 0.0),
        ("Research investment", -float(finance.get("research", 0)), 0.0),
        ("Market report cost", -float(finance.get("market_reports", 0)), 0.0),
        ("Debt interest", 0.0, float(finance.get("interest", 0))),
        ("Tax deduction", -float(finance.get("tax", 0)), 0.0),
        ("Project bonus", float(finance.get("project_bonus", 0)), 0.0),
    ]
    for item, cash_change, debt_change in finance_events:
        running_cash += cash_change
        running_debt += debt_change
        finance_rows.append([item, _flow(cash_change), _money(running_cash), _flow(debt_change), _money(running_debt)])
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
    for item in hr_rows:
        human_rows.append([
            item.get("employee", ""), item.get("previous", 0), item.get("laid", 0), item.get("quitted", 0),
            item.get("added", 0), item.get("promoted", 0), item.get("working", 0),
            _money(item.get("salary", 0)), _money(item.get("average", 0)),
        ])

    planned = int(production.get("planned", 0))
    produced = int(production.get("produced", 0))
    components = int(production.get("components", produced * 7))
    old_products = int(production.get("old_products", 0))
    sold = int(production.get("sold", 0))
    surplus = int(production.get("surplus", max(0, old_products + produced - sold)))
    component_material_price = production.get(
        "component_material_unit_price",
        float(finance.get("component_material", 0)) / max(components, 1),
    )
    product_material_price = production.get(
        "product_material_unit_price",
        float(finance.get("product_material", 0)) / max(produced, 1),
    )
    component_storage_price = production.get(
        "component_storage_unit_price",
        float(finance.get("component_storage", 0)) / max(int(production.get("component_storage_increase", 0)), 1),
    )
    product_storage_price = production.get(
        "product_storage_unit_price",
        float(finance.get("product_storage", 0)) / max(int(production.get("product_storage_increase", 0)), 1),
    )
    management_table = table([
        ["Management", "Management Investment", "Management Index"],
        ["", _money(finance.get("management", 0)), _num(production.get("ma_index", 0), 2)],
    ], [61 * mm, 61 * mm, 62 * mm], alignments={0: "CENTER", 1: "CENTER", 2: "CENTER"})
    overview_table = table([
        ["Overview", "Plan", "Previous", "Produced", "Total", "Used/Sold", "Surplus"],
        ["Components", planned * 7, 0, components, components, components, 0],
        ["Products", planned, old_products, produced, old_products + produced, sold, surplus],
    ], [34 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm],
       alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 6: "RIGHT"})
    details_table = table([
        ["Details", "Productivity", "Employees", "Production", "Material Price", "Material Cost"],
        ["Components", _num(production.get("component_productivity", 0), 3), _num(hr.get("workers", 0)), components, _money(component_material_price), _money(finance.get("component_material", 0))],
        ["Products", _num(production.get("product_productivity", 0), 3), _num(hr.get("engineers", 0)), produced, _money(product_material_price), _money(finance.get("product_material", 0))],
    ], [34 * mm, 30 * mm, 28 * mm, 28 * mm, 32 * mm, 32 * mm],
       alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"})
    storage_table = table([
        ["Storage", "Capacity Before", "Capacity After", "Increment", "Unit Price", "Storage Cost"],
        ["Components", production.get("component_storage_before", 0), production.get("component_storage_after", 0), production.get("component_storage_increase", 0), _money(component_storage_price), _money(finance.get("component_storage", 0))],
        ["Products", production.get("product_storage_before", 0), production.get("product_storage_after", 0), production.get("product_storage_increase", 0), _money(product_storage_price), _money(finance.get("product_storage", 0))],
    ], [34 * mm, 32 * mm, 32 * mm, 28 * mm, 28 * mm, 30 * mm],
       alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"})
    quality_table = table([
        ["Quality", "Quality Investment", "Old Products", "New Products", "Product Quality Index"],
        ["", _money(production.get("quality_investment", finance.get("quality", 0))), old_products, produced, _num(production.get("qi_index", 0), 2)],
    ], [34 * mm, 39 * mm, 34 * mm, 34 * mm, 43 * mm], alignments={1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER"})
    research_table = table([
        ["Overview", "Previous", "Change", "After", "Accumulated Research Investment"],
        ["Patents", research.get("active_patents_this_round", 0), 1 if research.get("success") else 0, research.get("patents_after", 0), _money(research.get("investment", 0))],
    ], [34 * mm, 28 * mm, 28 * mm, 28 * mm, 66 * mm], alignments={1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER"})

    agent_rows: list[list[Any]] = [["Agents", "Previous", "Change", "After", "Change Cost", "Marketing Investment"]]
    sales_rows: list[list[Any]] = [["Market", "Competitive Power", "Sales Volume", "Market Share", "Price", "Sales"]]
    active_sales = [
        item
        for item in sales
        if int(item.get("agents", 0)) > 0
        or int(item.get("agents_previous", 0)) > 0
        or int(item.get("agent_change", 0)) != 0
        or float(item.get("marketing", 0)) > 0
        or int(item.get("sold", 0)) > 0
    ]
    for item in active_sales:
        agent_rows.append([
            item.get("city", ""), item.get("agents_previous", max(0, int(item.get("agents", 0)) - int(item.get("agent_change", 0)))),
            item.get("agent_change", 0), item.get("agents", 0), _money(item.get("agent_change_cost", 0)), _money(item.get("marketing", 0)),
        ])
        gross_sales = float(item.get("sold", 0)) * float(item.get("price", 0))
        net_sales = gross_sales - float(item.get("transport", 0))
        sales_rows.append([
            item.get("city", ""), _pct_points(item.get("cpi", 0)), _num(item.get("sold", 0)),
            _pct_points(float(item.get("market_share", 0)) * 100), _money(item.get("price", 0)), _money(net_sales),
        ])

    story: list[Any] = [
        header, Spacer(1, 3), section("Key Metrics"), key_metrics, Spacer(1, 2),
        *notes([
            "Net Profit = Sales Revenue - All Costs. It measures the result achieved in this round.",
            "Net Assets = Total Assets - Debt. The end-of-round value is used for ranking.",
        ]),
        section("Finance"),
        table(finance_rows, [55 * mm, 33 * mm, 34 * mm, 29 * mm, 33 * mm], font_size=5.9,
              alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT"}),
        CondPageBreak(45 * mm), section("Human Resources"),
        table(human_rows, [40 * mm, 18 * mm, 15 * mm, 17 * mm, 15 * mm, 18 * mm, 17 * mm, 22 * mm, 22 * mm],
              font_size=5.5, alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 6: "RIGHT", 7: "RIGHT", 8: "RIGHT"}),
        *notes([
            "Low-salary Effect: salaries below the home-market average reduce effective productivity and cause proportional quits.",
            "Layoff Cost: voluntary layoffs cost one month of salary and are shown separately in Finance.",
            "Salary-reduction / Quitting: each automatic quitter receives two months of the current salary.",
            "Worker Promotion: workers become experienced after completing two rounds.",
            "Engineer Promotion: engineers become experienced after completing two rounds.",
            "Compensation and payroll use the salary actually accepted by the system for this round.",
        ]),
        Spacer(1, 3), management_table, PageBreak(), section("Production"), overview_table,
        Spacer(1, 4), details_table,
        *notes([
            "Productivity shows output capacity per employee after experience and salary effects.",
            "Actual production is limited by the plan, components, employee capacity and available cash.",
        ]),
        Spacer(1, 4), storage_table,
        *notes(["Storage Cost is paid only for an increase in storage capacity."]),
        Spacer(1, 4), quality_table,
        *notes(["Product Quality Index = Quality Investment / (Old Products x 1.20 + New Products)."]),
        section("Research Investment"), research_table,
        *notes([
            "A successful patent becomes active in the following round and does not reduce this round's material cost.",
            "Research is resolved independently each round; the amount shown is the paid investment for this round.",
        ]),
        section("Sales"),
        table(agent_rows, [34 * mm, 28 * mm, 25 * mm, 25 * mm, 34 * mm, 38 * mm],
              alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"}),
        Spacer(1, 4),
        table(sales_rows, [36 * mm, 34 * mm, 28 * mm, 30 * mm, 27 * mm, 29 * mm],
              alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT"}),
        *notes(["Sales values are net of inter-city transport charges where applicable."]),
    ]

    for market in market_sections:
        summary_table = table([
            ["Population", "Penetration", "Market Size", "Total Sales Volume", "Avg. Price"],
            [_num(market.get("population", 0)), f"{float(market.get('penetration', 0)) * 100:.2f}%",
             _num(market.get("market_size", 0)), _num(market.get("total_volume", 0)), _money(market.get("average_price", 0))],
        ], [36 * mm, 34 * mm, 37 * mm, 41 * mm, 36 * mm], alignments={0: "CENTER", 1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER"})
        market_rows: list[list[Any]] = [[
            "Team", "Management Index", "Agents", "Marketing Investment",
            "Product Quality Index", "Price", "Sales Volume", "Market Share",
        ]]
        for item in market.get("rows", []):
            market_rows.append([
                item.get("code", ""), _num(item.get("ma_index", 0), 2), item.get("agents", 0),
                _money(item.get("marketing", 0)), _num(item.get("qi_index", 0), 2),
                _money(item.get("price", 0)), _num(item.get("sold", 0)),
                _pct_points(float(item.get("market_share", 0)) * 100),
            ])
        story.extend([
            PageBreak(), section(f"Market Report - {market.get('city', '')}"), summary_table, Spacer(1, 5),
            table(market_rows, [18 * mm, 29 * mm, 17 * mm, 31 * mm, 31 * mm, 22 * mm, 20 * mm, 16 * mm],
                  font_size=5.55, alignments={1: "RIGHT", 2: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 6: "RIGHT", 7: "RIGHT"}),
            Spacer(1, 3),
            paragraph(
                "Avg. Price = [sum(player price x player sales volume) + reference average x "
                "(market size - total player sales volume)] / market size.", tiny,
            ),
        ])

    def page_decorations(canvas: Any, document: Any) -> None:
        canvas.saveState()
        width, height = A4
        # Use a very pale solid colour instead of transparency: this stays
        # consistently faint across browser PDF viewers and print drivers.
        canvas.setFillColor(colors.HexColor("#edf3f1"))
        canvas.setFont(font_bold, 31)
        canvas.translate(width / 2, height / 2)
        canvas.rotate(38)
        canvas.drawCentredString(0, 0, "ASEEDER BUSINESS SIMULATION")
        canvas.restoreState()

        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#c2c6c4"))
        canvas.setLineWidth(0.35)
        canvas.line(13 * mm, 10.5 * mm, width - 13 * mm, 10.5 * mm)
        canvas.setFont(font_name, 6.3)
        canvas.setFillColor(muted)
        canvas.drawString(13 * mm, 7.2 * mm, f"{code} | Round {round_no} Official Report")
        canvas.drawRightString(width - 13 * mm, 7.2 * mm, f"Page {document.page}")
        if document.page > 1:
            canvas.setFont(font_bold, 6.8)
            canvas.setFillColor(ink)
            canvas.drawString(13 * mm, height - 9.5 * mm, "ASEEDER BUSINESS SIMULATION")
            canvas.setFont(font_name, 6.3)
            canvas.setFillColor(muted)
            canvas.drawRightString(width - 13 * mm, height - 9.5 * mm, f"Team {code} | Round {round_no}")
            canvas.setStrokeColor(colors.HexColor("#c2c6c4"))
            canvas.line(13 * mm, height - 11 * mm, width - 13 * mm, height - 11 * mm)
        canvas.restoreState()

    doc.build(story, onFirstPage=page_decorations, onLaterPages=page_decorations)
    return stream.getvalue()
