from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont


WIDTH = 2400
MARGIN = 80
INK = "#20242a"
MUTED = "#5f6670"
LIGHT = "#f3f1ed"
LINE = "#aeb2b7"
ACCENT = "#e14b3f"

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
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _money(value: Any) -> str:
    return f"¥{float(value or 0):,.0f}"


def _number(value: Any) -> str:
    return f"{float(value or 0):,.0f}"


def _percent(value: Any) -> str:
    return f"{float(value or 0) * 100:.2f}%"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _section_title(draw: ImageDraw.ImageDraw, y: int, title: str) -> int:
    font = _font(34, True)
    draw.text((MARGIN, y), title, fill=INK, font=font)
    line_y = y + 49
    draw.line((MARGIN, line_y, 920, line_y), fill=INK, width=3)
    return line_y + 22


def _table(
    draw: ImageDraw.ImageDraw,
    y: int,
    headers: list[str],
    rows: Iterable[list[str]],
    widths: list[int],
) -> int:
    header_font = _font(21, True)
    body_font = _font(22)
    header_height = 104
    row_height = 62
    x_positions = [MARGIN]
    for width in widths:
        x_positions.append(x_positions[-1] + width)
    draw.rectangle((MARGIN, y, x_positions[-1], y + header_height), fill=LIGHT, outline=LINE, width=2)
    for index, header in enumerate(headers):
        x0, x1 = x_positions[index], x_positions[index + 1]
        if index:
            draw.line((x0, y, x0, y + header_height), fill=LINE, width=2)
        lines = _wrap(draw, header, header_font, x1 - x0 - 18)
        top = y + max(8, (header_height - len(lines) * 27) // 2)
        for offset, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=header_font)
            draw.text((x0 + (x1 - x0 - (bbox[2] - bbox[0])) / 2, top + offset * 27), line, fill=INK, font=header_font)
    y += header_height
    for row_index, row in enumerate(rows):
        fill = "#ffffff" if row_index % 2 == 0 else "#faf9f7"
        draw.rectangle((MARGIN, y, x_positions[-1], y + row_height), fill=fill, outline=LINE, width=1)
        for index, value in enumerate(row):
            x0, x1 = x_positions[index], x_positions[index + 1]
            if index:
                draw.line((x0, y, x0, y + row_height), fill=LINE, width=1)
            text = str(value)
            bbox = draw.textbbox((0, 0), text, font=body_font)
            draw.text((x0 + (x1 - x0 - (bbox[2] - bbox[0])) / 2, y + 17), text, fill=INK, font=body_font)
        y += row_height
    return y + 34


def build_kds_png(settings: dict[str, Any], markets: Iterable[dict[str, Any] | Any]) -> bytes:
    """Render a high-resolution, official-style Key Data Sheet as PNG."""
    market_rows = [dict(row) for row in markets]
    canvas = Image.new("RGB", (WIDTH, 3800), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(46, True)
    subtitle_font = _font(27)
    small_font = _font(23)
    bullet_font = _font(25)

    draw.rectangle((MARGIN, 72, MARGIN + 34, 106), fill=ACCENT)
    draw.line((MARGIN + 17, 60, MARGIN + 17, 118), fill=ACCENT, width=7)
    draw.line((MARGIN - 12, 89, MARGIN + 46, 89), fill=ACCENT, width=7)
    draw.text((MARGIN + 64, 52), "BUSINESS SIMULATION", fill=INK, font=title_font)
    draw.text((WIDTH - MARGIN, 61), "KEY DATA SHEET", fill=INK, font=title_font, anchor="ra")
    draw.text((MARGIN, 126), "Official parameters for the current competition", fill=MUTED, font=subtitle_font)
    draw.text((WIDTH - MARGIN, 126), f"Initial Cash  {_money(settings.get('initial_cash', 0))}", fill=INK, font=subtitle_font, anchor="ra")
    draw.line((MARGIN, 176, WIDTH - MARGIN, 176), fill=INK, width=4)

    y = _section_title(draw, 220, "Market Details")
    headers = [
        "Market", "Round 1 Max Loan", "Minimum Loan", "Interest Rate", "Initial Worker Salary",
        "Initial Engineer Salary", "Population", "Penetration", "Initial Avg. Price", "Maximum Price",
    ]
    widths = [230, 260, 220, 160, 230, 230, 260, 200, 240, 210]
    rows = [
        [
            CITY_NAMES.get(str(row["city"]), str(row["city"])), _money(row["max_loan"]), _money(row["min_loan"]),
            _percent(row["interest_rate"]), _money(row["worker_initial_salary"]), _money(row["engineer_initial_salary"]),
            _number(row["population"]), _percent(row["penetration"]), _money(row["initial_avg_price"]), _money(row["max_price"]),
        ]
        for row in market_rows
    ]
    y = _table(draw, y, headers, rows, widths)

    y = _section_title(draw, y, "Costs by Market")
    cost_headers = [
        "Market", "Component Material", "Product Material", "Component Storage", "Product Storage",
        "Transport / Product", "Worker Training", "Engineer Training",
    ]
    cost_widths = [230, 300, 300, 300, 300, 270, 270, 270]
    cost_rows = [
        [
            CITY_NAMES.get(str(row["city"]), str(row["city"])), _money(row["component_material"]),
            _money(row["product_material"]), _money(row["component_storage"]), _money(row["product_storage"]),
            _money(row["transport_cost"]), _money(row["worker_training_cost"]), _money(row["engineer_training_cost"]),
        ]
        for row in market_rows
    ]
    y = _table(draw, y, cost_headers, cost_rows, cost_widths)

    y = _section_title(draw, y, "Equations, Ranges & Prices")
    bullets = [
        f"Component productivity = (504 / {settings.get('component_hours', 7)}) x (effective workers / {settings.get('component_workers', 3)}).",
        f"Product productivity = min[(504 / {settings.get('product_hours', 14)}) x (effective engineers / {settings.get('product_engineers', 4)}), component output / {settings.get('components_per_product', 7)}].",
        "Experienced workers and engineers produce 10% more after two completed rounds.",
        "Home-market average salary = [sum(headcount x chosen salary) + sum(headcount x KDS salary x 2)] / (total headcount x 3). Other home markets do not affect it.",
        "If salary is below the home-market average, employees leave proportionally. Each quitter receives compensation equal to two months of the current salary.",
        f"Salary range: {_money(settings.get('salary_min', 0))} to {_money(settings.get('salary_max', 0))}; maximum change per round: +/- {_money(settings.get('salary_change_limit', 0))}.",
        f"Round 2+ new-loan limit = Net Assets / {_money(settings.get('loan_asset_threshold', 0))} x {_money(settings.get('global_max_loan', 0))}, capped at the global maximum. Round 1 uses the home-market maximum.",
        "MA index = Management Investment / (Workers + Engineers).",
        "QI index = Quality Investment / (Old Products x 1.20 + New Products). QI large threshold = Market Maximum Price / 50.",
        "MI large threshold = QI large threshold x market size x 20% / Agent benefit / 1.5 / 2. Agent benefit = 1 + 10% x number of agents.",
        f"Add / remove one sales agent: {_money(settings.get('agent_add_cost', 0))} / {_money(settings.get('agent_remove_cost', 0))}. Add at most {int(settings.get('max_agent_add_per_city_round', 3))} per market each round.",
        f"Market report: {_money(settings.get('report_cost', 0))} per market. Cross-market transport and employee training use the market-specific table above.",
        f"Research success: 25% at {_money(settings.get('research_25', 0))}; 75% at {_money(settings.get('research_75', 0))}. A successful patent applies from the next round and multiplies material cost by {float(settings.get('patent_factor', 0.7)):.2f}.",
        "Secondary sales redistribute only CPI left unused because of stock shortages. A player with zero visible CPI cannot receive secondary sales.",
    ]
    for text in bullets:
        wrapped = _wrap(draw, text, bullet_font, WIDTH - MARGIN * 2 - 52)
        draw.ellipse((MARGIN + 4, y + 12, MARGIN + 16, y + 24), fill=INK)
        for line_index, line in enumerate(wrapped):
            draw.text((MARGIN + 38, y + line_index * 35), line, fill=INK, font=bullet_font)
        y += len(wrapped) * 35 + 18

    y += 24
    draw.line((MARGIN, y, WIDTH - MARGIN, y), fill=LINE, width=2)
    draw.text((MARGIN, y + 24), "Generated from the administrator's live KDS settings.", fill=MUTED, font=small_font)
    draw.text((WIDTH - MARGIN, y + 24), "Business Simulation System", fill=MUTED, font=small_font, anchor="ra")
    bottom = min(canvas.height, y + 90)
    output = BytesIO()
    canvas.crop((0, 0, WIDTH, bottom)).save(output, format="PNG", optimize=True)
    return output.getvalue()
