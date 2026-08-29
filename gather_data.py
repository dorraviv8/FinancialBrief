#!/usr/bin/env python3
"""Store an Israeli-market snapshot without calling AI or sending email."""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from israel_market import collect_israeli_market_data
import database


def gather():
    now_str = datetime.now().strftime("%H:%M %d/%m/%Y")
    print(f"📊 Gathering market snapshot – {now_str}")

    database.init_db()

    # Intraday snapshots focus on official TASE and Bank of Israel data. News is
    # handled by gather_weekly_sources.py to avoid unnecessary market requests.
    # Intraday snapshots need current prices and breadth, not hundreds of
    # historical chart rows. The full morning run refreshes technicals once.
    snapshot = collect_israeli_market_data(
        include_news=False,
        include_technicals=False,
    )

    database.save_market_snapshot(snapshot)
    database.record_fx_rates(snapshot)
    database.cleanup_old_snapshots(days=10)
    print(f"✅ Snapshot saved – {now_str}")


if __name__ == "__main__":
    gather()
