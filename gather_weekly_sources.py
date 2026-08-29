#!/usr/bin/env python3
"""Efficiently retain weekly Israeli news and official FX observations."""

from datetime import datetime

import database
from israel_market import (
    ISRAEL_TZ,
    get_boi_exchange_rates,
    get_israeli_news,
    get_maya_announcements,
)


def gather_weekly_sources() -> dict:
    database.init_db()
    if not database.should_collect_weekly_news(minimum_hours=4):
        return {"skipped": True, "reason": "collected_less_than_four_hours_ago"}

    now = datetime.now(ISRAEL_TZ).isoformat(timespec="seconds")
    maya = get_maya_announcements(limit=5)
    news = get_israeli_news(hours=36, per_source=5, limit=40)
    exchange_rates = get_boi_exchange_rates()
    news_result = database.save_weekly_news(maya + news)
    fx_result = database.record_fx_rates({
        "as_of": now,
        "exchange_rates": exchange_rates,
    })
    return {
        "skipped": False,
        "as_of": now,
        **news_result,
        **fx_result,
    }


if __name__ == "__main__":
    print(gather_weekly_sources())

