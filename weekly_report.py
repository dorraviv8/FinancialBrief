"""Deterministic weekly calculations and concise Hebrew report composition."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta


INDEX_ORDER = ("ת״א-35", "ת״א-90", "ת״א-125")

SECTOR_KEYWORDS = {
    "טכנולוגיה": ("טכנולוג", "סייבר", "תוכנה", "שבבים"),
    "ביומד": ("ביומד", "פארמה", "תרופ", "רפוא"),
    "בנקים": ("בנק", "לאומי", "פועלים", "מזרחי", "דיסקונט"),
    "ביטוח ושירותים פיננסיים": ("ביטוח", "פיננס", "אשראי"),
    "נדל״ן ובנייה": ("נדל", "בנייה", "בניה", "דיור", "דירות"),
    "תעשייה": ("תעש", "מפעל", "ייצור"),
    "מסחר ושירותים": ("קמעונ", "מסחר", "שירותים"),
    "השקעות ואחזקות": ("אחזקות", "החזקות", "חברת השקעות"),
    "אנרגיה ותשתיות": ("אנרג", "תשתיות", "חשמל"),
    "נפט וגז": ("נפט", "גז טבעי", "קידוח", "ניו מד", "נאוויטס", "קבוצת דלק"),
}

MATERIAL_TERMS = (
    "ריבית", "אינפלציה", "דירוג", "מלחמה", "הסכם", "רגול", "תקציב",
    "דוחות", "רווח", "הפסד", "תחזית", "עסקה", "מיזוג", "הנפק", "גז",
)

_MAGNITUDE_PATTERNS = {
    "thousand": re.compile(
        r"(?:(?<=\d)k\b|\bthousand(?:s)?\b|אל(?:ף|פים))",
        re.IGNORECASE,
    ),
    "million": re.compile(
        r"(?:(?<=\d)m\b|\bm(?:illion|illions|n)\b|מיל(?:יון|יוני|׳|')|מ[׳'])",
        re.IGNORECASE,
    ),
    "billion": re.compile(
        r"(?:(?<=\d)b\b|\bb(?:illion|illions|n)\b|מיליארד(?:י|ים)?)",
        re.IGNORECASE,
    ),
    "trillion": re.compile(
        r"(?:(?<=\d)t\b|\btrillion(?:s)?\b|טריליון(?:ים)?)",
        re.IGNORECASE,
    ),
}
_CURRENCY_PATTERNS = {
    "usd": re.compile(r"(?:\busd\b|\$|דולר(?:ים)?)", re.IGNORECASE),
    "eur": re.compile(r"(?:\beur\b|€|אירו)", re.IGNORECASE),
    "ils": re.compile(r"(?:\b(?:ils|nis)\b|₪|שקל(?:ים)?)", re.IGNORECASE),
}
_MEASURE_PATTERNS = {
    "percent": re.compile(r"(?:%|\bpercent\b|אחוז(?:ים)?)", re.IGNORECASE),
    "basis_points": re.compile(
        r"(?:\bbps\b|basis points?|נקוד(?:ה|ות) בסיס)", re.IGNORECASE
    ),
}


def _numeric_values(value: str) -> set[str]:
    """Normalize numbers so 4.60 and 4,6 compare as the same source fact."""
    result = set()
    for raw in re.findall(r"(?<![A-Za-zא-ת])[-+]?\d[\d,]*(?:\.\d+)?", value or ""):
        try:
            result.add(f"{float(raw.replace(',', '')):.8g}")
        except ValueError:
            continue
    return result


def _labels_by_nearby_number(value: str, patterns: dict) -> dict[str, set[str]]:
    number_spans = []
    for match in re.finditer(
        r"(?<![A-Za-zא-ת])[-+]?\d[\d,]*(?:\.\d+)?", value or ""
    ):
        try:
            normalized = f"{float(match.group().replace(',', '')):.8g}"
        except ValueError:
            continue
        number_spans.append((normalized, match.start(), match.end()))
    result = {}
    for label, pattern in patterns.items():
        for label_match in pattern.finditer(value or ""):
            nearby = []
            for number, start, end in number_spans:
                distance = min(
                    abs(label_match.start() - end),
                    abs(start - label_match.end()),
                )
                if distance <= 12:
                    nearby.append((distance, number))
            if nearby:
                _distance, number = min(nearby)
                result.setdefault(number, set()).add(label)
    return result


def translation_preserves_source_facts(source_text: str, translated_text: str) -> bool:
    """Reject translated claims that invent numbers, units or currency labels."""
    if not translated_text.strip():
        return False
    source_numbers = _numeric_values(source_text)
    translated_numbers = _numeric_values(translated_text)
    if not translated_numbers.issubset(source_numbers):
        return False
    for patterns in (
        _MAGNITUDE_PATTERNS,
        _CURRENCY_PATTERNS,
        _MEASURE_PATTERNS,
    ):
        source_labels = {
            label for label, pattern in patterns.items() if pattern.search(source_text or "")
        }
        translated_labels = {
            label for label, pattern in patterns.items() if pattern.search(translated_text or "")
        }
        if not translated_labels.issubset(source_labels):
            return False
        if patterns is _MAGNITUDE_PATTERNS or patterns is _MEASURE_PATTERNS:
            source_pairs = _labels_by_nearby_number(source_text, patterns)
            translated_pairs = _labels_by_nearby_number(translated_text, patterns)
            for number, labels in source_pairs.items():
                if number in translated_numbers and not labels.issubset(
                    translated_pairs.get(number, set())
                ):
                    return False
    return True


def _safe_source_summary(item: dict) -> str:
    title = str(item.get("title") or "").strip()
    return title or "פרטי הדיווח זמינים בקישור למקור."


def clean_ai_text(value: str) -> str:
    return str(value or "").replace('\\"', '"').replace("\\'", "'").strip()


def calendar_week_window(report_date: date) -> tuple[date, date]:
    """Return Monday through Saturday immediately preceding a Sunday report."""
    days_since_monday = report_date.weekday()
    start = report_date - timedelta(days=days_since_monday)
    if report_date.weekday() != 6:
        # Previewing outside Sunday uses the most recently completed Mon–Sat week.
        start -= timedelta(days=7)
    return start, start + timedelta(days=5)


def _close_points(history: list[dict]) -> list[tuple[date, float]]:
    points = {}
    for row in history or []:
        try:
            point_date = date.fromisoformat(str(row["date"]))
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            points[point_date] = close
    return sorted(points.items())


def weekly_performance(
    history: list[dict], window_start: date, window_end: date
) -> dict:
    """Calculate full-week close-to-close performance over actual sessions.

    The reference is the final close before the calendar week. This captures the
    first session's move and is the standard full-week comparison. The report
    always displays both dates used in the calculation.
    """
    points = _close_points(history)
    sessions = [(day, close) for day, close in points if window_start <= day <= window_end]
    if not sessions:
        return {"available": False, "sessions": 0}
    first_day, first_close = sessions[0]
    end_day, end_close = sessions[-1]
    earlier = [(day, close) for day, close in points if day < first_day]
    if earlier:
        start_day, start_close = earlier[-1]
        method = "previous_close_to_final_close"
    else:
        start_day, start_close = first_day, first_close
        method = "first_close_to_final_close"
    change = ((end_close / start_close) - 1) * 100 if start_close else None
    return {
        "available": change is not None,
        "sessions": len(sessions),
        "first_session_date": first_day.isoformat(),
        "last_session_date": end_day.isoformat(),
        "start_date": start_day.isoformat(),
        "start_close": start_close,
        "end_date": end_day.isoformat(),
        "end_close": end_close,
        "change_pct": round(change, 4) if change is not None else None,
        "method": method,
    }


def build_weekly_snapshot(data: dict, report_date: date) -> dict:
    window_start, window_end = calendar_week_window(report_date)
    return {
        "report_date": report_date.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "indices": {
            name: weekly_performance(
                data.get("indices", {}).get(name, {}).get("history_closes", []),
                window_start,
                window_end,
            )
            for name in INDEX_ORDER
        },
        "sectors": {
            name: weekly_performance(
                sector.get("history_closes", []), window_start, window_end
            )
            for name, sector in data.get("sectors", {}).items()
        },
    }


def weekly_fx_performance(history: dict) -> dict:
    result = {}
    for currency in ("USD", "EUR"):
        points = []
        for row in history.get("rates", {}).get(currency, []):
            try:
                point_date = date.fromisoformat(str(row["date"]))
                value = float(row["ils_rate"])
            except (KeyError, TypeError, ValueError):
                continue
            if value > 0:
                points.append((point_date, value))
        points.sort()
        if not points:
            result[currency] = {"available": False}
            continue
        first, last = points[0], points[-1]
        result[currency] = {
            "available": True,
            "start_date": first[0].isoformat(),
            "start_rate": first[1],
            "end_date": last[0].isoformat(),
            "end_rate": last[1],
            "change_pct": round(((last[1] / first[1]) - 1) * 100, 4),
        }
    return result


def _normalized(value) -> str:
    return " ".join(
        re.sub(r"[^0-9a-zA-Zא-ת]+", " ", str(value or "").lower()).split()
    )


def _news_relevant_to_sector(data: dict, sector_name: str, item: dict) -> bool:
    text = _normalized(" ".join([
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        " ".join(item.get("companies") or []),
    ]))
    if any(_normalized(term) in text for term in SECTOR_KEYWORDS.get(sector_name, ())):
        return True
    for stock in data.get("sectors", {}).get(sector_name, {}).get(
        "top_stocks_by_market_cap", []
    ):
        for identifier in (stock.get("name"), stock.get("symbol")):
            identifier = _normalized(identifier)
            if len(identifier) >= 4 and identifier in text:
                return True
    if item.get("reliability") == "official" and any(
        term in text for term in ("ריבית", "אינפלציה", "מטבע", "שקל", "תקציב")
    ):
        return True
    return False


def compact_weekly_ai_payload(
    snapshot: dict, fx_rates: dict, news_items: list[dict]
) -> dict:
    return {
        "week": [snapshot["window_start"], snapshot["window_end"]],
        "indices": snapshot["indices"],
        "sectors": snapshot["sectors"],
        "fx": fx_rates,
        "news": [
            {
                "id": f"W{item['id']}",
                "source": item.get("source"),
                "reliability": item.get("reliability"),
                "title": item.get("title"),
                "summary": str(item.get("summary") or "")[:360],
                "companies": item.get("companies", []),
                "published": item.get("published"),
            }
            for item in news_items
        ],
    }


def _ai_news_candidates(news_items: list[dict], maximum: int = 24) -> list[dict]:
    """Keep the AI request bounded while preserving source diversity."""
    ranked = sorted(
        news_items,
        key=lambda item: (
            1 if item.get("reliability") == "official" else 0,
            sum(
                term in _normalized(f"{item.get('title')} {item.get('summary')}")
                for term in MATERIAL_TERMS
            ),
            str(item.get("published") or ""),
        ),
        reverse=True,
    )
    selected = []
    source_counts = {}
    for item in ranked:
        source = str(item.get("source") or "")
        if source_counts.get(source, 0) >= 3:
            continue
        selected.append(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= maximum:
            break
    return selected


def weekly_ai_prompt(snapshot: dict, fx_rates: dict, news_items: list[dict]) -> str:
    payload = json.dumps(
        compact_weekly_ai_payload(
            snapshot, fx_rates, _ai_news_candidates(news_items)
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sector_names = ", ".join(snapshot.get("sectors", {}))
    return f"""אתה עורך שבועי של שוק ההון הישראלי. השתמש רק בנתונים ובכתבות שסופקו. כתוב עברית תקנית, בהירה וקצרה לקורא שאינו מכיר מונחים טכניים.

בחר עד שש כתבות שהיו המהותיות והמשפיעות ביותר למשקיעים במהלך השבוע. העדף מקור רשמי, אירוע בעל השפעה רחבה, דוחות/תחזיות/רגולציה ואירוע שמסביר תנועה במדד או בסקטור. אל תמציא עובדות מעבר לכותרת ולתקציר. לכל סקטור מותר לשייך לכל היותר כתבה אחת ורק אם הקשר ישיר.

כללי תרגום מחייבים: העתק במדויק כל מספר, אחוז, שנה, מטבע ויחידת גודל מהמקור. million, mn, m או מ׳ פירושם מיליון; billion או bn פירושם מיליארד. אסור להחליף מיליון במיליארד או להפך. אין לתרגם או לשנות שם חברה, סימול, שם מוצר או שם אדם. אם פרט באנגלית אינו חד-משמעי, השמט אותו במקום לנחש.

החזר אך ורק שורות במבנה הבא, בלי Markdown:
NEWS|Wמספר|סיכום עובדתי וברור של משפט אחד
SECTOR|שם סקטור|Wמספר או NONE|כיוון -1/0/1|מהותיות 0-5|משך צפוי בימים 1-30|גורם מרכזי במשפט קצר, או NONE אם אין קשר חדשותי ישיר

כתוב שורת SECTOR אחת לכל אחד מעשרת הסקטורים האלה: {sector_names}.
נתונים: {payload}"""


def _fallback_news(news_items: list[dict], maximum: int = 6) -> list[dict]:
    def score(item: dict) -> tuple[int, str]:
        text = _normalized(f"{item.get('title')} {item.get('summary')}")
        materiality = sum(term in text for term in MATERIAL_TERMS)
        official = 3 if item.get("reliability") == "official" else 0
        return official + materiality, str(item.get("published") or "")

    ranked = sorted(news_items, key=score, reverse=True)
    selected = []
    source_counts = {}
    for item in ranked:
        source = item.get("source")
        if source_counts.get(source, 0) >= 2:
            continue
        selected.append(item)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= maximum:
            break
    return selected


def parse_weekly_ai_response(
    data: dict,
    snapshot: dict,
    news_items: list[dict],
    response: str,
) -> dict:
    by_protocol_id = {f"W{item['id']}": item for item in news_items}
    selected = []
    summaries = {}
    factors = {}
    outlook = []
    translation_fallbacks = []
    for raw_line in (response or "").splitlines():
        line = raw_line.strip()
        if line.startswith("NEWS|"):
            parts = line.split("|", 2)
            if len(parts) != 3 or parts[1] not in by_protocol_id:
                continue
            item_id = parts[1]
            if item_id not in selected and len(selected) < 6:
                selected.append(item_id)
                summary = clean_ai_text(parts[2])[:360]
                item = by_protocol_id[item_id]
                source_text = f"{item.get('title') or ''} {item.get('summary') or ''}"
                if translation_preserves_source_facts(source_text, summary):
                    summaries[item_id] = summary
                else:
                    summaries[item_id] = _safe_source_summary(item)
                    translation_fallbacks.append(item_id)
        elif line.startswith("SECTOR|"):
            parts = line.split("|", 6)
            if len(parts) not in {4, 7} or parts[1] not in snapshot.get("sectors", {}):
                continue
            sector_name, item_id = parts[1], parts[2]
            if len(parts) == 7:
                direction_text, materiality_text, duration_text, reason = parts[3:]
            else:
                direction_text, materiality_text, duration_text, reason = "0", "0", "1", parts[3]
            reason = clean_ai_text(reason)
            item = by_protocol_id.get(item_id)
            source_text = (
                f"{item.get('title') or ''} {item.get('summary') or ''}"
                if item else ""
            )
            try:
                direction = max(-1, min(1, int(direction_text)))
                materiality = max(0, min(5, int(materiality_text)))
                duration_days = max(1, min(30, int(duration_text)))
            except ValueError:
                continue
            if (
                item
                and _news_relevant_to_sector(data, sector_name, item)
                and translation_preserves_source_facts(source_text, reason)
            ):
                factors[sector_name] = {
                    "news_id": int(item["id"]),
                    "text": reason[:300],
                    "direction": direction,
                    "materiality": materiality,
                    "duration_days": duration_days,
                    "reliability": item.get("reliability"),
                    "source": item.get("source"),
                    "published": item.get("published"),
                    "title": item.get("title"),
                }
        elif line.startswith("OUTLOOK|"):
            # Some models repeat a requested label as a middle protocol field.
            text = line.split("|")[-1].strip()
            if text and len(outlook) < 3:
                outlook.append(text[:360])

    if not selected:
        selected_items = _fallback_news(news_items)
        selected = [f"W{item['id']}" for item in selected_items]
    selected_items = [by_protocol_id[item_id] for item_id in selected]
    for item_id, item in zip(selected, selected_items):
        summaries.setdefault(item_id, str(item.get("title") or ""))

    if len(outlook) < 3:
        ta125 = snapshot.get("indices", {}).get("ת״א-125", {})
        change = ta125.get("change_pct")
        direction = (
            "חיובית" if isinstance(change, (int, float)) and change > 0.5
            else "שלילית" if isinstance(change, (int, float)) and change < -0.5
            else "מעורבת"
        )
        fallback = [
            f"תרחיש הבסיס לשבוע הבא הוא פתיחה {direction}, תוך בדיקה אם הכיוון של ת״א-125 נשמר.",
            "חדשות חיוביות ורוחב עליות גדול יותר בין הסקטורים עשויים לתמוך בשוק.",
            "החמרה באירועים המקומיים או חולשה במדדים ובשקל עלולות להכביד על המסחר.",
        ]
        outlook.extend(fallback[len(outlook):])
    return {
        "selected_news": [
            {
                **item,
                "ai_summary": summaries.get(f"W{item['id']}", item.get("title")),
                "translation_fallback": f"W{item['id']}" in translation_fallbacks,
            }
            for item in selected_items
        ],
        "sector_factors": factors,
        "outlook": outlook[:3],
        "translation_fallbacks": translation_fallbacks,
    }


def _format_date(value: str | None) -> str:
    try:
        return date.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return "לא זמין"


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


def _published_date(value) -> str:
    if not value:
        return ""


def _confidence_label(value) -> str:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0
    if confidence >= 75:
        return "גבוהה"
    if confidence >= 55:
        return "בינונית"
    return "נמוכה"


def build_weekly_market_in_60_seconds(
    snapshot: dict,
    analysis: dict,
    opportunity_scores: dict,
) -> str:
    available_indices = [
        (name, metric)
        for name, metric in snapshot.get("indices", {}).items()
        if metric.get("available")
    ]
    leading_index = max(
        available_indices,
        key=lambda item: item[1].get("change_pct", -999),
        default=("לא זמין", {}),
    )
    available_sectors = [
        (name, metric)
        for name, metric in snapshot.get("sectors", {}).items()
        if metric.get("available")
    ]
    leading_sector = max(
        available_sectors,
        key=lambda item: item[1].get("change_pct", -999),
        default=("לא זמין", {}),
    )
    ranked = sorted(
        opportunity_scores.items(),
        key=lambda item: item[1].get("final_score", 50),
        reverse=True,
    )
    leaders = ", ".join(
        f"{name} ({score.get('final_score', 50)})"
        for name, score in ranked[:3]
    ) or "הדירוג אינו זמין"
    selected_news = analysis.get("selected_news", [])
    if selected_news:
        event_item = selected_news[0]
        event_text = (
            event_item.get("title")
            if event_item.get("translation_fallback")
            else event_item.get("ai_summary")
        )
        event = f"{event_item.get('source')}: {event_text}"
    else:
        event = "לא זוהה אירוע יחיד ששינה את תמונת השוק"
    ta125_change = (
        snapshot.get("indices", {}).get("ת״א-125", {}).get("change_pct")
    )
    strongest_score = ranked[0][1].get("final_score", 50) if ranked else 50
    bottom_line = (
        "השבוע היה חיובי וגם קיימים סקטורים בעלי ציון חזק, אך יש לבדוק שהמגמה נשמרת בפתיחת השבוע."
        if isinstance(ta125_change, (int, float)) and ta125_change > 0 and strongest_score >= 70
        else "התמונה מעורבת; כדאי להתמקד בפערים בין הסקטורים ולא להסיק ממדד יחיד על כל השוק."
    )
    return (
        "### השוק ב-60 שניות\n"
        f"- **תקופת הסיכום:** {_format_date(snapshot.get('window_start'))}–"
        f"{_format_date(snapshot.get('window_end'))}.\n"
        f"- **המדד הבולט:** {leading_index[0]} "
        f"({_format_pct(leading_index[1].get('change_pct'))}).\n"
        f"- **הסקטור הבולט בביצועים:** {leading_sector[0]} "
        f"({_format_pct(leading_sector[1].get('change_pct'))}).\n"
        f"- **הסקטורים המובילים לשבועות הקרובים:** {leaders}.\n"
        f"- **האירוע המרכזי:** {event.rstrip('.')}.\n"
        f"- **שורה תחתונה:** {bottom_line}"
    )


def build_weekly_opportunity_map(opportunity_scores: dict) -> str:
    if not opportunity_scores:
        return (
            "### מפת ההזדמנויות השבועית\n"
            "- ציוני ההזדמנות אינם זמינים השבוע."
        )
    ranked = sorted(
        opportunity_scores.items(),
        key=lambda item: item[1].get("final_score", 50),
        reverse=True,
    )
    rows = [
        "| # | סקטור | שבועי | ציון נוכחי | שינוי מהשבוע הקודם | ביטחון |",
        "|---:|---|---:|---:|---:|---|",
    ]
    for position, (name, score) in enumerate(ranked, 1):
        previous = score.get("previous_weekly_score")
        delta = (
            f"{int(score.get('final_score', 50)) - int(previous):+d}"
            if previous is not None else "חדש"
        )
        rows.append(
            f"| {position} | {name} | {_format_pct(score.get('weekly_change_pct'))} | "
            f"{score.get('final_score', 50)}/100 – {score.get('label', '')} | "
            f"{delta} | {_confidence_label(score.get('v2_confidence_pct'))} |"
        )
    details = [
        "#### שלושת הסקטורים המובילים לבדיקה"
    ]
    for name, score in ranked[:3]:
        details.append(
            f"- **{name} – {score.get('final_score', 50)}/100:** "
            f"{score.get('opportunity_reason')}. "
            f"רמת הביטחון {_confidence_label(score.get('v2_confidence_pct'))}. "
            f"{score.get('invalidation')}"
        )
    explanation = (
        "הציון משווה אטרקטיביות ל-2–6 שבועות ומשלב נתוני גרף, "
        "כיול היסטורי ואירוע חדשותי רק לאחר אימות המקור. הוא אינו הבטחת תשואה."
    )
    return (
        "### מפת ההזדמנויות השבועית\n"
        f"{explanation}\n" + "\n".join(rows + details)
    )


def build_plain_language_weekly_outlook(snapshot: dict, fx_rates: dict) -> str:
    ta125 = snapshot.get("indices", {}).get("ת״א-125", {})
    change = ta125.get("change_pct")
    positive_sectors = sum(
        metric.get("available") and (metric.get("change_pct") or 0) > 0
        for metric in snapshot.get("sectors", {}).values()
    )
    sector_count = sum(
        metric.get("available")
        for metric in snapshot.get("sectors", {}).values()
    )
    usd_change = fx_rates.get("USD", {}).get("change_pct")
    base = (
        f"לאחר שינוי שבועי של {_format_pct(change)} בת״א-125, תרחיש הבסיס הוא "
        "המשך חיובי מתון, כל עוד העליות נשארות רחבות בין הסקטורים."
        if isinstance(change, (int, float)) and change > 0.5 else
        f"לאחר שינוי שבועי של {_format_pct(change)} בת״א-125, תרחיש הבסיס הוא "
        "מסחר זהיר עד שיופיע שיפור רחב יותר במדדים ובסקטורים."
        if isinstance(change, (int, float)) and change < -0.5 else
        "תרחיש הבסיס הוא מסחר מעורב, עד שתתקבל מגמה ברורה יותר בת״א-125."
    )
    support = (
        f"עליות ב-{positive_sectors} מתוך {sector_count} סקטורים גם בשבוע הבא "
        "יחזקו את התרחיש החיובי."
        if sector_count else
        "שיפור רחב במספר הסקטורים העולים יחזק את התרחיש החיובי."
    )
    risk = (
        f"הדולר התחזק השבוע מול השקל בשיעור {_format_pct(usd_change)}; התחזקות נוספת "
        "לצד היחלשות במדדים עלולה להגדיל את התנודתיות."
        if isinstance(usd_change, (int, float)) and usd_change > 0.5 else
        "ירידה בת״א-125 במקביל למעבר של רוב הסקטורים לירידות תחליש את התרחיש."
    )
    return "\n".join((
        f"- **תרחיש בסיס:** {base}",
        f"- **מה עשוי לתמוך בשוק:** {support}",
        f"- **מה עלול להכביד:** {risk}",
    ))
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


def build_weekly_brief(
    data: dict,
    snapshot: dict,
    fx_rates: dict,
    analysis: dict,
    opportunity_scores: dict | None = None,
) -> str:
    start_label = _format_date(snapshot["window_start"])
    end_label = _format_date(snapshot["window_end"])
    opportunity_scores = opportunity_scores or {}
    sections = [
        build_weekly_market_in_60_seconds(
            snapshot, analysis, opportunity_scores
        )
    ]

    news_lines = [
        f"- **תקופת הסיכום:** {start_label}–{end_label}. נבחרו רק אירועים בעלי חשיבות למשקיעים."
    ]
    for item in analysis.get("selected_news", []):
        source = item.get("source") or "מקור לא ידוע"
        link = item.get("link")
        title = item.get("title") or "כתבה"
        linked_title = f"[{title}]({link})" if link else title
        published = _published_date(item.get("published"))
        date_note = f", {published}" if published else ""
        if item.get("translation_fallback"):
            news_lines.append(f"- **{source}{date_note}:** {linked_title}")
        else:
            news_lines.append(
                f"- **{source}{date_note}:** {item.get('ai_summary')} ({linked_title})"
            )
    if len(news_lines) == 1:
        news_lines.append("- לא נשמרו השבוע חדשות מהותיות בעלות מקור וקישור תקינים.")
    sections.append("### החדשות המרכזיות של השבוע\n" + "\n".join(news_lines))

    for name in INDEX_ORDER:
        metric = snapshot.get("indices", {}).get(name, {})
        if not metric.get("available"):
            lines = ["- לא קיימים מספיק נתוני סגירה לחישוב השבוע."]
        else:
            lines = [
                f"- **השינוי השבועי:** {_format_pct(metric.get('change_pct'))} על פני {metric.get('sessions')} ימי מסחר.",
                f"- **ההשוואה המדויקת:** סגירת {_format_date(metric.get('start_date'))} ברמה {_format_number(metric.get('start_close'))}, מול סגירת {_format_date(metric.get('end_date'))} ברמה {_format_number(metric.get('end_close'))}.",
            ]
        source = data.get("indices", {}).get(name, {}).get("chart_source")
        if source:
            lines.append(f"- [גרף רשמי של {name}]({source})")
        sections.append(f"### מדד {name} — ביצוע שבועי\n" + "\n".join(lines))

    fx_lines = [
        "- השינוי מחושב בין השער היציג הראשון שפרסם בנק ישראל בשבוע לבין השער היציג האחרון שפרסם בו."
    ]
    for currency in ("USD", "EUR"):
        metric = fx_rates.get(currency, {})
        label = "דולר/שקל" if currency == "USD" else "אירו/שקל"
        if not metric.get("available"):
            fx_lines.append(f"- **{label}:** אין עדיין מספיק תצפיות שבועיות לחישוב.")
            continue
        fx_lines.append(
            f"- **{label}:** {_format_number(metric.get('start_rate'), 4)} ב-{_format_date(metric.get('start_date'))}, "
            f"לעומת {_format_number(metric.get('end_rate'), 4)} ב-{_format_date(metric.get('end_date'))}; "
            f"שינוי שבועי {_format_pct(metric.get('change_pct'))}."
        )
    source = data.get("exchange_rates", {}).get("source")
    if source:
        fx_lines.append(f"- [מקור: בנק ישראל]({source})")
    sections.append("### מטבע חוץ — שינוי שבועי\n" + "\n".join(fx_lines))

    sections.append(build_weekly_opportunity_map(opportunity_scores))

    sector_lines = [
        f"- **תקופת המדידה:** שבוע המסחר שבין {start_label} ל-{end_label}; החישוב הוא מסגירת הבסיס שלפני פתיחת השבוע ועד הסגירה האחרונה."
    ]
    for name, metric in snapshot.get("sectors", {}).items():
        factor = analysis.get("sector_factors", {}).get(name, {}).get("text")
        if metric.get("available"):
            sentence = (
                f"**{name}: {_format_pct(metric.get('change_pct'))}.** "
                f"הסקטור נסחר השבוע במשך {metric.get('sessions')} ימי מסחר"
            )
            sentence += f"; ברקע בלט: {factor.rstrip('.')}." if factor else "; לא נמצא אירוע חדשותי ישיר ומהותי שמסביר לבדו את השינוי."
        else:
            sentence = f"**{name}:** אין מספיק נתוני סגירה לחישוב שבועי אמין."
        sector_lines.append(f"- {sentence}")
    sections.append("### סקירת הסקטורים\n" + "\n".join(sector_lines))

    sections.append(
        "### מבט לשבוע הבא\n"
        + build_plain_language_weekly_outlook(snapshot, fx_rates)
    )
    return "\n\n".join(sections)
