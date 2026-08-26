#!/usr/bin/env python3
"""Daily AI briefing focused exclusively on the Israeli stock market."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from groq import Groq

import database
from israel_market import collect_israeli_market_data


load_dotenv()

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_TIMEOUT_SECONDS = float(os.getenv("GROQ_TIMEOUT_SECONDS", "90"))
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
OWNER_NAME = os.getenv("OWNER_NAME", "Admin")
OWNER_EMAIL = os.getenv("OWNER_EMAIL")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")
MIN_CHART_SESSIONS = 200
MIN_STOCK_CHART_SESSIONS = 20


def _today() -> datetime:
    return datetime.now(ISRAEL_TZ)


def _compact_technical(technical: dict | None) -> dict:
    technical = technical or {}
    if not technical.get("available"):
        return {"ok": False}
    return {
        "ok": True,
        "n": technical.get("sessions"),
        "date": technical.get("as_of"),
        "ret_1m_3m_6m_1y_pct": [
            technical.get("return_20d_pct"),
            technical.get("return_3m_pct"),
            technical.get("return_6m_pct"),
            technical.get("return_1y_pct"),
        ],
        "above_sma_20_50_200": [
            technical.get("above_sma_20"),
            technical.get("above_sma_50"),
            technical.get("above_sma_200"),
        ],
        "rsi14": technical.get("rsi_14"),
        "macd_hist": technical.get("macd_histogram"),
        "support_resistance_60d": [
            technical.get("support_60d"),
            technical.get("resistance_60d"),
        ],
        "turnover_x_avg20": technical.get("turnover_vs_20d_avg"),
        "trendline_60d_200d_pct": [
            technical.get("trendline_60d_pct"),
            technical.get("trendline_200d_pct"),
        ],
        "ann_vol_pct": technical.get("annualized_volatility_pct"),
        "bias": technical.get("directional_bias"),
    }


def _compact_market_payload(data: dict) -> dict:
    indices = {}
    for name, index in data.get("indices", {}).items():
        indices[name] = {
            "last_value": index.get("last_value"),
            "change_1d_pct": index.get("change_1d_pct"),
            "breadth": index.get("breadth"),
            "top_5_weight_pct": index.get("top_5_weight_pct"),
            "trend": _compact_technical(index.get("trend")),
        }
    return {
        "as_of": data.get("as_of"),
        "indices": indices,
    }


def _compact_sector_payload(data: dict, selected_names: list[str] | None = None) -> dict:
    sectors = {}
    for name, sector in data.get("sectors", {}).items():
        if selected_names is not None and name not in selected_names:
            continue
        stocks = []
        for stock in sector.get("top_stocks_by_market_cap", []):
            stocks.append({
                "name": stock.get("name"),
                "symbol": stock.get("symbol"),
                "security_number": stock.get("security_number"),
                "price_ils": stock.get("price_ils"),
                "change_1d_pct": stock.get("change_1d_pct"),
                "market_cap_m_ils": stock.get("market_cap_m_ils"),
                "index_weight_pct": stock.get("index_weight_pct"),
                "technical": _compact_technical(stock.get("technical_analysis")),
                "chart_source": stock.get("chart_source"),
            })
        sectors[name] = {
            "index_id": sector.get("index_id"),
            "last_value": sector.get("last_value"),
            "change_1d_pct": sector.get("change_1d_pct"),
            "constituents_count": sector.get("constituents_count"),
            "breadth": sector.get("breadth"),
            "trend": _compact_technical(sector.get("trend")),
            "top_stocks_by_market_cap": stocks,
            "source": sector.get("source"),
        }
    return {
        "as_of": data.get("as_of"),
        "horizon": "מספר שבועות, לפחות 15 ימי מסחר",
        "sectors": sectors,
    }


def _compact_sector_overview(data: dict) -> dict:
    return {
        "as_of": data.get("as_of"),
        "sectors": {
            name: {
                "change_1d_pct": sector.get("change_1d_pct"),
                "breadth": sector.get("breadth"),
                "trend": _compact_technical(sector.get("trend")),
            }
            for name, sector in data.get("sectors", {}).items()
        },
    }


def _format_number(value, decimals: int = 2) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "לא זמין"


def _format_pct(value) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "לא זמין"


def _technical_summary(technical: dict, value_unit: str = "") -> list[str]:
    if not technical.get("available"):
        return ["נתוני הגרף אינם זמינים כעת."]
    sessions = technical.get("sessions") or 0
    history_note = (
        f"הניתוח מבוסס על {sessions} ימי מסחר בחלון של שנה."
        if sessions >= MIN_CHART_SESSIONS
        else f"קיימים רק {sessions} ימי מסחר מקומיים; אין להסיק מכך מגמה שנתית מלאה."
    )
    sma_labels = []
    for period in (20, 50, 200):
        state = technical.get(f"above_sma_{period}")
        if state is not None:
            sma_labels.append(f"{'מעל' if state else 'מתחת'} SMA-{period}")
    rsi = technical.get("rsi_14")
    if rsi is None:
        rsi_text = "RSI-14 לא זמין"
    elif rsi >= 70:
        rsi_text = f"RSI-14 {_format_number(rsi, 1)} – קניית יתר"
    elif rsi <= 30:
        rsi_text = f"RSI-14 {_format_number(rsi, 1)} – מכירת יתר"
    else:
        rsi_text = f"RSI-14 {_format_number(rsi, 1)} – תחום ניטרלי"
    macd = technical.get("macd_histogram")
    macd_text = (
        "MACD לא זמין"
        if macd is None
        else f"MACD {'חיובי' if macd > 0 else 'שלילי' if macd < 0 else 'ניטרלי'} ({_format_number(macd, 3)})"
    )
    unit = f" {value_unit}" if value_unit else ""
    support = _format_number(technical.get("support_60d"))
    resistance = _format_number(technical.get("resistance_60d"))
    turnover = technical.get("turnover_vs_20d_avg")
    turnover_text = (
        "נתון מחזור אינו זמין"
        if turnover is None
        else f"המחזור האחרון הוא פי {_format_number(turnover)} מממוצע 20 הימים"
    )
    return [
        history_note,
        "תשואות חודש / 3 חודשים / 6 חודשים / שנה: "
        + " / ".join(
            _format_pct(technical.get(key))
            for key in ("return_20d_pct", "return_3m_pct", "return_6m_pct", "return_1y_pct")
        )
        + ".",
        f"ממוצעים נעים: {', '.join(sma_labels) if sma_labels else 'לא זמינים'}; {rsi_text}; {macd_text}.",
        f"תמיכה ל-60 יום: {support}{unit}; התנגדות: {resistance}{unit}; {turnover_text}.",
        "קווי מגמה ל-60 / 200 ימים: "
        f"{_format_pct(technical.get('trendline_60d_pct'))} / {_format_pct(technical.get('trendline_200d_pct'))}; "
        f"הטיה כמותית: {technical.get('directional_bias') or 'לא זמינה'}.",
    ]


def build_quantitative_cards(data: dict) -> str:
    """Build complete Hebrew chart cards independently of AI output limits."""
    sections = []
    for name in ("ת״א-35", "ת״א-90", "ת״א-125"):
        index = data.get("indices", {}).get(name, {})
        breadth = index.get("breadth", {})
        lines = [
            f"- **רמה אחרונה:** {_format_number(index.get('last_value'))}; שינוי יומי: {_format_pct(index.get('change_1d_pct'))}.",
            f"- **רוחב השוק:** {breadth.get('advancers', 0)} עולות, {breadth.get('decliners', 0)} יורדות ו-{breadth.get('unchanged', 0)} ללא שינוי; משקל חמש הגדולות: {_format_number(index.get('top_5_weight_pct'))}%.",
        ]
        lines.extend(f"- **תובנת גרף:** {line}" for line in _technical_summary(index.get("trend", {})))
        lines.append(f"- [הגרף הרשמי של {name}]({index.get('chart_source')})")
        sections.append(f"### מדד {name}\n" + "\n".join(lines))

    for name, sector in data.get("sectors", {}).items():
        breadth = sector.get("breadth", {})
        lines = [
            f"- **מדד הסקטור:** רמה {_format_number(sector.get('last_value'))}; שינוי יומי {_format_pct(sector.get('change_1d_pct'))}; רוחב: {breadth.get('advancers', 0)} עולות, {breadth.get('decliners', 0)} יורדות ו-{breadth.get('unchanged', 0)} ללא שינוי.",
        ]
        lines.extend(f"- **גרף הסקטור:** {line}" for line in _technical_summary(sector.get("trend", {})))
        lines.append(f"- [הגרף הרשמי של סקטור {name}]({sector.get('chart_source')})")
        lines.append("#### שלוש המניות הגדולות לפי שווי שוק")
        for stock in sector.get("top_stocks_by_market_cap", []):
            technical = stock.get("technical_analysis", {})
            chart_points = _technical_summary(technical, value_unit="₪")
            concise_chart = (
                " ".join((chart_points[1], chart_points[2], chart_points[3], chart_points[4]))
                if technical.get("available")
                else chart_points[0]
            )
            if technical.get("available") and (technical.get("sessions") or 0) < MIN_CHART_SESSIONS:
                concise_chart = f"{chart_points[0]} {concise_chart}"
            lines.append(
                f"- **{stock.get('name')} ({stock.get('symbol')})** – מחיר {_format_number(stock.get('price_ils'))} ₪; "
                f"שינוי יומי {_format_pct(stock.get('change_1d_pct'))}; שווי שוק {_format_number(stock.get('market_cap_m_ils'), 0)} מיליון ₪. "
                f"{concise_chart} [גרף רשמי]({stock.get('chart_source')})"
            )
        sections.append(f"### סקטור: {name}\n" + "\n".join(lines))
    return "\n\n".join(sections)


def build_source_context_cards(data: dict) -> str:
    """Render official macro, MAYA and Israeli-news source cards completely."""
    rates = data.get("exchange_rates", {}).get("rates", {})
    macro_lines = []
    for currency in ("USD", "EUR", "GBP"):
        rate = rates.get(currency, {})
        macro_lines.append(
            f"- **{currency}/ILS:** {_format_number(rate.get('ils_rate'), 4)}; שינוי {_format_pct(rate.get('change_pct'))}."
        )
    macro_lines.append(
        f"- [מקור: בנק ישראל]({data.get('exchange_rates', {}).get('source')})"
    )

    disclosure_lines = []
    for item in data.get("maya_announcements", [])[:5]:
        disclosure_lines.append(
            f"- **{item.get('source')}:** [{item.get('title')}]({item.get('link')})"
        )
    for item in data.get("news", [])[:10]:
        disclosure_lines.append(
            f"- **{item.get('source')}:** [{item.get('title')}]({item.get('link')})"
        )
    if not disclosure_lines:
        disclosure_lines.append("- לא נמצאו כותרות עדכניות בחלון האיסוף.")

    return "\n\n".join((
        "### מאקרו ישראלי ושער השקל\n" + "\n".join(macro_lines),
        "### דיווחי מאיה וחדשות מהותיות\n" + "\n".join(disclosure_lines),
    ))


def validate_market_data(data: dict) -> None:
    """Refuse to generate or send a briefing from partial critical market data."""
    problems = []
    for name in ("ת״א-35", "ת״א-90", "ת״א-125"):
        index = data.get("indices", {}).get(name, {})
        if not index.get("constituents_count"):
            problems.append(f"{name}: constituents missing")
        if (index.get("trend", {}).get("sessions") or 0) < MIN_CHART_SESSIONS:
            problems.append(f"{name}: one-year chart missing")

    for name, sector in data.get("sectors", {}).items():
        stocks = sector.get("top_stocks_by_market_cap", [])
        if len(stocks) != 3:
            problems.append(f"{name}: expected three leading stocks")
        if (sector.get("trend", {}).get("sessions") or 0) < MIN_CHART_SESSIONS:
            problems.append(f"{name}: one-year sector chart missing")
        for stock in stocks:
            if (stock.get("technical_analysis", {}).get("sessions") or 0) < MIN_STOCK_CHART_SESSIONS:
                problems.append(f"{name}/{stock.get('symbol') or stock.get('name')}: chart missing")

    if len(data.get("sectors", {})) != 10:
        problems.append("sector coverage incomplete")
    if problems:
        raise RuntimeError("Critical TASE data validation failed: " + "; ".join(problems))


def _groq_completion(
    client: Groq,
    model: str,
    prompt: str,
    max_tokens: int,
    purpose: str,
) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.25,
        max_tokens=max_tokens,
        reasoning_effort="low",
    )
    usage = getattr(response, "usage", None)
    if usage:
        print(
            f"AI usage [{purpose}]: prompt={getattr(usage, 'prompt_tokens', '?')}, "
            f"completion={getattr(usage, 'completion_tokens', '?')}, "
            f"total={getattr(usage, 'total_tokens', '?')}"
        )
    return response.choices[0].message.content.strip()


def _require_ai_sections(text: str, required_titles: tuple[str, ...]) -> None:
    missing = [title for title in required_titles if f"### {title}" not in text]
    if missing:
        raise RuntimeError("AI response missing required sections: " + ", ".join(missing))


def generate_hebrew_brief(data: dict) -> str:
    """Generate fact-grounded Israeli index and sector analysis in bounded calls."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is required to generate the briefing")

    client = Groq(
        api_key=GROQ_API_KEY,
        timeout=GROQ_TIMEOUT_SECONDS,
        max_retries=1,
    )
    model = GROQ_MODEL
    market_data = json.dumps(
        _compact_market_payload(data),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    date_label = _today().strftime("%d.%m.%Y")

    market_prompt = f"""אתה אנליסט הבורסה בתל אביב. כתוב בעברית תקנית וקצרה ל-{date_label}, על סמך הנתונים בלבד.

כללים: אין הקדמה, פרופיל משקיע, טבלה, מידע חיצוני, מספר מומצא, הבטחת תשואה או הוראת קנייה. הפרד עובדה מתחזית. n<200 פירושו היסטוריה קצרה משנה וביטחון מופחת. התייחס לתשואות 1/3/6/12 חודשים, SMA-20/50/200, RSI-14, MACD, תמיכה/התנגדות, מחזור וקווי מגמה. bias הוא אינדיקציה בלבד.

כתוב עד 300 מילים ובדיוק את הסעיפים הבאים:
### תמונת מצב בבורסה בתל אביב
סיכום הסגירה והמסר המרכזי.

### מבט להמשך
שתי פסקאות: "הימים הקרובים" (1–5 ימי מסחר) ו"השבועות הקרובים" (2–6 שבועות). בכל אחת: תרחיש בסיס, חיובי ושלילי, טריגר ואות ביטול, במילים פשוטות.

מקרא: ret_1m_3m_6m_1y_pct=תשואות; above_sma_20_50_200=מיקום מעל הממוצעים; macd_hist=היסטוגרמת MACD; support_resistance_60d=תמיכה/התנגדות; turnover_x_avg20=מחזור יחסי; trendline_60d_200d_pct=קווי מגמה.

נתונים:
{market_data}"""

    sector_overview = json.dumps(
        _compact_sector_overview(data),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sector_prompt = f"""אתה אנליסט סקטורים של הבורסה בתל אביב. כתוב בעברית תקנית, קצרה וברורה.

דרג את כל עשרת הסקטורים לפי שילוב של ביצועי חודש/3/6/12 חודשים, רוחב, תנודתיות, ממוצעים נעים, RSI, MACD וקווי מגמה. אל תסתמך על יום אחד ואל תמציא נתונים.

כתוב עד 220 מילים ובדיוק שני סעיפים. אין להשתמש בטבלה. כתוב רק את התבליטים המוגדרים:
### סקטורים בולטים ותובנות AI
- **מועדף – שם הסקטור:** משפט אחד עם תזה, סיכון, תנאי אישור וביטול.
- **מעקב – שם הסקטור:** משפט אחד באותו מבנה.
- **להמתין – שם הסקטור:** משפט אחד באותו מבנה.
- **חלשים:** משפט אחד שמציין את הסקטורים החלשים והסיבה.

### מבט סקטוריאלי להמשך
- **תרחיש בסיס:** משפט אחד, מובילים ורמת ביטחון.
- **תרחיש חיובי:** משפט אחד, טריגר ומובילים.
- **תרחיש שלילי:** משפט אחד, טריגר והסקטורים הפגיעים.

מקרא: ret_1m_3m_6m_1y_pct = תשואות חודש/3/6/12 חודשים; above_sma_20_50_200 = מעל ממוצעים 20/50/200; macd_hist = היסטוגרמת MACD; trendline_60d_200d_pct = קווי מגמה.

נתונים:
{sector_overview}"""

    market_brief = _groq_completion(
        client, model, market_prompt, max_tokens=900, purpose="market"
    )
    _require_ai_sections(market_brief, ("תמונת מצב בבורסה בתל אביב", "מבט להמשך"))
    sector_brief = _groq_completion(
        client, model, sector_prompt, max_tokens=700, purpose="sectors"
    )
    _require_ai_sections(
        sector_brief,
        ("סקטורים בולטים ותובנות AI", "מבט סקטוריאלי להמשך"),
    )
    quantitative_cards = build_quantitative_cards(data)
    source_cards = build_source_context_cards(data)
    return "\n\n".join([market_brief, quantitative_cards, sector_brief, source_cards])


def _render_inline_markdown(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" style="color:#1e5d9b;">\1</a>',
        escaped,
    )
    return escaped


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_markdown_block(text: str) -> str:
    """Render the small Markdown subset used by the brief into RTL-safe HTML."""
    lines = text.splitlines()
    rendered = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line == "---":
            index += 1
            continue

        if (
            "|" in line
            and index + 1 < len(lines)
            and _is_table_separator(lines[index + 1])
        ):
            headers = _table_cells(line)
            index += 2
            rows = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_cells(lines[index]))
                index += 1
            header_html = "".join(
                f'<th scope="col">{_render_inline_markdown(cell)}</th>'
                for cell in headers
            )
            row_html = "".join(
                "<tr>" + "".join(
                    f"<td>{_render_inline_markdown(cell)}</td>" for cell in row
                ) + "</tr>"
                for row in rows
            )
            rendered.append(
                f'<div class="table-wrap" dir="rtl"><table dir="rtl"><thead><tr>{header_html}'
                f'</tr></thead><tbody>{row_html}</tbody></table></div>'
            )
            continue

        if re.match(r"^[-*]\s+", line):
            items = []
            while index < len(lines):
                match = re.match(r"^[-*]\s+(.+)", lines[index].strip())
                if not match:
                    break
                items.append(f'<li dir="rtl">{_render_inline_markdown(match.group(1))}</li>')
                index += 1
            rendered.append(f'<ul dir="rtl">{"".join(items)}</ul>')
            continue

        if re.match(r"^\d+[.)]\s+", line):
            items = []
            while index < len(lines):
                match = re.match(r"^\d+[.)]\s+(.+)", lines[index].strip())
                if not match:
                    break
                items.append(f'<li dir="rtl">{_render_inline_markdown(match.group(1))}</li>')
                index += 1
            rendered.append(f'<ol dir="rtl">{"".join(items)}</ol>')
            continue

        if line.startswith("####"):
            rendered.append(
                f'<div class="subheading" dir="rtl">{_render_inline_markdown(line[4:].strip())}</div>'
            )
            index += 1
            continue

        paragraph = [line]
        index += 1
        while index < len(lines):
            candidate = lines[index].strip()
            if (
                not candidate
                or candidate == "---"
                or candidate.startswith("####")
                or re.match(r"^[-*]\s+", candidate)
                or re.match(r"^\d+[.)]\s+", candidate)
                or ("|" in candidate and index + 1 < len(lines) and _is_table_separator(lines[index + 1]))
            ):
                break
            paragraph.append(candidate)
            index += 1
        rendered.append(
            f'<p dir="rtl">{_render_inline_markdown(" ".join(paragraph))}</p>'
        )
    return "".join(rendered)


def build_html_email(brief_text: str, unsubscribe_url: str = "#") -> str:
    now = _today()
    date_str = now.strftime("%d/%m/%Y")
    normalized = re.sub(r"(?m)^\s*#{1,2}\s+###\s+", "### ", brief_text.strip())
    headings = list(re.finditer(r"(?m)^\s*###\s+(.+?)\s*$", normalized))
    cards = []
    for position, heading in enumerate(headings):
        title_text = heading.group(1).strip()
        if "פרופיל" in title_text:
            continue
        body_start = heading.end()
        body_end = headings[position + 1].start() if position + 1 < len(headings) else len(normalized)
        title = html.escape(title_text)
        body = _render_markdown_block(normalized[body_start:body_end].strip())
        card_class = "card index-card" if title_text.startswith("מדד ת״א-") else "card"
        cards.append(
            f'<div class="{card_class}" dir="rtl" align="right"><div class="card-title" dir="rtl">{title}</div>'
            f'<div class="card-body" dir="rtl" align="right">{body}</div></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>תדריך שוק ההון הישראלי – {date_str}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family:Arial,"Noto Sans Hebrew",sans-serif; background:#e8edf4; color:#1e2d42; direction:rtl; text-align:right; }}
    .wrapper {{ max-width:720px; margin:0 auto; padding:24px 16px 40px; }}
    .header {{ background:linear-gradient(135deg,#123767,#1f5797); border-radius:16px; padding:34px; text-align:center; }}
    .logo {{ color:#9ec3e8; font-size:10px; letter-spacing:3px; margin-bottom:10px; }}
    h1 {{ color:#f0c040; font-size:28px; margin-bottom:8px; }}
    .date {{ color:#c1d8ef; font-size:14px; }}
    .accent {{ height:3px; margin:18px 40px; background:linear-gradient(90deg,transparent,#f0c040,#e08030,#f0c040,transparent); }}
    .card {{ background:#f7faff; border:1px solid #c8d8ea; border-radius:14px; padding:22px 26px; margin-bottom:14px; box-shadow:0 2px 10px rgba(30,60,100,.08); }}
    .index-card {{ border-right:5px solid #1f5797; }}
    .card-title {{ color:#163e70; font-size:17px; font-weight:700; border-bottom:2px solid #dce8f4; padding-bottom:10px; margin-bottom:14px; }}
    .card-body {{ color:#2c3e52; font-size:14.5px; line-height:1.9; direction:rtl; text-align:right; unicode-bidi:plaintext; }}
    .card-body p {{ margin:0 0 12px; direction:rtl; text-align:right; }}
    .card-body ul,.card-body ol {{ margin:0 0 14px; padding:0 22px 0 0; direction:rtl; text-align:right; }}
    .card-body li {{ margin-bottom:7px; padding-right:2px; direction:rtl; text-align:right; }}
    .subheading {{ color:#214f82; font-weight:700; margin:13px 0 7px; direction:rtl; text-align:right; }}
    .table-wrap {{ width:100%; overflow-x:auto; margin:10px 0 14px; direction:rtl; }}
    table {{ width:100%; border-collapse:collapse; direction:rtl; text-align:right; }}
    th {{ background:#e5eef8; color:#163e70; font-weight:700; }}
    th,td {{ border:1px solid #c8d8ea; padding:8px 9px; vertical-align:top; direction:rtl; text-align:right; }}
    strong {{ color:#0f2d5e; }}
    .notice {{ background:#fff8df; border:1px solid #ead58b; color:#66551c; border-radius:10px; padding:13px 16px; margin-top:16px; font-size:12px; line-height:1.7; }}
    .footer {{ text-align:center; color:#71869d; font-size:11px; padding:18px; line-height:1.8; }}
  </style>
</head>
<body dir="rtl" align="right"><div class="wrapper" dir="rtl" align="right">
  <div class="header"><div class="logo">ISRAEL MARKET INTELLIGENCE</div><h1>תדריך שוק ההון הישראלי</h1><div class="date">{date_str}</div></div>
  <div class="accent"></div>
  {''.join(cards)}
  <div class="notice">התחזיות בתדריך הן תרחישים המבוססים על נתוני עבר ומידע ציבורי. הן אינן הבטחת תשואה, המלצה אישית או תחליף לייעוץ השקעות מורשה.</div>
  <div class="footer" dir="rtl">מקורות: הבורסה לניירות ערך ומאיה · בנק ישראל · הלמ״ס · משרד האוצר · גלובס · כלכליסט · TheMarker · Bizportal · Funder<br>
    <a href="{html.escape(unsubscribe_url, quote=True)}" style="color:#4a7aaa;">ביטול הרשמה</a>
  </div>
</div></body></html>"""


def send_email(html_content: str, subject: str, recipient_email: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = recipient_email
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipient_email, msg.as_string())


def run(send: bool = True, include_news: bool = True) -> dict:
    database.init_db()
    if OWNER_EMAIL:
        database.seed_owner(OWNER_NAME, OWNER_EMAIL)
    backup_status = database.snapshot_subscribers_once_daily()

    data = collect_israeli_market_data(include_news=include_news)
    validate_market_data(data)
    brief_text = generate_hebrew_brief(data)
    result = {
        "as_of": data.get("as_of"),
        "collection_stats": data.get("collection_stats", {}),
        "backup": backup_status,
        "subscriber_count": 0,
        "sent": 0,
        "failed": [],
        "brief": brief_text,
    }
    if not send:
        return result

    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD are required to send email")

    subscribers = database.get_active_subscribers()
    result["subscriber_count"] = len(subscribers)
    subject = f"תדריך שוק ההון הישראלי – {_today().strftime('%d/%m/%Y')}"
    for subscriber in subscribers:
        try:
            unsubscribe_url = f"{BASE_URL}/unsubscribe/{subscriber['token']}"
            content = build_html_email(brief_text, unsubscribe_url)
            send_email(content, subject, subscriber["email"])
            result["sent"] += 1
        except Exception as exc:
            result["failed"].append({"email": subscriber["email"], "error": str(exc)})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Israeli stock-market morning briefing")
    parser.add_argument("--dry-run", action="store_true", help="Generate but do not send email")
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Validate sources without AI generation, database writes, or email",
    )
    parser.add_argument("--no-news", action="store_true", help="Skip press feeds during diagnostics")
    args = parser.parse_args()
    if args.collect_only:
        data = collect_israeli_market_data(include_news=not args.no_news)
        validate_market_data(data)
        summary = {
            "as_of": data.get("as_of"),
            "indices": {
                name: {
                    "constituents": index.get("constituents_count"),
                    "chart_sessions": index.get("trend", {}).get("sessions"),
                }
                for name, index in data.get("indices", {}).items()
            },
            "sectors": {
                name: {
                    "top_stocks": len(sector.get("top_stocks_by_market_cap", [])),
                    "chart_sessions": sector.get("trend", {}).get("sessions"),
                    "stock_charts": sum(
                        stock.get("technical_analysis", {}).get("available") is True
                        for stock in sector.get("top_stocks_by_market_cap", [])
                    ),
                }
                for name, sector in data.get("sectors", {}).items()
            },
            "maya_announcements": len(data.get("maya_announcements", [])),
            "news_headlines": len(data.get("news", [])),
            "boi_rates": sorted(data.get("exchange_rates", {}).get("rates", {})),
            "collection_stats": data.get("collection_stats", {}),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    result = run(send=not args.dry_run, include_news=not args.no_news)
    printable = {key: value for key, value in result.items() if key != "brief"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("\n" + result["brief"])


if __name__ == "__main__":
    main()
