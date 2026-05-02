import unittest
from unittest.mock import Mock, patch

import financial_brief


class FinancialBriefDataTests(unittest.TestCase):
    def test_build_currencies_commodities_keeps_required_rates(self):
        market = {
            "USD/ILS": {"price": 3.72, "change_pct": 0.2, "arrow": "▲"},
            "EUR/ILS": {"price": 4.01, "change_pct": -0.1, "arrow": "▼"},
            "Bitcoin": {"price": 64000, "change_pct": 1.5, "arrow": "▲"},
            "Ethereum": {"price": 3200, "change_pct": -0.4, "arrow": "▼"},
            "SPY (S&P 500)": {"price": 520, "change_pct": 0.3, "arrow": "▲"},
        }

        result = financial_brief.build_currencies_commodities(market)

        self.assertEqual(
            list(result),
            ["USD/ILS", "EUR/ILS", "Bitcoin", "Ethereum"],
        )
        self.assertEqual(result["EUR/ILS"]["price"], 4.01)
        self.assertNotIn("SPY (S&P 500)", result)

    def test_filter_sp500_quotes_excludes_non_members(self):
        quotes = [
            {
                "symbol": "TSLA",
                "shortName": "Tesla, Inc.",
                "regularMarketPrice": 230.125,
                "regularMarketChangePercent": 4.567,
            },
            {
                "symbol": "XYZ",
                "shortName": "Not In Index",
                "regularMarketPrice": 10,
                "regularMarketChangePercent": 20,
            },
            {
                "symbol": "BRK.B",
                "shortName": "Berkshire Hathaway Inc.",
                "regularMarketPrice": 410.444,
                "regularMarketChangePercent": -1.234,
            },
        ]

        result = financial_brief.filter_sp500_quotes(quotes, {"TSLA", "BRK-B"})

        self.assertEqual([q["symbol"] for q in result], ["TSLA", "BRK-B"])
        self.assertEqual(result[0]["name"], "Tesla, Inc.")
        self.assertEqual(result[0]["price"], 230.12)
        self.assertEqual(result[1]["change_pct"], -1.23)

    @patch("financial_brief.requests.get")
    @patch("financial_brief.get_sp500_symbols", return_value=set())
    def test_get_top_movers_skips_section_when_sp500_membership_unavailable(
        self, mock_symbols, mock_get
    ):
        result = financial_brief.get_top_movers()

        self.assertEqual(result, {"gainers": [], "losers": []})
        mock_get.assert_not_called()
        mock_symbols.assert_called_once()

    @patch("financial_brief.requests.get")
    @patch("financial_brief.get_sp500_symbols", return_value={"AAPL"})
    def test_get_top_movers_returns_only_sp500_gainers_and_losers(
        self, mock_symbols, mock_get
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "finance": {
                "result": [
                    {
                        "quotes": [
                            {
                                "symbol": "AAPL",
                                "shortName": "Apple Inc.",
                                "regularMarketPrice": 188.123,
                                "regularMarketChangePercent": 2.345,
                            },
                            {
                                "symbol": "SMALL",
                                "shortName": "Small Cap",
                                "regularMarketPrice": 1,
                                "regularMarketChangePercent": 50,
                            },
                        ]
                    }
                ]
            }
        }
        mock_get.return_value = response

        result = financial_brief.get_top_movers()

        self.assertEqual(result["gainers"][0]["symbol"], "AAPL")
        self.assertEqual(result["losers"][0]["symbol"], "AAPL")
        self.assertEqual(mock_get.call_count, 2)
        mock_symbols.assert_called_once()


if __name__ == "__main__":
    unittest.main()
