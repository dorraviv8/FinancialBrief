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


def _compact_sector_overview(
    data: dict,
    calibration: dict | None = None,
) -> dict:
    calibration = calibration or {}
    return {
        "as_of": data.get("as_of"),
        "sectors": {
            name: {
                "graph_score_0_100": calculate_sector_graph_score(sector),
                "historical_calibration_adjustment": calibration.get(name, {}).get(
                    "adjustment", 0
                ),
                "change_1d_pct": sector.get("change_1d_pct"),
                "breadth": sector.get("breadth"),
                "trend": _compact_technical(sector.get("trend")),
                "largest_stocks": [
                    stock.get("symbol") or stock.get("name")
                    for stock in sector.get("top_stocks_by_market_cap", [])
                ],
            }
            for name, sector in data.get("sectors", {}).items()
        },
    }


def _compact_sector_news(data: dict) -> list[dict]:
    items = []
    for item in data.get("maya_announcements", [])[:5]:
        items.append({
            "source": item.get("source"),
            "reliability": item.get("reliability"),
            "title": item.get("title"),
            "companies": item.get("companies", []),
        })
    for item in data.get("news", [])[:8]:
        items.append({
            "source": item.get("source"),
            "reliability": item.get("reliability"),
            "title": item.get("title"),
        })
    return items


def _clamp(value: float, minimum: float = 0, maximum: float = 100) -> float:
    return max(minimum, min(maximum, value))


def _normalized_score(value, lower: float, upper: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 50.0
    return _clamp(((numeric - lower) / (upper - lower)) * 100)


def calculate_sector_graph_score(sector: dict) -> int:
    """Score graph strength for a balanced 2–6 week horizon.

    The score is deliberately deterministic and transparent. AI may later
    adjust it by at most ten points when supplied Israeli news is directly
    relevant to the sector.
    """
    trend = sector.get("trend", {})
    if not trend.get("available"):
        return 50

    weighted_parts = [
        (_normalized_score(trend.get("return_20d_pct"), -10, 10), 0.10),
        (_normalized_score(trend.get("return_3m_pct"), -20, 20), 0.12),
        (_normalized_score(trend.get("return_6m_pct"), -30, 30), 0.10),
        (_normalized_score(trend.get("return_1y_pct"), -50, 50), 0.08),
    ]

    sma_values = []
    for period in (20, 50, 200):
        state = trend.get(f"above_sma_{period}")
        sma_values.append(50 if state is None else 100 if state else 0)
    weighted_parts.append((sum(sma_values) / len(sma_values), 0.18))

    breadth = sector.get("breadth", {})
    advancers = float(breadth.get("advancers") or 0)
    decliners = float(breadth.get("decliners") or 0)
    breadth_score = (
        (advancers / (advancers + decliners)) * 100
        if advancers + decliners
        else 50
    )
    weighted_parts.append((breadth_score, 0.12))

    try:
        rsi_score = _clamp(100 - (abs(float(trend.get("rsi_14")) - 55) * 3))
    except (TypeError, ValueError):
        rsi_score = 50
    weighted_parts.append((rsi_score, 0.10))

    try:
        macd = float(trend.get("macd_histogram"))
    except (TypeError, ValueError):
        macd = None
    macd_score = 50 if macd is None else 75 if macd > 0 else 25 if macd < 0 else 50
    weighted_parts.append((macd_score, 0.08))

    bias_score = {"חיובית": 80, "ניטרלית": 50, "שלילית": 20}.get(
        trend.get("directional_bias"), 50
    )
    weighted_parts.append((bias_score, 0.07))

    volatility_score = 100 - _normalized_score(
        trend.get("annualized_volatility_pct"), 15, 50
    )
    weighted_parts.append((volatility_score, 0.05))

    return int(round(sum(score * weight for score, weight in weighted_parts)))


def _sector_score_label(score: int) -> str:
    if score >= 85:
        return "חזקה מאוד"
    if score >= 70:
        return "חזקה"
    if score >= 55:
        return "חיובית בזהירות"
    if score >= 40:
        return "ניטרלית"
    return "חלשה"


def _parse_sector_scores(
    data: dict,
    ai_text: str,
    calibration: dict | None = None,
) -> dict:
    calibration = calibration or {}
    adjustments = {}
    for line in ai_text.splitlines():
        if not line.startswith("SCORE|"):
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        _marker, name, adjustment_text, reason = parts
        name = name.strip()
        if name not in data.get("sectors", {}):
            continue
        try:
            adjustment = int(adjustment_text.strip())
        except ValueError:
            continue
        adjustments[name] = {
            "news_adjustment": int(_clamp(adjustment, -10, 10)),
            "reason": reason.strip().replace("**", "")[:280],
        }

    scores = {}
    for name, sector in data.get("sectors", {}).items():
        graph_score = calculate_sector_graph_score(sector)
        adjustment = adjustments.get(name, {}).get("news_adjustment", 0)
        calibration_adjustment = int(
            _clamp(calibration.get(name, {}).get("adjustment", 0), -5, 5)
        )
        reason = adjustments.get(name, {}).get("reason") or (
            "לא זוהתה בכותרות שסופקו השפעת חדשות ענפית מהותית; "
            "הציון נשען על נתוני הגרף."
        )
        final_score = int(round(_clamp(
            graph_score + adjustment + calibration_adjustment
        )))
        scores[name] = {
            "graph_score": graph_score,
            "news_adjustment": adjustment,
            "calibration_adjustment": calibration_adjustment,
            "final_score": final_score,
            "label": _sector_score_label(final_score),
            "reason": reason,
        }
    return scores


def _strip_score_protocol(ai_text: str) -> str:
    return "\n".join(
        line for line in ai_text.splitlines() if not line.startswith("SCORE|")
    ).strip()


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


def _format_market_date(value: str | None) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "לא זמין"


def _hebrew_count(value, singular: str, plural: str) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 0
    return f"{count} {singular if count == 1 else plural}"


def _market_close_notice(data: dict) -> str:
    close_dates = {
        name: _format_market_date(index.get("trend", {}).get("as_of"))
        for name, index in data.get("indices", {}).items()
        if name in {"ת״א-35", "ת״א-90", "ת״א-125"}
    }
    available_dates = {date for date in close_dates.values() if date != "לא זמין"}
    if len(available_dates) == 1 and len(close_dates) == 3:
        close_date = next(iter(available_dates))
        return (
            f"**מועד הסגירה:** כל מחירי המדדים בסעיף זה מתייחסים "
            f"לסגירת המסחר ביום {close_date}."
        )
    details = "; ".join(
        f"{name}: {close_dates.get(name, 'לא זמין')}"
        for name in ("ת״א-35", "ת״א-90", "ת״א-125")
    )
    return f"**מועדי הסגירה של המדדים:** {details}."


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


def _sector_chart_summary(technical: dict) -> list[str]:
    """Explain a sector chart in plain Hebrew without assuming TA knowledge."""
    if not technical.get("available"):
        return ["**מצב הנתונים:** נתוני הגרף אינם זמינים כעת."]

    sessions = technical.get("sessions") or 0
    close_date = _format_market_date(technical.get("as_of"))
    period_note = (
        f"{sessions} ימי מסחר, בקירוב שנת מסחר מלאה"
        if sessions >= MIN_CHART_SESSIONS
        else f"{sessions} ימי מסחר בלבד; לכן הביטחון במגמה ארוכת הטווח נמוך יותר"
    )

    average_states = []
    period_names = {20: "טווח קצר", 50: "טווח בינוני", 200: "טווח ארוך"}
    for period in (20, 50, 200):
        state = technical.get(f"above_sma_{period}")
        if state is not None:
            average_states.append(
                f"{period_names[period]}: {'מעל' if state else 'מתחת'} למחיר הממוצע"
            )

    rsi = technical.get("rsi_14")
    if rsi is None:
        strength_text = "עוצמת הקונים והמוכרים אינה זמינה"
    elif rsi >= 70:
        strength_text = "העלייה חזקה מאוד, ולכן גדל הסיכוי למימוש זמני"
    elif rsi <= 30:
        strength_text = "לחץ המכירות חזק; ייתכן ניסיון התאוששות, אך הוא עדיין לא מאושר"
    else:
        strength_text = "אין כרגע מצב קיצוני של קניות או מכירות"

    macd = technical.get("macd_histogram")
    momentum_text = (
        "כיוון התנועה בטווח הקצר אינו זמין"
        if macd is None
        else (
            "התנועה בטווח הקצר מתחזקת"
            if macd > 0
            else "התנועה בטווח הקצר נחלשת"
            if macd < 0
            else "התנועה בטווח הקצר יציבה"
        )
    )
    turnover = technical.get("turnover_vs_20d_avg")
    turnover_text = (
        "היקף המסחר האחרון אינו זמין"
        if turnover is None
        else f"היקף המסחר האחרון היה פי {_format_number(turnover)} מהממוצע של 20 הימים האחרונים"
    )

    return [
        f"**תקופת הגרף:** עד סגירת {close_date}; {period_note}.",
        "**ביצועים:** חודש "
        f"{_format_pct(technical.get('return_20d_pct'))}, שלושה חודשים "
        f"{_format_pct(technical.get('return_3m_pct'))}, חצי שנה "
        f"{_format_pct(technical.get('return_6m_pct'))}, שנה "
        f"{_format_pct(technical.get('return_1y_pct'))}.",
        f"**כיוון המגמה:** {technical.get('directional_bias') or 'לא זמין'}; "
        f"{'; '.join(average_states) if average_states else 'השוואת המחיר הממוצע אינה זמינה'}.",
        f"**עוצמת התנועה:** {strength_text}; {momentum_text}.",
        "**רמות שכדאי לעקוב אחריהן:** אזור תמיכה, שבו ירידות נבלמו לאחרונה, "
        f"בסביבות {_format_number(technical.get('support_60d'))}; אזור התנגדות, שבו עליות נבלמו, "
        f"בסביבות {_format_number(technical.get('resistance_60d'))}. {turnover_text}.",
    ]


def _sector_calibration_summary(calibration: dict | None) -> str:
    calibration = calibration or {}
    horizon_labels = {10: "שבועיים", 20: "4 שבועות", 30: "6 שבועות"}
    parts = []
    for horizon in (10, 20, 30):
        stats = calibration.get("horizons", {}).get(horizon, {})
        sample_size = int(stats.get("sample_size") or 0)
        if sample_size < database.CALIBRATION_MIN_DISPLAY_CASES:
            parts.append(
                f"{horizon_labels[horizon]}: {sample_size} מקרים שהושלמו; "
                f"דרושים לפחות "
                f"{database.CALIBRATION_MIN_DISPLAY_CASES} להצגת תוצאה"
            )
            continue
        parts.append(
            f"{horizon_labels[horizon]}: סקטורים עם ציון דומה עקפו את ת״א-125 ב-"
            f"{_format_number(stats.get('hit_rate_pct'), 1)}% מתוך {sample_size} מקרים; "
            f"תשואה עודפת ממוצעת "
            f"{_format_pct(stats.get('avg_excess_return_pct'))}; "
            f"רמת ביטחון {stats.get('confidence')}"
        )
    return "; ".join(parts) + "."


def build_quantitative_cards(
    data: dict,
    sector_scores: dict | None = None,
    calibration: dict | None = None,
) -> str:
    """Build complete Hebrew chart cards independently of AI output limits."""
    sector_scores = sector_scores or _parse_sector_scores(data, "")
    calibration = calibration or {}
    sections = []
    for name in ("ת״א-35", "ת״א-90", "ת״א-125"):
        index = data.get("indices", {}).get(name, {})
        breadth = index.get("breadth", {})
        close_date = _format_market_date(index.get("trend", {}).get("as_of"))
        lines = [
            f"- **נתוני הסגירה ליום {close_date}:** המדד ננעל ברמה {_format_number(index.get('last_value'))}; שינוי יומי: {_format_pct(index.get('change_1d_pct'))}.",
            f"- **רוחב השוק:** {_hebrew_count(breadth.get('advancers'), 'עולה', 'עולות')}, "
            f"{_hebrew_count(breadth.get('decliners'), 'יורדת', 'יורדות')} ו-"
            f"{_hebrew_count(breadth.get('unchanged'), 'ללא שינוי', 'ללא שינוי')}; "
            f"משקל חמש הגדולות: {_format_number(index.get('top_5_weight_pct'))}%.",
        ]
        lines.extend(f"- **תובנת גרף:** {line}" for line in _technical_summary(index.get("trend", {})))
        lines.append(f"- [הגרף הרשמי של {name}]({index.get('chart_source')})")
        sections.append(f"### מדד {name}\n" + "\n".join(lines))

    if data.get("sectors"):
        sections.append(
            "### איך לקרוא את ציוני הסקטורים\n"
            "- **מה הציון מודד:** אטרקטיביות יחסית להשקעה כעת, באופק של 2–6 שבועות ובגישה מאוזנת.\n"
            "- **איך הוא מחושב:** ציון גרף של 0–100 המבוסס על תשואות, ממוצעי מחיר, רוחב, עוצמת התנועה ותנודתיות; ה-AI רשאי להוסיף או להפחית עד 10 נקודות רק בגלל חדשות ישראליות שסופקו למערכת.\n"
            "- **כיול על פי תוצאות אמת:** המערכת משווה כל ציון לתשואה העודפת של הסקטור מול ת״א-125 לאחר 2, 4 ו-6 שבועות. הכיול מוגבל ל-5± נקודות ומופעל רק לאחר 30 מקרים דומים שהושלמו.\n"
            "- **פירוש מהיר:** 85–100 חזקה מאוד; 70–84 חזקה; 55–69 חיובית בזהירות; 40–54 ניטרלית; מתחת ל-40 חלשה.\n"
            "- **חשוב:** זהו כלי השוואתי ולא הבטחת תשואה או המלצת השקעה אישית."
        )

    for name, sector in data.get("sectors", {}).items():
        breadth = sector.get("breadth", {})
        score = sector_scores[name]
        lines = [
            f"- **ציון אטרקטיביות להשקעה כעת: {score['final_score']}/100 – {score['label']}.** "
            f"ציון הגרף: {score['graph_score']}/100; השפעת החדשות: {score['news_adjustment']:+d} נקודות; "
            f"כיול היסטורי: {score.get('calibration_adjustment', 0):+d} נקודות. "
            f"{score['reason']}",
            "- **בדיקת הציון מול תוצאות אמת:** "
            + _sector_calibration_summary(calibration.get(name)),
            f"- **מדד הסקטור:** רמה {_format_number(sector.get('last_value'))}; שינוי יומי {_format_pct(sector.get('change_1d_pct'))}; "
            f"רוחב: {_hebrew_count(breadth.get('advancers'), 'עולה', 'עולות')}, "
            f"{_hebrew_count(breadth.get('decliners'), 'יורדת', 'יורדות')} ו-"
            f"{_hebrew_count(breadth.get('unchanged'), 'ללא שינוי', 'ללא שינוי')}.",
        ]
        lines.append("#### קריאת גרף הסקטור במילים פשוטות")
        lines.extend(f"- {line}" for line in _sector_chart_summary(sector.get("trend", {})))
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


def _score_delta(name: str, score: dict, previous_scores: dict) -> int | None:
    previous = previous_scores.get(name)
    if not previous:
        return None
    try:
        return int(score["final_score"]) - int(previous["final_score"])
    except (KeyError, TypeError, ValueError):
        return None


def _score_delta_text(delta: int | None) -> str:
    if delta is None:
        return "חדש"
    if delta == 0:
        return "ללא שינוי"
    return f"{delta:+d}"


def _score_change_reason(name: str, score: dict, previous_scores: dict) -> str:
    previous = previous_scores.get(name, {})
    if not previous:
        return "נקודת בסיס ראשונה להשוואות בדוחות הבאים"
    graph_delta = score.get("graph_score", 0) - previous.get("graph_score", 0)
    news_delta = score.get("news_adjustment", 0) - previous.get("news_adjustment", 0)
    calibration_delta = (
        score.get("calibration_adjustment", 0)
        - previous.get("calibration_adjustment", 0)
    )
    reasons = []
    if graph_delta:
        reasons.append(f"שינוי בנתוני הגרף {graph_delta:+d}")
    if news_delta:
        reasons.append(f"שינוי בהשפעת החדשות {news_delta:+d}")
    if calibration_delta:
        reasons.append(f"שינוי בכיול ההיסטורי {calibration_delta:+d}")
    return "; ".join(reasons) if reasons else "הרכב הציון לא השתנה"


def build_reader_dashboard(
    sector_scores: dict,
    previous_scores: dict | None = None,
) -> str:
    """Put changes and decisions before the detailed daily evidence."""
    previous_scores = previous_scores or {}
    ranked = sorted(
        sector_scores.items(),
        key=lambda item: item[1]["final_score"],
        reverse=True,
    )
    lines = []
    if not previous_scores:
        lines.append(
            "- **שינוי מהדוח הקודם:** זהו דוח הבסיס הראשון; "
            "מהדוח הבא יוצג כאן מה השתנה ולמה."
        )
    else:
        changes = [
            (name, score, _score_delta(name, score, previous_scores))
            for name, score in ranked
        ]
        material = sorted(
            (item for item in changes if item[2]),
            key=lambda item: abs(item[2]),
            reverse=True,
        )[:4]
        if material:
            for name, score, delta in material:
                lines.append(
                    f"- **{name}: {score['final_score']}/100 ({delta:+d}).** "
                    f"{_score_change_reason(name, score, previous_scores)}."
                )
        else:
            lines.append(
                "- **שינוי מהדוח הקודם:** לא חל שינוי בציוני הסקטורים."
            )

    leaders = ", ".join(
        f"{name} ({score['final_score']})" for name, score in ranked[:3]
    )
    laggards = ", ".join(
        f"{name} ({score['final_score']})" for name, score in ranked[-2:]
    )
    lines.extend((
        f"- **המובילים כעת:** {leaders}.",
        f"- **להמתין ולעקוב:** {laggards}.",
    ))
    return "### מה השתנה ומה חשוב הבוקר\n" + "\n".join(lines)


def _compact_trend_sentence(sector: dict) -> str:
    trend = sector.get("trend", {})
    if not trend.get("available"):
        return "נתוני הגרף אינם זמינים כעת"
    average_states = [
        trend.get(f"above_sma_{period}") for period in (20, 50, 200)
    ]
    above_count = sum(state is True for state in average_states)
    rsi = trend.get("rsi_14")
    if rsi is None:
        demand = "עוצמת הביקוש אינה זמינה"
    elif rsi >= 65:
        demand = "הביקוש חזק"
    elif rsi <= 35:
        demand = "לחץ המכירות חזק"
    else:
        demand = "הביקוש וההיצע מאוזנים יחסית"
    movement = (
        "התנועה הקצרה מתחזקת"
        if (trend.get("macd_histogram") or 0) > 0
        else "התנועה הקצרה נחלשת"
        if (trend.get("macd_histogram") or 0) < 0
        else "התנועה הקצרה יציבה"
    )
    return (
        f"חודש {_format_pct(trend.get('return_20d_pct'))}, "
        f"שלושה חודשים {_format_pct(trend.get('return_3m_pct'))}; "
        f"המחיר מעל {above_count} מתוך 3 ממוצעי המחיר; {demand}; {movement}"
    )


def _compact_calibration_sentence(calibration: dict | None) -> str:
    calibration = calibration or {}
    ready = []
    labels = {10: "שבועיים", 20: "4 שבועות", 30: "6 שבועות"}
    for horizon in (10, 20, 30):
        stats = calibration.get("horizons", {}).get(horizon, {})
        if (stats.get("sample_size") or 0) >= database.CALIBRATION_MIN_DISPLAY_CASES:
            ready.append(
                f"{labels[horizon]}: {_format_number(stats.get('hit_rate_pct'), 1)}% "
                f"הצלחה ו-{_format_pct(stats.get('avg_excess_return_pct'))} "
                f"תשואה עודפת בממוצע"
            )
    if not ready:
        return "הכיול ההיסטורי עדיין אוסף מקרים; טרם מוצגת מסקנה"
    return "; ".join(ready)


def _selected_sector_names(
    sector_scores: dict,
    previous_scores: dict,
    maximum: int = 5,
) -> list[str]:
    ranked = sorted(
        sector_scores,
        key=lambda name: sector_scores[name]["final_score"],
        reverse=True,
    )
    selected = ranked[:3]
    extras = sorted(
        (
            name for name in ranked[3:]
            if abs(_score_delta(name, sector_scores[name], previous_scores) or 0) >= 5
            or sector_scores[name].get("news_adjustment", 0) != 0
        ),
        key=lambda name: (
            abs(_score_delta(name, sector_scores[name], previous_scores) or 0),
            abs(sector_scores[name].get("news_adjustment", 0)),
        ),
        reverse=True,
    )
    for name in extras:
        if name not in selected and len(selected) < maximum:
            selected.append(name)
    return selected


def build_reader_quantitative_cards(
    data: dict,
    sector_scores: dict,
    calibration: dict | None = None,
    previous_scores: dict | None = None,
) -> str:
    """Render a compact decision-oriented email instead of a data appendix."""
    calibration = calibration or {}
    previous_scores = previous_scores or {}
    close_dates = {
        _format_market_date(index.get("trend", {}).get("as_of"))
        for index in data.get("indices", {}).values()
        if index.get("trend", {}).get("as_of")
    }
    close_note = (
        f"כל הנתונים מתייחסים לסגירת {next(iter(close_dates))}."
        if len(close_dates) == 1
        else "תאריך הסגירה מוצג בכל שורה."
    )
    index_rows = [
        "| מדד | סגירה | יומי | חודש | מגמה |",
        "|---|---:|---:|---:|---|",
    ]
    for name in ("ת״א-35", "ת״א-90", "ת״א-125"):
        index = data.get("indices", {}).get(name, {})
        trend = index.get("trend", {})
        index_rows.append(
            f"| [{name}]({index.get('chart_source')}) | "
            f"{_format_number(index.get('last_value'))} | "
            f"{_format_pct(index.get('change_1d_pct'))} | "
            f"{_format_pct(trend.get('return_20d_pct'))} | "
            f"{trend.get('directional_bias') or 'לא זמין'} |"
        )
    index_card = "### מדדי תל אביב – תמונת סגירה\n" + close_note + "\n" + "\n".join(index_rows)

    ranked = sorted(
        sector_scores,
        key=lambda name: sector_scores[name]["final_score"],
        reverse=True,
    )
    ranking_rows = [
        "| # | סקטור | ציון | שינוי בציון | מגמת חודש |",
        "|---:|---|---:|---:|---:|",
    ]
    for position, name in enumerate(ranked, 1):
        sector = data.get("sectors", {}).get(name, {})
        score = sector_scores[name]
        ranking_rows.append(
            f"| {position} | {name} | {score['final_score']} – {score['label']} | "
            f"{_score_delta_text(_score_delta(name, score, previous_scores))} | "
            f"{_format_pct(sector.get('trend', {}).get('return_20d_pct'))} |"
        )
    calibration_ready = any(
        (stats.get("sample_size") or 0)
        >= database.CALIBRATION_MIN_ADJUSTMENT_CASES
        for item in calibration.values()
        for stats in item.get("horizons", {}).values()
    )
    calibration_note = (
        "הציון כולל כיול היסטורי מוגבל של 5± נקודות."
        if calibration_ready
        else "הכיול ההיסטורי עדיין אוסף מקרים ואינו משנה את הציונים כעת."
    )
    ranking_card = (
        "### דירוג כל הסקטורים\n"
        "הציון מודד אטרקטיביות יחסית ל-2–6 שבועות; הוא אינו הבטחת תשואה. "
        + calibration_note + "\n" + "\n".join(ranking_rows)
    )

    detail_lines = []
    for name in _selected_sector_names(sector_scores, previous_scores):
        sector = data.get("sectors", {}).get(name, {})
        score = sector_scores[name]
        detail_lines.extend((
            f"#### {name} – {score['final_score']}/100",
            f"- **למה הסקטור נבחר:** {_compact_trend_sentence(sector)}. {score['reason']}",
            f"- **מרכיבי הציון:** גרף {score['graph_score']}; חדשות {score['news_adjustment']:+d}; כיול {score.get('calibration_adjustment', 0):+d}. {_compact_calibration_sentence(calibration.get(name))}.",
        ))
        stocks = []
        for stock in sector.get("top_stocks_by_market_cap", []):
            technical = stock.get("technical_analysis", {})
            stocks.append(
                f"[{stock.get('name')} ({stock.get('symbol')})]({stock.get('chart_source')}): "
                f"יומי {_format_pct(stock.get('change_1d_pct'))}, "
                f"חודש {_format_pct(technical.get('return_20d_pct'))}"
            )
        detail_lines.append(
            "- **שלוש הגדולות:** " + "; ".join(stocks) + "."
        )
        detail_lines.append(
            f"- [גרף הסקטור הרשמי]({sector.get('chart_source')})"
        )
    detail_card = "### העמקה בסקטורים הרלוונטיים\n" + "\n".join(detail_lines)
    return "\n\n".join((index_card, ranking_card, detail_card))


def build_reader_context_card(data: dict, sector_scores: dict) -> str:
    """Show only news and currency moves that materially affect the reader."""
    lines = []
    affected = [
        (name, score) for name, score in sector_scores.items()
        if score.get("news_adjustment", 0)
    ]
    if affected:
        for name, score in sorted(
            affected,
            key=lambda item: abs(item[1]["news_adjustment"]),
            reverse=True,
        ):
            lines.append(
                f"- **{name} ({score['news_adjustment']:+d}):** {score['reason']}"
            )
    else:
        lines.append(
            "- **חדשות:** לא זוהתה היום כותרת ששינתה ציון סקטוריאלי."
        )

    material_rates = []
    for currency in ("USD", "EUR", "GBP"):
        rate = data.get("exchange_rates", {}).get("rates", {}).get(currency, {})
        try:
            change = float(rate.get("change_pct"))
        except (TypeError, ValueError):
            continue
        if abs(change) >= 0.5:
            material_rates.append(f"{currency}/ILS {_format_pct(change)}")
    if material_rates:
        lines.append("- **שערי מט״ח מהותיים:** " + ", ".join(material_rates) + ".")
    else:
        lines.append("- **שערי מט״ח:** לא נרשמה תנועה יומית של 0.5% או יותר בדולר, באירו או בליש״ט.")

    source_links = []
    ta125 = data.get("indices", {}).get("ת״א-125", {})
    if ta125.get("source"):
        source_links.append(f"[הבורסה לניירות ערך]({ta125['source']})")
    boi_source = data.get("exchange_rates", {}).get("source")
    if boi_source:
        source_links.append(f"[בנק ישראל]({boi_source})")
    maya = data.get("maya_announcements", [])
    if maya and maya[0].get("link"):
        source_links.append(f"[מאיה]({maya[0]['link']})")
    seen_sources = set()
    for item in data.get("news", []):
        source = item.get("source")
        if source and source not in seen_sources and item.get("link"):
            source_links.append(f"[{source}]({item['link']})")
            seen_sources.add(source)
        if len(seen_sources) >= 4:
            break
    lines.append("- **מקורות מרכזיים:** " + ", ".join(source_links) + ".")
    return "### חדשות, מאקרו ומקורות\n" + "\n".join(lines)


def validate_market_data(data: dict) -> None:
    """Refuse to generate or send a briefing from partial critical market data."""
    problems = []
    for name in ("ת״א-35", "ת״א-90", "ת״א-125"):
        index = data.get("indices", {}).get(name, {})
        if not index.get("constituents_count"):
            problems.append(f"{name}: constituents missing")
        if (index.get("trend", {}).get("sessions") or 0) < MIN_CHART_SESSIONS:
            problems.append(f"{name}: one-year chart missing")
        if not index.get("trend", {}).get("as_of"):
            problems.append(f"{name}: closing date missing")

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


def generate_hebrew_brief(
    data: dict,
    calibration: dict | None = None,
    previous_scores: dict | None = None,
    score_observer=None,
) -> str:
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

כללים: אין הקדמה, פרופיל משקיע, טבלה, מידע חיצוני, מספר מומצא, הבטחת תשואה או הוראת קנייה. הפרד עובדה מתחזית. n<200 פירושו היסטוריה קצרה משנה וביטחון מופחת. התייחס לתשואות 1/3/6/12 חודשים, לממוצעי המחיר, לעוצמת הקונים והמוכרים, לכיוון התנועה, לתמיכה ולהתנגדות. הנתונים הטכניים הם אינדיקציה בלבד.

כתוב לקורא ללא רקע בניתוח טכני, עד 220 מילים ובדיוק את הסעיפים הבאים:
### תמונת מצב בבורסה בתל אביב
סיכום הסגירה והמסר המרכזי. אל תציין תאריך בעצמך; המערכת תוסיף את תאריך הסגירה הרשמי.

### מבט להמשך
חלק לשתי פסקאות: "הימים הקרובים" (1–5 ימי מסחר) ו"השבועות הקרובים" (2–6 שבועות). בכל פסקה השתמש בארבעה משפטים קצרים עם התוויות: **הכיוון הסביר**, **מה יכול לשפר את המצב**, **מה עלול להחליש את השוק**, **מתי נשנה את ההערכה**. הסבר את הסיבה במילים יומיומיות.

אסור להשתמש בלי הסבר במילים טריגר, מומנטום, אישור, ביטול, שורי, דובי, RSI, MACD או SMA. אם חייבים לציין מדד טכני, כתוב קודם את המשמעות הפשוטה ורק אחר כך את שמו בסוגריים. לדוגמה: "המחיר נשאר מעל הממוצע של 50 הימים האחרונים (SMA-50)".

מקרא: ret_1m_3m_6m_1y_pct=תשואות; above_sma_20_50_200=מיקום מעל הממוצעים; macd_hist=היסטוגרמת MACD; support_resistance_60d=תמיכה/התנגדות; turnover_x_avg20=מחזור יחסי; trendline_60d_200d_pct=קווי מגמה.

נתונים:
{market_data}"""

    sector_overview = json.dumps(
        _compact_sector_overview(data, calibration),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sector_news = json.dumps(
        _compact_sector_news(data),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sector_prompt = f"""אתה אנליסט סקטורים של הבורסה בתל אביב. כתוב בעברית תקנית, קצרה וברורה.

לכל סקטור כבר חושב graph_score_0_100 שקוף לפי ביצועי חודש/3/6/12 חודשים, רוחב, תנודתיות, ממוצעי מחיר, RSI, MACD ומגמה. historical_calibration_adjustment הוא כיול מוגבל של 5± נקודות על סמך מקרים היסטוריים שהושלמו. נתח את כותרות החדשות הישראליות שסופקו וקבע news_adjustment בין 10- ל-10+ בלבד. אם אין כותרת שקשורה ישירות לסקטור או לאחת המניות הגדולות בו, ההתאמה חייבת להיות 0. אל תשתמש בידע חיצוני ואל תמציא קשר סיבתי.

בתחילת התשובה כתוב בדיוק עשר שורות מכונה, אחת לכל סקטור ובשמות שסופקו, בפורמט הבא וללא Markdown:
SCORE|שם הסקטור|התאמת חדשות כמספר שלם בין 10- ל-10+|הסבר עברי קצר שמציין את החדשה או שאין חדשות מהותיות

הציון הסופי הוא graph_score_0_100 ועוד התאמת החדשות ועוד historical_calibration_adjustment, מוגבל ל-0–100. לאחר עשר שורות SCORE, דרג את הסקטורים לפי הציון הסופי.

כתוב לאחר שורות המכונה עד 170 מילים ובדיוק שני סעיפים. אין להשתמש בטבלה. כתוב רק את התבליטים המוגדרים:
### סקטורים בולטים ותובנות AI
- **מועדף – שם הסקטור והציון:** משפט אחד עם הסבר פשוט, סיכון ומה ישנה את ההערכה.
- **מעקב – שם הסקטור והציון:** משפט אחד באותו מבנה.
- **להמתין – שם הסקטור והציון:** משפט אחד באותו מבנה.
- **חלשים:** משפט אחד שמציין את הסקטורים החלשים והסיבה.

### מבט סקטוריאלי להמשך
- **תרחיש בסיס:** משפט אחד, מובילים ורמת ביטחון.
- **תרחיש חיובי:** משפט אחד, האירוע שישפר את המצב והסקטורים שיובילו.
- **תרחיש שלילי:** משפט אחד, האירוע שיחליש את המצב והסקטורים הפגיעים.

מקרא: ret_1m_3m_6m_1y_pct = תשואות חודש/3/6/12 חודשים; above_sma_20_50_200 = מעל ממוצעים 20/50/200; macd_hist = היסטוגרמת MACD; trendline_60d_200d_pct = קווי מגמה.

נתונים:
סקטורים וגרפים: {sector_overview}
חדשות שסופקו: {sector_news}"""

    market_brief = _groq_completion(
        client, model, market_prompt, max_tokens=1200, purpose="market"
    )
    _require_ai_sections(market_brief, ("תמונת מצב בבורסה בתל אביב", "מבט להמשך"))
    market_brief = market_brief.replace(
        "### תמונת מצב בבורסה בתל אביב",
        "### תמונת מצב בבורסה בתל אביב\n" + _market_close_notice(data) + "\n",
        1,
    )
    sector_brief_raw = _groq_completion(
        client, model, sector_prompt, max_tokens=1100, purpose="sectors"
    )
    _require_ai_sections(
        sector_brief_raw,
        ("סקטורים בולטים ותובנות AI", "מבט סקטוריאלי להמשך"),
    )
    sector_scores = _parse_sector_scores(data, sector_brief_raw, calibration)
    if score_observer is not None:
        score_observer(sector_scores)
    sector_brief = _strip_score_protocol(sector_brief_raw)
    dashboard = build_reader_dashboard(sector_scores, previous_scores)
    quantitative_cards = build_reader_quantitative_cards(
        data,
        sector_scores,
        calibration,
        previous_scores,
    )
    context_card = build_reader_context_card(data, sector_scores)
    return "\n\n".join([
        market_brief,
        dashboard,
        sector_brief,
        quantitative_cards,
        context_card,
    ])


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
    th,td {{ border:1px solid #c8d8ea; padding:8px 9px; vertical-align:top; direction:rtl; text-align:right; unicode-bidi:plaintext; }}
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
    tracking = {
        "close_rows_upserted": 0,
        "outcomes_settled": 0,
        "predictions_upserted": 0,
    }
    if send:
        tracking.update(database.record_market_close_history(data))
        tracking.update(database.settle_sector_score_outcomes())

    graph_scores = {
        name: calculate_sector_graph_score(sector)
        for name, sector in data.get("sectors", {}).items()
    }
    calibration = database.get_sector_score_calibration(graph_scores)
    previous_scores = database.get_latest_sector_scores()
    generated_scores = {}
    brief_text = generate_hebrew_brief(
        data,
        calibration=calibration,
        previous_scores=previous_scores,
        score_observer=generated_scores.update,
    )
    if send:
        tracking.update(database.record_sector_score_predictions(
            data, generated_scores
        ))
    result = {
        "as_of": data.get("as_of"),
        "collection_stats": data.get("collection_stats", {}),
        "backup": backup_status,
        "score_tracking": tracking,
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
