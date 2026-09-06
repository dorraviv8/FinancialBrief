#!/usr/bin/env python3
"""Daily AI briefing focused exclusively on the Israeli stock market."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from difflib import SequenceMatcher
import fcntl
import hashlib
import html
import json
import os
import re
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from groq import Groq

import database
import recommendation_v2
import weekly_report
from israel_market import collect_israeli_market_data, get_boi_exchange_rate_history


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


def _delivery_lock_path() -> str:
    configured = os.getenv("FINANCIAL_BRIEF_LOCK_PATH")
    if configured:
        return configured
    return os.path.join(
        os.path.dirname(os.path.abspath(database.DB_PATH)),
        "financial-brief.lock",
    )


@contextmanager
def _delivery_process_lock():
    """Prevent scheduled and manual delivery processes from overlapping."""
    lock_path = _delivery_lock_path()
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    handle = open(lock_path, "a", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another FinancialBrief delivery process is already running"
            ) from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _daily_message_id(report_date: str, recipient_email: str) -> str:
    """Return a stable RFC-style ID for one report/recipient delivery."""
    normalized_email = recipient_email.strip().lower()
    digest = hashlib.sha256(
        f"{report_date}\0{normalized_email}".encode("utf-8")
    ).hexdigest()[:24]
    sender_domain = (
        GMAIL_USER.rsplit("@", 1)[1]
        if GMAIL_USER and "@" in GMAIL_USER
        else "financialbrief.local"
    )
    return f"<financialbrief-{report_date}-{digest}@{sender_domain}>"


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
    v2_bundle: dict | None = None,
) -> dict:
    calibration = calibration or {}
    v2_bundle = v2_bundle or {}
    return {
        "as_of": data.get("as_of"),
        "sectors": {
            name: {
                "graph_score_0_100": calculate_sector_graph_score(sector),
                "largest_stocks": [
                    stock.get("symbol") or stock.get("name")
                    for stock in sector.get("top_stocks_by_market_cap", [])
                ],
                "v2_shadow": {
                    "horizon_scores_10_20_30": [
                        v2_bundle.get("sectors", {})
                        .get(name, {})
                        .get("horizons", {})
                        .get(horizon, {})
                        .get("final_score")
                        for horizon in recommendation_v2.HORIZONS
                    ],
                    "probability_positive_20d_pct": (
                        v2_bundle.get("sectors", {})
                        .get(name, {})
                        .get("horizons", {})
                        .get(20, {})
                        .get("probability_positive_pct")
                    ),
                    "probability_outperform_20d_pct": (
                        v2_bundle.get("sectors", {})
                        .get(name, {})
                        .get("horizons", {})
                        .get(20, {})
                        .get("probability_outperform_pct")
                    ),
                    "risk_quality": (
                        v2_bundle.get("sectors", {})
                        .get(name, {})
                        .get("risk_quality")
                    ),
                    "price_state": {
                        key: v2_bundle.get("sectors", {})
                        .get(name, {})
                        .get("features", {})
                        .get(key)
                        for key in (
                            "relative_return_20d_pct",
                            "relative_return_63d_pct",
                            "sector_drawdown_60d_pct",
                        )
                    },
                },
            }
            for name, sector in data.get("sectors", {}).items()
        },
    }


def _compact_sector_news(data: dict) -> list[dict]:
    items = []
    for position, item in enumerate(data.get("maya_announcements", [])[:5]):
        items.append({
            "id": f"M{position}",
            "source": item.get("source"),
            "reliability": item.get("reliability"),
            "title": item.get("title"),
            "companies": item.get("companies", []),
            "published": item.get("published"),
        })
    for position, item in enumerate(data.get("news", [])[:8]):
        items.append({
            "id": f"N{position}",
            "source": item.get("source"),
            "reliability": item.get("reliability"),
            "title": item.get("title"),
            "published": item.get("published"),
        })
    return items


def _parse_published_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ISRAEL_TZ)
        return parsed.astimezone(ISRAEL_TZ)
    except (TypeError, ValueError):
        return None


SECTOR_NEWS_KEYWORDS = {
    "טכנולוגיה": ("טכנולוג", "סייבר", "תוכנה", "שבבים", "מוליכים למחצה"),
    "ביומד": ("ביומד", "פארמה", "תרופ", "רפוא", "טבע", "בריינסוויי"),
    "בנקים": ("בנק", "לאומי", "פועלים", "מזרחי", "דיסקונט"),
    "ביטוח ושירותים פיננסיים": (
        "ביטוח", "פיננס", "אשראי", "הפניקס", "הראל", "מנורה"
    ),
    "נדל״ן ובנייה": (
        "נדל", "בנייה", "בניה", "דיור", "דירות", "אאורה", "עזריאלי", "מליסרון"
    ),
    "תעשייה": ("תעש", "מפעל", "ייצור", "יצרנית"),
    "מסחר ושירותים": ("קמעונ", "מסחר", "שירותים", "רשת חנויות"),
    "השקעות ואחזקות": ("אחזקות", "החזקות", "חברת השקעות"),
    "אנרגיה ותשתיות": (
        "אנרג", "תשתיות", "חשמל", "אנלייט", "או פי סי", "אורמת"
    ),
    "נפט וגז": (
        "נפט", "גז טבעי", "קידוח", "ניו מד", "ניו-מד", "נאוויטס",
        "קבוצת דלק", "דלק קבוצה"
    ),
}


def _normalized_relevance_text(value) -> str:
    return " ".join(
        re.sub(r"[^0-9a-zA-Zא-ת]+", " ", str(value or "").lower()).split()
    )


def _news_item_relevant_to_sector(data: dict, sector_name: str, item: dict) -> bool:
    """Reject plausible-sounding but unsupported AI sector/news associations."""
    text_parts = [item.get("title")]
    text_parts.extend(item.get("companies") or [])
    haystack = _normalized_relevance_text(" ".join(
        str(part) for part in text_parts if part
    ))
    if not haystack:
        return False
    if any(
        _normalized_relevance_text(keyword) in haystack
        for keyword in SECTOR_NEWS_KEYWORDS.get(sector_name, ())
    ):
        return True
    sector = data.get("sectors", {}).get(sector_name, {})
    for stock in sector.get("top_stocks_by_market_cap", []):
        for identifier in (stock.get("name"), stock.get("symbol")):
            normalized = _normalized_relevance_text(identifier)
            if len(normalized) >= 4 and normalized in haystack:
                return True
    return False


def _parse_news_catalysts(data: dict, ai_text: str) -> dict:
    """Convert AI event classification into bounded, source-aware points."""
    news_items = _compact_sector_news(data)
    by_id = {item["id"]: item for item in news_items}
    catalysts = {}
    for line in ai_text.splitlines():
        if not line.startswith("CATALYST|"):
            continue
        parts = line.split("|", 7)
        if len(parts) == 8:
            (
                _marker,
                sector_name,
                item_id,
                event_type,
                direction_text,
                materiality_text,
                duration_text,
                reason,
            ) = parts
        elif len(parts) == 7:
            # Compatibility with the first V2 protocol used in fixtures.
            _marker, sector_name, item_id, direction_text, materiality_text, duration_text, reason = parts
            event_type = "other"
        else:
            continue
        sector_name = sector_name.strip()
        item_id = item_id.strip().upper()
        event_type = event_type.strip().lower()
        allowed_event_types = {
            "earnings", "guidance", "regulatory", "rates", "currency",
            "commodity", "corporate", "macro", "other", "none",
        }
        if event_type not in allowed_event_types:
            event_type = "other"
        if sector_name not in data.get("sectors", {}):
            continue
        if item_id == "NONE":
            catalysts[sector_name] = {
                "adjustment": 0,
                "reason": reason.strip()[:280] or "לא זוהה אירוע מהותי.",
                "news_id": None,
                "event_type": "none",
            }
            continue
        item = by_id.get(item_id)
        if not item:
            continue
        try:
            direction = max(-1, min(1, int(direction_text.strip())))
            materiality = max(0, min(5, int(materiality_text.strip())))
            duration_days = max(1, min(30, int(duration_text.strip())))
        except ValueError:
            continue
        if not _news_item_relevant_to_sector(data, sector_name, item):
            continue
        source_text = f"{item.get('title') or ''} {item.get('summary') or ''}"
        clean_reason = weekly_report.clean_ai_text(reason).replace("**", "")[:280]
        if not weekly_report.translation_preserves_source_facts(
            source_text, clean_reason
        ):
            clean_reason = (
                f"לפי כותרת המקור: {str(item.get('title') or '').strip()}"
            )[:280]
        reliability_weight = 1.0 if item.get("reliability") == "official" else 0.7
        published = _parse_published_time(item.get("published"))
        age_days = max(
            0.0,
            ((_today() - published).total_seconds() / 86400) if published else 1.0,
        )
        freshness = 0.5 ** (age_days / duration_days)
        adjustment = int(round(
            direction * materiality * reliability_weight * freshness
        ))
        catalysts[sector_name] = {
            "adjustment": max(-5, min(5, adjustment)),
            "direction": direction,
            "materiality": materiality,
            "duration_days": duration_days,
            "reliability": item.get("reliability"),
            "source": item.get("source"),
            "news_id": item_id,
            "event_type": event_type,
            "title": item.get("title"),
            "reason": clean_reason,
            "relevance_verified": True,
        }

    # Backward-compatible fallback for saved fixtures and older model output.
    if not catalysts:
        for line in ai_text.splitlines():
            if not line.startswith("SCORE|"):
                continue
            parts = line.split("|", 3)
            if len(parts) != 4 or parts[1].strip() not in data.get("sectors", {}):
                continue
            try:
                adjustment = max(-5, min(5, int(parts[2].strip())))
            except ValueError:
                continue
            catalysts[parts[1].strip()] = {
                "adjustment": adjustment,
                "reason": parts[3].strip()[:280],
                "news_id": None,
            }
    for sector_name in data.get("sectors", {}):
        catalysts.setdefault(sector_name, {
            "adjustment": 0,
            "reason": "לא זוהה אירוע חדשותי מהותי ומאומת לסקטור.",
            "news_id": None,
        })
    return catalysts


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


def _legacy_scores_from_catalysts(
    data: dict,
    catalysts: dict,
    calibration: dict | None = None,
) -> dict:
    """Keep V1 reproducible while replacing subjective AI points with catalysts."""
    calibration = calibration or {}
    scores = {}
    for name, sector in data.get("sectors", {}).items():
        graph_score = calculate_sector_graph_score(sector)
        news_adjustment = int(max(
            -5, min(5, catalysts.get(name, {}).get("adjustment", 0))
        ))
        calibration_adjustment = int(max(
            -5, min(5, calibration.get(name, {}).get("adjustment", 0))
        ))
        final_score = int(round(_clamp(
            graph_score + news_adjustment + calibration_adjustment
        )))
        scores[name] = {
            "graph_score": graph_score,
            "news_adjustment": news_adjustment,
            "calibration_adjustment": calibration_adjustment,
            "final_score": final_score,
            "legacy_final_score": final_score,
            "label": _sector_score_label(final_score),
            "reason": catalysts.get(name, {}).get("reason") or (
                "לא זוהה אירוע חדשותי מהותי ומאומת לסקטור."
            ),
        }
    return scores


def _combine_recommendation_scores(
    legacy_scores: dict,
    v2_bundle: dict,
    model_status: dict,
) -> dict:
    active_model = model_status.get("active_model", "v1")
    combined = {}
    for name, legacy in legacy_scores.items():
        item = dict(legacy)
        v2 = v2_bundle.get("sectors", {}).get(name, {})
        primary = v2.get("horizons", {}).get(20, {})
        item.update({
            "active_model": active_model,
            "v2_score": primary.get("final_score", 50),
            "v2_confidence_pct": primary.get("confidence_pct", 0),
            "v2_probability_positive_pct": primary.get(
                "probability_positive_pct", 50
            ),
            "v2_probability_outperform_pct": primary.get(
                "probability_outperform_pct", 50
            ),
            "v2_expected_excess_return_pct": primary.get(
                "expected_excess_return_pct", 0
            ),
            "v2_expected_excess_low_pct": primary.get(
                "expected_excess_low_pct"
            ),
            "v2_expected_excess_high_pct": primary.get(
                "expected_excess_high_pct"
            ),
            "v2_risk_quality": primary.get("risk_quality", 50),
            "v2_live_sample_size": model_status.get("comparison", {}).get(
                "sample_size", 0
            ),
            "v2_backtest_sample_size": v2_bundle.get("evaluation", {})
            .get(20, {})
            .get("sample_count", 0),
            "v2_backtest_brier_skill_pct": v2_bundle.get("evaluation", {})
            .get(20, {})
            .get("outperform_brier_skill_pct"),
            "v2_backtest_top_three_excess_pct": v2_bundle.get("evaluation", {})
            .get(20, {})
            .get("top_three_avg_excess_return_pct"),
            "v2_horizon_scores": {
                horizon: v2.get("horizons", {}).get(horizon, {}).get(
                    "final_score", 50
                )
                for horizon in recommendation_v2.HORIZONS
            },
        })
        if active_model == "v2":
            item["final_score"] = int(item["v2_score"])
            item["label"] = _sector_score_label(item["final_score"])
        combined[name] = item
    return combined


def _strip_score_protocol(ai_text: str) -> str:
    return "\n".join(
        line for line in ai_text.splitlines()
        if not line.startswith(("SCORE|", "CATALYST|"))
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


def build_market_in_60_seconds(
    data: dict,
    sector_scores: dict,
    previous_scores: dict | None = None,
) -> str:
    """Give the reader one concise, deterministic decision summary."""
    previous_scores = previous_scores or {}
    indices = data.get("indices", {})
    ta125 = indices.get("ת״א-125", {})
    ta125_trend = ta125.get("trend", {})
    ranked_indices = sorted(
        (
            (name, index)
            for name, index in indices.items()
            if name in {"ת״א-35", "ת״א-90", "ת״א-125"}
        ),
        key=lambda item: float(item[1].get("change_1d_pct") or -999),
        reverse=True,
    )
    leading_index = ranked_indices[0] if ranked_indices else ("לא זמין", {})
    ranked_sectors = sorted(
        sector_scores.items(),
        key=lambda item: item[1].get("final_score", 50),
        reverse=True,
    )
    leaders = ", ".join(
        f"{name} ({score.get('final_score', 50)})"
        for name, score in ranked_sectors[:3]
    ) or "לא זמינים"
    score_changes = [
        (name, score, _score_delta(name, score, previous_scores))
        for name, score in ranked_sectors
    ]
    score_changes = [item for item in score_changes if item[2] is not None]
    largest_change = max(
        score_changes,
        key=lambda item: abs(item[2]),
        default=None,
    )
    if largest_change and largest_change[2]:
        change_text = (
            f"{largest_change[0]} עבר ל-{largest_change[1]['final_score']}/100 "
            f"({largest_change[2]:+d})"
        )
    elif largest_change:
        change_text = "לא חל שינוי מהותי בציוני הסקטורים"
    else:
        change_text = "זהו בסיס ההשוואה הראשון לציונים"

    affected = sorted(
        (
            (name, score) for name, score in sector_scores.items()
            if score.get("news_adjustment", 0)
        ),
        key=lambda item: abs(item[1]["news_adjustment"]),
        reverse=True,
    )
    news_text = (
        f"{affected[0][0]} ({affected[0][1]['news_adjustment']:+d}): "
        f"{str(affected[0][1]['reason']).rstrip('.')}"
        if affected else "לא זוהתה כותרת מאומתת ששינתה הבוקר ציון סקטוריאלי"
    )
    market_bias = ta125_trend.get("directional_bias") or "לא זמינה"
    strongest_score = ranked_sectors[0][1].get("final_score", 50) if ranked_sectors else 50
    bottom_line = (
        "התמונה חיובית יחסית, אך כדאי לוודא שהעליות נשארות רחבות לפני הגדלת חשיפה."
        if market_bias == "חיובית" and strongest_score >= 70
        else "התמונה מעורבת; עדיף להתמקד בסקטורים המובילים ולשמור על משמעת סיכון."
        if strongest_score >= 55
        else "התמונה חלשה יחסית; עדיף להמתין לשיפור במדד ובציוני הסקטורים."
    )
    return (
        "### השוק ב-60 שניות\n"
        f"- **מועד הנתונים:** סגירת {_format_market_date(ta125_trend.get('as_of'))}.\n"
        f"- **תמונת השוק:** ת״א-125 ברמה {_format_number(ta125.get('last_value'))}, "
        f"שינוי יומי {_format_pct(ta125.get('change_1d_pct'))} וחודשי "
        f"{_format_pct(ta125_trend.get('return_20d_pct'))}; המגמה הכמותית {market_bias}.\n"
        f"- **המדד המוביל היום:** {leading_index[0]} "
        f"({_format_pct(leading_index[1].get('change_1d_pct'))}).\n"
        f"- **הסקטורים המובילים כעת:** {leaders}.\n"
        f"- **השינוי הבולט בציונים:** {change_text}.\n"
        f"- **החדשות ששינו את התמונה:** {news_text}.\n"
        f"- **שורה תחתונה:** {bottom_line}"
    )


def build_plain_language_market_outlook(data: dict) -> str:
    """Create a clear outlook from verified index fields without translation."""
    indices = data.get("indices", {})
    ta125 = indices.get("ת״א-125", {})
    trend = ta125.get("trend", {})
    bias = trend.get("directional_bias") or "ניטרלית"
    support = _format_number(trend.get("support_60d"))
    resistance = _format_number(trend.get("resistance_60d"))
    rsi = trend.get("rsi_14")
    breadth = ta125.get("breadth", {})
    advancers = int(breadth.get("advancers") or 0)
    decliners = int(breadth.get("decliners") or 0)
    breadth_risk = (
        " במקביל, מספר המניות היורדות גדול כעת ממספר המניות העולות."
        if decliners > advancers else ""
    )
    short_direction = {
        "חיובית": "הנטייה החיובית עשויה להימשך כל עוד ת״א-125 נשאר מעל אזור התמיכה.",
        "שלילית": "הלחץ עשוי להימשך עד שיופיע שיפור ברור ברוחב השוק ובמחיר המדד.",
    }.get(bias, "המסחר עשוי להישאר מעורב עד שייווצר כיוון ברור יותר במדד וברוחב השוק.")
    strength_risk = (
        f"עוצמת הקניות גבוהה יחסית (RSI-14 ברמה {_format_number(rsi, 1)}), ולכן ייתכן מימוש זמני."
        if isinstance(rsi, (int, float)) and rsi >= 70 else
        f"לחץ המכירות גבוה יחסית (RSI-14 ברמה {_format_number(rsi, 1)}), ולכן נדרש אישור לפני הסקת התאוששות."
        if isinstance(rsi, (int, float)) and rsi <= 30 else
        "לא נרשם מצב קיצוני בעוצמת הקניות או המכירות."
    )
    averages_above = sum(
        trend.get(f"above_sma_{period}") is True for period in (20, 50, 200)
    )
    medium_direction = (
        f"ת״א-125 נמצא מעל {averages_above} מתוך שלושת ממוצעי המחיר המרכזיים; "
        f"התשואה בשלושת החודשים האחרונים היא {_format_pct(trend.get('return_3m_pct'))}."
    )
    return (
        "### מבט להמשך\n"
        "#### הימים הקרובים\n"
        f"- **הכיוון הסביר:** {short_direction}\n"
        f"- **מה יכול לשפר את המצב:** סגירה מעל אזור ההתנגדות {resistance}, לצד יותר מניות עולות מיורדות, תחזק את ההערכה החיובית.\n"
        f"- **מה עלול להחליש את השוק:** {strength_risk}{breadth_risk}\n"
        f"- **מתי נשנה את ההערכה:** סגירה מתחת לאזור התמיכה {support} תחליש את התרחיש הנוכחי.\n"
        "#### השבועות הקרובים\n"
        f"- **הכיוון הסביר:** {medium_direction}\n"
        "- **מה יכול לשפר את המצב:** הישארות מעל ממוצעי 50 ו-200 הימים, לצד עליות ביותר מניות, תתמוך בהמשך המגמה.\n"
        "- **מה עלול להחליש את השוק:** היחלשות בו-זמנית במדד, ברוחב השוק ובסקטורים המובילים תגדיל את הסיכון לשינוי מגמה.\n"
        "- **מתי נשנה את ההערכה:** מעבר של המחיר מתחת לממוצע 200 הימים ומגמה כמותית שלילית יחייבו הערכה מחודשת."
    )


def build_sector_recommendation_summary(
    sector_scores: dict,
    v2_bundle: dict,
) -> str:
    """Render decision prose from verified model fields, not free-form AI text."""
    ranked = sorted(
        sector_scores,
        key=lambda name: sector_scores[name].get("v2_score", 50),
        reverse=True,
    )
    if not ranked:
        return (
            "### סקטורים בולטים ותובנות AI\n"
            "- אין נתוני סקטורים זמינים.\n\n"
            "### מבט סקטוריאלי להמשך\n"
            "- אין בסיס מספק לתרחיש סקטוריאלי."
        )

    def evidence(name: str) -> str:
        score = sector_scores[name]
        catalyst = v2_bundle.get("sectors", {}).get(name, {}).get("catalyst", {})
        catalyst_text = (
            f" אירוע מאומת: {catalyst.get('reason')}"
            if catalyst.get("adjustment")
            else " לא זוהה אירוע חדשותי מאומת ששינה את הציון."
        )
        return (
            f"V2 {score.get('v2_score', 50)}/100, סיכוי לתשואה חיובית "
            f"{_format_number(score.get('v2_probability_positive_pct'), 1)}%, "
            f"סיכוי לעקוף את ת״א-125 "
            f"{_format_number(score.get('v2_probability_outperform_pct'), 1)}%, "
            f"ואיכות סיכון {score.get('v2_risk_quality', 50)}/100."
            f"{catalyst_text}"
        )

    preferred = ranked[0]
    watch = ranked[1] if len(ranked) > 1 else ranked[0]
    wait = min(
        (name for name in ranked if name not in {preferred, watch}),
        key=lambda name: abs(sector_scores[name].get("v2_score", 50) - 50),
        default=ranked[-1],
    )
    weak = ranked[-2:] if len(ranked) >= 2 else ranked
    weak_text = ", ".join(
        f"{name} ({sector_scores[name].get('v2_score', 50)})" for name in weak
    )

    preferred_horizons = sector_scores[preferred].get("v2_horizon_scores", {})
    near_score = preferred_horizons.get(10, 50)
    long_score = preferred_horizons.get(30, 50)
    durability = (
        "היתרון מתחזק באופק הארוך יותר"
        if long_score >= near_score + 3
        else "היתרון חזק יותר בטווח הקרוב"
        if near_score >= long_score + 3
        else "ההערכה דומה בין שבועיים לשישה שבועות"
    )
    return "\n\n".join((
        "### סקטורים בולטים ותובנות AI\n"
        f"- **מועדף לבדיקה – {preferred}:** {evidence(preferred)}\n"
        f"- **מעקב – {watch}:** {evidence(watch)}\n"
        f"- **ניטרלי כעת – {wait}:** {evidence(wait)}\n"
        f"- **החלשים ב-V2:** {weak_text}; הציון המשולב שלהם נמוך יחסית לשאר הסקטורים.",
        "### מבט סקטוריאלי להמשך\n"
        f"- **תרחיש בסיס:** {preferred} ו{watch} מובילים כעת; {durability}.\n"
        "- **תרחיש חיובי:** שיפור ברוחב הסקטורים ועלייה בסיכוי לעקוף את ת״א-125 "
        "במדידה הבאה יחזקו את ההערכה.\n"
        "- **תרחיש שלילי:** היחלשות בשלוש המניות הגדולות, עלייה בתנודתיות או "
        "ירידה בהסתברות לתשואה חיובית יפחיתו את הציונים.",
    ))


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


def _weekly_catalysts_from_analysis(analysis: dict) -> dict:
    """Turn source-linked weekly AI classifications into bounded score inputs."""
    catalysts = {}
    for sector_name, factor in analysis.get("sector_factors", {}).items():
        try:
            direction = max(-1, min(1, int(factor.get("direction", 0))))
            materiality = max(0, min(5, int(factor.get("materiality", 0))))
            duration_days = max(1, min(30, int(factor.get("duration_days", 1))))
        except (TypeError, ValueError):
            continue
        reliability_weight = 1.0 if factor.get("reliability") == "official" else 0.7
        published = _parse_published_time(factor.get("published"))
        age_days = max(
            0.0,
            ((_today() - published).total_seconds() / 86400) if published else 1.0,
        )
        freshness = 0.5 ** (age_days / duration_days)
        adjustment = int(round(
            direction * materiality * reliability_weight * freshness
        ))
        catalysts[sector_name] = {
            "adjustment": max(-5, min(5, adjustment)),
            "direction": direction,
            "materiality": materiality,
            "duration_days": duration_days,
            "reliability": factor.get("reliability"),
            "source": factor.get("source"),
            "news_id": factor.get("news_id"),
            "event_type": "weekly_material_event",
            "title": factor.get("title"),
            "reason": factor.get("text") or "אירוע שבועי מאומת.",
            "relevance_verified": True,
        }
    return catalysts


def build_weekly_opportunity_scores(
    data: dict,
    snapshot: dict,
    analysis: dict,
    calibration: dict,
    previous_weekly_scores: dict,
    model_status: dict,
) -> tuple[dict, dict]:
    """Build Sunday's auditable 2–6 week opportunity ranking."""
    catalysts = _weekly_catalysts_from_analysis(analysis)
    v2_bundle = recommendation_v2.apply_catalysts(
        recommendation_v2.build_recommendation_bundle(data), catalysts
    )
    legacy_scores = _legacy_scores_from_catalysts(data, catalysts, calibration)
    scores = _combine_recommendation_scores(
        legacy_scores, v2_bundle, model_status
    )
    for sector_name, score in scores.items():
        sector = data.get("sectors", {}).get(sector_name, {})
        trend = sector.get("trend", {})
        weekly_metric = snapshot.get("sectors", {}).get(sector_name, {})
        prior = previous_weekly_scores.get(sector_name, {})
        score["previous_weekly_score"] = prior.get("final_score")
        score["weekly_change_pct"] = weekly_metric.get("change_pct")
        score["opportunity_reason"] = (
            f"השבוע {_format_pct(weekly_metric.get('change_pct'))}, חודש "
            f"{_format_pct(trend.get('return_20d_pct'))}; "
            f"ציון גרף {score.get('graph_score', 50)}/100"
            + (
                f" והשפעת חדשות {score.get('news_adjustment', 0):+d}"
                if score.get("news_adjustment") else ""
            )
        )
        support = trend.get("support_60d")
        score["invalidation"] = (
            f"ההערכה תיחלש אם מדד הסקטור יסגור מתחת לאזור התמיכה "
            f"{_format_number(support)} ובמקביל יעבור מתחת לממוצע 50 הימים."
            if support is not None else
            "ההערכה תיחלש אם המגמה תעבור לשלילית והמחיר ירד מתחת לממוצע 50 הימים."
        )
    return scores, v2_bundle


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
    if any("v2_score" in score for score in sector_scores.values()):
        v2_ranked = sorted(
            sector_scores,
            key=lambda name: sector_scores[name].get("v2_score", 50),
            reverse=True,
        )
        for name in v2_ranked[:2]:
            if name not in selected and len(selected) < maximum:
                selected.append(name)
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
    active_model = next(iter(sector_scores.values()), {}).get(
        "active_model", "v1"
    )
    if active_model == "v2":
        model_note = (
            "V2 הוא המודל הפעיל: הציון משלב הסתברות לתשואה חיובית, "
            "סיכוי לעקוף את ת״א-125, סיכון וביטחון."
        )
    else:
        model_evidence = next(iter(sector_scores.values()), {})
        live_sample_size = model_evidence.get("v2_live_sample_size", 0)
        backtest_size = model_evidence.get("v2_backtest_sample_size", 0)
        brier_skill = model_evidence.get("v2_backtest_brier_skill_pct")
        top_three_excess = model_evidence.get("v2_backtest_top_three_excess_pct")
        calibration_result = (
            f"כיול ההסתברות שיפר את קו הבסיס ב-{_format_number(brier_skill, 1)}%"
            if brier_skill is not None and brier_skill > 0
            else "כיול ההסתברות עדיין לא הראה יתרון על קו הבסיס"
        )
        model_note = (
            "V2 פועל כעת במצב צל ונמדד מול הציון הפעיל. הוא יוכל להפוך "
            "לפעיל רק לאחר לפחות 60 תחזיות אמת שהושלמו והציגו יתרון עקבי. "
            f"הושלמו עד כה {live_sample_size} מתוך 60. בבדיקה כרונולוגית "
            f"של {backtest_size} מקרים לאופק 4 שבועות, שלושת המדורגים ראשונים "
            f"השיגו בממוצע {_format_pct(top_three_excess)} מול ת״א-125; "
            f"{calibration_result}."
        )
    ranking_rows = [
        "| # | סקטור | ציון פעיל | V2 ל-4 שבועות | סיכוי לחיובי | סיכוי לעקוף ת״א-125 | רמת ביטחון |",
        "|---:|---|---:|---:|---:|---:|---|",
    ]
    for position, name in enumerate(ranked, 1):
        score = sector_scores[name]
        ranking_rows.append(
            f"| {position} | {name} | {score['final_score']} – {score['label']} | "
            f"{score.get('v2_score', 50)} | "
            f"{_format_number(score.get('v2_probability_positive_pct'), 1)}% | "
            f"{_format_number(score.get('v2_probability_outperform_pct'), 1)}% | "
            f"{recommendation_v2.confidence_label(score.get('v2_confidence_pct', 0))} |"
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
        + model_note + " " + calibration_note + "\n" + "\n".join(ranking_rows)
    )

    detail_lines = []
    for name in _selected_sector_names(sector_scores, previous_scores):
        sector = data.get("sectors", {}).get(name, {})
        score = sector_scores[name]
        detail_lines.extend((
            f"#### {name} – {score['final_score']}/100",
            f"- **למה הסקטור נבחר:** {_compact_trend_sentence(sector)}. {score['reason']}",
            f"- **מרכיבי הציון:** גרף {score['graph_score']}; חדשות {score['news_adjustment']:+d}; כיול {score.get('calibration_adjustment', 0):+d}. {_compact_calibration_sentence(calibration.get(name))}.",
            "- **בדיקת V2 לפי אופק:** "
            f"שבועיים {score.get('v2_horizon_scores', {}).get(10, 50)}/100; "
            f"4 שבועות {score.get('v2_horizon_scores', {}).get(20, 50)}/100; "
            f"6 שבועות {score.get('v2_horizon_scores', {}).get(30, 50)}/100.",
            "- **הסתברויות V2 ל-4 שבועות:** "
            f"תשואה חיובית {_format_number(score.get('v2_probability_positive_pct'), 1)}%; "
            f"עקיפת ת״א-125 {_format_number(score.get('v2_probability_outperform_pct'), 1)}%; "
            f"רמת ביטחון {recommendation_v2.confidence_label(score.get('v2_confidence_pct', 0))} "
            f"({_format_number(score.get('v2_confidence_pct'), 0)}%); "
            f"איכות סיכון {score.get('v2_risk_quality', 50)}/100.",
            "- **טווח תשואה עודפת שנצפה במקרים דומים:** "
            f"{_format_pct(score.get('v2_expected_excess_low_pct'))} עד "
            f"{_format_pct(score.get('v2_expected_excess_high_pct'))}; "
            "זהו טווח אמפירי ולא יעד תשואה.",
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
    """Show macro context and evidence links without repeating the top summary."""
    lines = []
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

    interest = data.get("boi_interest", {})
    if interest.get("current_interest_pct") is not None:
        next_decision = _parse_published_time(interest.get("next_decision_date"))
        next_decision_text = (
            next_decision.strftime("%d.%m.%Y") if next_decision else "לא זמין"
        )
        lines.append(
            f"- **ריבית בנק ישראל:** {_format_number(interest.get('current_interest_pct'), 2)}%; "
            f"החלטה הבאה: {next_decision_text}."
        )

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
    return "### מאקרו ומקורות\n" + "\n".join(lines)


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
    response = None
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.25,
                max_tokens=max_tokens,
                reasoning_effort="low",
            )
            break
        except Exception as exc:
            if getattr(exc, "status_code", None) != 429 or attempt == 2:
                raise
            retry_after = 0.0
            headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
            try:
                retry_after = float(headers.get("retry-after", 0))
            except (TypeError, ValueError):
                retry_after = 0.0
            wait_seconds = min(10.0, max(2.0, retry_after + 0.5, 2.0 ** attempt))
            print(
                f"AI rate limit [{purpose}]; retrying in {wait_seconds:.1f}s "
                f"({attempt + 1}/2)"
            )
            time.sleep(wait_seconds)
    if response is None:
        raise RuntimeError(f"AI request [{purpose}] returned no response")
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
    recommendation_observer=None,
    model_status: dict | None = None,
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
    model_status = model_status or {
        "configured_mode": "shadow",
        "active_model": "v1",
        "comparison": {"sample_size": 0},
    }
    v2_base_bundle = recommendation_v2.build_recommendation_bundle(data)
    sector_event_scope = json.dumps(
        {
            name: {
                "largest_stocks": [
                    stock.get("name") or stock.get("symbol")
                    for stock in sector.get("top_stocks_by_market_cap", [])
                ]
            }
            for name, sector in data.get("sectors", {}).items()
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    compact_news = _compact_sector_news(data)
    sector_news = json.dumps(
        compact_news,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sector_prompt = f"""אתה אנליסט אירועים של הבורסה בתל אביב. כתוב בעברית תקנית, קצרה וברורה, והשתמש רק בנתונים ובכותרות שסופקו.

אסור לך לבחור ציון או לכתוב המלצה. תפקידך היחיד הוא לזהות לכל סקטור לכל היותר אירוע חדשותי אחד, ישיר, עדכני ומהותי. דיווח רשמי עדיף על עיתונות. אין להשתמש בידע חיצוני, בכותרת שאינה קשורה ישירות לסקטור או לאחת משלוש המניות הגדולות בו, או באותה כותרת בלי קשר ברור. דמיון בשם אינו קשר: למשל "דלק רכב" אינה "קבוצת דלק", וחברת נדל״ן אינה חברת אנרגיה.

בתחילת התשובה כתוב בדיוק עשר שורות מכונה, אחת לכל סקטור ובשמות שסופקו, בפורמט הבא וללא Markdown:
CATALYST|שם הסקטור|מזהה חדשות כגון M0/N2 או NONE|סוג אירוע earnings/guidance/regulatory/rates/currency/commodity/corporate/macro/other/none|כיוון -1/0/1|מהותיות 0-5|משך צפוי בימים 1-30|הסבר עברי קצר שמציין עובדה מהכותרת

אם אין אירוע ישיר ומהותי, השתמש בדיוק ב-NONE|none|0|0|1. earnings ו-guidance מיועדים לדוחות כספיים או תחזית חברה מפורשים בלבד. המערכת – ולא אתה – תחשב התאמה של עד 5± נקודות לפי כיוון, מהותיות, אמינות המקור וגיל הכותרת. אל תכתוב שום טקסט לפני או אחרי עשר שורות CATALYST.

נתונים:
סקטורים וחברות מובילות: {sector_event_scope}
חדשות שסופקו: {sector_news}"""

    market_brief = build_plain_language_market_outlook(data)
    sector_brief_raw = _groq_completion(
        client, model, sector_prompt, max_tokens=700, purpose="sector-events"
    )
    catalysts = _parse_news_catalysts(data, sector_brief_raw)
    v2_bundle = recommendation_v2.apply_catalysts(
        v2_base_bundle, catalysts
    )
    legacy_scores = _legacy_scores_from_catalysts(
        data, catalysts, calibration
    )
    sector_scores = _combine_recommendation_scores(
        legacy_scores, v2_bundle, model_status
    )
    if score_observer is not None:
        score_observer(sector_scores)
    if recommendation_observer is not None:
        recommendation_observer({
            "bundle": v2_bundle,
            "model_status": model_status,
            "catalysts": catalysts,
        })
    market_summary = build_market_in_60_seconds(
        data, sector_scores, previous_scores
    )
    quantitative_cards = build_reader_quantitative_cards(
        data,
        sector_scores,
        calibration,
        previous_scores,
    )
    context_card = build_reader_context_card(data, sector_scores)
    return "\n\n".join([
        market_summary,
        market_brief,
        quantitative_cards,
        context_card,
    ])


def _temporary_weekly_news(
    data: dict,
    existing: list[dict],
    window_start,
    window_end,
) -> list[dict]:
    """Merge current source results into a dry-run without mutating the ledger."""
    merged = list(existing)
    fingerprints = {
        (str(item.get("source") or ""), _normalized_relevance_text(item.get("title")))
        for item in merged
    }
    next_id = 900_000
    for item in data.get("maya_announcements", []) + data.get("news", []):
        published = _parse_published_time(item.get("published"))
        if published and not (window_start <= published.date() <= window_end):
            continue
        key = (str(item.get("source") or ""), _normalized_relevance_text(item.get("title")))
        if not key[1] or key in fingerprints:
            continue
        merged.append({**item, "id": next_id})
        fingerprints.add(key)
        next_id += 1
    return merged


def generate_weekly_hebrew_brief(
    data: dict,
    report_date,
    fx_rates: dict,
    news_items: list[dict],
    calibration: dict,
    previous_weekly_scores: dict,
    model_status: dict,
) -> tuple[str, dict, dict, dict]:
    """Generate one bounded weekly editorial pass over deterministic metrics."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is required to generate the weekly briefing")
    snapshot = weekly_report.build_weekly_snapshot(data, report_date)
    client = Groq(
        api_key=GROQ_API_KEY,
        timeout=GROQ_TIMEOUT_SECONDS,
        max_retries=1,
    )
    response = _groq_completion(
        client,
        GROQ_MODEL,
        weekly_report.weekly_ai_prompt(snapshot, fx_rates, news_items),
        max_tokens=1500,
        purpose="weekly-summary",
    )
    analysis = weekly_report.parse_weekly_ai_response(
        data, snapshot, news_items, response
    )
    opportunity_scores, _v2_bundle = build_weekly_opportunity_scores(
        data,
        snapshot,
        analysis,
        calibration,
        previous_weekly_scores,
        model_status,
    )
    brief = weekly_report.build_weekly_brief(
        data, snapshot, fx_rates, analysis, opportunity_scores
    )
    metadata = {
        "window_start": snapshot["window_start"],
        "window_end": snapshot["window_end"],
        "selected_news_ids": [
            int(item["id"])
            for item in analysis.get("selected_news", [])
            if int(item["id"]) < 900_000
        ],
        "selected_news": [
            {
                "source": item.get("source"),
                "title": item.get("title"),
                "source_summary": str(item.get("summary") or "")[:500],
                "report_summary": item.get("ai_summary"),
                "report_market_impact": item.get("market_impact"),
                "published": item.get("published"),
            }
            for item in analysis.get("selected_news", [])
        ],
        "translation_fallbacks": len(analysis.get("translation_fallbacks", [])),
        "active_recommendation_model": model_status.get("active_model", "v1"),
    }
    return brief, metadata, snapshot, opportunity_scores


def _quality_fact_tokens(value: str) -> list[str]:
    return re.findall(r"[+-]?\d[\d,.]*(?:%|/100)?", value or "")


def _deterministic_quality_issues(
    brief_text: str,
    report_type: str,
    context: dict,
) -> list[str]:
    issues = []
    if len(re.findall(r"[א-ת]", brief_text or "")) < 120:
        issues.append("report does not contain enough Hebrew content")
    for residue in ("CATALYST|", "SCORE|", "OUTLOOK|", "QA|", "```"):
        if residue in brief_text:
            issues.append(f"machine or invalid residue remains: {residue}")
    for invalid_value in ("None", "nan", "true", "false"):
        if re.search(rf"(?<![A-Za-z]){invalid_value}(?![A-Za-z])", brief_text):
            issues.append(f"standalone invalid value remains: {invalid_value}")
    if "פרופיל משקיע" in brief_text:
        issues.append("removed investor-profile section returned")

    if report_type == "weekly":
        required = (
            "### השוק ב-60 שניות",
            "### החדשות המרכזיות של השבוע",
            "### מדד ת״א-35 — ביצוע שבועי",
            "### מדד ת״א-90 — ביצוע שבועי",
            "### מדד ת״א-125 — ביצוע שבועי",
            "### מטבע חוץ — שינוי שבועי",
            "### מפת ההזדמנויות השבועית",
            "### סקירת הסקטורים",
            "### מבט לשבוע הבא",
        )
        if "אין עדיין מספיק תצפיות שבועיות" in brief_text:
            issues.append("weekly FX calculation is incomplete")
        if (
            "לא נשמרו השבוע חדשות מהותיות" not in brief_text
            and "**השפעה על השוק:**" not in brief_text
        ):
            issues.append("weekly news items are missing market-impact explanations")
        snapshot = context.get("weekly_snapshot", {})
        for key in ("window_start", "window_end"):
            value = snapshot.get(key)
            if value:
                formatted = _format_market_date(value).replace(".", "/")
                if formatted not in brief_text:
                    issues.append(f"weekly window date is missing: {value}")
    else:
        required = (
            "### השוק ב-60 שניות",
            "### מבט להמשך",
            "### מדדי תל אביב – תמונת סגירה",
            "### דירוג כל הסקטורים",
            "### מאקרו ומקורות",
        )
        for duplicate_title in (
            "### תמונת מצב בבורסה בתל אביב",
            "### מה השתנה ומה חשוב הבוקר",
            "### סקטורים בולטים ותובנות AI",
            "### מבט סקטוריאלי להמשך",
        ):
            if duplicate_title in brief_text:
                issues.append(
                    f"duplicate legacy summary section remains: {duplicate_title}"
                )
    for title in required:
        if title not in brief_text:
            issues.append(f"required section missing: {title}")
    if brief_text.count("### השוק ב-60 שניות") != 1:
        issues.append("the 60-second market summary must appear exactly once")
    if report_type == "weekly" and brief_text.count("### סקירת הסקטורים") != 1:
        issues.append("weekly sectors must appear in exactly one card")
    if brief_text.count("https://") < 3:
        issues.append("source links are incomplete")
    return issues


def _parse_quality_response(value: str) -> dict | None:
    start = (value or "").find("{")
    end = (value or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(value[start:end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("status") not in {"pass", "revise"}:
        return None
    if not isinstance(parsed.get("replacements", []), list):
        return None
    return parsed


def _apply_quality_replacements(
    brief_text: str,
    replacements: list[dict],
) -> tuple[str, list[dict]]:
    corrected = brief_text
    applied = []
    for replacement in replacements[:5]:
        old = str(replacement.get("old") or "").strip()
        new = weekly_report.clean_ai_text(replacement.get("new"))
        if not old or not new or old == new or corrected.count(old) != 1:
            continue
        if "\n" in old or "\n" in new or "###" in new or "|" in new:
            continue
        if _quality_fact_tokens(old) != _quality_fact_tokens(new):
            continue
        if not weekly_report.translation_preserves_source_facts(old, new):
            continue
        if re.findall(r"https?://[^\s)]+", old) != re.findall(r"https?://[^\s)]+", new):
            continue
        if len(new) > max(500, len(old) * 2):
            continue
        corrected = corrected.replace(old, new, 1)
        applied.append({
            "old": old,
            "new": new,
            "reason": str(replacement.get("reason") or "")[:240],
        })
    return corrected, applied


def _quality_repair_candidates(brief_text: str, review: dict) -> list[str]:
    """Return a small set of exact report lines near rejected reviewer text."""
    lines = [
        line.strip()
        for line in brief_text.splitlines()
        if line.strip()
        and not line.startswith("###")
        and not (line.startswith("|") and line.endswith("|"))
        and len(line.strip()) <= 600
    ]
    requested = [
        str(item.get("old") or "").strip()
        for item in review.get("replacements", [])
        if item.get("old")
    ]
    if not requested:
        return lines[:18]
    ranked = []
    for line in lines:
        score = max(
            SequenceMatcher(None, old, line).ratio() for old in requested
        )
        ranked.append((score, line))
    return [line for _score, line in sorted(ranked, reverse=True)[:12]]


def review_and_correct_brief(
    brief_text: str,
    report_type: str,
    fact_context: dict,
) -> tuple[str, dict]:
    """Read the completed report, apply safe prose corrections, and approve it."""
    structural_issues = _deterministic_quality_issues(
        brief_text, report_type, fact_context
    )
    if structural_issues:
        raise RuntimeError("Report quality gate failed: " + "; ".join(structural_issues))
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is required for report quality review")

    prompt = f"""אתה עורך בקרה אחרון לדוח שוק ההון הישראלי. קרא את הדוח המלא ובדוק עברית, בהירות לקורא ללא ידע טכני, סתירות פנימיות וטענות שאינן נתמכות בהקשר העובדתי. המספרים, התאריכים, הטבלאות והקישורים חושבו מקומית: אין לשנות אותם. אין להוסיף מידע חיצוני, תחזית חדשה או המלצת קנייה.

בדוק במיוחד תרגום וסיכום מאנגלית או מעברית לעברית: כל מספר, אחוז, שנה, מטבע ויחידת גודל חייבים לשמור בדיוק על משמעות המקור. million, mn, m או מ׳ הם מיליון; billion או bn הם מיליארד. אסור להחליף מיליון במיליארד, לשנות שם חברה/אדם/מוצר, להרחיב קיצור באמצעות ניחוש או לשנות את הפעולה שתוארה במקור — למשל מכירה אינה מחיקה ורכישה אינה הנפקה. ירידה במחיר מניה אינה "ירידה בתשואת המניה" אלא אם המקור עסק במפורש בתשואה. ודא שהסבר ההשפעה על השוק מנוסח בזהירות ואינו הופך אפשרות לעובדה. במקרה של ספק יש לנסח בלי הפרט הלא-ודאי.

אם הדוח ברור ונתמך, החזר JSON בלבד:
{{"status":"pass","summary":"הסבר קצר","replacements":[]}}

אם נדרש תיקון, החזר עד שלושה תיקוני משפטים מדויקים:
{{"status":"revise","summary":"הסבר קצר","replacements":[{{"old":"משפט מדויק שמופיע פעם אחת בדוח","new":"ניסוח עברי מתוקן עם אותם מספרים","reason":"סיבה"}}]}}

אסור לשנות כותרות Markdown, קישורים, מספרים או תאריכים. כל old ו-new חייבים להיות משפט יחיד ללא ירידת שורה.

סוג הדוח: {report_type}
הקשר עובדתי מאומת: {json.dumps(fact_context, ensure_ascii=False, separators=(",", ":"))}

הדוח המלא:
{brief_text}"""
    client = Groq(
        api_key=GROQ_API_KEY,
        timeout=GROQ_TIMEOUT_SECONDS,
        max_retries=1,
    )
    review = None
    for attempt in range(2):
        raw = _groq_completion(
            client,
            GROQ_MODEL,
            prompt,
            max_tokens=500,
            purpose="quality-review" if attempt == 0 else "quality-review-retry",
        )
        review = _parse_quality_response(raw)
        if review is not None:
            break
        prompt += "\n\nהתשובה הקודמת לא הייתה JSON תקין. החזר כעת רק את אובייקט ה-JSON."
    if review is None:
        raise RuntimeError("Report quality reviewer returned invalid output twice")

    corrected = brief_text
    applied = []
    if review["status"] == "revise":
        corrected, applied = _apply_quality_replacements(
            brief_text, review.get("replacements", [])
        )
        if not applied:
            print(
                "QA requested revision but its first correction was unsafe; "
                "requesting one constrained repair"
            )
            repair_prompt = f"""אתה מתקן בדוח עברי רק בעיית ניסוח שכבר זוהתה. התיקון הראשון לא התאים לטקסט המדויק או ניסה לשנות מספר/קישור.

החזר JSON בלבד. אם לאחר בדיקה נוספת אין בעיה מהותית, החזר:
{{"status":"pass","summary":"הסבר קצר","replacements":[]}}

אחרת החזר תיקון אחד עד שלושה. שדה old חייב להיות העתק מדויק לחלוטין של שורה אחת מרשימת השורות, כולל סימני Markdown. שדה new חייב לשמור ללא שינוי כל מספר, תאריך וקישור:
{{"status":"revise","summary":"הסבר קצר","replacements":[{{"old":"שורה מדויקת","new":"ניסוח מתוקן","reason":"סיבה"}}]}}

הביקורת הראשונה: {json.dumps(review, ensure_ascii=False, separators=(",", ":"))}
שורות מדויקות אפשריות מתוך הדוח: {json.dumps(_quality_repair_candidates(brief_text, review), ensure_ascii=False, separators=(",", ":"))}"""
            repair_raw = _groq_completion(
                client,
                GROQ_MODEL,
                repair_prompt,
                max_tokens=400,
                purpose="quality-correction-retry",
            )
            repair_review = _parse_quality_response(repair_raw)
            if repair_review is None:
                raise RuntimeError(
                    "Report quality correction retry returned invalid output"
                )
            if repair_review["status"] == "revise":
                corrected, applied = _apply_quality_replacements(
                    brief_text, repair_review.get("replacements", [])
                )
                if not applied:
                    raise RuntimeError(
                        "Report quality correction retry supplied no safe correction"
                    )
            review = repair_review
    post_issues = _deterministic_quality_issues(
        corrected, report_type, fact_context
    )
    if post_issues:
        raise RuntimeError("Corrected report failed quality gate: " + "; ".join(post_issues))
    return corrected, {
        "status": "approved",
        "review_result": review["status"],
        "summary": str(review.get("summary") or "")[:400],
        "corrections_applied": len(applied),
        "corrections": applied,
        "reviewed_at": _today().isoformat(timespec="seconds"),
        "model": GROQ_MODEL,
    }


def send_preparation_failure_alert(error: Exception) -> None:
    """Notify the owner when the 08:00 safety gate withholds a report."""
    recipient = OWNER_EMAIL or GMAIL_USER
    if not recipient or not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return
    report_date = _today().strftime("%d/%m/%Y")
    reason = html.escape(str(error)[:1000])
    content = (
        '<div dir="rtl" style="font-family:Arial,sans-serif;text-align:right">'
        f"<h2>דוח FinancialBrief לא נשלח – {report_date}</h2>"
        "<p>שלב השליחה בשעה 08:00 לא מצא דוח מאושר להיום. "
        "המערכת מנעה שליחת דוח לא בדוק.</p>"
        f"<p><strong>סיבה:</strong> {reason}</p></div>"
    )
    send_email(
        content,
        f"FinancialBrief: הדוח לא נשלח – {report_date}",
        recipient,
    )


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


def build_html_email(
    brief_text: str,
    unsubscribe_url: str = "#",
    report_type: str = "daily",
) -> str:
    now = _today()
    date_str = now.strftime("%d/%m/%Y")
    report_title = (
        "סיכום שבועי — שוק ההון הישראלי"
        if report_type == "weekly"
        else "תדריך שוק ההון הישראלי"
    )
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
  <title>{report_title} – {date_str}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family:Arial,"Noto Sans Hebrew",sans-serif; background:#e8edf4; color:#1e2d42; direction:rtl; text-align:right; }}
    .wrapper {{ max-width:720px; margin:0 auto; padding:24px 16px 40px; }}
    .header {{ background:linear-gradient(135deg,#123767,#1f5797); border-radius:16px; padding:34px; text-align:center; }}
    .logo {{ color:#9ec3e8; font-size:12px; letter-spacing:3px; margin-bottom:10px; }}
    h1 {{ color:#f0c040; font-size:30px; margin-bottom:8px; }}
    .date {{ color:#c1d8ef; font-size:16px; }}
    .accent {{ height:3px; margin:18px 40px; background:linear-gradient(90deg,transparent,#f0c040,#e08030,#f0c040,transparent); }}
    .card {{ background:#f7faff; border:1px solid #c8d8ea; border-radius:14px; padding:22px 26px; margin-bottom:14px; box-shadow:0 2px 10px rgba(30,60,100,.08); }}
    .index-card {{ border-right:5px solid #1f5797; }}
    .card-title {{ color:#163e70; font-size:19px; font-weight:700; border-bottom:2px solid #dce8f4; padding-bottom:10px; margin-bottom:14px; }}
    .card-body {{ color:#2c3e52; font-size:16.5px; line-height:1.9; direction:rtl; text-align:right; unicode-bidi:plaintext; }}
    .card-body p {{ margin:0 0 12px; direction:rtl; text-align:right; }}
    .card-body ul,.card-body ol {{ margin:0 0 14px; padding:0 22px 0 0; direction:rtl; text-align:right; }}
    .card-body li {{ margin-bottom:7px; padding-right:2px; direction:rtl; text-align:right; }}
    .subheading {{ color:#214f82; font-weight:700; margin:13px 0 7px; direction:rtl; text-align:right; }}
    .table-wrap {{ width:100%; overflow-x:auto; margin:10px 0 14px; direction:rtl; }}
    table {{ width:100%; border-collapse:collapse; direction:rtl; text-align:right; }}
    th {{ background:#e5eef8; color:#163e70; font-weight:700; }}
    th,td {{ border:1px solid #c8d8ea; padding:8px 9px; vertical-align:top; direction:rtl; text-align:right; unicode-bidi:plaintext; }}
    strong {{ color:#0f2d5e; }}
    .notice {{ background:#fff8df; border:1px solid #ead58b; color:#66551c; border-radius:10px; padding:13px 16px; margin-top:16px; font-size:14px; line-height:1.7; }}
    .footer {{ text-align:center; color:#71869d; font-size:13px; padding:18px; line-height:1.8; }}
  </style>
</head>
<body dir="rtl" align="right"><div class="wrapper" dir="rtl" align="right">
  <div class="header"><div class="logo">ISRAEL MARKET INTELLIGENCE</div><h1>{report_title}</h1><div class="date">{date_str}</div></div>
  <div class="accent"></div>
  {''.join(cards)}
  <div class="notice">התחזיות בתדריך הן תרחישים המבוססים על נתוני עבר ומידע ציבורי. הן אינן הבטחת תשואה, המלצה אישית או תחליף לייעוץ השקעות מורשה.</div>
  <div class="footer" dir="rtl">מקורות: הבורסה לניירות ערך ומאיה · בנק ישראל · הלמ״ס · משרד האוצר · גלובס · כלכליסט · TheMarker · Bizportal · Funder<br>
    <a href="{html.escape(unsubscribe_url, quote=True)}" style="color:#4a7aaa;">ביטול הרשמה</a>
  </div>
</div></body></html>"""


def send_email(
    html_content: str,
    subject: str,
    recipient_email: str,
    message_id: str | None = None,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = recipient_email
    if message_id:
        msg["Message-ID"] = message_id
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipient_email, msg.as_string())


def run(
    send: bool = True,
    include_news: bool = True,
    force_send: bool = False,
    report_type: str | None = None,
    persist_report: bool = False,
    approved_only: bool = False,
) -> dict:
    if force_send and not send:
        raise ValueError("force_send requires email delivery")
    if approved_only and not send:
        raise ValueError("approved_only requires email delivery")

    persist_report = bool(persist_report or send)

    database.init_db()
    if OWNER_EMAIL:
        database.seed_owner(OWNER_NAME, OWNER_EMAIL)
    backup_status = database.snapshot_subscribers_once_daily()

    if send and (not GMAIL_USER or not GMAIL_APP_PASSWORD):
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD are required to send email")

    now = _today()
    resolved_report_type = report_type or (
        "weekly" if now.weekday() == 6 else "daily"
    )
    if resolved_report_type not in {"daily", "weekly"}:
        raise ValueError("report_type must be 'daily' or 'weekly'")
    report_date = now.date().isoformat()
    existing_run = database.get_briefing_run(report_date) if persist_report else None
    if approved_only and existing_run is None:
        raise RuntimeError(
            f"No prepared and approved report exists for {report_date}"
        )
    if approved_only and existing_run.get("qa_status") != "approved":
        raise RuntimeError(
            f"Prepared report for {report_date} is not QA-approved"
        )
    if existing_run and not send and existing_run.get("qa_status") != "approved":
        raise RuntimeError(
            f"Existing report for {report_date} predates the approval workflow"
        )
    reused_brief = existing_run is not None
    tracking = {
        "close_rows_upserted": 0,
        "outcomes_settled": 0,
        "predictions_upserted": 0,
        "v2_outcomes_settled": 0,
        "v2_features_upserted": 0,
        "v2_predictions_upserted": 0,
        "weekly_opportunities_upserted": 0,
    }
    weekly_generated_scores = {}

    if existing_run:
        brief_text = existing_run["brief_text"]
        as_of = existing_run.get("as_of")
        resolved_report_type = existing_run.get("report_type") or resolved_report_type
        try:
            report_metadata = json.loads(existing_run.get("metadata_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            report_metadata = {}
        collection_stats = {}
        briefing_run = existing_run
    else:
        collection_options = {"include_news": include_news}
        if resolved_report_type == "weekly":
            collection_options.update({
                "news_hours": 192,
                "news_per_source": 8,
                "news_limit": 60,
            })
        data = collect_israeli_market_data(**collection_options)
        validate_market_data(data)
        if persist_report:
            tracking.update(database.record_market_close_history(data))
            tracking.update(database.settle_sector_score_outcomes())
            tracking.update(database.settle_v2_prediction_outcomes())
            tracking.update(database.record_fx_rates(data))
            if include_news:
                tracking.update(database.save_weekly_news(
                    data.get("maya_announcements", []) + data.get("news", [])
                ))

        if resolved_report_type == "weekly":
            preliminary = weekly_report.build_weekly_snapshot(data, now.date())
            window_start = datetime.strptime(
                preliminary["window_start"], "%Y-%m-%d"
            ).date()
            window_end = datetime.strptime(
                preliminary["window_end"], "%Y-%m-%d"
            ).date()
            news_items = database.get_weekly_news(window_start, window_end)
            if not persist_report:
                news_items = _temporary_weekly_news(
                    data, news_items, window_start, window_end
                )
            stored_fx_rates = database.get_weekly_fx_rates(window_start, window_end)
            try:
                official_fx_history = get_boi_exchange_rate_history(
                    window_start, window_end
                )
                fx_rates = weekly_report.weekly_fx_performance(
                    official_fx_history
                )
            except Exception as exc:
                print(f"BOI weekly FX history unavailable; using stored rates: {exc}")
                fx_rates = stored_fx_rates
            else:
                for currency in ("USD", "EUR"):
                    if not fx_rates.get(currency, {}).get("available"):
                        fx_rates[currency] = stored_fx_rates.get(
                            currency, {"available": False}
                        )
            weekly_graph_scores = {
                name: calculate_sector_graph_score(sector)
                for name, sector in data.get("sectors", {}).items()
            }
            weekly_calibration = database.get_sector_score_calibration(
                weekly_graph_scores
            )
            previous_weekly_scores = database.get_previous_weekly_opportunity_scores(
                report_date
            )
            weekly_model_status = database.choose_active_recommendation_model(
                os.getenv("RECOMMENDATION_V2_MODE", "shadow")
            )
            (
                brief_text,
                report_metadata,
                weekly_snapshot,
                weekly_generated_scores,
            ) = generate_weekly_hebrew_brief(
                data,
                now.date(),
                fx_rates,
                news_items,
                weekly_calibration,
                previous_weekly_scores,
                weekly_model_status,
            )
            market_close_date = (
                weekly_snapshot.get("indices", {})
                .get("ת״א-125", {})
                .get("end_date")
            )
            quality_context = {
                "weekly_snapshot": weekly_snapshot,
                "fx_rates": fx_rates,
                "selected_news": report_metadata.get("selected_news", []),
                "weekly_opportunity_scores": {
                    name: {
                        "score": score.get("final_score"),
                        "weekly_change_pct": score.get("weekly_change_pct"),
                        "confidence_pct": score.get("v2_confidence_pct"),
                    }
                    for name, score in weekly_generated_scores.items()
                },
            }
        else:
            graph_scores = {
                name: calculate_sector_graph_score(sector)
                for name, sector in data.get("sectors", {}).items()
            }
            calibration = database.get_sector_score_calibration(graph_scores)
            previous_scores = database.get_latest_sector_scores()
            model_status = database.choose_active_recommendation_model(
                os.getenv("RECOMMENDATION_V2_MODE", "shadow")
            )
            generated_scores = {}
            recommendation_capture = {}
            brief_text = generate_hebrew_brief(
                data,
                calibration=calibration,
                previous_scores=previous_scores,
                score_observer=generated_scores.update,
                recommendation_observer=recommendation_capture.update,
                model_status=model_status,
            )
            report_metadata = {}
            market_close_date = (
                data.get("indices", {})
                .get("ת״א-125", {})
                .get("trend", {})
                .get("as_of")
            )
            quality_context = {
                "market": _compact_market_payload(data),
                "sector_scores": {
                    name: {
                        "score": score.get("final_score"),
                        "graph": score.get("graph_score"),
                        "news_adjustment": score.get("news_adjustment"),
                    }
                    for name, score in generated_scores.items()
                },
                "source_headlines": _compact_sector_news(data),
            }

        if persist_report:
            brief_text, qa_result = review_and_correct_brief(
                brief_text,
                resolved_report_type,
                quality_context,
            )
            report_metadata["qa"] = qa_result
            if resolved_report_type == "daily":
                tracking.update(database.record_sector_score_predictions(
                    data, generated_scores
                ))
                if recommendation_capture.get("bundle"):
                    tracking.update(database.record_v2_recommendation_bundle(
                        data,
                        recommendation_capture["bundle"],
                        generated_scores,
                        model_status["active_model"],
                    ))
            else:
                tracking.update(database.save_weekly_opportunity_scores(
                    report_date, weekly_generated_scores
                ))
            briefing_run = database.save_briefing_run(
                report_date,
                market_close_date,
                data.get("as_of"),
                brief_text,
                report_type=resolved_report_type,
                metadata=report_metadata,
                qa_status="approved",
            )
            # In the unlikely event of a concurrent insert, the ledger's
            # immutable copy is the authoritative content to deliver.
            brief_text = briefing_run["brief_text"]
        else:
            briefing_run = None
        as_of = data.get("as_of")
        collection_stats = data.get("collection_stats", {})

    result = {
        "run_id": briefing_run["id"] if briefing_run else None,
        "report_date": report_date,
        "report_type": resolved_report_type,
        "qa_status": briefing_run.get("qa_status") if briefing_run else None,
        "prepared_at": briefing_run.get("prepared_at") if briefing_run else None,
        "reused_brief": reused_brief,
        "as_of": as_of,
        "collection_stats": collection_stats,
        "backup": backup_status,
        "score_tracking": tracking,
        "subscriber_count": 0,
        "sent": 0,
        "failed": [],
        "ambiguous": [],
        "skipped": [],
        "brief": brief_text,
    }
    if not send:
        return result

    subscribers = database.get_active_subscribers()
    result["subscriber_count"] = len(subscribers)
    subject_date = datetime.strptime(report_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    subject = (
        f"סיכום שבועי — שוק ההון הישראלי – {subject_date}"
        if resolved_report_type == "weekly"
        else f"תדריך שוק ההון הישראלי – {subject_date}"
    )
    for subscriber in subscribers:
        email_address = subscriber["email"]
        message_id = _daily_message_id(report_date, email_address)
        claim = database.claim_email_delivery(
            briefing_run["id"],
            email_address,
            message_id,
            force=force_send,
        )
        if not claim["send"]:
            result["skipped"].append({
                "email": email_address,
                "reason": claim["reason"],
            })
            continue
        try:
            unsubscribe_url = f"{BASE_URL}/unsubscribe/{subscriber['token']}"
            content = build_html_email(
                brief_text,
                unsubscribe_url,
                report_type=resolved_report_type,
            )
            send_email(content, subject, email_address, message_id=message_id)
        except Exception as exc:
            database.mark_email_delivery_failed(
                briefing_run["id"], email_address, str(exc)
            )
            result["failed"].append({"email": email_address, "error": str(exc)})
            continue
        try:
            database.mark_email_delivery_sent(briefing_run["id"], email_address)
            result["sent"] += 1
        except Exception as exc:
            # SMTP has already accepted the message. Keep the row in `sending`
            # so a normal retry cannot risk delivering a duplicate.
            result["ambiguous"].append({
                "email": email_address,
                "error": f"SMTP accepted; ledger update failed: {exc}",
            })
    result["delivery"] = database.finalize_briefing_run(briefing_run["id"])
    if resolved_report_type == "weekly" and result["delivery"]["status"] == "completed":
        try:
            window_start = datetime.strptime(
                report_metadata["window_start"], "%Y-%m-%d"
            ).date()
            window_end = datetime.strptime(
                report_metadata["window_end"], "%Y-%m-%d"
            ).date()
            result["weekly_news_cleanup"] = database.finalize_weekly_news(
                report_date,
                window_start,
                window_end,
                report_metadata.get("selected_news_ids", []),
            )
        except (KeyError, TypeError, ValueError):
            result["weekly_news_cleanup"] = {
                "skipped": True,
                "reason": "weekly metadata unavailable",
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Israeli stock-market morning briefing")
    parser.add_argument("--dry-run", action="store_true", help="Generate but do not send email")
    parser.add_argument(
        "--weekly-preview",
        action="store_true",
        help="Generate the most recently completed weekly report (requires --dry-run)",
    )
    parser.add_argument(
        "--force-send",
        action="store_true",
        help="Intentionally resend today's persisted report to active recipients",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Validate sources without AI generation, database writes, or email",
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Gather, generate, QA-approve, and persist today's report without sending",
    )
    parser.add_argument(
        "--send-prepared",
        action="store_true",
        help="Send only today's already prepared and QA-approved report",
    )
    parser.add_argument("--no-news", action="store_true", help="Skip press feeds during diagnostics")
    args = parser.parse_args()
    selected_modes = sum(bool(value) for value in (
        args.dry_run,
        args.collect_only,
        args.prepare,
        args.send_prepared,
    ))
    if selected_modes > 1:
        parser.error(
            "--dry-run, --collect-only, --prepare and --send-prepared are mutually exclusive"
        )
    if args.force_send and (args.dry_run or args.collect_only or args.prepare):
        parser.error(
            "--force-send cannot be combined with --dry-run, --collect-only or --prepare"
        )
    if args.weekly_preview and not args.dry_run:
        parser.error("--weekly-preview requires --dry-run")
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
            "boi_interest_pct": data.get("boi_interest", {}).get(
                "current_interest_pct"
            ),
            "collection_stats": data.get("collection_stats", {}),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if args.dry_run:
        result = run(
            send=False,
            include_news=not args.no_news,
            report_type="weekly" if args.weekly_preview else None,
        )
    elif args.prepare:
        with _delivery_process_lock():
            result = run(
                send=False,
                include_news=not args.no_news,
                persist_report=True,
            )
    elif args.send_prepared:
        with _delivery_process_lock():
            try:
                result = run(
                    send=True,
                    approved_only=True,
                    force_send=args.force_send,
                )
            except Exception as exc:
                try:
                    send_preparation_failure_alert(exc)
                except Exception as alert_exc:
                    print(f"Could not send preparation failure alert: {alert_exc}")
                raise
    else:
        with _delivery_process_lock():
            result = run(
                send=True,
                include_news=not args.no_news,
                force_send=args.force_send,
            )
    printable = {key: value for key, value in result.items() if key != "brief"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("\n" + result["brief"])


if __name__ == "__main__":
    main()
