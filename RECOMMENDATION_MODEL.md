# Recommendation model

## Production policy

Recommendation V2 starts in `shadow` mode. The email shows its probability,
risk, confidence, horizon and expected-excess-return evidence, while V1 remains
the active ranking. This avoids replacing a transparent baseline before the new
model has genuine forward results.

Set `RECOMMENDATION_V2_MODE` to one of:

- `shadow` — V1 is active and both models are recorded.
- `auto` — V2 is promoted only after at least 60 matured 20-session forecasts,
  its probability Brier score is at least 5% better than V1, and its directional
  accuracy is no worse.
- `active` — manual V2 activation. Use only after reviewing the stored evidence.

## V2 forecast

The system builds independent forecasts for 10, 20 and 30 TASE sessions. It
compares the current state with source-date historical cases using robust-scaled
distance. Cases are spaced five sessions apart to reduce repeated, highly
correlated observations. Inputs include:

- sector return and excess return over TA-125 for 20 and 63 sessions;
- sector and market volatility;
- current drawdown;
- continuous distance from 20- and 50-session averages;
- TA-125 return and volatility regime;
- current breadth and confirmation by the three largest stocks;
- current Bank of Israel rate and USD/ILS move in the feature ledger, ready for
  future learning once enough forward observations exist.

For the closest matured cases, the engine estimates:

- probability of a positive sector return;
- probability of outperforming TA-125;
- expected excess return and its 25th–75th percentile range.

The opportunity score is:

```text
45% probability of positive return
+ 35% probability of outperforming TA-125
+ 20% risk quality
```

Confidence reflects training-case count, feature completeness and chronological
walk-forward quality. It shrinks uncertain forecasts toward 50:

```text
score before news = 50 + confidence × (opportunity score - 50)
final V2 score = score before news + verified catalyst adjustment
```

## News and disclosures

The AI does not choose a score or write the sector recommendation. It may only
classify one supplied event per sector by source ID, event type, direction,
materiality and expected duration. Local deterministic code verifies that the
headline actually names the sector or one of its leading companies, then applies
source reliability and time decay, capped at ±5 points. Invalid, unlinked or
misclassified IDs receive zero points. Official MAYA and government sources
receive full reliability weight; financial press receives a reduced weight.

## Evaluation and audit trail

SQLite stores every current feature snapshot, every live forecast, the generated
historical audit cases and all matured outcomes. Walk-forward evaluation always
trains on earlier dates and evaluates later dates. It records probability Brier
scores, directional accuracy, sector-ranking correlation, top-three hit rate and
top-three average excess return.

The historical TASE cache retains verified observations for up to five years.
The public endpoint currently supplies a trailing window, so older history grows
naturally rather than being fabricated. Historical cases use price/regime fields
only; breadth, news, fundamentals and current macro values enter learning only
after genuine daily snapshots have matured.

Scores are comparative research signals, not guaranteed returns, personal advice
or automatic trade instructions.
