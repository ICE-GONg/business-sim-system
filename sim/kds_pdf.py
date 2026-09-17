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
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
    ink = colors.HexColor("#182c3b")
    muted = colors.HexColor("#536875")
    border = colors.HexColor("#cdd8de")
    pale = colors.HexColor("#edf3f5")
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=13 * mm, bottomMargin=14 * mm,
        title="公开 KDS - Key Data Sheet", author="Business Simulation",
        subject="公开比赛参数与城市比较", invariant=1,
    )
    body = ParagraphStyle(
        "KDSBody", fontName=font, fontSize=10, leading=14,
        textColor=ink, wordWrap="CJK",
    )
    small = ParagraphStyle("KDSSmall", parent=body, fontSize=8.5, leading=11)
    title = ParagraphStyle("KDSTitle", parent=body, fontSize=20, leading=26, spaceAfter=6)
    section = ParagraphStyle("KDSSection", parent=body, fontSize=12, leading=17)
    note = ParagraphStyle("KDSNote", parent=small, textColor=muted)

    def p(value: Any, style: ParagraphStyle = body) -> Paragraph:
        # Keep Latin text and numeric tables in Helvetica so CID-font fallback
        # in readers cannot change their spacing or substitute punctuation.
        pieces = re.split(r"([\x20-\x7e]+)", str(value))
        text = "".join(
            f'<font name="Helvetica">{escape(piece)}</font>' if piece.isascii()
            else escape(piece)
            for piece in pieces if piece
        )
        return Paragraph(text, style)

    def number(value: Any) -> str:
        return f"{float(value):,.0f}"

    def money(value: Any) -> str:
        return f"RMB {number(value)}"

    def table(heading: str, headers: list[str], rows: list[list[Any]], widths: list[float]) -> Table:
        data = [[p(heading, section)] + [""] * (len(headers) - 1)]
        data.append([p(value, small) for value in headers])
        data.extend([[p(value, small) for value in row] for row in rows])
        result = Table(data, colWidths=[width * mm for width in widths], repeatRows=2, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 1), pale),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 1), (-1, 1), 0.7, border),
            ("LINEBELOW", (0, 2), (-1, -1), 0.3, border),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        return result

    story = [
        p("公开 KDS | Key Data Sheet", title),
        p(f"初始现金：{money(public['initial_cash'])}    |    城市比较与公开规则", body),
        p("本文件采用当前已保存的比赛参数。金额单位为人民币；城市工资为初始月薪。", note),
        Spacer(1, 5 * mm),
    ]
    if cities:
        story.append(table(
            "城市参数 | 资金与人员",
            ["城市", "第一轮最高贷款", "利率", "工人初始月薪", "工程师初始月薪"],
            [[row["city"], number(row["max_loan"]), f"{float(row['interest_rate']) * 100:.2f}%",
              number(row["worker_initial_salary"]), number(row["engineer_initial_salary"])] for row in cities],
            [49, 65, 35, 60, 60],
        ))
        story.extend([Spacer(1, 5 * mm), table(
            "城市参数 | 材料、仓储与市场",
            ["城市", "零件材料单价", "产品材料单价", "零件仓储单价", "产品仓储单价", "人口", "初始渗透率", "初始均价"],
            [[row["city"], number(row["component_material"]), number(row["product_material"]),
              number(row["component_storage"]), number(row["product_storage"]), number(row["population"]),
              f"{float(row['penetration']) * 100:.2f}%", number(row["initial_avg_price"])] for row in cities],
            [35, 32, 32, 32, 32, 39, 35, 32],
        )])
    else:
        story.append(p("当前尚未配置城市。", body))
    story.extend([
        Spacer(1, 3 * mm),
        p("最高贷款、工资、价格和市场渗透率可能随轮次变化。市场渗透率表示该城市对购买产品有兴趣的人口比例。", note),
        PageBreak(),
        p("公式、范围与费用", title),
        p("Equations, Ranges & Prices", note),
        Spacer(1, 5 * mm),
    ])
    rules = [
        ["零件生产", f"1 个零件 = {number(public['component_workers'])} 名无经验工人 + {number(public['component_hours'])} 小时 + 1 份零件材料"],
        ["产品生产", f"1 个产品 = {number(public['product_engineers'])} 名无经验工程师 + {number(public['product_hours'])} 小时 + {number(public['components_per_product'])} 个零件 + 1 份产品材料"],
        ["员工经验", "有经验的工人和工程师单位时间产量比无经验员工高 10%。"],
        ["培训费", f"每名新工人 {money(public['worker_training_cost'])}；每名新工程师 {money(public['engineer_training_cost'])}。"],
        ["产品质量指数", "质量投入 ÷（旧产品 × 1.20 + 新产品）"],
        ["管理指数", "管理投入 ÷（工人数 + 工程师数）"],
        ["工资范围", f"{money(public['salary_min'])} - {money(public['salary_max'])}"],
        ["产品售价范围", f"{money(public['price_min'])} - {money(public['price_max'])}"],
        ["跨城运输费", f"每件产品 {money(public['transport_cost'])}，仅对主场之外实际售出的产品收取。"],
        ["新增销售 Agent", money(public["agent_add_cost"])],
        ["移除销售 Agent", money(public["agent_remove_cost"])],
        ["单城市市场报告", money(public["report_cost"])],
        ["研发投入", "研发投入未成功时会累计到下一轮；成功后累计投入清零。"],
        ["专利效果", f"每项专利将材料成本乘以 {float(public['patent_factor']):.2f}，中奖后的下一轮开始生效。"],
    ]
    story.append(table("公开规则", ["项目", "公式或参数"], rules, [49, 220]))

    def footer(canvas: Canvas, document: SimpleDocTemplate) -> None:
        canvas.saveState()
        width, _ = landscape(A4)
        canvas.setStrokeColor(border)
        canvas.line(14 * mm, 10 * mm, width - 14 * mm, 10 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(muted)
        canvas.drawString(14 * mm, 6 * mm, "PUBLIC KDS | Key Data Sheet")
        canvas.drawRightString(width - 14 * mm, 6 * mm, f"Page {document.page}")
        canvas.restoreState()

    # Identical saved public settings produce the same file from either role.
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
