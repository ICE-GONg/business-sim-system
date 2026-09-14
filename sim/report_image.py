from __future__ import annotations

import textwrap
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageFont


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


def _text(value: Any) -> str:
    result = str(value if value is not None else "")
    for chinese, english in CITY_NAMES.items():
        result = result.replace(chinese, english)
    return result


def _money(value: Any) -> str:
    return f"¥{float(value or 0):,.0f}"


def _flow(value: Any) -> str:
    amount = float(value or 0)
    if abs(amount) < 0.005:
        return "--"
    return f"{'+' if amount > 0 else '-'} ¥{abs(amount):,.0f}"


def _num(value: Any, decimals: int = 0) -> str:
    return f"{float(value or 0):,.{decimals}f}"


def _pct_points(value: Any) -> str:
    return f"{float(value or 0):.2f}%"


def _font(size: int, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = []
    if bold and italic:
        names.extend(("DejaVuSansMono-BoldOblique.ttf", "/System/Library/Fonts/Supplemental/Courier New Bold Italic.ttf"))
    elif bold:
        names.extend(("DejaVuSansMono-Bold.ttf", "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"))
    elif italic:
        names.extend(("DejaVuSansMono-Oblique.ttf", "/System/Library/Fonts/Supplemental/Courier New Italic.ttf"))
    else:
        names.extend(("DejaVuSansMono.ttf", "/System/Library/Fonts/Supplemental/Courier New.ttf"))
    # Preserve Chinese company names locally and on common Linux images. The
    # report body remains monospaced; these are only fallbacks when that face
    # cannot render or is unavailable on the deployment host.
    if bold:
        names.extend((
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ))
    else:
        names.extend((
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ))
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _cjk_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return _font(size, bold=bold)


def _display_font(value: str, font: ImageFont.ImageFont, *, bold: bool = False) -> ImageFont.ImageFont:
    if any(ord(character) > 127 for character in value):
        return _cjk_font(int(getattr(font, "size", 17)), bold=bold)
    return font


class _LongReport:
    width = 1180
    margin = 42
    ink = "#202428"
    muted = "#61686d"
    line = "#9ca3a7"
    faint = "#d9dddf"
    pale = "#ffffff"
    brand = "#e34a43"

    def __init__(self, estimated_height: int) -> None:
        self.image = Image.new("RGB", (self.width, max(estimated_height, 5000)), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.y = 36
        self.body = _font(17)
        self.body_bold = _font(17, bold=True)
        self.small = _font(14)
        self.small_italic = _font(14, italic=True)
        self.section_font = _font(23, bold=True)
        self.header_font = _font(19, bold=True)
        self.title_font = _font(18)
        self._watermark()

    @property
    def content_width(self) -> int:
        return self.width - self.margin * 2

    def _watermark(self) -> None:
        patch = Image.new("RGBA", (900, 180), (255, 255, 255, 0))
        patch_draw = ImageDraw.Draw(patch)
        patch_draw.text(
            (30, 62),
            "ASEEDER BUSINESS SIMULATION",
            font=_font(33, bold=True),
            fill=(105, 130, 125, 22),
        )
        patch = patch.rotate(31, expand=True, resample=Image.Resampling.BICUBIC)
        for y in range(220, self.image.height, 620):
            x = -80 if (y // 620) % 2 == 0 else 300
            self.image.paste(patch, (x, y), patch)

    def line_break(self, amount: int = 12) -> None:
        self.y += amount

    def header(self, company: dict[str, Any], round_no: int) -> None:
        name = _text(company.get("name") or "Business Simulation")
        code = _text(company.get("code") or "-")
        # A small vector mark avoids relying on an emoji glyph in Streamlit
        # Cloud's font set.
        mark_x, mark_y = self.margin + 11, self.y + 16
        self.draw.line((mark_x - 10, mark_y, mark_x + 10, mark_y), fill=self.brand, width=4)
        self.draw.line((mark_x, mark_y - 10, mark_x, mark_y + 10), fill=self.brand, width=4)
        self.draw.line((mark_x - 7, mark_y - 7, mark_x + 7, mark_y + 7), fill="#38a8a0", width=3)
        self.draw.line((mark_x - 7, mark_y + 7, mark_x + 7, mark_y - 7), fill="#f0a629", width=3)
        self.draw.text((self.margin + 36, self.y + 4), "ASEEDER", font=_font(22, bold=True), fill=self.ink)
        self.draw.text((255, self.y + 2), name, font=_display_font(name, self.title_font), fill=self.ink)
        round_label = "Test Round Report" if round_no < 0 else f"Round {round_no} Report"
        self.draw.text((255, self.y + 28), round_label, font=self.small, fill=self.muted)
        right = f"Team Number:  {code}"
        right_width = self.draw.textlength(right, font=self.header_font)
        self.draw.text((self.width - self.margin - right_width, self.y + 20), right, font=self.header_font, fill=self.ink)
        self.y += 72
        self.draw.line((self.margin, self.y, self.width - self.margin, self.y), fill=self.line, width=2)
        self.y += 18

    def section(self, title: str) -> None:
        self.y += 15
        self.draw.text((self.margin, self.y), title, font=self.section_font, fill=self.ink)
        self.y += 32
        line_width = min(self.content_width, max(430, int(self.draw.textlength(title, font=self.section_font) + 190)))
        self.draw.line((self.margin, self.y, self.margin + line_width, self.y), fill=self.ink, width=2)
        self.y += 12

    def note(self, value: str) -> None:
        lines = textwrap.wrap(value, width=122, break_long_words=False)
        for index, line in enumerate(lines):
            self.draw.text((self.margin + 8, self.y), f"• {line}" if index == 0 else f"  {line}", font=self.small_italic, fill=self.muted)
            self.y += 21
        self.y += 4

    def table(
        self,
        headers: list[str],
        rows: list[list[Any]],
        weights: list[float],
        *,
        aligns: dict[int, str] | None = None,
        row_height: int = 34,
        header_height: int = 43,
        font: ImageFont.ImageFont | None = None,
    ) -> None:
        aligns = aligns or {}
        font = font or self.body
        total_weight = sum(weights)
        widths = [self.content_width * weight / total_weight for weight in weights]
        xs = [float(self.margin)]
        for width in widths:
            xs.append(xs[-1] + width)
        self.draw.rectangle((self.margin, self.y, self.width - self.margin, self.y + header_height), fill=self.pale)
        self.draw.line((self.margin, self.y, self.width - self.margin, self.y), fill=self.line, width=1)
        self._table_row(headers, xs, header_height, self.small, aligns, bold=True)
        self.y += header_height
        self.draw.line((self.margin, self.y, self.width - self.margin, self.y), fill=self.line, width=1)
        for row in rows:
            self._table_row(row, xs, row_height, font, aligns)
            self.y += row_height
            self.draw.line((self.margin, self.y, self.width - self.margin, self.y), fill=self.faint, width=1)
        for x in xs:
            self.draw.line((round(x), self.y - header_height - row_height * len(rows), round(x), self.y), fill=self.faint, width=1)
        self.y += 7

    def _table_row(
        self,
        row: list[Any],
        xs: list[float],
        height: int,
        font: ImageFont.ImageFont,
        aligns: dict[int, str],
        bold: bool = False,
    ) -> None:
        actual_font = self.body_bold if bold else font
        for index, raw in enumerate(row):
            value = _text(raw)
            cell_left, cell_right = xs[index], xs[index + 1]
            lines = value.split("\n")
            line_height = 17 if bold else 20
            top = self.y + max(4, (height - line_height * len(lines)) / 2)
            for line_index, line in enumerate(lines):
                line_font = _display_font(line, actual_font, bold=bold)
                max_width = max(8.0, cell_right - cell_left - 14)
                display = line
                while display and self.draw.textlength(display, font=line_font) > max_width:
                    display = display[:-1]
                if display != line and len(display) > 1:
                    display = display[:-1] + "…"
                text_width = self.draw.textlength(display, font=line_font)
                alignment = aligns.get(index, "center")
                if alignment == "left":
                    x = cell_left + 7
                elif alignment == "right":
                    x = cell_right - text_width - 7
                else:
                    x = cell_left + (cell_right - cell_left - text_width) / 2
                self.draw.text((x, top + line_index * line_height), display, font=line_font, fill=self.ink)

    def finish(self) -> bytes:
        bottom = min(self.image.height, self.y + 42)
        result = self.image.crop((0, 0, self.width, bottom))
        stream = BytesIO()
        result.save(stream, format="JPEG", quality=95, optimize=True, progressive=True, subsampling=0)
        return stream.getvalue()


def build_round_report_jpg(
    company: dict[str, Any],
    round_no: int,
    report: dict[str, Any],
    rank: int | str,
    market_sections: list[dict[str, Any]],
) -> bytes:
    """Map one complete round report to a single official-style JPEG long image."""
    market_rows = sum(max(1, len(section.get("rows", []))) for section in market_sections)
    estimated_height = 4400 + len(market_sections) * 260 + market_rows * 35
    canvas = _LongReport(estimated_height)
    canvas.header(company, round_no)

    metrics = report.get("key_metrics", {})
    finance = report.get("finance", {})
    hr = report.get("human_resources", {})
    production = report.get("production", {})
    research = report.get("research", {})
    sales = report.get("sales", [])

    canvas.section("Key Metrics")
    canvas.table(
        ["Total Assets", "", "Debt", "", "Net Assets", "", "Rank"],
        [[_money(metrics.get("total_assets")), "-", _money(metrics.get("debt")), "+", _money(metrics.get("net_assets")), "", rank]],
        [1.15, 0.16, 1.05, 0.16, 1.15, 0.08, 0.46],
    )
    canvas.table(
        ["Sales Revenue", "", "Cost", "", "Net Profit"],
        [[_money(metrics.get("sales_revenue")), "-", _money(metrics.get("cost")), "+", _money(metrics.get("net_profit"))]],
        [1.2, 0.16, 1.05, 0.16, 1.2],
    )
    canvas.note("Net Profit = Sales Revenue - All Costs. This is the direct indicator of your achievement in this round.")
    canvas.note("Net Assets = Total Assets - Debt. Your result through this round is used for ranking.")

    start_cash = float(finance.get("round_begins", 0))
    start_debt = float(finance.get(
        "starting_debt",
        float(metrics.get("debt", 0)) - float(finance.get("loan_change", 0)) - float(finance.get("interest", 0)),
    ))
    running_cash, running_debt = start_cash, start_debt
    finance_rows: list[list[Any]] = [["Round begins", "--", _money(running_cash), "--", _money(running_debt)]]
    events = [
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
        ("Change sales agents", -float(finance.get("agents", 0)), 0.0),
        ("Marketing investment", -float(finance.get("marketing", 0)), 0.0),
        ("Quality investment", -float(finance.get("quality", 0)), 0.0),
        ("Management investment", -float(finance.get("management", 0)), 0.0),
        ("Sales revenue", float(metrics.get("sales_revenue", finance.get("sales_revenue", 0))), 0.0),
        ("Market report cost", -float(finance.get("market_reports", 0)), 0.0),
        ("Research investment", -float(finance.get("research", 0)), 0.0),
        ("Transportation cost", -float(finance.get("transport", 0)), 0.0),
        ("Debt interest", 0.0, float(finance.get("interest", 0))),
        ("Tax deduction", -float(finance.get("tax", 0)), 0.0),
        ("Project bonus", float(finance.get("project_bonus", 0)), 0.0),
    ]
    for label, cash_change, debt_change in events:
        running_cash += cash_change
        running_debt += debt_change
        finance_rows.append([label, _flow(cash_change), _money(running_cash), _flow(debt_change), _money(running_debt)])
    finance_rows.append(["Round ends", "--", _money(finance.get("round_ends", running_cash)), "--", _money(metrics.get("debt", running_debt))])
    canvas.section("Finance")
    canvas.table(
        ["Items", "Cash Flow", "Cash", "Debt Change", "Debt"],
        finance_rows,
        [1.48, 1, 1, 0.95, 1],
        aligns={0: "left", 1: "right", 2: "right", 3: "right", 4: "right"},
        row_height=31,
        font=canvas.small,
    )

    hr_rows = hr.get("rows") or [
        {"employee": "Inexperienced Workers", "previous": hr.get("previous_workers", 0), "laid": max(0, -int(hr.get("worker_delta", 0))), "quitted": 0, "added": max(0, int(hr.get("worker_delta", 0))), "promoted": 0, "working": hr.get("workers", 0), "salary": hr.get("worker_salary", 0), "average": hr.get("average_worker_salary", 0)},
        {"employee": "Experienced Workers", "previous": 0, "laid": 0, "quitted": 0, "added": 0, "promoted": 0, "working": 0, "salary": hr.get("worker_salary", 0), "average": hr.get("average_worker_salary", 0)},
        {"employee": "Inexperienced Engineers", "previous": hr.get("previous_engineers", 0), "laid": max(0, -int(hr.get("engineer_delta", 0))), "quitted": 0, "added": max(0, int(hr.get("engineer_delta", 0))), "promoted": 0, "working": hr.get("engineers", 0), "salary": hr.get("engineer_salary", 0), "average": hr.get("average_engineer_salary", 0)},
        {"employee": "Experienced Engineers", "previous": 0, "laid": 0, "quitted": 0, "added": 0, "promoted": 0, "working": 0, "salary": hr.get("engineer_salary", 0), "average": hr.get("average_engineer_salary", 0)},
    ]
    canvas.section("Human Resources")
    canvas.table(
        ["Employees", "Previous", "Laid", "Quitted", "Added", "Promoted", "Working", "Salary", "Avg."],
        [[item.get("employee", ""), item.get("previous", 0), item.get("laid", 0), item.get("quitted", 0), item.get("added", 0), item.get("promoted", 0), item.get("working", 0), _money(item.get("salary", 0)), _money(item.get("average", 0))] for item in hr_rows],
        [1.85, 0.75, 0.62, 0.75, 0.62, 0.78, 0.72, 0.9, 0.9],
        aligns={0: "left", 1: "right", 2: "right", 3: "right", 4: "right", 5: "right", 6: "right", 7: "right", 8: "right"},
        font=canvas.small,
    )
    canvas.note("Low-salary Effect: a relatively low salary limits hiring and may cause employees to quit.")
    canvas.note("Layoff Cost: employees you dismiss receive one month's salary; low-salary quitters receive two months' compensation.")
    canvas.note("Experienced employees produce 10% more after promotion. Compensation is based on the salary of the previous round.")

    canvas.table(
        ["Management", "Management Investment", "Management Index"],
        [["", _money(finance.get("management", 0)), _num(production.get("ma_index", 0), 2)]],
        [1, 1.2, 1],
    )

    planned = int(production.get("planned", 0))
    produced = int(production.get("produced", 0))
    components_per_product = int(production.get("components_per_product", 7))
    components = int(production.get("components", produced * components_per_product))
    old_products = int(production.get("old_products", 0))
    old_components = int(production.get("old_components", 0))
    sold = int(production.get("sold", 0))
    surplus = int(production.get("surplus", max(0, old_products + produced - sold)))
    component_material_price = production.get("component_material_unit_price", float(finance.get("component_material", 0)) / max(components, 1))
    product_material_price = production.get("product_material_unit_price", float(finance.get("product_material", 0)) / max(produced, 1))
    component_storage_price = production.get("component_storage_unit_price", float(finance.get("component_storage", 0)) / max(int(production.get("component_storage_increase", 0)), 1))
    product_storage_price = production.get("product_storage_unit_price", float(finance.get("product_storage", 0)) / max(int(production.get("product_storage_increase", 0)), 1))

    canvas.section("Production")
    canvas.table(
        ["Overview", "Plan", "Previous", "Produced", "Total", "Used/Sold", "Surplus"],
        [
            ["Components", planned * components_per_product, old_components, components, old_components + components, production.get("component_used", components), production.get("component_surplus", 0)],
            ["Products", planned, old_products, produced, old_products + produced, sold, surplus],
        ],
        [1.25, 0.8, 0.9, 0.9, 0.9, 0.9, 0.9],
        aligns={0: "left", 1: "right", 2: "right", 3: "right", 4: "right", 5: "right", 6: "right"},
    )
    canvas.table(
        ["Details", "Productivity", "Employees", "Production", "Material Price", "Material Cost"],
        [
            ["Components", _num(production.get("component_productivity", 0), 3), hr.get("workers", 0), components, _money(component_material_price), _money(finance.get("component_material", 0))],
            ["Products", _num(production.get("product_productivity", 0), 3), hr.get("engineers", 0), produced, _money(product_material_price), _money(finance.get("product_material", 0))],
        ],
        [1.2, 1, 0.85, 0.9, 1.05, 1.1],
        aligns={0: "left", 1: "right", 2: "right", 3: "right", 4: "right", 5: "right"},
    )
    canvas.note("Productivity shows how many items one employee can produce in a round after salary and experience effects.")
    canvas.note("Actual production is limited by the plan, employee capacity, components, materials, storage and available cash.")
    canvas.table(
        ["Storage", "Capacity Before", "Capacity After", "Increment", "Unit Price", "Storage Cost"],
        [
            ["Components", production.get("component_storage_before", 0), production.get("component_storage_after", 0), production.get("component_storage_increase", 0), _money(component_storage_price), _money(finance.get("component_storage", 0))],
            ["Products", production.get("product_storage_before", 0), production.get("product_storage_after", 0), production.get("product_storage_increase", 0), _money(product_storage_price), _money(finance.get("product_storage", 0))],
        ],
        [1.15, 1.2, 1.2, 0.9, 0.85, 1.05],
        aligns={0: "left", 1: "right", 2: "right", 3: "right", 4: "right", 5: "right"},
    )
    canvas.note("Storage Cost is paid only when storage capacity increases.")
    canvas.table(
        ["Quality", "Quality Investment", "Old Products", "New Products", "Product Quality Index"],
        [["", _money(production.get("quality_investment", finance.get("quality", 0))), old_products, produced, _num(production.get("qi_index", 0), 2)]],
        [0.8, 1.25, 1, 1, 1.35],
    )
    canvas.note("Product Quality Index = Quality Investment / (Old Products x 1.20 + New Products).")

    canvas.section("Research Investment")
    canvas.table(
        ["Overview", "Previous", "Change", "After", "Accumulated Research Investment"],
        [["Patents", research.get("active_patents_this_round", 0), 1 if research.get("success") else 0, research.get("patents_after", 0), _money(research.get("accumulated_after", 0))]],
        [1, 0.8, 0.8, 0.8, 1.9],
    )
    canvas.note("Unsuccessful research remains accumulated for the next round. After success, the balance resets and the patent becomes active next round.")

    active_sales = [
        item for item in sales
        if int(item.get("agents", 0)) > 0
        or int(item.get("agents_previous", 0)) > 0
        or int(item.get("agent_change", 0)) != 0
        or float(item.get("marketing", 0)) > 0
        or int(item.get("sold", 0)) > 0
    ]
    canvas.section("Sales")
    canvas.table(
        ["Agents", "Previous", "Change", "After", "Change Cost", "Marketing Investment"],
        [[item.get("city", ""), item.get("agents_previous", max(0, int(item.get("agents", 0)) - int(item.get("agent_change", 0)))), item.get("agent_change", 0), item.get("agents", 0), _money(item.get("agent_change_cost", 0)), _money(item.get("marketing", 0))] for item in active_sales],
        [1.2, 0.85, 0.8, 0.75, 1.1, 1.45],
        aligns={0: "left", 1: "right", 2: "right", 3: "right", 4: "right", 5: "right"},
    )
    canvas.table(
        ["Market", "Competitive Power", "Sales Volume", "Market Share", "Price", "Sales"],
        [[item.get("city", ""), _pct_points(item.get("cpi", 0)), _num(item.get("sold", 0)), _pct_points(float(item.get("market_share", 0)) * 100), _money(item.get("price", 0)), _money(float(item.get("sold", 0)) * float(item.get("price", 0)))] for item in active_sales],
        [1.15, 1.2, 1, 1, 0.9, 1.15],
        aligns={0: "left", 1: "right", 2: "right", 3: "right", 4: "right", 5: "right"},
    )

    for market in market_sections:
        canvas.section(f"Market Report - {_text(market.get('city', ''))}")
        canvas.table(
            ["Population", "Penetration", "Market Size", "Total Sales Volume", "Avg. Price"],
            [[_num(market.get("population", 0)), f"{float(market.get('penetration', 0)) * 100:.2f}%", _num(market.get("market_size", 0)), _num(market.get("total_volume", 0)), _money(market.get("average_price", 0))]],
            [1, 1, 1, 1.15, 1],
        )
        canvas.table(
            ["Team", "Management\nIndex", "Agents", "Marketing\nInvestment", "Product Quality\nIndex", "Price", "Sales\nVolume", "Market\nShare"],
            [[item.get("code", ""), _num(item.get("ma_index", 0), 2), item.get("agents", 0), _money(item.get("marketing", 0)), _num(item.get("qi_index", 0), 2), _money(item.get("price", 0)), _num(item.get("sold", 0)), _pct_points(float(item.get("market_share", 0)) * 100)] for item in market.get("rows", [])],
            [0.65, 1, 0.55, 1.1, 1.1, 0.8, 0.8, 0.75],
            aligns={0: "center", 1: "right", 2: "right", 3: "right", 4: "right", 5: "right", 6: "right", 7: "right"},
            row_height=31,
            header_height=54,
            font=canvas.small,
        )

    return canvas.finish()
