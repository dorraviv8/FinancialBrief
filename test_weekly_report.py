import unittest
from datetime import date

import weekly_report


class WeeklyReportTests(unittest.TestCase):
    def test_shortened_week_uses_previous_close_and_actual_sessions(self):
        metric = weekly_report.weekly_performance(
            [
                {"date": "2026-08-21", "close": 100},
                {"date": "2026-08-24", "close": 103},
                {"date": "2026-08-25", "close": 101},
                {"date": "2026-08-28", "close": 110},
            ],
            date(2026, 8, 24),
            date(2026, 8, 29),
        )

        self.assertEqual(metric["sessions"], 3)
        self.assertEqual(metric["start_date"], "2026-08-21")
        self.assertEqual(metric["end_date"], "2026-08-28")
        self.assertEqual(metric["change_pct"], 10.0)

    def test_sunday_window_covers_preceding_monday_through_saturday(self):
        self.assertEqual(
            weekly_report.calendar_week_window(date(2026, 8, 30)),
            (date(2026, 8, 24), date(2026, 8, 29)),
        )

    def test_official_weekly_fx_uses_first_and_last_published_rates(self):
        result = weekly_report.weekly_fx_performance({
            "rates": {
                "USD": [
                    {"date": "2026-08-24", "ils_rate": 2.994},
                    {"date": "2026-08-28", "ils_rate": 2.968},
                ],
                "EUR": [
                    {"date": "2026-08-24", "ils_rate": 3.4929},
                    {"date": "2026-08-28", "ils_rate": 3.4567},
                ],
            }
        })

        self.assertEqual(result["USD"]["start_date"], "2026-08-24")
        self.assertEqual(result["USD"]["end_date"], "2026-08-28")
        self.assertAlmostEqual(result["USD"]["change_pct"], -0.8684)

    def test_weekly_report_renders_separate_indices_and_one_sector_card(self):
        history = [
            {"date": "2026-08-21", "close": 100},
            {"date": "2026-08-24", "close": 101},
            {"date": "2026-08-28", "close": 105},
        ]
        sector_names = tuple(weekly_report.SECTOR_KEYWORDS)
        data = {
            "indices": {
                name: {"history_closes": history, "chart_source": "https://tase.example/index"}
                for name in weekly_report.INDEX_ORDER
            },
            "sectors": {
                name: {
                    "history_closes": history,
                    "top_stocks_by_market_cap": [],
                }
                for name in sector_names
            },
            "exchange_rates": {"source": "https://boi.example/rates"},
        }
        snapshot = weekly_report.build_weekly_snapshot(data, date(2026, 8, 30))
        news = [{
            "id": 1,
            "source": "בנק ישראל",
            "reliability": "official",
            "title": "בנק ישראל פרסם החלטת ריבית",
            "summary": "",
            "link": "https://boi.example/news",
            "companies": [],
            "published": "2026-08-25T08:00:00+03:00",
        }]
        response = ["NEWS|W1|בנק ישראל פרסם החלטת ריבית חדשה."]
        response.extend(
            f"SECTOR|{name}|{'W1' if name == 'בנקים' else 'NONE'}|"
            f"{'החלטת הריבית השפיעה על סביבת הפעילות.' if name == 'בנקים' else 'NONE'}"
            for name in sector_names
        )
        response.extend((
            "OUTLOOK|בסיס|המסחר עשוי להיפתח במגמה מעורבת.",
            "OUTLOOK|חדשות חיוביות עשויות לתמוך בשוק.",
            "OUTLOOK|אירועים מקומיים עלולים להכביד.",
        ))
        analysis = weekly_report.parse_weekly_ai_response(
            data, snapshot, news, "\n".join(response)
        )
        brief = weekly_report.build_weekly_brief(
            data,
            snapshot,
            {
                "USD": {"available": False},
                "EUR": {"available": False},
            },
            analysis,
        )

        self.assertEqual(brief.count("### מדד ת״א-"), 3)
        self.assertEqual(brief.count("### סקירת הסקטורים"), 1)
        self.assertIn("סגירת 21/08/2026", brief)
        self.assertIn("סגירת 28/08/2026", brief)
        for name in sector_names:
            self.assertIn(f"**{name}:", brief)
        self.assertIn("### מבט לשבוע הבא", brief)
        self.assertNotIn("בסיס|", brief)


if __name__ == "__main__":
    unittest.main()
