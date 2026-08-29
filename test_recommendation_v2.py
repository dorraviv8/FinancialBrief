import unittest
from datetime import date, timedelta

import recommendation_v2


def _series(daily_return: float, sessions: int = 320) -> list[dict]:
    value = 100.0
    points = []
    start = date(2025, 1, 1)
    for index in range(sessions):
        value *= 1 + daily_return + (((index % 9) - 4) * 0.00008)
        points.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "close": round(value, 6),
        })
    return points


def _sector(history: list[dict], advancers: int, decliners: int) -> dict:
    return {
        "history_closes": history,
        "breadth": {"advancers": advancers, "decliners": decliners},
        "top_5_weight_pct": 60,
        "top_stocks_by_market_cap": [
            {
                "avg_daily_turnover_6m_ils": 20_000_000,
                "technical_analysis": {"return_20d_pct": 2},
            }
            for _index in range(3)
        ],
    }


class RecommendationV2Tests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "indices": {
                "ת״א-125": {"history_closes": _series(0.0004)}
            },
            "sectors": {
                "חזק": _sector(_series(0.0015), 8, 2),
                "חלש": _sector(_series(-0.0006), 2, 8),
            },
        }

    def test_bundle_has_horizon_probabilities_risk_and_walk_forward_metrics(self):
        bundle = recommendation_v2.build_recommendation_bundle(self.data)

        strong = bundle["sectors"]["חזק"]
        weak = bundle["sectors"]["חלש"]
        self.assertEqual(set(strong["horizons"]), {10, 20, 30})
        self.assertGreater(
            strong["horizons"][20]["probability_outperform_pct"],
            weak["horizons"][20]["probability_outperform_pct"],
        )
        self.assertGreater(strong["risk_quality"], weak["risk_quality"])
        self.assertGreater(strong["primary_score"], weak["primary_score"])
        self.assertLessEqual(
            strong["horizons"][20]["confidence_pct"], 65
        )
        self.assertGreater(len(bundle["historical_samples"]), 80)
        self.assertGreater(bundle["evaluation"][20]["sample_count"], 0)
        self.assertIn(
            "outperform_brier_skill_pct", bundle["evaluation"][20]
        )

    def test_catalyst_changes_score_but_not_model_probabilities(self):
        base = recommendation_v2.build_recommendation_bundle(self.data)
        adjusted = recommendation_v2.apply_catalysts(base, {
            "חזק": {"adjustment": 5, "reason": "דיווח רשמי"},
            "חלש": {"adjustment": -5, "reason": "דיווח רשמי"},
        })

        for horizon in recommendation_v2.HORIZONS:
            base_forecast = base["sectors"]["חזק"]["horizons"][horizon]
            adjusted_forecast = adjusted["sectors"]["חזק"]["horizons"][horizon]
            self.assertEqual(
                base_forecast["probability_outperform_pct"],
                adjusted_forecast["probability_outperform_pct"],
            )
            self.assertEqual(
                adjusted_forecast["final_score"],
                min(100, base_forecast["final_score"] + 5),
            )


if __name__ == "__main__":
    unittest.main()
