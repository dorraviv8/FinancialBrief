# Weekend Report – Weekly Summary

Every Sunday, the 07:45 preparation path automatically generates and QA-approves
a weekly Israeli-market report instead of the daily report. The approved report
is sent at 08:00. Weekday behavior is unchanged.

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
- Sunday uses one bounded AI request to select up to six material articles,
  identify only directly supported sector context, and write a plain-language
  coming-week outlook. All percentages and dates are calculated locally.
- Once every active recipient has received the weekly report, unused articles
  from that window are deleted. Selected evidence is retained for 90 days.

For a local, non-delivery preview, run:

```bash
python financial_brief.py --dry-run --weekly-preview
```
