import unittest
from datetime import date

import weekly_report


class WeeklyReportTests(unittest.TestCase):
    def test_translation_guard_rejects_million_to_billion_error(self):
        source = "Supply agreement worth $4.6m through 2027"

        self.assertTrue(
            weekly_report.translation_preserves_source_facts(
                source,
                "הסכם אספקה בהיקף 4.6 מיליון דולר עד 2027",
            )
        )
        self.assertFalse(
            weekly_report.translation_preserves_source_facts(
                source,
                "הסכם אספקה בהיקף 4.6 מיליארד דולר עד 2027",
            )
        )
        self.assertFalse(
            weekly_report.translation_preserves_source_facts(
                source,
                "הסכם אספקה בהיקף 4.6 דולר עד 2027",
            )
        )

    def test_unfaithful_news_translation_falls_back_to_source_title(self):
        data = {"sectors": {"טכנולוגיה": {}}}
        snapshot = {"indices": {}, "sectors": {"טכנולוגיה": {}}}
        news = [{
            "id": 7,
            "source": "מאיה",
            "reliability": "official",
            "title": "Supply agreement worth $4.6m through 2027",
            "summary": "",
            "companies": [],
        }]

        parsed = weekly_report.parse_weekly_ai_response(
            data,
            snapshot,
            news,
            "NEWS|W7|הסכם בהיקף 4.6 מיליארד דולר עד 2027",
        )

        self.assertEqual(parsed["translation_fallbacks"], ["W7"])
        self.assertTrue(parsed["selected_news"][0]["translation_fallback"])
        self.assertIn("$4.6m", parsed["selected_news"][0]["ai_summary"])

    def test_translation_guard_preserves_percent_and_currency_meaning(self):
        source = "The company sold 4% for NIS 450 million"

        self.assertTrue(
            weekly_report.translation_preserves_source_facts(
                source, "החברה מכרה 4% תמורת 450 מיליון שקל"
            )
        )
        self.assertFalse(
            weekly_report.translation_preserves_source_facts(
                source, "החברה מכרה 4 מניות תמורת 450 מיליון שקל"
            )
        )

    def test_weekly_news_parses_summary_and_market_impact(self):
        data = {"sectors": {"בנקים": {}}}
        snapshot = {"indices": {}, "sectors": {"בנקים": {}}}
        news = [{
            "id": 8,
            "source": "בנק ישראל",
            "reliability": "official",
            "title": "בנק ישראל הודיע על שינוי בריבית",
            "summary": "",
            "companies": [],
        }]

        parsed = weekly_report.parse_weekly_ai_response(
            data,
            snapshot,
            news,
            "NEWS|W8|בנק ישראל הודיע על שינוי בריבית.|המהלך עשוי להשפיע על עלויות המימון ועל מניות הבנקים.",
        )

        self.assertEqual(
            parsed["selected_news"][0]["ai_summary"],
            "בנק ישראל הודיע על שינוי בריבית.",
        )
        self.assertIn("עלויות המימון", parsed["selected_news"][0]["market_impact"])

    def test_weekly_candidates_exclude_routine_close_and_admin_notice(self):
        candidates = weekly_report._ai_news_candidates([
            {
                "id": 1,
                "title": "נעילה חיובית: ת״א-35 עלה והביטוח ירד",
                "summary": "",
                "source": "עיתון",
            },
            {
                "id": 2,
                "title": "דוח הצעת מדף ומועד תשלום",
                "summary": "",
                "source": "מאיה",
                "reliability": "official",
            },
            {
                "id": 3,
                "title": "הפחתת הריבית הוזילה את עלויות המימון בענף הנדל״ן",
                "summary": "",
                "source": "בנק ישראל",
                "reliability": "official",
            },
        ])

        self.assertEqual([item["id"] for item in candidates], [3])

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
            f"{'1|4|14|החלטת הריבית השפיעה על סביבת הפעילות.' if name == 'בנקים' else '0|0|1|NONE'}"
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
        opportunities = {
            name: {
                "final_score": 75 - position,
                "label": "חזקה",
                "weekly_change_pct": 5 - position,
                "previous_weekly_score": 70 - position,
                "v2_confidence_pct": 60,
                "opportunity_reason": "המגמה התחזקה השבוע",
                "invalidation": "ההערכה תיחלש בירידה מתחת לתמיכה.",
            }
            for position, name in enumerate(sector_names)
        }
        brief = weekly_report.build_weekly_brief(
            data,
            snapshot,
            {
                "USD": {"available": False},
                "EUR": {"available": False},
            },
            analysis,
            opportunities,
        )

        self.assertIn("### השוק ב-60 שניות", brief)
        self.assertIn("### מפת ההזדמנויות השבועית", brief)
        self.assertIn("שלושת הסקטורים המובילים לבדיקה", brief)
        self.assertEqual(brief.count("### מדד ת״א-"), 3)
        self.assertEqual(brief.count("### סקירת הסקטורים"), 1)
        self.assertIn("סגירת 21/08/2026", brief)
        self.assertIn("סגירת 28/08/2026", brief)
        for name in sector_names:
            self.assertIn(f"**{name}:", brief)
        self.assertIn("### מבט לשבוע הבא", brief)
        self.assertIn("- **מה קרה:**", brief)
        self.assertIn("**השפעה על השוק:**", brief)
        self.assertIn("[למקור המלא – בנק ישראל]", brief)
        self.assertIn("אירוע 1 · 25/08/2026", brief)
        self.assertNotIn("בסיס|", brief)


if __name__ == "__main__":
    unittest.main()
