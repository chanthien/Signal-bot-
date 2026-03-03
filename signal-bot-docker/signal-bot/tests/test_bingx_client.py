import unittest
from unittest.mock import patch

from exchange.bingx import BingXClient


class TestBingXClientHelpers(unittest.TestCase):
    def test_signed_params_does_not_mutate_input(self):
        client = BingXClient()
        params = {"symbol": "BTC-USDT", "quantity": 1.23}
        with patch("exchange.bingx.time.time", return_value=1700000000):
            signed = client._signed_params(params)

        self.assertEqual(params, {"symbol": "BTC-USDT", "quantity": 1.23})
        self.assertIn("timestamp", signed)
        self.assertIn("signature", signed)
        self.assertEqual(signed["symbol"], "BTC-USDT")
        self.assertEqual(signed["quantity"], "1.23")

    def test_to_dataframe_supports_ns_timestamp(self):
        client = BingXClient()
        candles = [
            [1700000000000000000, "1", "2", "0.5", "1.5", "100", 1700000001000000000],
            [1700000001000000000, "1.5", "2.2", "1.2", "2.0", "120", 1700000002000000000],
        ]
        df = client._to_dataframe(candles)
        self.assertEqual(len(df), 2)
        self.assertIn("open_time", df.columns)
        self.assertAlmostEqual(df.iloc[-1]["close"], 2.0)


if __name__ == "__main__":
    unittest.main()
