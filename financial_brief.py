#!/usr/bin/env python3
"""
Financial Morning Brief Agent
Collects financial data from multiple free sources and sends a Hebrew daily brief via email.
"""

import os
import json
import smtplib
import requests
import feedparser
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from groq import Groq
import re
import database

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY")
FRED_API_KEY      = os.getenv("FRED_API_KEY")
FINNHUB_KEY       = os.getenv("FINNHUB_KEY")
GMAIL_USER        = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD= os.getenv("GMAIL_APP_PASSWORD")
OWNER_NAME        = os.getenv("OWNER_NAME", "Admin")
OWNER_EMAIL       = os.getenv("OWNER_EMAIL")
BASE_URL          = os.getenv("BASE_URL", "http://localhost:5000")

TODAY          = datetime.now().strftime("%d/%m/%Y")
TODAY_ISO      = datetime.now().strftime("%Y-%m-%d")
TODAY_SHORT    = datetime.now().strftime("%d.%m")
TOMORROW_SHORT = (datetime.now() + timedelta(days=1)).strftime("%d.%m")

# ── Trading day awareness ─────────────────────────────────────────────────────
# The brief runs at 07:00 Israel time. US markets close at ~midnight Israel time
# (16:00 ET). Israeli markets close at ~17:15 Israel time.
# At 07:00, NEITHER market is open — all data is from the previous session close.

def _last_us_trading_day() -> datetime:
    """Most recent day US markets (Mon-Fri) were open, as of 07:00 Israel time."""
    wd = datetime.now().weekday()   # Mon=0 … Sun=6
    if wd == 0: return datetime.now() - timedelta(days=3)   # Mon  → Fri
    if wd == 5: return datetime.now() - timedelta(days=1)   # Sat  → Fri
    if wd == 6: return datetime.now() - timedelta(days=2)   # Sun  → Fri
    return datetime.now() - timedelta(days=1)               # Tue–Fri → yesterday

def _last_il_trading_day() -> datetime:
    """Most recent day Israeli markets (Sun-Thu) were open, as of 07:00 Israel time."""
    wd = datetime.now().weekday()
    if wd == 4: return datetime.now() - timedelta(days=1)   # Fri  → Thu
    if wd == 5: return datetime.now() - timedelta(days=2)   # Sat  → Thu
    if wd == 6: return datetime.now() - timedelta(days=3)   # Sun  → Thu
    return datetime.now() - timedelta(days=1)               # Mon–Thu → yesterday

_HE_DAYS = {0: "יום שני", 1: "יום שלישי", 2: "יום רביעי",
            3: "יום חמישי", 4: "יום שישי", 5: "שבת", 6: "יום ראשון"}

LAST_US_CLOSE    = _last_us_trading_day().strftime("%d.%m")
LAST_IL_CLOSE    = _last_il_trading_day().strftime("%d.%m")
LAST_US_DAY_HE   = _HE_DAYS[_last_us_trading_day().weekday()]
LAST_IL_DAY_HE   = _HE_DAYS[_last_il_trading_day().weekday()]
IL_OPEN_TODAY    = datetime.now().weekday() in [0, 1, 2, 3, 6]   # Sun–Thu
US_OPEN_TODAY    = datetime.now().weekday() in [0, 1, 2, 3, 4]   # Mon–Fri


# ── 1. Market Snapshot (yfinance) ─────────────────────────────────────────────
def get_market_snapshot():
    """Fetch key indices, futures, and currencies."""
    tickers = {
        "SPY (S&P 500)":  "SPY",
        "QQQ (Nasdaq)":   "QQQ",
        "Dow Jones":      "^DJI",
        "Russell 2000":   "^RUT",
        "VIX":            "^VIX",
        "S&P500 Futures": "ES=F",
        "Nasdaq Futures": "NQ=F",
        "USD/ILS":        "ILS=X",
        "EUR/USD":        "EURUSD=X",
        "Gold":           "GC=F",
        "Oil (WTI)":      "CL=F",
        "Bitcoin":        "BTC-USD",
    }
    results = {}
    for name, symbol in tickers.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev_close = hist["Close"].iloc[-2]
                last_price = hist["Close"].iloc[-1]
                change_pct = ((last_price - prev_close) / prev_close) * 100
                results[name] = {
                    "price": round(float(last_price), 2),
                    "change_pct": round(float(change_pct), 2),
                    "arrow": "▲" if change_pct >= 0 else "▼"
                }
            elif len(hist) == 1:
                results[name] = {
                    "price": round(float(hist["Close"].iloc[-1]), 2),
                    "change_pct": 0.0,
                    "arrow": "–"
                }
        except Exception:
            results[name] = {"price": "N/A", "change_pct": 0.0, "arrow": "–"}
    return results


# ── 2. Global Markets (yfinance) ──────────────────────────────────────────────
def get_global_markets():
    """Fetch major global indices."""
    global_tickers = {
        "FTSE 100 (לונדון)":    "^FTSE",
        "DAX (גרמניה)":          "^GDAXI",
        "CAC 40 (צרפת)":         "^FCHI",
        "Nikkei 225 (יפן)":      "^N225",
        "Hang Seng (הונג קונג)": "^HSI",
        "Shanghai (סין)":        "000001.SS",
        "TA-125 (תל אביב)":      "^TA125.TA",
    }
    results = {}
    for name, symbol in global_tickers.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev_close = hist["Close"].iloc[-2]
                last_price = hist["Close"].iloc[-1]
                change_pct = ((last_price - prev_close) / prev_close) * 100
                results[name] = {
                    "price": round(float(last_price), 2),
                    "change_pct": round(float(change_pct), 2),
                    "arrow": "▲" if change_pct >= 0 else "▼"
                }
        except Exception:
            results[name] = {"price": "N/A", "change_pct": 0.0, "arrow": "–"}
    return results


# ── 2b. TASE Trending Stocks (yfinance) ──────────────────────────────────────
def get_tase_stocks():
    """Fetch key Israeli stocks from Tel Aviv Stock Exchange."""
    tase_tickers = {
        "טבע":          "TEVA.TA",
        "נייס סיסטמס":  "NICE.TA",
        "ICL":          "ICL.TA",
        "אלביט מערכות": "ESLT.TA",
        "בנק הפועלים":  "POLI.TA",
        "בנק לאומי":    "LUMI.TA",
        "מזרחי טפחות":  "MZTF.TA",
        "שופרסל":       "SAE.TA",
        "ביג":          "BIG.TA",
    }
    results = {}
    for name, symbol in tase_tickers.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev = hist["Close"].iloc[-2]
                last = hist["Close"].iloc[-1]
                chg  = ((last - prev) / prev) * 100
                results[name] = {
                    "symbol": symbol.replace(".TA", ""),
                    "price": round(float(last), 2),
                    "change_pct": round(float(chg), 2),
                    "arrow": "▲" if chg >= 0 else "▼"
                }
        except Exception:
            pass
    return results


# ── 3. Treasury Yields (yfinance) ─────────────────────────────────────────────
def get_treasury_yields():
    """Fetch US Treasury yields."""
    yields = {
        "2Y":  "^IRX",
        "10Y": "^TNX",
        "30Y": "^TYX",
    }
    results = {}
    for name, symbol in yields.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if len(hist) >= 1:
                results[name] = round(float(hist["Close"].iloc[-1]), 3)
        except Exception:
            results[name] = "N/A"
    return results


# ── 4. Top Movers (Yahoo Finance screener – real-time ranked list) ─────────────
def get_top_movers():
    """Fetch today's top gainers and losers directly from Yahoo Finance screener."""
    headers = {"User-Agent": "Mozilla/5.0"}
    base = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    params = {"formatted": "false", "count": "5"}

    def fetch(screen_id):
        try:
            r = requests.get(base, params={**params, "scrIds": screen_id}, headers=headers, timeout=10)
            r.raise_for_status()
            quotes = r.json()["finance"]["result"][0]["quotes"]
            return [
                {
                    "symbol":     q.get("symbol", ""),
                    "price":      round(float(q.get("regularMarketPrice", 0)), 2),
                    "change_pct": round(float(q.get("regularMarketChangePercent", 0)), 2),
                }
                for q in quotes
            ]
        except Exception:
            return []

    return {"gainers": fetch("day_gainers"), "losers": fetch("day_losers")}


# ── 5. Sector Performance (yfinance ETFs) ─────────────────────────────────────
def get_sector_performance():
    """Fetch sector ETF performance as proxy for sector rotation."""
    sectors = {
        "טכנולוגיה":       "XLK",
        "בריאות":           "XLV",
        "פיננסים":          "XLF",
        "אנרגיה":           "XLE",
        "צרכנות בסיסית":   "XLP",
        "צרכנות שיקולית":  "XLY",
        "תעשייה":           "XLI",
        'נדל"ן':            "XLRE",
        "תקשורת":           "XLC",
        "חומרים":           "XLB",
        "תשתיות":           "XLU",
    }
    results = {}
    for name, symbol in sectors.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev = hist["Close"].iloc[-2]
                last = hist["Close"].iloc[-1]
                chg  = ((last - prev) / prev) * 100
                results[name] = {"symbol": symbol, "change_pct": round(float(chg), 2), "arrow": "▲" if chg >= 0 else "▼"}
        except Exception:
            pass
    return results


# ── 6. Fear & Greed Index (CNN) ───────────────────────────────────────────────
def get_fear_greed():
    """Fetch Fear & Greed Index — tries CNN then falls back to VIX-based estimate."""
    translations = {
        "Extreme Fear":  "פחד קיצוני",
        "Fear":          "פחד",
        "Neutral":       "ניטרלי",
        "Greed":         "חמדנות",
        "Extreme Greed": "חמדנות קיצונית",
    }
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Accept": "application/json",
        }
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=headers, timeout=10
        )
        data = r.json()
        if "fear_and_greed" in data:
            score  = float(data["fear_and_greed"]["score"])
            rating = data["fear_and_greed"]["rating"]
            return {"score": round(score, 1), "rating": translations.get(rating, rating), "source": "CNN"}
    except Exception:
        pass
    # Fallback: derive sentiment from VIX
    try:
        vix = float(yf.Ticker("^VIX").history(period="1d")["Close"].iloc[-1])
        if vix < 12:   score, rating = 82, "חמדנות קיצונית"
        elif vix < 16: score, rating = 65, "חמדנות"
        elif vix < 20: score, rating = 50, "ניטרלי"
        elif vix < 28: score, rating = 32, "פחד"
        else:          score, rating = 14, "פחד קיצוני"
        return {"score": score, "rating": rating, "source": f"VIX={round(vix,1)} (אומדן)"}
    except Exception:
        return {"score": "N/A", "rating": "לא זמין"}


# ── 7. Financial News (RSS Feeds) ─────────────────────────────────────────────
def get_news_rss():
    """Fetch headlines from financial AND world/geopolitical RSS feeds.
    World news gives the AI raw material to connect global events to markets.
    """
    financial_feeds = [
        ("Reuters Finance",     "https://feeds.reuters.com/reuters/businessNews"),
        ("CNBC",                "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
        ("MarketWatch",         "https://feeds.marketwatch.com/marketwatch/topstories/"),
        ("Yahoo Finance",       "https://finance.yahoo.com/news/rssindex"),
        ("Calcalist (כלכליסט)", "https://www.calcalist.co.il/rss/"),
        ("Globes (גלובס)",      "https://www.globes.co.il/webservice/rss/rssfeeder.asmx/FeederNode?iID=1"),
    ]
    world_feeds = [
        ("Reuters World",   "https://feeds.reuters.com/Reuters/worldNews"),
        ("BBC World",       "http://feeds.bbci.co.uk/news/world/rss.xml"),
        ("AP News",         "https://feeds.apnews.com/apnews/topnews"),
        ("Times of Israel", "https://www.timesofisrael.com/feed/"),
    ]

    def fetch(source, url, max_entries):
        items = []
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_entries]:
                title = entry.get("title", "").strip()
                if title:
                    items.append({"source": source, "title": title})
        except Exception:
            pass
        return items

    financial = []
    for source, url in financial_feeds:
        financial.extend(fetch(source, url, 2))

    world = []
    for source, url in world_feeds:
        world.extend(fetch(source, url, 2))

    return (
        [{"category": "finance", **h} for h in financial[:8]] +
        [{"category": "world",   **h} for h in world[:6]]
    )


# ── 8. Finnhub – Earnings Calendar ────────────────────────────────────────────
def get_earnings_today():
    """Fetch today's earnings releases from Finnhub."""
    if not FINNHUB_KEY:
        return []
    try:
        url = (f"https://finnhub.io/api/v1/calendar/earnings"
               f"?from={TODAY_ISO}&to={TODAY_ISO}&token={FINNHUB_KEY}")
        r    = requests.get(url, timeout=10)
        data = r.json()
        return [
            {"symbol": e.get("symbol", ""), "estimate": e.get("epsEstimate", "N/A")}
            for e in data.get("earningsCalendar", [])[:10]
        ]
    except Exception:
        return []


# ── 9. FRED – Economic Indicators ─────────────────────────────────────────────
def get_fred_indicators():
    """Fetch key macro indicators from FRED."""
    if not FRED_API_KEY:
        return {}
    series = {
        "אינפלציה (CPI שנתי)": "CPIAUCSL",
        "ריבית פד":             "FEDFUNDS",
        "אבטלה":                "UNRATE",
        "GDP צמיחה":            "A191RL1Q225SBEA",
    }
    results = {}
    for name, series_id in series.items():
        try:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id={series_id}&api_key={FRED_API_KEY}"
                   f"&sort_order=desc&limit=2&file_type=json")
            r   = requests.get(url, timeout=10)
            obs = r.json().get("observations", [])
            if obs:
                results[name] = {
                    "latest": obs[0].get("value", "N/A"),
                    "prev":   obs[1].get("value", "N/A") if len(obs) > 1 else "N/A"
                }
        except Exception:
            pass
    return results


# ── 10. Alpha Vantage – Market Movers ─────────────────────────────────────────
def get_alpha_vantage_movers():
    """Fetch market movers from Alpha Vantage."""
    if not ALPHA_VANTAGE_KEY:
        return {}
    try:
        url  = f"https://www.alphavantage.co/query?function=TOP_GAINERS_LOSERS&apikey={ALPHA_VANTAGE_KEY}"
        r    = requests.get(url, timeout=10)
        data = r.json()
        return {
            "gainers": [{"symbol": x.get("ticker"), "change_pct": x.get("change_percentage")}
                        for x in data.get("top_gainers", [])[:5]],
            "losers":  [{"symbol": x.get("ticker"), "change_pct": x.get("change_percentage")}
                        for x in data.get("top_losers", [])[:5]],
        }
    except Exception:
        return {}


# ── 11a. Trend Summary from stored snapshots ──────────────────────────────────
def build_trend_summary() -> str:
    """Build a compact time-series summary from yesterday's 5 snapshots.
    Gives Groq a picture of how markets evolved throughout the day,
    not just a single end-of-day snapshot.
    """
    snapshots = database.get_snapshots_last_24h()
    if not snapshots:
        return ""

    lines = ["=== מגמות שוק לאורך יום המסחר האחרון ==="]
    for snap in snapshots:
        try:
            dt_utc = datetime.fromisoformat(snap["time"])
            dt_il  = dt_utc + timedelta(hours=3)   # UTC → Israel (IDT)
            t      = dt_il.strftime("%H:%M")
        except Exception:
            t = snap["time"][:16]

        d   = snap["data"]
        spy = d.get("market", {}).get("SPY (S&P 500)", {})
        vix = d.get("market", {}).get("VIX", {})
        ta  = d.get("global",  {}).get("TA-125 (תל אביב)", {})
        fg  = d.get("fear_greed", {})
        oil = d.get("market", {}).get("Oil (WTI)", {})

        lines.append(
            f"{t}: SPY {spy.get('price','?')} ({spy.get('arrow','')} {spy.get('change_pct','?')}%) | "
            f"VIX {vix.get('price','?')} | "
            f"ת\"א-125 {ta.get('price','?')} ({ta.get('arrow','')} {ta.get('change_pct','?')}%) | "
            f"נפט {oil.get('price','?')} | "
            f"סנטימנט: {fg.get('rating','?')} ({fg.get('score','?')})"
        )

    return "\n".join(lines)


# ── 11b. AI Analysis (Groq – two-call split) ──────────────────────────────────
def generate_hebrew_brief(market, global_mkts, yields, movers, sectors, fear_greed, news, earnings, fred, tase_stocks):
    """Generate the Hebrew brief using two Groq calls to stay under the 12k TPM limit.
    Call 1: US markets, global, macro, sectors, movers, news, conclusions.
    Call 2: Israeli market only.
    Results are stitched together into one brief before sending.
    """
    client = Groq(api_key=GROQ_API_KEY)

    # Split news by category and source
    israeli_sources = {"Calcalist (כלכליסט)", "Globes (גלובס)"}
    news_israel  = [n for n in news if n["source"] in israeli_sources][:5]
    news_finance = [n for n in news if n.get("category") == "finance" and n["source"] not in israeli_sources][:6]
    news_world   = [n for n in news if n.get("category") == "world"][:6]

    # ── Call 1: US & Global (all sections except Israeli market) ─────────────
    trend_summary = build_trend_summary()

    data_us = (
        (f"{trend_summary}\n\n" if trend_summary else "") +
        f"שווקים אמריקאים: {json.dumps(market, ensure_ascii=False)}\n"
        f"שווקים גלובליים: {json.dumps(global_mkts, ensure_ascii=False)}\n"
        f"תשואות אג\"ח: {json.dumps(yields, ensure_ascii=False)}\n"
        f"סקטורים: {json.dumps(sectors, ensure_ascii=False)}\n"
        f"פחד ותאוות בצע: {json.dumps(fear_greed, ensure_ascii=False)}\n"
        f"עולי/יורדי שער: {json.dumps(movers, ensure_ascii=False)}\n"
        f"רווחים היום: {json.dumps(earnings, ensure_ascii=False)}\n"
        f"מאקרו FRED: {json.dumps(fred, ensure_ascii=False)}\n"
        f"כותרות פיננסיות: {json.dumps(news_finance, ensure_ascii=False)}\n"
        f"אירועים עולמיים: {json.dumps(news_world, ensure_ascii=False)}"
    )

    il_status   = f"השוק הישראלי {'פתוח' if IL_OPEN_TODAY else 'סגור'} היום, {TODAY_SHORT}"
    us_status   = f"השוק האמריקאי {'ייפתח היום, ' + TODAY_SHORT + ' בשעה 16:30' if US_OPEN_TODAY else 'סגור היום, ' + TODAY_SHORT}"

    prompt_us = f"""אתה אנליסט פיננסי בכיר ומומחה מאקרו-כלכלי. כתוב בריפינג בוקר **בעברית בלבד**, טרמינולוגיה כמו בכלכליסט/גלובס.
כל סעיף מתחיל ב-### ואחריו שם הסעיף. SPY=מדד S&P 500, QQQ=מדד נאסד"ק 100.

== חוקי זמן — חובה לפעול לפיהם ==
הבריפינג נשלח ב-{TODAY_SHORT} בשעה 07:00 בישראל. בשעה זו המסחר טרם נפתח.
• נתוני ארה"ב הם ממחירי הסגירה של {LAST_US_DAY_HE}, {LAST_US_CLOSE} — כתוב "בסגירת {LAST_US_CLOSE}" או "בסגירת {LAST_US_DAY_HE}" ולא "היום"
• נתוני ישראל הם ממחירי הסגירה של {LAST_IL_DAY_HE}, {LAST_IL_CLOSE} — כתוב "בסגירת {LAST_IL_CLOSE}" ולא "היום"
• {il_status}
• {us_status}
• אסור לכתוב "השוק עלה היום" — כתוב "בסגירה האחרונה עלה" / "ב-{LAST_US_CLOSE} עלה"
• המילה "היום" מותרת רק בהקשר של יומן אירועים עתידיים (מה צפוי לקרות ב-{TODAY_SHORT})
• "מחר" = {TOMORROW_SHORT}

### סיכום יומי – סגירת {LAST_US_CLOSE}
ניתוח מקיף של יום המסחר האחרון: מה קרה, מה הניע, אווירה, נרטיבים שולטים.
אם קיימים נתוני מגמות לאורך היום (בנתונים) — תאר כיצד השוק התפתח משעה לשעה.
חבר בין אירועים גלובליים לתנועות השוק — שרשרת סיבה-ותוצאה. לפחות 6 משפטים.

### גיאופוליטיקה, מגמות ונרטיבים שולטים
זהה קשרים לא-ברורים בין אירועים עולמיים לשווקים:
• תאר את האירוע/המגמה הגלובלית
• הסבר שרשרת ההשפעה: אירוע → נפט/ריבית/מטבע → סקטור → שוק
• ציין אם קצרת/ארוכת טווח
לפחות 4 קשרים. זהו מגמות גדולות שמעצבות את השוק כרגע.

### השווקים האמריקאים והכלכלה – סגירת {LAST_US_CLOSE}
כל הנתונים מתייחסים לסגירת {LAST_US_CLOSE}. אל תשתמש בלשון הווה כאילו השוק פתוח.
SPY/QQQ/דאו/ראסל עם מחירים ואחוזים. VIX. חוזים, זהב, נפט. מאקרו עם מספרים. מדיניות הפד. תשואות אג"ח 2/10/30. 6-8 משפטים.

### שווקים גלובליים
ביצועי אסיה ואירופה בסגירה האחרונה, עם מספרים. הסבר קצר לתנועה חריגה.

### מדד פחד ותאוות בצע
0-25=פחד קיצוני, 26-45=פחד, 46-55=ניטרלי, 56-75=חמדנות, 76-100=חמדנות קיצונית. פרש את הציון הנוכחי.

### סבב סקטורים
לכל סקטור: שם, סימבול ETF, אחוז שינוי בסגירה האחרונה, חץ ▲/▼. נתח מה הרוטציה מעידה.

### מניות בולטות – עולי ויורדי שער
מניות שעלו/ירדו בצורה חריגה בסגירה האחרונה. הסבר סיבה אם ידועה.

### יומן אירועים כלכליים – {TODAY_SHORT} ו-{TOMORROW_SHORT}
אירועים שצפויים להתרחש היום, {TODAY_SHORT} ומחר, {TOMORROW_SHORT} — זה העתיד, לא העבר.
רק אירועים משמעותיים (ריבית פד, CPI/PPI/GDP, רווחים >100B$, נאומי הפד).
אם אין — "אין אירועים מהותיים צפויים ב-{TODAY_SHORT}."

### ניתוח חדשות מרכזיות
5-6 חדשות מהימים האחרונים. לכל חדשה: כותרת נושא, מה קרה עם מספרים, משמעות למשקיעים.

### מסקנות ומה לעקוב אחריו ב-{TODAY_SHORT}
3-4 נקודות ממוספרות — כולל לפחות נקודה אחת על מגמה/סיכון גלובלי.

נתונים:
{data_us}"""

    resp1 = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt_us}],
        temperature=0.3,
        max_tokens=5000,
    )
    brief_us = resp1.choices[0].message.content

    # ── Call 2: Israeli Market only ───────────────────────────────────────────
    data_israel = (
        f"מניות ת\"א: {json.dumps(tase_stocks, ensure_ascii=False)}\n"
        f"מדד ת\"א-125: {json.dumps(global_mkts.get('TA-125 (תל אביב)', {}), ensure_ascii=False)}\n"
        f"דולר/שקל: {json.dumps(market.get('USD/ILS', {}), ensure_ascii=False)}\n"
        f"S&P500 (לקורלציה): {json.dumps(market.get('SPY (S&P 500)', {}), ensure_ascii=False)}\n"
        f"כותרות ישראליות: {json.dumps(news_israel, ensure_ascii=False)}\n"
        f"אירועים עולמיים (לקשרים): {json.dumps(news_world[:4], ensure_ascii=False)}"
    )

    prompt_israel = f"""אתה אנליסט פיננסי בכיר המתמחה בשוק הישראלי. כתוב **בעברית בלבד** כמו בכלכליסט/גלובס.
כל סעיף מתחיל ב-### ואחריו שם הסעיף.

== חוקי זמן ==
הבריפינג נשלח ב-{TODAY_SHORT} בשעה 07:00. המסחר טרם נפתח.
• נתוני ת"א הם מסגירת {LAST_IL_DAY_HE}, {LAST_IL_CLOSE} — כתוב "בסגירת {LAST_IL_CLOSE}" ולא "היום"
• {il_status}
• אסור לכתוב "השוק עלה היום" — כתוב "בסגירת {LAST_IL_CLOSE} עלה" או "בסגירה האחרונה"

### שוק ההון הישראלי – סגירת {LAST_IL_CLOSE}
• ביצועי מדד ת"א-125 בסגירת {LAST_IL_CLOSE} — מספרים ואחוזי שינוי
• מניות מגמה בולטות: סימבול, מחיר, אחוז שינוי, חץ ▲/▼
• מומנטום כללי — עלייה/ירידה ומה הניע
• שער דולר/שקל בסגירה האחרונה — פרשנות וקשר לאירועים גלובליים
• קורלציה עם וול סטריט בסגירת {LAST_US_CLOSE}
• חדשות כלכליות ישראליות רלוונטיות
לפחות 7 משפטים.

נתונים:
{data_israel}"""

    resp2 = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt_israel}],
        temperature=0.3,
        max_tokens=2000,
    )
    brief_israel = resp2.choices[0].message.content

    # ── Stitch together: Israel section goes after the daily summary ──────────
    # Split Call 1 output into individual ### sections
    sections = re.split(r'\n(?=###)', brief_us.strip())
    if len(sections) >= 2:
        # sections[0] = daily summary, sections[1:] = everything else
        return sections[0] + "\n\n" + brief_israel.strip() + "\n\n" + "\n\n".join(sections[1:])
    # Fallback: append Israel at the end
    return brief_us + "\n\n" + brief_israel


# ── 12. HTML Email Builder ─────────────────────────────────────────────────────
def build_html_email(brief_text: str, unsubscribe_url: str = "#") -> str:
    """Wrap the Hebrew brief in a dark-themed, bubble-card RTL HTML email."""
    day_names = {
        "Monday": "יום שני", "Tuesday": "יום שלישי", "Wednesday": "יום רביעי",
        "Thursday": "יום חמישי", "Friday": "יום שישי", "Saturday": "שבת", "Sunday": "יום ראשון"
    }
    day_he   = day_names.get(datetime.now().strftime("%A"), "")
    date_str = f"{day_he}, {TODAY}"

    # Split the AI output into bubble cards by ### headings
    sections = re.split(r'###\s*', brief_text.strip())
    cards_html = ""
    for sec in sections:
        if not sec.strip():
            continue
        lines      = sec.strip().split('\n', 1)
        title      = lines[0].strip()
        body       = lines[1].strip() if len(lines) > 1 else ""
        body_html  = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', body)
        body_html  = body_html.replace('\n', '<br>')
        # Start a new line after each sentence-ending period
        body_html  = re.sub(r'\.\s+(?=[א-תA-Z])', '.<br>', body_html)
        cards_html += f"""
    <div class="card">
      <div class="card-title">{title}</div>
      <div class="card-body">{body_html}</div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>תדריך פיננסי יומי – {TODAY}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', Tahoma, Arial, sans-serif;
      background: #e8edf4;
      direction: rtl;
      text-align: right;
      color: #1e2d42;
    }}
    .wrapper {{
      max-width: 680px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }}
    /* ── Header — dark navy anchor, gold title ── */
    .header {{
      background: linear-gradient(135deg, #1a3a6e 0%, #1e4d8c 60%, #1a3a6e 100%);
      border: 1px solid #2a509a;
      border-radius: 16px;
      padding: 34px 36px 28px;
      text-align: center;
      margin-bottom: 6px;
    }}
    .header .logo {{
      font-size: 10px;
      color: #7aaad8;
      letter-spacing: 4px;
      text-transform: uppercase;
      margin-bottom: 12px;
    }}
    .header h1 {{
      color: #f0c040;
      font-size: 30px;
      font-weight: 800;
      margin-bottom: 8px;
      letter-spacing: 0.5px;
    }}
    .header .date {{
      color: #a8c8e8;
      font-size: 14px;
    }}
    /* ── Gold accent line ── */
    .accent {{
      height: 3px;
      background: linear-gradient(90deg, transparent, #f0c040, #e08030, #f0c040, transparent);
      margin: 18px 40px;
      border-radius: 2px;
    }}
    /* ── Bubble Cards ── */
    .card {{
      background: #f0f5fc;
      border: 1px solid #c8d8ea;
      border-radius: 14px;
      padding: 22px 26px 20px;
      margin-bottom: 14px;
      box-shadow: 0 2px 10px rgba(30,60,100,0.08);
    }}
    .card-title {{
      font-size: 16px;
      font-weight: 700;
      color: #1a3a6e;
      border-bottom: 2px solid #dce8f4;
      padding-bottom: 10px;
      margin-bottom: 14px;
      letter-spacing: 0.3px;
      direction: rtl;
      text-align: right;
    }}
    .card-body {{
      font-size: 15px;
      line-height: 2.0;
      color: #2c3e52;
      direction: rtl;
      text-align: right;
      unicode-bidi: embed;
    }}
    .card-body * {{
      direction: rtl;
      text-align: right;
    }}
    strong {{
      color: #0f2d5e;
      font-weight: 700;
    }}
    /* ── Footer ── */
    .footer {{
      text-align: center;
      padding: 18px 20px 4px;
      font-size: 11px;
      color: #7a90a8;
      direction: rtl;
      line-height: 1.8;
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <div class="logo">Financial Intelligence Agent</div>
      <h1>תדריך פיננסי יומי</h1>
      <div class="date">{date_str}</div>
    </div>
    <div class="accent"></div>
    {cards_html}
    <div class="footer">
      נוצר אוטומטית ב-{TODAY} &nbsp;|&nbsp; Yahoo Finance · FRED · Finnhub · Alpha Vantage · RSS<br>
      <a href="{unsubscribe_url}" style="color:#4a7aaa; font-size:11px;">ביטול הרשמה</a>
      &nbsp;|&nbsp;
      <span style="color:#8a9fb8;">המידע מיועד לצרכי מידע בלבד ואינו מהווה ייעוץ השקעות</span>
    </div>
  </div>
</body>
</html>"""


# ── 13. Send Email ─────────────────────────────────────────────────────────────
def send_email(html_content: str, subject: str, recipient_email: str):
    """Send the HTML email to a single recipient via Gmail SMTP."""
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = recipient_email
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipient_email, msg.as_string())
    print(f"  ✅ Sent to {recipient_email}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"🚀 Starting Financial Brief Agent – {TODAY}")

    # Ensure DB exists and owner is always subscribed
    database.init_db()
    if OWNER_EMAIL:
        database.seed_owner(OWNER_NAME, OWNER_EMAIL)

    print("📈 Collecting US market data...")
    market      = get_market_snapshot()
    global_mkts = get_global_markets()
    tase_stocks = get_tase_stocks()
    yields      = get_treasury_yields()
    movers      = get_top_movers()
    sectors     = get_sector_performance()
    fear_greed  = get_fear_greed()

    print("📰 Fetching news from RSS feeds...")
    news = get_news_rss()

    print("📅 Fetching earnings calendar...")
    earnings = get_earnings_today()

    print("📊 Fetching macro indicators (FRED)...")
    fred = get_fred_indicators()

    print("🤖 Generating Hebrew brief with Groq AI...")
    brief_text = generate_hebrew_brief(
        market, global_mkts, yields, movers,
        sectors, fear_greed, news, earnings, fred, tase_stocks
    )

    print("📧 Sending to all subscribers...")
    subject     = f"תדריך פיננסי יומי – {TODAY}"
    subscribers = database.get_active_subscribers()
    print(f"   Found {len(subscribers)} subscriber(s)")

    for sub in subscribers:
        unsubscribe_url = f"{BASE_URL}/unsubscribe/{sub['token']}"
        html = build_html_email(brief_text, unsubscribe_url)
        send_email(html, subject, sub["email"])

    print(f"✅ All done! Sent to {len(subscribers)} subscriber(s)")


if __name__ == "__main__":
    main()
