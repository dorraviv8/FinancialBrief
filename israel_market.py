"""Israeli market data collection and quantitative trend helpers.

Primary market data comes from the public Tel Aviv Stock Exchange interfaces.
MAYA disclosures and Bank of Israel exchange rates are also fetched directly
from their official public endpoints. Financial press is used only for context.
"""

from __future__ import annotations

import math
import json
import os
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
TASE_API_BASE = "https://api.tase.co.il/api"
MAYA_API_BASE = "https://maya.tase.co.il/api/v1"
BOI_RATES_URL = "https://www.boi.org.il/PublicApi/GetExchangeRates?asXml=false"
BOI_INTEREST_URL = "https://www.boi.org.il/PublicApi/GetInterest"

TASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://market.tase.co.il",
    "Referer": "https://market.tase.co.il/",
    "User-Agent": "Mozilla/5.0 (compatible; FinancialBrief/2.0)",
}

# Official TASE identifiers. The broad end-of-day indices fill classification
# gaps where TASE does not publish a single modern live sector index.
CORE_INDICES = {
    "ת״א-35": "142",
    "ת״א-90": "143",
    "ת״א-125": "137",
}

SECTOR_INDICES = {
    "טכנולוגיה": "169",
    "ביומד": "167",
    "בנקים": "013",
    "ביטוח ושירותים פיננסיים": "171",
    "נדל״ן ובנייה": "149",
    "תעשייה": "178",
    "מסחר ושירותים": "041",
    "השקעות ואחזקות": "117",
    "אנרגיה ותשתיות": "180",
    "נפט וגז": "170",
}


GENERIC_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (compatible; FinancialBrief/2.0)",
}

_SESSION_LOCAL = threading.local()
_STATS_LOCK = threading.Lock()
_COLLECTION_STATS = {
    "http_requests": 0,
    "component_same_day_hits": 0,
    "history_same_day_hits": 0,
    "history_incremental_refreshes": 0,
    "history_full_refreshes": 0,
}

_default_data_dir = os.path.dirname(
    os.path.abspath(
        os.getenv(
            "FINANCIAL_BRIEF_DB_PATH",
            os.path.join(os.path.dirname(__file__), "subscribers.db"),
        )
    )
)
HISTORY_CACHE_DIR = Path(
    os.getenv(
        "FINANCIAL_BRIEF_CACHE_DIR",
        os.path.join(_default_data_dir, "market-cache"),
    )
)
HISTORY_CACHE_ENABLED = os.getenv("TASE_HISTORY_CACHE", "1") != "0"
HISTORY_FULL_REFRESH_DAYS = max(
    1, int(os.getenv("TASE_HISTORY_FULL_REFRESH_DAYS", "7"))
)
HISTORY_RETENTION_DAYS = max(
    400, int(os.getenv("TASE_HISTORY_RETENTION_DAYS", "1830"))
)


def _increment_stat(name: str) -> None:
    with _STATS_LOCK:
        _COLLECTION_STATS[name] += 1


def _reset_collection_stats() -> None:
    with _STATS_LOCK:
        for name in _COLLECTION_STATS:
            _COLLECTION_STATS[name] = 0


def get_collection_stats() -> dict:
    with _STATS_LOCK:
        return dict(_COLLECTION_STATS)


def _session(headers: dict | None = None) -> requests.Session:
    header_key = "tase" if headers is TASE_HEADERS else "generic"
    sessions = getattr(_SESSION_LOCAL, "sessions", None)
    if sessions is None:
        sessions = {}
        _SESSION_LOCAL.sessions = sessions
    if header_key in sessions:
        return sessions[header_key]

    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(headers or GENERIC_HEADERS)
    sessions[header_key] = session
    return session


def _request_json(method: str, url: str, headers: dict | None = None, **kwargs) -> Any:
    timeout = kwargs.pop("timeout", 15)
    try:
        _increment_stat("http_requests")
        response = _session(headers).request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return {} if method.upper() == "GET" else {}


def _tase_get(path: str, params: dict | None = None) -> Any:
    return _request_json(
        "GET", f"{TASE_API_BASE}/{path}", headers=TASE_HEADERS, params=params
    )


def _tase_post(path: str, payload: dict) -> Any:
    return _request_json(
        "POST", f"{TASE_API_BASE}/{path}", headers=TASE_HEADERS, json=payload
    )


def _localized_items(payload: Any) -> list[dict]:
    """Return the English list from a TASE localized response."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("en-US", "en", "he-IL", "he"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def get_index_components(index_id: str) -> list[dict]:
    cache = _read_daily_list_cache("index-components", index_id)
    if cache is not None:
        _increment_stat("component_same_day_hits")
        return cache

    payload = {
        "dType": 1,
        "TotalRec": 1,
        "pageNum": 1,
        "oId": str(index_id),
        "lang": "1",
    }
    data = _tase_post("index/components", payload)
    if not isinstance(data, dict):
        return []
    items = list(data.get("Items", []))
    total = int(data.get("TotalRec") or len(items))
    page_size = max(1, len(items))
    pages = min(25, math.ceil(total / page_size))
    for page in range(2, pages + 1):
        payload["pageNum"] = page
        next_page = _tase_post("index/components", payload)
        if not isinstance(next_page, dict):
            break
        page_items = next_page.get("Items", [])
        if not page_items:
            break
        items.extend(page_items)
    _write_daily_list_cache("index-components", index_id, items)
    return items


def get_index_constituents(index_id: str) -> list[dict]:
    data = _tase_get("index/securities/index", {"indexId": str(index_id)})
    return _localized_items(data)


def _daily_list_cache_path(kind: str, object_id: str) -> Path:
    safe_id = "".join(character for character in str(object_id) if character.isalnum())
    return HISTORY_CACHE_DIR / f"{kind}-{safe_id}.json"


def _read_daily_list_cache(kind: str, object_id: str) -> list[dict] | None:
    if not HISTORY_CACHE_ENABLED:
        return None
    try:
        payload = json.loads(
            _daily_list_cache_path(kind, object_id).read_text("utf-8")
        )
        if (
            payload.get("version") != 1
            or payload.get("updated_date")
            != datetime.now(ISRAEL_TZ).date().isoformat()
            or not isinstance(payload.get("items"), list)
        ):
            return None
        return payload["items"]
    except (OSError, ValueError, TypeError):
        return None


def _write_daily_list_cache(kind: str, object_id: str, items: list[dict]) -> None:
    if not HISTORY_CACHE_ENABLED:
        return
    target = _daily_list_cache_path(kind, object_id)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = {
        "version": 1,
        "updated_date": datetime.now(ISRAEL_TZ).date().isoformat(),
        "items": items,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _history_cache_path(path: str, object_id: str) -> Path:
    kind = path.replace("/", "-")
    safe_id = "".join(character for character in str(object_id) if character.isalnum())
    return HISTORY_CACHE_DIR / f"{kind}-{safe_id}.json"


def _read_history_cache(path: str, object_id: str) -> dict | None:
    if not HISTORY_CACHE_ENABLED:
        return None
    try:
        payload = json.loads(_history_cache_path(path, object_id).read_text("utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("items"), list):
            return None
        return payload
    except (OSError, ValueError, TypeError):
        return None


def _write_history_cache(
    path: str,
    object_id: str,
    items: list[dict],
    full_refresh_date: str,
) -> None:
    if not HISTORY_CACHE_ENABLED:
        return
    target = _history_cache_path(path, object_id)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = {
        "version": 1,
        "updated_date": datetime.now(ISRAEL_TZ).date().isoformat(),
        "full_refresh_date": full_refresh_date,
        "items": items,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _deduplicate_history(items: list[dict]) -> list[dict]:
    by_date = {}
    cutoff = datetime.now(ISRAEL_TZ).date() - timedelta(
        days=HISTORY_RETENTION_DAYS
    )
    for item in items:
        trade_date = item.get("TradeDate")
        if not trade_date:
            continue
        try:
            parsed = datetime.strptime(trade_date, "%d/%m/%Y").date()
        except (TypeError, ValueError):
            continue
        if parsed >= cutoff:
            by_date[trade_date] = item
    return list(by_date.values())


def _get_tase_history(path: str, object_id: str, period_type: int = 4) -> list[dict]:
    """Fetch and de-duplicate every page in a TASE historical period.

    TASE period type 4 covers the trailing year. The public API returns up to
    30 sessions per page, so using only its first response would materially
    distort long-term indicators.
    """
    today = datetime.now(ISRAEL_TZ).date()
    cache = _read_history_cache(path, object_id)
    if cache and cache.get("updated_date") == today.isoformat():
        _increment_stat("history_same_day_hits")
        return cache["items"]

    full_refresh_due = True
    if cache:
        try:
            last_full_refresh = datetime.strptime(
                cache.get("full_refresh_date", ""), "%Y-%m-%d"
            ).date()
            full_refresh_due = (today - last_full_refresh).days >= HISTORY_FULL_REFRESH_DAYS
        except (TypeError, ValueError):
            pass

    payload = {
        "pType": period_type,
        "TotalRec": 1,
        "pageNum": 1,
        "oId": str(object_id),
        "lang": "1",
    }
    data = _tase_post(path, payload)
    if not isinstance(data, dict):
        return []

    items = list(data.get("Items", []))
    if cache and not full_refresh_due:
        items = _deduplicate_history(items + cache["items"])
        _write_history_cache(
            path,
            object_id,
            items,
            cache.get("full_refresh_date") or today.isoformat(),
        )
        _increment_stat("history_incremental_refreshes")
        return items

    total = int(data.get("TotalRec") or len(items))
    page_size = max(1, len(items))
    pages = min(20, math.ceil(total / page_size))
    for page in range(2, pages + 1):
        payload["pageNum"] = page
        next_page = _tase_post(path, payload)
        if not isinstance(next_page, dict):
            break
        page_items = next_page.get("Items", [])
        if not page_items:
            break
        items.extend(page_items)

    # The endpoint supplies a trailing window. Keep older audited cache points
    # so the recommendation engine grows toward a multi-year sample over time.
    items = _deduplicate_history(items + (cache.get("items", []) if cache else []))
    _write_history_cache(path, object_id, items, today.isoformat())
    _increment_stat("history_full_refreshes")
    return items


def get_index_history(index_id: str) -> list[dict]:
    """Fetch one year of official end-of-day index graph data."""
    return _get_tase_history("index/historyeod", index_id, period_type=4)


def get_security_history(security_number: str) -> list[dict]:
    """Fetch one year of official end-of-day security graph data."""
    return _get_tase_history("security/historyeod", security_number, period_type=4)


def normalize_security_history_to_ils(history: list[dict]) -> list[dict]:
    """Convert agorot to ILS and apply TASE corporate-action adjustments."""
    normalized_history = []
    for row in history:
        normalized = dict(row)
        try:
            raw_close = float(row.get("CloseRate"))
            adjusted_close = float(row.get("AdjustmentRate") or raw_close)
            adjustment_factor = adjusted_close / raw_close if raw_close else 1.0
        except (TypeError, ValueError):
            adjustment_factor = 1.0
        for field in ("BaseRate", "OpenRate", "CloseRate", "HighRate", "LowRate"):
            try:
                normalized[field] = float(row[field]) * adjustment_factor / 100.0
            except (KeyError, TypeError, ValueError):
                pass
        normalized_history.append(normalized)
    return normalized_history


def get_index_major_data(index_id: str) -> dict:
    data = _tase_get("index/majordata", {"indexId": str(index_id), "lang": 1})
    return data if isinstance(data, dict) else {}


def _pct_change(current: float, previous: float) -> float:
    if not previous:
        return 0.0
    return ((current / previous) - 1.0) * 100.0


def _simple_moving_average(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * multiplier) + (result[-1] * (1 - multiplier)))
    return result


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    seed = changes[-period:]
    average_gain = sum(max(change, 0) for change in seed) / period
    average_loss = sum(max(-change, 0) for change in seed) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _trendline_change(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    sample = values[-period:]
    x_mean = (period - 1) / 2
    y_mean = sum(sample) / period
    denominator = sum((index - x_mean) ** 2 for index in range(period))
    if not denominator or not y_mean:
        return None
    slope = sum(
        (index - x_mean) * (value - y_mean)
        for index, value in enumerate(sample)
    ) / denominator
    return (slope * (period - 1) / y_mean) * 100


def _period_return(closes: list[float], sessions: int, use_available: bool = False) -> float | None:
    if len(closes) > sessions:
        return _pct_change(closes[-1], closes[-(sessions + 1)])
    if use_available and len(closes) >= 200:
        return _pct_change(closes[-1], closes[0])
    return None


def calculate_trend_metrics(history: list[dict]) -> dict:
    """Calculate one-year chart insights and transparent technical indicators.

    The forecast range is a one-standard-deviation, 15-session range based on
    recent realized volatility. It is a scenario aid, not a price target.
    """
    observations = []
    for row in history:
        close = row.get("CloseRate")
        if close is None:
            continue
        try:
            dt = datetime.strptime(row["TradeDate"], "%d/%m/%Y")
            observations.append({
                "date": dt,
                "close": float(close),
                "high": float(row.get("HighRate") or close),
                "low": float(row.get("LowRate") or close),
                "turnover": float(row.get("TurnOverValueShekel") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue

    observations.sort(key=lambda item: item["date"])
    closes = [item["close"] for item in observations]
    if len(closes) < 2:
        return {"available": False}

    daily_returns = [
        _pct_change(closes[i], closes[i - 1]) for i in range(1, len(closes))
    ]
    one_day = daily_returns[-1]
    five_day = _period_return(closes, 5)
    twenty_day = _period_return(closes, 20)
    three_month = _period_return(closes, 63)
    six_month = _period_return(closes, 126)
    one_year = _period_return(closes, 252, use_available=True)
    period_return = _pct_change(closes[-1], closes[0])
    daily_vol = statistics.stdev(daily_returns) if len(daily_returns) >= 2 else 0.0
    annualized_vol = daily_vol * math.sqrt(252)
    forecast_range = daily_vol * math.sqrt(15)
    positive_ratio = sum(value > 0 for value in daily_returns) / len(daily_returns)

    def maximum_drawdown(sample: list[float]) -> float:
        peak = sample[0]
        drawdown = 0.0
        for value in sample:
            peak = max(peak, value)
            drawdown = min(drawdown, _pct_change(value, peak))
        return drawdown

    moving_averages = {
        period: _simple_moving_average(closes, period) for period in (20, 50, 200)
    }
    ema_12 = _ema_series(closes, 12)
    ema_26 = _ema_series(closes, 26)
    macd_series = [fast - slow for fast, slow in zip(ema_12, ema_26)]
    signal_series = _ema_series(macd_series, 9)
    macd_value = macd_series[-1] if macd_series else None
    macd_signal = signal_series[-1] if signal_series else None
    rsi_value = _rsi(closes)

    support_window = observations[-60:]
    support = min(item["low"] for item in support_window)
    resistance = max(item["high"] for item in support_window)
    turnovers = [item["turnover"] for item in observations if item["turnover"] > 0]
    latest_turnover = observations[-1]["turnover"]
    average_turnover_20 = (
        sum(turnovers[-20:]) / min(20, len(turnovers)) if turnovers else None
    )
    volume_ratio = (
        latest_turnover / average_turnover_20
        if latest_turnover and average_turnover_20
        else None
    )

    momentum = five_day if five_day is not None else period_return
    if twenty_day is not None:
        momentum = (0.6 * five_day) + (0.4 * twenty_day)
    if momentum > 1.0 and positive_ratio >= 0.50:
        bias = "חיובית"
    elif momentum < -1.0 and positive_ratio <= 0.50:
        bias = "שלילית"
    else:
        bias = "ניטרלית"

    return {
        "available": True,
        "as_of": observations[-1]["date"].strftime("%Y-%m-%d"),
        "sessions": len(closes),
        "last_close": round(closes[-1], 2),
        "change_1d_pct": round(one_day, 2),
        "return_5d_pct": round(five_day, 2) if five_day is not None else None,
        "return_20d_pct": round(twenty_day, 2) if twenty_day is not None else None,
        "return_3m_pct": round(three_month, 2) if three_month is not None else None,
        "return_6m_pct": round(six_month, 2) if six_month is not None else None,
        "return_1y_pct": round(one_year, 2) if one_year is not None else None,
        "period_return_pct": round(period_return, 2),
        "positive_sessions_pct": round(positive_ratio * 100, 1),
        "annualized_volatility_pct": round(annualized_vol, 2),
        "sma_20": round(moving_averages[20], 2) if moving_averages[20] else None,
        "sma_50": round(moving_averages[50], 2) if moving_averages[50] else None,
        "sma_200": round(moving_averages[200], 2) if moving_averages[200] else None,
        "above_sma_20": closes[-1] > moving_averages[20] if moving_averages[20] else None,
        "above_sma_50": closes[-1] > moving_averages[50] if moving_averages[50] else None,
        "above_sma_200": closes[-1] > moving_averages[200] if moving_averages[200] else None,
        "distance_sma_20_pct": (
            round(_pct_change(closes[-1], moving_averages[20]), 2)
            if moving_averages[20]
            else None
        ),
        "distance_sma_50_pct": (
            round(_pct_change(closes[-1], moving_averages[50]), 2)
            if moving_averages[50]
            else None
        ),
        "distance_sma_200_pct": (
            round(_pct_change(closes[-1], moving_averages[200]), 2)
            if moving_averages[200]
            else None
        ),
        "max_drawdown_60d_pct": round(maximum_drawdown(closes[-60:]), 2),
        "max_drawdown_period_pct": round(maximum_drawdown(closes), 2),
        "rsi_14": round(rsi_value, 1) if rsi_value is not None else None,
        "macd": round(macd_value, 3) if macd_value is not None else None,
        "macd_signal": round(macd_signal, 3) if macd_signal is not None else None,
        "macd_histogram": (
            round(macd_value - macd_signal, 3)
            if macd_value is not None and macd_signal is not None
            else None
        ),
        "support_60d": round(support, 2),
        "resistance_60d": round(resistance, 2),
        "turnover_vs_20d_avg": round(volume_ratio, 2) if volume_ratio else None,
        "trendline_60d_pct": (
            round(_trendline_change(closes, 60), 2)
            if _trendline_change(closes, 60) is not None
            else None
        ),
        "trendline_200d_pct": (
            round(_trendline_change(closes, 200), 2)
            if _trendline_change(closes, 200) is not None
            else None
        ),
        "directional_bias": bias,
        "forecast_horizon_sessions": 15,
        "historical_volatility_range_pct": round(forecast_range, 2),
        "forecast_note": "טווח סטטיסטי על בסיס תנודתיות עבר; אינו יעד מחיר או הבטחה",
    }


def _history_close_series(history: list[dict]) -> list[dict]:
    """Return a small, chronological close series for local score evaluation."""
    points = {}
    for row in history:
        try:
            trade_date = datetime.strptime(row["TradeDate"], "%d/%m/%Y").date()
            close_value = float(row["CloseRate"])
        except (KeyError, TypeError, ValueError):
            continue
        if close_value > 0:
            points[trade_date.isoformat()] = round(close_value, 6)
    return [
        {"date": trade_date, "close": points[trade_date]}
        for trade_date in sorted(points)
    ]


def _component_lookup(components: list[dict]) -> tuple[dict, dict]:
    by_symbol = {}
    by_number = {}
    for component in components:
        symbol = str(component.get("Symbol") or "").strip().upper()
        number = str(component.get("SecurityNumber") or "").lstrip("0")
        if symbol:
            by_symbol[symbol] = component
        if number:
            by_number[number] = component
    return by_symbol, by_number


def _merge_constituents(components: list[dict], live_rows: list[dict]) -> list[dict]:
    by_symbol, by_number = _component_lookup(components)
    merged = []
    seen = set()
    for live in live_rows:
        symbol = str(live.get("Symbol") or "").strip().upper()
        number = str(live.get("Id") or "").lstrip("0")
        component = by_symbol.get(symbol) or by_number.get(number) or {}
        key = symbol or number
        seen.add(key)
        merged.append({**component, **live})

    for component in components:
        symbol = str(component.get("Symbol") or "").strip().upper()
        number = str(component.get("SecurityNumber") or "").lstrip("0")
        if (symbol or number) not in seen:
            merged.append(component)
    return merged


def _stock_summary(row: dict) -> dict:
    last_rate = row.get("LastRate")
    market_value = row.get("MarketValue")
    try:
        price_ils = round(float(last_rate) / 100.0, 2)
    except (TypeError, ValueError):
        price_ils = None
    return {
        "name": row.get("ShortName") or row.get("Name"),
        "symbol": row.get("Symbol"),
        "security_number": row.get("SecurityNumber") or row.get("Id"),
        "price_ils": price_ils,
        "change_1d_pct": row.get("Change"),
        "market_cap_m_ils": market_value,
        "index_weight_pct": row.get("Weight"),
        "avg_daily_turnover_6m_ils": row.get("SemiAnnDailyTurnOverAvg"),
    }


def build_index_analysis(
    name: str,
    index_id: str,
    top_stock_limit: int = 3,
    include_technicals: bool = True,
) -> dict:
    history = get_index_history(index_id) if include_technicals else []
    components = get_index_components(index_id)
    live_rows = get_index_constituents(index_id)
    major = get_index_major_data(index_id)
    merged = _merge_constituents(components, live_rows)

    changes = []
    for row in merged:
        try:
            changes.append(float(row.get("Change")))
        except (TypeError, ValueError):
            continue

    ranked_by_cap = sorted(
        components,
        key=lambda row: float(row.get("MarketValue") or 0),
        reverse=True,
    )
    merged_by_symbol = {
        str(row.get("Symbol") or "").upper(): row for row in merged
    }
    top_stocks = []
    for component in ranked_by_cap[:top_stock_limit]:
        symbol = str(component.get("Symbol") or "").upper()
        top_stocks.append(_stock_summary(merged_by_symbol.get(symbol, component)))

    last_days = major.get("LastDaysData", []) if isinstance(major, dict) else []
    latest = last_days[0] if last_days else {}
    top_weights = sorted(
        (float(row.get("Weight") or 0) for row in components), reverse=True
    )
    trend = calculate_trend_metrics(history) if include_technicals else {"available": False}

    return {
        "name": name,
        "index_id": str(index_id),
        "last_value": latest.get("LastRate") or trend.get("last_close"),
        "change_1d_pct": latest.get("Change"),
        "turnover_k_ils": latest.get("TurnOver"),
        "constituents_count": len(components) or len(live_rows),
        "breadth": {
            "advancers": sum(value > 0 for value in changes),
            "decliners": sum(value < 0 for value in changes),
            "unchanged": sum(value == 0 for value in changes),
        },
        "top_5_weight_pct": round(sum(top_weights[:5]), 2),
        "trend": trend,
        # Kept out of AI prompts; used locally to settle prior score predictions
        # after exactly 10, 20 and 30 TASE trading sessions.
        "history_closes": _history_close_series(history),
        "top_stocks_by_market_cap": top_stocks,
        "top_gainers": [
            _stock_summary(row)
            for row in sorted(
                merged,
                key=lambda item: float(item.get("Change") or -999),
                reverse=True,
            )[:3]
        ],
        "top_decliners": [
            _stock_summary(row)
            for row in sorted(
                merged,
                key=lambda item: float(item.get("Change") or 999),
            )[:3]
        ],
        "source": f"https://market.tase.co.il/en/market_data/index/{index_id}/major_data",
        "chart_source": f"https://market.tase.co.il/en/market_data/index/{index_id}/historical_data",
    }


def _collect_group(index_map: dict[str, str], include_technicals: bool = True) -> dict:
    results = {}
    workers = max(1, min(int(os.getenv("TASE_MAX_WORKERS", "3")), len(index_map)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                build_index_analysis,
                name,
                index_id,
                3,
                include_technicals,
            ): name
            for name, index_id in index_map.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = {"name": name, "available": False, "error": str(exc)}
    return {name: results.get(name, {}) for name in index_map}


def get_core_indices(include_technicals: bool = True) -> dict:
    return _collect_group(CORE_INDICES, include_technicals=include_technicals)


def get_sector_analysis(include_technicals: bool = True) -> dict:
    return _collect_group(SECTOR_INDICES, include_technicals=include_technicals)


def enrich_stock_technicals(sectors: dict) -> None:
    """Attach one-year chart analysis to each unique referenced top stock."""
    references: dict[str, list[dict]] = {}
    for sector in sectors.values():
        for stock in sector.get("top_stocks_by_market_cap", []):
            security_number = str(stock.get("security_number") or "").strip()
            if security_number:
                references.setdefault(security_number, []).append(stock)

    if not references:
        return

    workers = max(1, min(int(os.getenv("TASE_STOCK_WORKERS", "3")), len(references)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(get_security_history, security_number): security_number
            for security_number in references
        }
        for future in as_completed(futures):
            security_number = futures[future]
            try:
                history_ils = normalize_security_history_to_ils(future.result())
                technicals = calculate_trend_metrics(history_ils)
            except Exception as exc:
                technicals = {"available": False, "error": str(exc)}
            for stock in references[security_number]:
                stock["technical_analysis"] = technicals
                stock["chart_source"] = (
                    "https://market.tase.co.il/en/market_data/security/"
                    f"{security_number}/historical_data"
                )


def get_boi_exchange_rates() -> dict:
    data = _request_json("GET", BOI_RATES_URL)
    rates = {}
    if isinstance(data, dict):
        for item in data.get("exchangeRates", []):
            if item.get("key") in {"USD", "EUR", "GBP"}:
                rates[item["key"]] = {
                    "ils_rate": item.get("currentExchangeRate"),
                    "change_pct": item.get("currentChange"),
                    "unit": item.get("unit"),
                    "last_update": item.get("lastUpdate"),
                }
    return {
        "rates": rates,
        "source": "https://www.boi.org.il/en/economic-roles/financial-markets/exchange-rates/",
    }


def get_boi_interest_rate() -> dict:
    """Fetch the current official policy rate without scraping prose."""
    data = _request_json("GET", BOI_INTEREST_URL)
    return {
        "current_interest_pct": (
            data.get("currentInterest") if isinstance(data, dict) else None
        ),
        "next_decision_date": (
            data.get("nextInterestDate") if isinstance(data, dict) else None
        ),
        "source": "https://www.boi.org.il/PublicApi/GetInterest",
    }


def get_maya_announcements(limit: int = 5) -> list[dict]:
    safe_limit = max(1, min(limit, 5))
    data = _request_json(
        "GET",
        f"{MAYA_API_BASE}/reports/breaking-announcement",
        params={"limit": safe_limit},
    )
    if not isinstance(data, list):
        return []

    announcements = []
    for item in data:
        companies = [company.get("name") for company in item.get("companies", [])]
        attachments = item.get("attachments", [])
        attachment = attachments[0].get("url") if attachments else None
        link = (
            f"https://mayafiles.tase.co.il/{attachment}"
            if attachment
            else f"https://maya.tase.co.il/he/reports/companies/{item.get('id')}"
        )
        announcements.append({
            "source": "מאיה – הבורסה לניירות ערך",
            "reliability": "official",
            "title": item.get("title"),
            "companies": [name for name in companies if name],
            "published": item.get("publishDate"),
            "link": link,
        })
    return announcements


def _google_news_url(domain: str, topic: str = "בורסה OR כלכלה OR מניות") -> str:
    query = quote_plus(f"site:{domain} ({topic})")
    return f"https://news.google.com/rss/search?q={query}&hl=he&gl=IL&ceid=IL:he"


NEWS_FEEDS = [
    ("בנק ישראל", "official", _google_news_url("boi.org.il")),
    ("הלשכה המרכזית לסטטיסטיקה", "official", _google_news_url("cbs.gov.il")),
    ("משרד האוצר", "official", _google_news_url("gov.il", "משרד האוצר כלכלה")),
    ("גלובס", "financial_press", _google_news_url("globes.co.il")),
    ("כלכליסט", "financial_press", _google_news_url("calcalist.co.il")),
    ("TheMarker", "financial_press", _google_news_url("themarker.com")),
    ("Bizportal", "financial_press", _google_news_url("bizportal.co.il")),
    ("Funder", "financial_press", _google_news_url("funder.co.il")),
]


def get_israeli_news(hours: int = 72, per_source: int = 3, limit: int = 18) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    headlines = []
    seen = set()
    for source, reliability, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": TASE_HEADERS["User-Agent"]})
            accepted = 0
            for entry in feed.entries:
                title = str(entry.get("title") or "").strip()
                normalized = " ".join(title.lower().split())
                if not title or normalized in seen:
                    continue
                published_tuple = entry.get("published_parsed") or entry.get("updated_parsed")
                if not published_tuple:
                    continue
                published = datetime(*published_tuple[:6], tzinfo=timezone.utc)
                if published < cutoff:
                    continue
                seen.add(normalized)
                headlines.append({
                    "source": source,
                    "reliability": reliability,
                    "title": title,
                    "published": published.isoformat(),
                    "link": entry.get("link"),
                })
                accepted += 1
                if accepted >= per_source:
                    break
        except Exception:
            continue
    headlines.sort(key=lambda item: item["published"], reverse=True)
    return headlines[:limit]


def collect_israeli_market_data(
    include_news: bool = True,
    include_technicals: bool = True,
) -> dict:
    _reset_collection_stats()
    now = datetime.now(ISRAEL_TZ)
    indices = get_core_indices(include_technicals=include_technicals)
    sectors = get_sector_analysis(include_technicals=include_technicals)
    if include_technicals:
        enrich_stock_technicals(sectors)
    data = {
        "as_of": now.isoformat(timespec="seconds"),
        "market_scope": "Israel / Tel Aviv Stock Exchange",
        "indices": indices,
        "sectors": sectors,
        "exchange_rates": get_boi_exchange_rates(),
        "boi_interest": get_boi_interest_rate(),
        "maya_announcements": get_maya_announcements(),
        "news": get_israeli_news() if include_news else [],
        "collection_stats": get_collection_stats(),
        "methodology": {
            "profile": "balanced",
            "horizon": "several weeks / 15 trading sessions",
            "top_stocks": "three largest constituents by official TASE market capitalization",
            "charts": "a one-year official TASE query window for every referenced index and stock; shorter available listing history is labeled",
            "technicals": "1/3/6/12-month returns, SMA 20/50/200, RSI 14, MACD 12/26/9, 60-session support/resistance, turnover and regression trend lines",
            "forecast": "technical, breadth and historical-volatility scenarios; not guaranteed",
        },
    }
    return data
