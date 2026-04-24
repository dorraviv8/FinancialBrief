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
import traceback
import re

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY")
FRED_API_KEY      = os.getenv("FRED_API_KEY")
FINNHUB_KEY       = os.getenv("FINNHUB_KEY")
GMAIL_USER        = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD= os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL   = os.getenv("RECIPIENT_EMAIL")

TODAY        = datetime.now().strftime("%d/%m/%Y")
TODAY_ISO    = datetime.now().strftime("%Y-%m-%d")
TODAY_SHORT  = datetime.now().strftime("%d.%m")
TOMORROW_SHORT = (datetime.now() + timedelta(days=1)).strftime("%d.%m")


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


# ── 4. Top Movers (yfinance – S&P100 sample) ──────────────────────────────────
def get_top_movers():
    """Get biggest gainers and losers from a representative S&P100 sample."""
    sp100_sample = [
        "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","BRK-B","JPM","V",
        "UNH","XOM","JNJ","WMT","MA","PG","HD","CVX","MRK","ABBV",
        "LLY","AVGO","COST","PEP","KO","BAC","TMO","CSCO","ACN","MCD",
        "ABT","ADBE","CRM","NKE","DHR","TXN","NEE","PM","RTX","AMGN",
        "QCOM","HON","IBM","GE","CAT","SPGI","LOW","INTU","BLK","AXP"
    ]
    movers = []
    for sym in sp100_sample:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev = hist["Close"].iloc[-2]
                last = hist["Close"].iloc[-1]
                chg  = ((last - prev) / prev) * 100
                movers.append({"symbol": sym, "change_pct": round(float(chg), 2), "price": round(float(last), 2)})
        except Exception:
            pass
    movers.sort(key=lambda x: x["change_pct"], reverse=True)
    return {"gainers": movers[:5], "losers": movers[-5:][::-1]}


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
                results[name] = {"change_pct": round(float(chg), 2), "arrow": "▲" if chg >= 0 else "▼"}
        except Exception:
            pass
    return results


# ── 6. Fear & Greed Index (CNN) ───────────────────────────────────────────────
def get_fear_greed():
    """Fetch CNN Fear & Greed Index."""
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        score  = data["fear_and_greed"]["score"]
        rating = data["fear_and_greed"]["rating"]
        translations = {
            "Extreme Fear":  "פחד קיצוני",
            "Fear":          "פחד",
            "Neutral":       "ניטרלי",
            "Greed":         "חמדנות",
            "Extreme Greed": "חמדנות קיצונית",
        }
        return {"score": round(float(score), 1), "rating": translations.get(rating, rating)}
    except Exception:
        return {"score": "N/A", "rating": "לא זמין"}


# ── 7. Financial News (RSS Feeds) ─────────────────────────────────────────────
def get_news_rss():
    """Fetch headlines from multiple financial RSS feeds."""
    feeds = [
        ("Reuters Finance",     "https://feeds.reuters.com/reuters/businessNews"),
        ("CNBC",                "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
        ("MarketWatch",         "https://feeds.marketwatch.com/marketwatch/topstories/"),
        ("Yahoo Finance",       "https://finance.yahoo.com/news/rssindex"),
        ("Investing.com",       "https://www.investing.com/rss/news.rss"),
        ("Calcalist (כלכליסט)", "https://www.calcalist.co.il/rss/"),
        ("Globes (גלובס)",      "https://www.globes.co.il/webservice/rss/rssfeeder.asmx/FeederNode?iID=1"),
    ]
    all_headlines = []
    for source, url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                title   = entry.get("title", "").strip()
                summary = entry.get("summary", "")[:300].strip()
                if title:
                    all_headlines.append({"source": source, "title": title, "summary": summary})
        except Exception:
            pass
    return all_headlines[:40]


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


# ── 11. AI Analysis (Google Gemini) ───────────────────────────────────────────
def generate_hebrew_brief(market, global_mkts, yields, movers, sectors, fear_greed, news, earnings, fred):
    """Send all collected data to Gemini and get a full Hebrew brief."""
    client = Groq(api_key=GROQ_API_KEY)

    data_summary = f"""
=== נתוני שוק להיום {TODAY} ===

--- שווקים אמריקאים ---
{json.dumps(market, ensure_ascii=False, indent=2)}

--- שווקים גלובליים ---
{json.dumps(global_mkts, ensure_ascii=False, indent=2)}

--- תשואות אג"ח אמריקאי ---
{json.dumps(yields, ensure_ascii=False, indent=2)}

--- ביצועי סקטורים ---
{json.dumps(sectors, ensure_ascii=False, indent=2)}

--- מדד פחד ותאוות בצע ---
{json.dumps(fear_greed, ensure_ascii=False, indent=2)}

--- עולי ויורדי שער ---
{json.dumps(movers, ensure_ascii=False, indent=2)}

--- פרסומי רווחים היום ---
{json.dumps(earnings, ensure_ascii=False, indent=2)}

--- אינדיקטורים מאקרו (FRED) ---
{json.dumps(fred, ensure_ascii=False, indent=2)}

--- כותרות חדשות ---
{json.dumps(news, ensure_ascii=False, indent=2)}
"""

    prompt = f"""
אתה אנליסט פיננסי בכיר ומומחה לשווקי ההון. קיבלת נתוני שוק מקיפים להיום.
עליך לכתוב בריפינג בוקר מלא ומקצועי **בעברית בלבד**.

חשוב מאוד:
- השתמש אך ורק בטרמינולוגיה פיננסית עברית מקצועית כפי שנהוג בכלכליסט ובגלובס
- אל תשתמש במילים באנגלית – תרגם הכל לעברית פיננסית תקנית
- הסגנון: מקצועי, תמציתי, מדויק, קריא
- במקום המילה "היום" כתוב תמיד "היום, {TODAY_SHORT}" ובמקום "מחר" כתוב "מחר, {TOMORROW_SHORT}"
- SPY מייצג את מדד ה-S&P 500, ו-QQQ מייצג את מדד הנאסד"ק 100
- כל סעיף חייב להתחיל בשורה ### (שלושה סולמיות) ואחריה שם הסעיף

כתוב את הסעיפים הבאים:

### סיכום יומי – {TODAY_SHORT}
סעיף זה חייב להיות הארוך והמפורט ביותר בבריפינג.
כתוב ניתוח מקיף הכולל: מה קרה בשווקים, מה הניע אותם, מה הייתה האווירה הכללית, אילו נרטיבים שולטים בשוק כרגע, מה ההקשר המאקרו-כלכלי, ואיך כל זה משתלב לתמונה אחת. לפחות 6-8 משפטים.

### ביצועי מדדים – SPY ו-QQQ
ביצועי SPY ו-QQQ עם מחירים ואחוזי שינוי מדויקים. כלול גם Dow Jones, Russell 2000, VIX, חוזים עתידיים, זהב, נפט ומטבע קריפטו.

### שווקים גלובליים
ביצועי אסיה ואירופה עם מספרים. הסבר קצר אם יש תנועה חריגה.

### שוק האג"ח ומדיניות מוניטרית
תשואות אג"ח ל-2, 10 ו-30 שנה. פרש את עקום התשואות ומה המשמעות למשקיעים.

### מדד פחד ותאוות בצע
ציון המדד הוא {{}}: 0-25 = פחד קיצוני (שוק מכרה יתר, הזדמנות קנייה אפשרית), 26-45 = פחד, 46-55 = ניטרלי, 56-75 = חמדנות (שוק עולה, זהירות), 76-100 = חמדנות קיצונית (שוק קנה יתר, סיכון תיקון).
פרש את הציון הנוכחי בהקשר זה והסבר מה זה אומר לגבי הסנטימנט הנוכחי בשוק.

### סבב סקטורים
אילו סקטורים מובילים ואילו מפגרים. נתח מה זה מעיד על הרוטציה בשוק.

### מניות בולטות – עולי ויורדי שער
מניות שעלו/ירדו בצורה חריגה עם אחוזים. נסה להסביר את הסיבה אם ידועה מהחדשות.

### יומן אירועים כלכליים – היום, {TODAY_SHORT} ומחר, {TOMORROW_SHORT}
**חשוב: כלול רק אירועים משמעותיים** כגון: החלטות ריבית פד, נתוני CPI/PPI/GDP/תעסוקה, פרסומי רווחים של חברות גדולות (שווי שוק מעל 100 מיליארד דולר), נאומי יו"ר הפד.
אם אין אירועים משמעותיים – כתוב: "אין אירועים מהותיים צפויים היום, {TODAY_SHORT}."

### ניתוח חדשות מרכזיות
5-7 כותרות חשובות עם ניתוח קצר של כל אחת – מה המשמעות למשקיעים.

### שוק ההון הישראלי
סעיף זה חייב להיות מורחב ומפורט.
כלול: ביצועי מדד ת"א-125 עם מספרים, ניתוח הסקטורים המובילים בבורסה הישראלית, שער הדולר/שקל עם פרשנות, השפעת האירועים הגלובליים על השוק הישראלי, חדשות כלכליות ישראליות מהרשימה אם קיימות, והמלצות מה לעקוב אחריו בשוק הישראלי. לפחות 6-8 משפטים.

### מסקנות ומה לעקוב אחריו היום, {TODAY_SHORT}
3-5 נקודות מפתח ממוספרות. לכל נקודה – מה לעקוב ולמה זה חשוב.

---
הנתונים:
{data_summary}
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=6000,
    )
    return response.choices[0].message.content


# ── 12. HTML Email Builder ─────────────────────────────────────────────────────
def build_html_email(brief_text: str) -> str:
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
  <title>בריפינג פיננסי בוקר – {TODAY}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', Tahoma, Arial, sans-serif;
      background: #0d1117;
      direction: rtl;
      text-align: right;
      color: #cdd6e0;
    }}
    .wrapper {{
      max-width: 680px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }}
    /* ── Header ── */
    .header {{
      background: linear-gradient(135deg, #111827 0%, #1a2f52 60%, #1e3a6e 100%);
      border: 1px solid #2a3f6a;
      border-radius: 16px;
      padding: 34px 36px 28px;
      text-align: center;
      margin-bottom: 6px;
    }}
    .header .logo {{
      font-size: 10px;
      color: #5a7fa8;
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
      color: #8aaac8;
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
      background: #161b27;
      border: 1px solid #232d42;
      border-radius: 14px;
      padding: 20px 24px 18px;
      margin-bottom: 14px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.35);
    }}
    .card-title {{
      font-size: 14px;
      font-weight: 700;
      color: #f0c040;
      border-bottom: 1px solid #2a3a56;
      padding-bottom: 10px;
      margin-bottom: 13px;
      letter-spacing: 0.3px;
    }}
    .card-body {{
      font-size: 14px;
      line-height: 1.9;
      color: #b8c8d8;
      direction: rtl;
      text-align: right;
    }}
    strong {{
      color: #e8d090;
      font-weight: 600;
    }}
    /* ── Footer ── */
    .footer {{
      text-align: center;
      padding: 18px 20px 4px;
      font-size: 11px;
      color: #3a5070;
      direction: rtl;
      line-height: 1.8;
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <div class="logo">Financial Intelligence Agent</div>
      <h1>בריפינג פיננסי בוקר</h1>
      <div class="date">{date_str}</div>
    </div>
    <div class="accent"></div>
    {cards_html}
    <div class="footer">
      נוצר אוטומטית ב-{TODAY} &nbsp;|&nbsp; Yahoo Finance · FRED · Finnhub · Alpha Vantage · RSS<br>
      <span style="color:#2a3d55;">המידע מיועד לצרכי מידע בלבד ואינו מהווה ייעוץ השקעות</span>
    </div>
  </div>
</body>
</html>"""


# ── 13. Send Email ─────────────────────────────────────────────────────────────
def send_email(html_content: str, subject: str):
    """Send the HTML email via Gmail SMTP."""
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
    print(f"✅ Email sent to {RECIPIENT_EMAIL}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"🚀 Starting Financial Brief Agent – {TODAY}")

    print("📈 Collecting US market data...")
    market      = get_market_snapshot()
    global_mkts = get_global_markets()
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

    print("🤖 Generating Hebrew brief with Gemini AI...")
    brief_text = generate_hebrew_brief(
        market, global_mkts, yields, movers,
        sectors, fear_greed, news, earnings, fred
    )

    print("📧 Building and sending email...")
    html    = build_html_email(brief_text)
    subject = f"בריפינג פיננסי בוקר – {TODAY}"
    send_email(html, subject)

    print("✅ All done!")


if __name__ == "__main__":
    main()
