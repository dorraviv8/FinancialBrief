#!/usr/bin/env python3
"""
Market data gathering script — runs 5x daily.
Collects a market snapshot and stores it in SQLite.
Does NOT call Groq and does NOT send email — lightweight and fast.

Scheduled times (Israel time):
  08:00, 10:00, 13:30, 16:00, 18:30

The morning brief (financial_brief.py, 07:00) reads these snapshots
to understand how markets evolved throughout the previous day.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from financial_brief import (
    get_market_snapshot,
    get_global_markets,
    get_tase_stocks,
    get_treasury_yields,
    get_sector_performance,
    get_fear_greed,
    get_news_rss,
)
import database


def gather():
    now_str = datetime.now().strftime("%H:%M %d/%m/%Y")
    print(f"📊 Gathering market snapshot – {now_str}")

    database.init_db()

    snapshot = {
        "market":     get_market_snapshot(),
        "global":     get_global_markets(),
        "tase":       get_tase_stocks(),
        "yields":     get_treasury_yields(),
        "sectors":    get_sector_performance(),
        "fear_greed": get_fear_greed(),
        "news":       get_news_rss(),
    }

    database.save_market_snapshot(snapshot)
    database.cleanup_old_snapshots(days=7)
    print(f"✅ Snapshot saved – {now_str}")


if __name__ == "__main__":
    gather()
