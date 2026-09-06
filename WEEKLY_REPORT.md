# Weekend Report – Weekly Summary

Every Sunday, the 07:45 preparation path automatically generates and QA-approves
a weekly Israeli-market report instead of the daily report. The approved report
is sent at 08:00. The same prepare, review, 07:55 recovery, and 08:00 delivery
workflow runs for daily reports on every other day.

The report opens with a deterministic `Market in 60 Seconds` card. It also
contains a weekly opportunity map for all ten sectors: full-week performance,
current 0–100 score, change from the preceding Sunday, confidence, and explicit
invalidation conditions for the three leaders. Approved Sunday scores are stored
separately so subsequent reports compare Sunday with Sunday rather than with the
latest weekday observation.

## Calculation rules

- The calendar window is Monday through Saturday immediately preceding the
  Sunday delivery. Only actual TASE trading sessions found in official history
  are counted, so shortened holiday weeks work without special cases.
- Index and sector performance compares the final official close before the
  first session of the week with the final official close inside the week. This
  includes the first session's move. Both comparison dates and the number of
  actual sessions are shown to readers.
- USD/ILS and EUR/ILS use the first and last representative rates published by
  the Bank of Israel within the weekly window. The Sunday run fetches the
  official historical range directly, with stored observations as a fallback.

## News lifecycle and AI limits

- The 07:45 preparation and the source-only 13:05/19:05 timer collect Israeli RSS,
  MAYA disclosures and Bank of Israel rates. The source-only job downloads no
  TASE charts and makes no AI request.
- Articles are deduplicated by source and normalized title. Up to 60 weekly
  source items can be retained, while a balanced maximum of 24 candidates is
  sent to the model.
- Sunday uses one bounded AI request to select up to six material articles and
  identify only directly supported sector context. All percentages, dates,
  opportunity scores and coming-week outlook sentences are calculated or
  composed locally from verified fields.
- Translated summaries are rejected if they add a number, currency or magnitude,
  or change million to billion. Rejected wording is replaced with the exact
  source headline. The final QA correction gate enforces the same rule.
- Once every active recipient has received the weekly report, unused articles
  from that window are deleted. Selected evidence is retained for 90 days.

For a local, non-delivery preview, run:

```bash
python financial_brief.py --dry-run --weekly-preview
```
