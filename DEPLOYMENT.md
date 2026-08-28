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
```

The cache refreshes index constituent metadata once daily. It fetches all graph pages only on its first run and weekly
reconciliation. Other mornings fetch only the newest page and merge it into
the cached one-year series. A second run on the same day reuses the cache.

The same persistent SQLite database stores compact sector-score predictions,
official sector/TA-125 closes, and their realized 10/20/30-session outcomes.
The email displays historical results after 20 comparable completed cases and
permits a bounded calibration adjustment only after 30. Dry runs do not add
predictions or outcomes.

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

The example systemd service and timer in `deploy/` run the briefing every day at
07:00 in the `Asia/Jerusalem` timezone. The service safely delivers each daily
briefing at most once per recipient and exits. `Persistent=true` allows a missed
run to execute after a VM restart without duplicating an already completed
delivery.

Before deployment, replace `YOUR_GCP_USER`. The web unit binds only to the
private Docker gateway (`172.18.0.1:5001`) and Caddy publishes the app below
`/financialbrief`. Then install and enable the units:

```bash
sudo cp deploy/financial-brief.service /etc/systemd/system/
sudo cp deploy/financial-brief.timer /etc/systemd/system/
sudo cp deploy/financial-brief-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now financial-brief-web.service
sudo systemctl enable --now financial-brief.timer
```

Check scheduling and logs:

```bash
systemctl list-timers financial-brief.timer
journalctl -u financial-brief.service -n 100 --no-pager
```

## Safe validation order

1. Run unit tests locally.
2. On the VM, run a collection-only diagnostic or a dry run before enabling the timer.
3. Confirm the sender, subscriber count, source freshness, and generated Hebrew output.
4. Send one controlled test email.
5. Enable the daily timer.
