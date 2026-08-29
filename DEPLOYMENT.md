# GCP Deployment Plan

The local project is prepared for deployment to the GCP VM at `34.69.156.167`,
but no server connection or production change should be made until the owner
explicitly approves deployment.

## Production layout

Keep code and subscriber data separate:

```bash
/opt/financialbrief/                  # application code
/var/lib/financialbrief/subscribers.db # persistent subscriber data
/etc/financialbrief.env               # secrets, mode 600
```

Set this in the production environment file:

```bash
FINANCIAL_BRIEF_DB_PATH=/var/lib/financialbrief/subscribers.db
FINANCIAL_BRIEF_CACHE_DIR=/var/lib/financialbrief/market-cache
BASE_URL=https://trader.34.69.156.167.nip.io/financialbrief
RECOMMENDATION_V2_MODE=shadow
```

The cache refreshes index constituent metadata once daily. It fetches all graph pages only on its first run and weekly
reconciliation. Other mornings fetch only the newest page and merge it into
the cached one-year series. A second run on the same day reuses the cache.

The same persistent SQLite database stores compact sector-score predictions,
official sector/TA-125 closes, and their realized 10/20/30-session outcomes.
The email displays historical results after 20 comparable completed cases and
permits a bounded calibration adjustment only after 30. Dry runs do not add
predictions or outcomes.

Recommendation V2 records daily feature snapshots, separate 10/20/30-session
probability forecasts, chronological walk-forward metrics and realized live
outcomes. Keep it in `shadow` mode initially. `auto` promotion requires at least
60 matured 20-session forecasts plus a better probability score and no loss of
directional accuracy against V1. See `RECOMMENDATION_MODEL.md` for the formula,
audit trail and activation policy.

It also stores one immutable briefing per Israel calendar date and a delivery
record for every recipient. Normal reruns reuse the persisted briefing, retry
explicitly failed recipients, and skip addresses already marked as sent. A
process lock next to the database prevents scheduled and manual send commands
from overlapping. If an operator intentionally needs to resend today's report,
use `python financial_brief.py --force-send`; this override should not be part of
the timer unit.

Never upload `.env` or `subscribers.db`. The repository `.rsync-filter` already
excludes them.

## Morning schedule

The morning workflow is deliberately split into two services. At 07:45 in the
`Asia/Jerusalem` timezone, the preparation service gathers fresh data, generates
the report, reads the completed output through deterministic and AI clarity/fact
checks, applies only guarded prose corrections, and stores it with
`qa_status=approved`. At 08:00, the delivery service sends only today's stored,
approved report. It refuses to generate a replacement or send an unapproved
report. On Sunday the preparation stage automatically creates the weekly summary
for the preceding Monday–Saturday calendar window, using actual TASE sessions.

Both timers use `Persistent=true`. If a restart causes both missed jobs to run,
the send service is ordered after preparation and still enforces the approval
check. The delivery ledger safely sends each report at most once per recipient.

The source collector runs at 13:05 and 19:05 every day. It makes no AI call and
does not download index charts: it only deduplicates Israeli RSS/MAYA items and
stores official USD/ILS and EUR/ILS observations for the weekly report. The
07:45 preparation performs the third daily source collection. After a weekly
report is delivered to all active recipients, unused articles from that week are
deleted; selected source evidence is retained for 90 days.

Before deployment, replace `YOUR_GCP_USER`. The web unit binds only to the
private Docker gateway (`172.18.0.1:5001`) and Caddy publishes the app below
`/financialbrief`. Then install and enable the units:

```bash
sudo cp deploy/financial-brief.service /etc/systemd/system/
sudo cp deploy/financial-brief.timer /etc/systemd/system/
sudo cp deploy/financial-brief-send.service /etc/systemd/system/
sudo cp deploy/financial-brief-send.timer /etc/systemd/system/
sudo cp deploy/financial-brief-sources.service /etc/systemd/system/
sudo cp deploy/financial-brief-sources.timer /etc/systemd/system/
sudo cp deploy/financial-brief-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now financial-brief-web.service
sudo systemctl enable --now financial-brief.timer
sudo systemctl enable --now financial-brief-send.timer
sudo systemctl enable --now financial-brief-sources.timer
```

Check scheduling and logs:

```bash
systemctl list-timers financial-brief.timer financial-brief-send.timer financial-brief-sources.timer
journalctl -u financial-brief.service -n 100 --no-pager
```

## Safe validation order

1. Run unit tests locally.
2. On the VM, run a collection-only diagnostic or a dry run before enabling the timer.
3. Confirm the sender, subscriber count, source freshness, and generated Hebrew output.
4. Send one controlled test email.
5. Enable the daily timer.
