import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import financial_brief
import israel_market


class IsraelMarketTests(unittest.TestCase):
    def test_calculate_trend_metrics_returns_multiweek_signal(self):
        history = [
            {"TradeDate": f"{day:02d}/08/2026", "CloseRate": 100 + day}
            for day in range(1, 22)
        ]

        result = israel_market.calculate_trend_metrics(history)

        self.assertTrue(result["available"])
        self.assertEqual(result["sessions"], 21)
        self.assertGreater(result["return_5d_pct"], 0)
        self.assertGreater(result["return_20d_pct"], 0)
        self.assertEqual(result["directional_bias"], "חיובית")
        self.assertEqual(result["forecast_horizon_sessions"], 15)

    @patch("israel_market.HISTORY_CACHE_ENABLED", False)
    @patch("israel_market._tase_post")
    def test_one_year_history_fetches_every_page(self, mock_post):
        mock_post.side_effect = [
            {
                "TotalRec": 3,
                "Items": [
                    {"TradeDate": "03/08/2026", "CloseRate": 103},
                    {"TradeDate": "02/08/2026", "CloseRate": 102},
                ],
            },
            {
                "Items": [
                    {"TradeDate": "01/08/2026", "CloseRate": 101},
                ]
            },
        ]

        result = israel_market.get_index_history("142")

        self.assertEqual(len(result), 3)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_post.call_args_list[0].args[1]["pType"], 4)
        self.assertEqual(mock_post.call_args_list[1].args[1]["pageNum"], 2)

    @patch("israel_market._tase_post")
    def test_history_cache_avoids_duplicate_same_day_download(self, mock_post):
        trade_date = datetime.now(israel_market.ISRAEL_TZ).strftime("%d/%m/%Y")
        mock_post.return_value = {
            "TotalRec": 1,
            "Items": [{"TradeDate": trade_date, "CloseRate": 101}],
        }

        with TemporaryDirectory() as cache_dir, patch.object(
            israel_market, "HISTORY_CACHE_DIR", Path(cache_dir)
        ):
            first = israel_market.get_index_history("cache-test")
            second = israel_market.get_index_history("cache-test")

        self.assertEqual(first, second)
        self.assertEqual(mock_post.call_count, 1)

    @patch("israel_market._tase_post")
    def test_component_cache_keeps_live_metadata_to_one_download_daily(self, mock_post):
        mock_post.return_value = {
            "TotalRec": 1,
            "Items": [{"Symbol": "TEST", "MarketValue": 100}],
        }

        with TemporaryDirectory() as cache_dir, patch.object(
            israel_market, "HISTORY_CACHE_DIR", Path(cache_dir)
        ):
            first = israel_market.get_index_components("cache-test")
            second = israel_market.get_index_components("cache-test")

        self.assertEqual(first, second)
        self.assertEqual(mock_post.call_count, 1)

    @patch("israel_market.get_maya_announcements", return_value=[])
    @patch("israel_market.get_boi_interest_rate", return_value={})
    @patch("israel_market.get_boi_exchange_rates", return_value={})
    @patch("israel_market.enrich_stock_technicals")
    @patch("israel_market.get_sector_analysis", return_value={})
    @patch("israel_market.get_core_indices", return_value={})
    def test_lightweight_snapshot_skips_all_historical_technicals(
        self,
        mock_core,
        mock_sectors,
        mock_enrich,
        _mock_boi,
        _mock_interest,
        _mock_maya,
    ):
        israel_market.collect_israeli_market_data(
            include_news=False,
            include_technicals=False,
        )

        mock_core.assert_called_once_with(include_technicals=False)
        mock_sectors.assert_called_once_with(include_technicals=False)
        mock_enrich.assert_not_called()

    def test_one_year_technical_analysis_contains_requested_indicators(self):
        history = []
        for day in range(241):
            date = datetime(2025, 1, 1) + timedelta(days=day)
            close = 100 + (day * 0.2) + ((day % 7) - 3)
            history.append({
                "TradeDate": date.strftime("%d/%m/%Y"),
                "CloseRate": close,
                "HighRate": close + 2,
                "LowRate": close - 2,
                "TurnOverValueShekel": 1_000_000 + (day * 1_000),
            })

        result = israel_market.calculate_trend_metrics(history)

        self.assertEqual(result["sessions"], 241)
        self.assertIsNotNone(result["return_20d_pct"])
        self.assertIsNotNone(result["return_3m_pct"])
        self.assertIsNotNone(result["return_6m_pct"])
        self.assertIsNotNone(result["return_1y_pct"])
        self.assertIsNotNone(result["sma_20"])
        self.assertIsNotNone(result["sma_50"])
        self.assertIsNotNone(result["sma_200"])
        self.assertIsNotNone(result["rsi_14"])
        self.assertIsNotNone(result["macd_histogram"])
        self.assertIsNotNone(result["support_60d"])
        self.assertIsNotNone(result["resistance_60d"])
        self.assertIsNotNone(result["turnover_vs_20d_avg"])
        self.assertIsNotNone(result["trendline_60d_pct"])
        self.assertIsNotNone(result["trendline_200d_pct"])

    def test_security_history_uses_adjusted_prices_and_ils(self):
        result = israel_market.normalize_security_history_to_ils([{
            "TradeDate": "01/08/2026",
            "CloseRate": 1000,
            "HighRate": 1100,
            "LowRate": 900,
            "AdjustmentRate": 100,
        }])

        self.assertEqual(result[0]["CloseRate"], 1.0)
        self.assertEqual(result[0]["HighRate"], 1.1)
        self.assertEqual(result[0]["LowRate"], 0.9)

    @patch("israel_market.get_index_major_data")
    @patch("israel_market.get_index_constituents")
    @patch("israel_market.get_index_components")
    @patch("israel_market.get_index_history")
    def test_sector_top_three_are_ranked_by_official_market_cap(
        self, mock_history, mock_components, mock_live, mock_major
    ):
        mock_history.return_value = [
            {"TradeDate": "01/08/2026", "CloseRate": 100},
            {"TradeDate": "02/08/2026", "CloseRate": 101},
        ]
        mock_components.return_value = [
            {"ShortName": "Small", "Symbol": "S", "MarketValue": 100, "Weight": 1},
            {"ShortName": "Largest", "Symbol": "L", "MarketValue": 900, "Weight": 9},
            {"ShortName": "Third", "Symbol": "T", "MarketValue": 300, "Weight": 3},
            {"ShortName": "Second", "Symbol": "M", "MarketValue": 600, "Weight": 6},
        ]
        mock_live.return_value = [
            {"Name": "Largest", "Symbol": "L", "LastRate": 1200, "Change": 1.2},
            {"Name": "Second", "Symbol": "M", "LastRate": 800, "Change": -0.4},
            {"Name": "Third", "Symbol": "T", "LastRate": 500, "Change": 0.1},
            {"Name": "Small", "Symbol": "S", "LastRate": 200, "Change": 0},
        ]
        mock_major.return_value = {
            "LastDaysData": [{"LastRate": 101, "Change": 1, "TurnOver": 50}]
        }

        result = israel_market.build_index_analysis("בדיקה", "999")

        stocks = result["top_stocks_by_market_cap"]
        self.assertEqual([stock["symbol"] for stock in stocks], ["L", "M", "T"])
        self.assertEqual(stocks[0]["market_cap_m_ils"], 900)
        self.assertEqual(stocks[0]["price_ils"], 12.0)
        self.assertEqual(result["breadth"], {"advancers": 2, "decliners": 1, "unchanged": 1})
        self.assertEqual(result["history_closes"], [
            {"date": "2026-08-01", "close": 100.0},
            {"date": "2026-08-02", "close": 101.0},
        ])

    def test_compact_sector_payload_keeps_only_required_analysis(self):
        data = {
            "as_of": "2026-08-25T07:00:00+03:00",
            "sectors": {
                "בנקים": {
                    "index_id": "013",
                    "trend": {"directional_bias": "חיובית"},
                    "top_stocks_by_market_cap": [{"symbol": "LUMI"}],
                    "top_gainers": [{"symbol": "UNNEEDED"}],
                }
            },
        }

        result = financial_brief._compact_sector_payload(data)

        self.assertEqual(result["sectors"]["בנקים"]["index_id"], "013")
        self.assertNotIn("top_gainers", result["sectors"]["בנקים"])

    def test_validation_rejects_partial_chart_data(self):
        with self.assertRaisesRegex(RuntimeError, "one-year chart missing"):
            financial_brief.validate_market_data({
                "indices": {
                    name: {"constituents_count": 1, "trend": {"sessions": 20}}
                    for name in ("ת״א-35", "ת״א-90", "ת״א-125")
                },
                "sectors": {},
            })

    def test_quantitative_cards_are_complete_without_ai(self):
        unavailable = {"available": False}
        data = {
            "indices": {
                name: {"trend": unavailable, "breadth": {}, "chart_source": "https://example.com"}
                for name in ("ת״א-35", "ת״א-90", "ת״א-125")
            },
            "sectors": {
                "בנקים": {
                    "trend": unavailable,
                    "breadth": {},
                    "chart_source": "https://example.com/sector",
                    "top_stocks_by_market_cap": [
                        {
                            "name": name,
                            "symbol": symbol,
                            "technical_analysis": unavailable,
                            "chart_source": f"https://example.com/{symbol}",
                        }
                        for name, symbol in (("לאומי", "LUMI"), ("פועלים", "POLI"), ("מזרחי", "MZTF"))
                    ],
                }
            },
        }

        cards = financial_brief.build_quantitative_cards(data)

        self.assertEqual(cards.count("### מדד ת״א-"), 3)
        self.assertIn("### סקטור: בנקים", cards)
        for symbol in ("LUMI", "POLI", "MZTF"):
            self.assertIn(symbol, cards)

    def test_market_snapshot_and_index_cards_show_official_closing_date(self):
        data = {
            "indices": {
                name: {
                    "last_value": 2500,
                    "change_1d_pct": 0.5,
                    "breadth": {},
                    "trend": {"available": False, "as_of": "2026-08-25"},
                }
                for name in ("ת״א-35", "ת״א-90", "ת״א-125")
            }
        }

        notice = financial_brief._market_close_notice(data)
        cards = financial_brief.build_quantitative_cards(data)

        self.assertIn("סגירת המסחר ביום 25.08.2026", notice)
        self.assertEqual(cards.count("נתוני הסגירה ליום 25.08.2026"), 3)

    def test_sector_chart_is_split_into_plain_language_rows(self):
        summary = financial_brief._sector_chart_summary({
            "available": True,
            "as_of": "2026-08-25",
            "sessions": 241,
            "return_20d_pct": 1.2,
            "return_3m_pct": 2.3,
            "return_6m_pct": 3.4,
            "return_1y_pct": 4.5,
            "above_sma_20": True,
            "above_sma_50": True,
            "above_sma_200": False,
            "rsi_14": 55,
            "macd_histogram": 0.5,
            "support_60d": 100,
            "resistance_60d": 120,
            "turnover_vs_20d_avg": 1.1,
            "directional_bias": "חיובית",
        })

        rendered = "\n".join(summary)
        for label in (
            "תקופת הגרף",
            "ביצועים",
            "כיוון המגמה",
            "עוצמת התנועה",
            "רמות שכדאי לעקוב אחריהן",
        ):
            self.assertIn(label, rendered)
        self.assertIn("אזור תמיכה, שבו ירידות נבלמו לאחרונה", rendered)
        self.assertEqual(financial_brief._hebrew_count(1, "יורדת", "יורדות"), "1 יורדת")
        self.assertEqual(financial_brief._hebrew_count(3, "יורדת", "יורדות"), "3 יורדות")

    def test_sector_graph_score_rewards_stronger_multiweek_data(self):
        strong = {
            "breadth": {"advancers": 8, "decliners": 2},
            "trend": {
                "available": True,
                "return_20d_pct": 8,
                "return_3m_pct": 16,
                "return_6m_pct": 25,
                "return_1y_pct": 40,
                "above_sma_20": True,
                "above_sma_50": True,
                "above_sma_200": True,
                "rsi_14": 58,
                "macd_histogram": 2,
                "directional_bias": "חיובית",
                "annualized_volatility_pct": 20,
            },
        }
        weak = {
            "breadth": {"advancers": 2, "decliners": 8},
            "trend": {
                "available": True,
                "return_20d_pct": -8,
                "return_3m_pct": -16,
                "return_6m_pct": -25,
                "return_1y_pct": -40,
                "above_sma_20": False,
                "above_sma_50": False,
                "above_sma_200": False,
                "rsi_14": 25,
                "macd_histogram": -2,
                "directional_bias": "שלילית",
                "annualized_volatility_pct": 45,
            },
        }

        strong_score = financial_brief.calculate_sector_graph_score(strong)
        weak_score = financial_brief.calculate_sector_graph_score(weak)

        self.assertGreaterEqual(strong_score, 75)
        self.assertLessEqual(weak_score, 30)
        self.assertGreater(strong_score, weak_score)

    def test_news_adjustment_is_bounded_and_score_is_added_to_sector_card(self):
        sector = {
            "breadth": {"advancers": 5, "decliners": 5},
            "trend": {
                "available": True,
                "as_of": "2026-08-25",
                "sessions": 241,
                "return_20d_pct": 0,
                "return_3m_pct": 0,
                "return_6m_pct": 0,
                "return_1y_pct": 0,
                "above_sma_20": True,
                "above_sma_50": False,
                "above_sma_200": True,
                "rsi_14": 55,
                "macd_histogram": 0,
                "directional_bias": "ניטרלית",
                "annualized_volatility_pct": 30,
            },
            "top_stocks_by_market_cap": [],
        }
        data = {"indices": {}, "sectors": {"טכנולוגיה": sector}}

        calibration = {
            "טכנולוגיה": {
                "adjustment": -3,
                "horizons": {
                    10: {"sample_size": 20, "hit_rate_pct": 40, "avg_excess_return_pct": -1.2, "confidence": "ראשונית"},
                    20: {"sample_size": 0},
                    30: {"sample_size": 0},
                },
            }
        }
        scores = financial_brief._parse_sector_scores(
            data,
            "SCORE|טכנולוגיה|99|כותרת חיובית שסופקה",
            calibration,
        )
        cards = financial_brief.build_quantitative_cards(data, scores, calibration)

        self.assertEqual(scores["טכנולוגיה"]["news_adjustment"], 10)
        self.assertEqual(
            scores["טכנולוגיה"]["final_score"],
            min(100, scores["טכנולוגיה"]["graph_score"] + 10 - 3),
        )
        self.assertIn("ציון אטרקטיביות להשקעה כעת", cards)
        self.assertIn("השפעת החדשות: +10 נקודות", cards)
        self.assertIn("כיול היסטורי: -3 נקודות", cards)
        self.assertIn("עקפו את ת״א-125 ב-40.0%", cards)
        self.assertNotIn("SCORE|", financial_brief._strip_score_protocol(
            "SCORE|טכנולוגיה|2|סיבה\n### סעיף\nתוכן"
        ))

    def test_reader_email_prioritizes_changes_and_limits_sector_detail(self):
        trend = {
            "available": True,
            "as_of": "2026-08-26",
            "return_20d_pct": 2,
            "return_3m_pct": 5,
            "above_sma_20": True,
            "above_sma_50": True,
            "above_sma_200": False,
            "rsi_14": 55,
            "macd_histogram": 1,
            "directional_bias": "חיובית",
        }
        sector_names = ["בנקים", "ביטוח", "ביומד", "טכנולוגיה"]
        data = {
            "indices": {
                name: {
                    "last_value": 2000,
                    "change_1d_pct": 0.5,
                    "trend": trend,
                    "chart_source": f"https://example.com/{name}",
                }
                for name in ("ת״א-35", "ת״א-90", "ת״א-125")
            },
            "sectors": {},
        }
        for name in sector_names:
            data["sectors"][name] = {
                "change_1d_pct": 0.3,
                "trend": dict(trend),
                "chart_source": f"https://example.com/sector/{name}",
                "top_stocks_by_market_cap": [
                    {
                        "name": f"מניה {index}",
                        "symbol": f"S{index}",
                        "change_1d_pct": index,
                        "technical_analysis": {"return_20d_pct": index + 1},
                        "chart_source": f"https://example.com/stock/{index}",
                    }
                    for index in range(1, 4)
                ],
            }
        scores = {
            name: {
                "final_score": score,
                "label": "חזקה" if score >= 70 else "חלשה",
                "graph_score": score,
                "news_adjustment": 0,
                "calibration_adjustment": 0,
                "reason": "לא זוהתה השפעת חדשות.",
            }
            for name, score in zip(sector_names, (80, 70, 60, 30))
        }
        previous = {
            name: {
                "final_score": score - (4 if name == "בנקים" else 0),
                "graph_score": score - (4 if name == "בנקים" else 0),
                "news_adjustment": 0,
                "calibration_adjustment": 0,
            }
            for name, score in zip(sector_names, (80, 70, 60, 30))
        }

        dashboard = financial_brief.build_reader_dashboard(scores, previous)
        cards = financial_brief.build_reader_quantitative_cards(
            data, scores, previous_scores=previous
        )

        self.assertIn("בנקים: 80/100 (+4)", dashboard)
        self.assertIn("דירוג כל הסקטורים", cards)
        self.assertEqual(cards.count("#### "), 3)
        self.assertNotIn("#### טכנולוגיה", cards)
        self.assertIn("כל הנתונים מתייחסים לסגירת 26.08.2026", cards)

    def test_reader_context_hides_immaterial_currency_noise(self):
        card = financial_brief.build_reader_context_card(
            {
                "indices": {},
                "exchange_rates": {
                    "rates": {
                        "USD": {"change_pct": 0.1},
                        "EUR": {"change_pct": -0.2},
                    }
                },
                "maya_announcements": [],
                "news": [],
            },
            {"בנקים": {"news_adjustment": 0}},
        )

        self.assertIn("לא נרשמה תנועה יומית של 0.5%", card)
        self.assertNotIn("USD/ILS +0.10%", card)

    def test_source_context_cards_include_official_and_press_sources(self):
        cards = financial_brief.build_source_context_cards({
            "exchange_rates": {
                "source": "https://boi.example",
                "rates": {"USD": {"ils_rate": 3.1, "change_pct": -0.2}},
            },
            "maya_announcements": [{
                "source": "מאיה",
                "title": "דיווח רשמי",
                "link": "https://maya.example",
            }],
            "news": [{
                "source": "גלובס",
                "title": "כותרת",
                "link": "https://globes.example",
            }],
        })

        self.assertIn("בנק ישראל", cards)
        self.assertIn("דיווח רשמי", cards)
        self.assertIn("גלובס", cards)

    def test_ai_section_gate_rejects_truncated_output(self):
        with self.assertRaisesRegex(RuntimeError, "מבט סקטוריאלי להמשך"):
            financial_brief._require_ai_sections(
                "### סקטורים בולטים ותובנות AI\nתוכן",
                ("סקטורים בולטים ותובנות AI", "מבט סקטוריאלי להמשך"),
            )

    @patch.object(financial_brief, "GROQ_API_KEY", "test-key")
    @patch("financial_brief._groq_completion")
    @patch("financial_brief.Groq")
    def test_ai_generation_uses_exactly_two_bounded_requests(
        self, mock_groq, mock_completion
    ):
        mock_completion.side_effect = [
            "### תמונת מצב בבורסה בתל אביב\nמצב\n### מבט להמשך\nתחזית",
            "### סקטורים בולטים ותובנות AI\nתובנה\n### מבט סקטוריאלי להמשך\nתחזית",
        ]

        brief = financial_brief.generate_hebrew_brief({"indices": {}, "sectors": {}})

        self.assertEqual(mock_completion.call_count, 2)
        self.assertEqual(mock_completion.call_args_list[0].kwargs["max_tokens"], 1200)
        self.assertEqual(mock_completion.call_args_list[1].kwargs["max_tokens"], 700)
        market_prompt = mock_completion.call_args_list[0].args[2]
        sector_prompt = mock_completion.call_args_list[1].args[2]
        self.assertIn("הכיוון הסביר", market_prompt)
        self.assertIn("מתי נשנה את ההערכה", market_prompt)
        self.assertIn("אסור להשתמש בלי הסבר", market_prompt)
        self.assertIn("CATALYST|שם הסקטור", sector_prompt)
        self.assertIn("מזהה חדשות", sector_prompt)
        self.assertIn("עד 5± נקודות", sector_prompt)
        self.assertIn("אסור לך לבחור ציון", sector_prompt)
        self.assertLess(
            brief.index("### מה השתנה ומה חשוב הבוקר"),
            brief.index("### דירוג כל הסקטורים"),
        )
        mock_groq.return_value.models.list.assert_not_called()

    def test_html_email_escapes_ai_html_and_renders_markdown_links(self):
        brief = "### בדיקה\n**עובדה** <script>bad()</script> [מקור](https://example.com)"

        rendered = financial_brief.build_html_email(brief, "https://example.com/unsubscribe")

        self.assertIn("<strong>עובדה</strong>", rendered)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", rendered)
        self.assertIn('href="https://example.com"', rendered)
        self.assertNotIn("<script>bad()", rendered)

    def test_html_email_is_rtl_splits_indices_and_removes_profile_preamble(self):
        brief = """פרופיל משקיע: מאוזן
### מדד ת״א-35
- **מגמה:** חיובית
### מדד ת״א-90
נתון נפרד
### מדד ת״א-125
נתון נפרד
### מבט להמשך
הימים הקרובים והשבועות הקרובים
"""

        rendered = financial_brief.build_html_email(brief)

        self.assertIn('<body dir="rtl" align="right">', rendered)
        self.assertEqual(rendered.count('class="card index-card"'), 3)
        self.assertIn("<ul dir=\"rtl\">", rendered)
        self.assertIn("מבט להמשך", rendered)
        self.assertNotIn("פרופיל משקיע", rendered)

    def test_news_catalysts_are_source_weighted_freshness_adjustments(self):
        fixed_now = datetime(
            2026, 8, 28, 7, 0, tzinfo=financial_brief.ISRAEL_TZ
        )
        data = {
            "sectors": {"בנקים": {}, "טכנולוגיה": {}},
            "maya_announcements": [{
                "source": "מאיה",
                "reliability": "official",
                "title": "דיווח רשמי של בנק לאומי",
                "published": "2026-08-28T06:00:00+03:00",
            }],
            "news": [{
                "source": "עיתונות",
                "reliability": "financial_press",
                "title": "רגולציה חדשה לענף הטכנולוגיה",
                "published": "2026-08-28T06:00:00+03:00",
            }],
        }
        protocol = (
            "CATALYST|בנקים|M0|earnings|1|5|10|אירוע חיובי רשמי\n"
            "CATALYST|טכנולוגיה|N0|regulatory|-1|5|10|אירוע שלילי בעיתונות"
        )

        with patch.object(financial_brief, "_today", return_value=fixed_now):
            catalysts = financial_brief._parse_news_catalysts(data, protocol)

        self.assertEqual(catalysts["בנקים"]["adjustment"], 5)
        self.assertEqual(catalysts["טכנולוגיה"]["adjustment"], -3)
        self.assertEqual(catalysts["בנקים"]["news_id"], "M0")
        self.assertEqual(catalysts["בנקים"]["event_type"], "earnings")

    def test_news_catalyst_rejects_name_collision_across_sectors(self):
        data = {
            "sectors": {"נפט וגז": {}, "אנרגיה ותשתיות": {}},
            "maya_announcements": [],
            "news": [
                {
                    "source": "כלכליסט",
                    "reliability": "financial_press",
                    "title": "דלק רכב קפצה לאחר הדוח",
                    "published": "2026-08-28T06:00:00+03:00",
                },
                {
                    "source": "ביזפורטל",
                    "reliability": "financial_press",
                    "title": "אאורה דיווחה על מכירת דירות",
                    "published": "2026-08-28T06:00:00+03:00",
                },
            ],
        }
        protocol = (
            "CATALYST|נפט וגז|N0|earnings|1|5|10|דלק רכב עלתה\n"
            "CATALYST|אנרגיה ותשתיות|N1|earnings|1|5|אאורה עלתה"
        )

        catalysts = financial_brief._parse_news_catalysts(data, protocol)

        self.assertEqual(catalysts["נפט וגז"]["adjustment"], 0)
        self.assertEqual(catalysts["אנרגיה ותשתיות"]["adjustment"], 0)

    def test_delivery_run_reuses_brief_retries_failure_then_skips_duplicate(self):
        fixed_now = datetime(
            2026, 8, 28, 7, 0, tzinfo=financial_brief.ISRAEL_TZ
        )
        data = {
            "as_of": "2026-08-28T06:55:00+03:00",
            "indices": {
                "ת״א-125": {"trend": {"as_of": "2026-08-27"}}
            },
            "sectors": {},
            "collection_stats": {"network_requests": 1},
        }

        with TemporaryDirectory() as directory:
            original_db_path = financial_brief.database.DB_PATH
            financial_brief.database.DB_PATH = str(Path(directory) / "brief.db")
            try:
                financial_brief.database.init_db()
                financial_brief.database.add_subscriber(
                    "Reader", "reader@example.com"
                )
                with (
                    patch.object(financial_brief, "OWNER_EMAIL", None),
                    patch.object(financial_brief, "GMAIL_USER", "sender@example.com"),
                    patch.object(financial_brief, "GMAIL_APP_PASSWORD", "secret"),
                    patch.object(financial_brief, "_today", return_value=fixed_now),
                    patch.object(
                        financial_brief,
                        "collect_israeli_market_data",
                        return_value=data,
                    ) as collect,
                    patch.object(financial_brief, "validate_market_data"),
                    patch.object(
                        financial_brief,
                        "generate_hebrew_brief",
                        return_value="### בדיקה\nתוכן קבוע",
                    ) as generate,
                    patch.object(
                        financial_brief,
                        "review_and_correct_brief",
                        return_value=(
                            "### בדיקה\nתוכן קבוע",
                            {"status": "approved", "corrections_applied": 0},
                        ),
                    ),
                    patch.object(
                        financial_brief,
                        "send_email",
                        side_effect=(RuntimeError("temporary SMTP failure"), None),
                    ) as send,
                ):
                    first = financial_brief.run(send=True)
                    second = financial_brief.run(send=True)
                    third = financial_brief.run(send=True)

                self.assertEqual(first["sent"], 0)
                self.assertEqual(len(first["failed"]), 1)
                self.assertEqual(first["delivery"]["status"], "partial")
                self.assertTrue(second["reused_brief"])
                self.assertEqual(second["sent"], 1)
                self.assertEqual(second["delivery"]["status"], "completed")
                self.assertTrue(third["reused_brief"])
                self.assertEqual(third["sent"], 0)
                self.assertEqual(
                    third["skipped"][0]["reason"], "already_sent"
                )
                collect.assert_called_once_with(include_news=True)
                generate.assert_called_once()
                self.assertEqual(send.call_count, 2)
                first_message_id = send.call_args_list[0].kwargs["message_id"]
                second_message_id = send.call_args_list[1].kwargs["message_id"]
                self.assertEqual(first_message_id, second_message_id)

                with financial_brief.database.get_connection() as conn:
                    run_count = conn.execute(
                        "SELECT COUNT(*) FROM briefing_runs"
                    ).fetchone()[0]
                    delivery = conn.execute(
                        "SELECT status, attempts FROM email_deliveries"
                    ).fetchone()
                self.assertEqual(run_count, 1)
                self.assertEqual(delivery["status"], "sent")
                self.assertEqual(delivery["attempts"], 2)
            finally:
                financial_brief.database.DB_PATH = original_db_path

    def test_prepare_then_send_uses_only_the_qa_approved_stored_report(self):
        fixed_now = datetime(
            2026, 8, 31, 7, 45, tzinfo=financial_brief.ISRAEL_TZ
        )
        data = {
            "as_of": "2026-08-31T07:45:00+03:00",
            "indices": {"ת״א-125": {"trend": {"as_of": "2026-08-28"}}},
            "sectors": {},
            "collection_stats": {"http_requests": 1},
        }
        brief = "### דוח מאושר\nתוכן שנבדק ונשמר לפני שליחתו לקוראים."
        qa = {
            "status": "approved",
            "review_result": "pass",
            "corrections_applied": 0,
        }

        with TemporaryDirectory() as directory:
            original_db_path = financial_brief.database.DB_PATH
            financial_brief.database.DB_PATH = str(Path(directory) / "brief.db")
            try:
                financial_brief.database.init_db()
                financial_brief.database.add_subscriber(
                    "Reader", "reader@example.com"
                )
                with (
                    patch.object(financial_brief, "OWNER_EMAIL", None),
                    patch.object(financial_brief, "GMAIL_USER", "sender@example.com"),
                    patch.object(financial_brief, "GMAIL_APP_PASSWORD", "secret"),
                    patch.object(financial_brief, "_today", return_value=fixed_now),
                    patch.object(
                        financial_brief,
                        "collect_israeli_market_data",
                        return_value=data,
                    ) as collect,
                    patch.object(financial_brief, "validate_market_data"),
                    patch.object(
                        financial_brief,
                        "generate_hebrew_brief",
                        return_value=brief,
                    ) as generate,
                    patch.object(
                        financial_brief,
                        "review_and_correct_brief",
                        return_value=(brief, qa),
                    ) as review,
                    patch.object(financial_brief, "send_email") as send,
                ):
                    prepared = financial_brief.run(
                        send=False, persist_report=True
                    )
                    delivered = financial_brief.run(
                        send=True, approved_only=True
                    )

                self.assertEqual(prepared["sent"], 0)
                self.assertEqual(delivered["sent"], 1)
                self.assertTrue(delivered["reused_brief"])
                collect.assert_called_once_with(include_news=True)
                generate.assert_called_once()
                review.assert_called_once()
                send.assert_called_once()
                with financial_brief.database.get_connection() as conn:
                    row = conn.execute(
                        "SELECT qa_status, prepared_at, metadata_json FROM briefing_runs"
                    ).fetchone()
                self.assertEqual(row["qa_status"], "approved")
                self.assertIsNotNone(row["prepared_at"])
                self.assertIn('"status": "approved"', row["metadata_json"])
            finally:
                financial_brief.database.DB_PATH = original_db_path

    def test_approved_only_refuses_legacy_unreviewed_report(self):
        fixed_now = datetime(
            2026, 8, 31, 8, 0, tzinfo=financial_brief.ISRAEL_TZ
        )
        with TemporaryDirectory() as directory:
            original_db_path = financial_brief.database.DB_PATH
            financial_brief.database.DB_PATH = str(Path(directory) / "brief.db")
            try:
                financial_brief.database.init_db()
                financial_brief.database.save_briefing_run(
                    "2026-08-31", "2026-08-28", fixed_now.isoformat(), "legacy"
                )
                with (
                    patch.object(financial_brief, "OWNER_EMAIL", None),
                    patch.object(financial_brief, "GMAIL_USER", "sender@example.com"),
                    patch.object(financial_brief, "GMAIL_APP_PASSWORD", "secret"),
                    patch.object(financial_brief, "_today", return_value=fixed_now),
                ):
                    with self.assertRaisesRegex(RuntimeError, "not QA-approved"):
                        financial_brief.run(send=True, approved_only=True)
            finally:
                financial_brief.database.DB_PATH = original_db_path

    def test_quality_corrections_cannot_change_report_numbers(self):
        brief = "המדד עלה ב-2.50% השבוע. המשפט הזה אינו ברור."
        corrected, applied = financial_brief._apply_quality_replacements(
            brief,
            [
                {
                    "old": "המדד עלה ב-2.50% השבוע.",
                    "new": "המדד עלה ב-3.50% השבוע.",
                    "reason": "unsafe numeric change",
                },
                {
                    "old": "המשפט הזה אינו ברור.",
                    "new": "המשפט נוסח מחדש בצורה ברורה.",
                    "reason": "clarity",
                },
            ],
        )

        self.assertIn("2.50%", corrected)
        self.assertNotIn("3.50%", corrected)
        self.assertIn("נוסח מחדש", corrected)
        self.assertEqual(len(applied), 1)

    def test_quality_review_retries_an_unsafe_correction_with_exact_text(self):
        brief = "### בדיקה\nהמשפט הזה אינו ברור לקורא."
        first_review = json.dumps({
            "status": "revise",
            "summary": "נדרש ניסוח ברור",
            "replacements": [{
                "old": "משפט שאינו מופיע בדוח.",
                "new": "ניסוח אחר.",
                "reason": "בהירות",
            }],
        }, ensure_ascii=False)
        repair_review = json.dumps({
            "status": "revise",
            "summary": "תיקון מדויק",
            "replacements": [{
                "old": "המשפט הזה אינו ברור לקורא.",
                "new": "המשפט נוסח מחדש באופן ברור לקורא.",
                "reason": "בהירות",
            }],
        }, ensure_ascii=False)

        with (
            patch.object(financial_brief, "GROQ_API_KEY", "test-key"),
            patch.object(financial_brief, "Groq"),
            patch.object(
                financial_brief,
                "_deterministic_quality_issues",
                return_value=[],
            ),
            patch.object(
                financial_brief,
                "_groq_completion",
                side_effect=(first_review, repair_review),
            ) as completion,
        ):
            corrected, qa = financial_brief.review_and_correct_brief(
                brief, "daily", {}
            )

        self.assertIn("נוסח מחדש באופן ברור", corrected)
        self.assertEqual(qa["status"], "approved")
        self.assertEqual(qa["corrections_applied"], 1)
        self.assertEqual(completion.call_count, 2)

    def test_missing_approved_report_alerts_only_the_owner(self):
        with (
            patch.object(financial_brief, "OWNER_EMAIL", "owner@example.com"),
            patch.object(financial_brief, "GMAIL_USER", "sender@example.com"),
            patch.object(financial_brief, "GMAIL_APP_PASSWORD", "secret"),
            patch.object(financial_brief, "send_email") as send,
        ):
            financial_brief.send_preparation_failure_alert(
                RuntimeError("No approved report")
            )

        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], "owner@example.com")
        self.assertIn("לא נשלח", send.call_args.args[1])

    def test_process_lock_rejects_overlapping_delivery(self):
        with TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"FINANCIAL_BRIEF_LOCK_PATH": str(Path(directory) / "brief.lock")},
        ):
            with financial_brief._delivery_process_lock():
                with self.assertRaisesRegex(
                    RuntimeError, "already running"
                ):
                    with financial_brief._delivery_process_lock():
                        pass


class DeploymentScheduleTests(unittest.TestCase):
    def test_morning_pipeline_has_bounded_separate_recovery_before_delivery(self):
        deploy_dir = Path(__file__).resolve().parent / "deploy"
        primary_timer = (deploy_dir / "financial-brief.timer").read_text()
        primary_service = (deploy_dir / "financial-brief.service").read_text()
        retry_timer = (deploy_dir / "financial-brief-retry.timer").read_text()
        retry_service = (deploy_dir / "financial-brief-retry.service").read_text()
        send_timer = (deploy_dir / "financial-brief-send.timer").read_text()
        send_service = (deploy_dir / "financial-brief-send.service").read_text()

        self.assertIn("07:45:00 Asia/Jerusalem", primary_timer)
        self.assertNotIn("07:55:00", primary_timer)
        self.assertIn("TimeoutStartSec=9min", primary_service)
        self.assertIn("07:55:00 Asia/Jerusalem", retry_timer)
        self.assertIn("TimeoutStartSec=4min", retry_service)
        self.assertIn("--prepare", retry_service)
        self.assertIn("08:00:00 Asia/Jerusalem", send_timer)
        self.assertIn("financial-brief-retry.service", send_service)
        self.assertIn("--send-prepared", send_service)


if __name__ == "__main__":
    unittest.main()
