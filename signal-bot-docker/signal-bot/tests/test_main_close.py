import json
import unittest
from unittest.mock import AsyncMock, patch

import main


class DummyState:
    def __init__(self):
        self.direction = "LONG"
        self.layers = 1
        self.trail_active = False
        self.peak_price = 0
        self.avg_entry = 100.0

    def reset(self):
        self.direction = ""
        self.layers = 0


class DummyStrategy:
    def __init__(self):
        self.state = DummyState()


class TestCloseSymbol(unittest.IsolatedAsyncioTestCase):
    async def test_close_uses_pre_reset_direction(self):
        symbol = "BTC-USDT"
        old_strategies = main.engine.strategies
        main.engine.strategies = {symbol: DummyStrategy()}
        try:
            with patch.object(main.gateway, "fetch_ticker", AsyncMock(return_value=105.0)), \
                 patch.object(main.engine, "_estimate_pnl_pct", return_value=5.0), \
                 patch.object(main.gateway, "close_all_positions", AsyncMock(return_value=True)), \
                 patch.object(main.gateway, "cancel_all_orders", AsyncMock(return_value=True)), \
                 patch.object(main.notifier, "send_close", AsyncMock()) as send_close, \
                 patch("main.EXECUTION_ENABLED", True):
                response = await main.close_symbol(symbol)

                self.assertEqual(response.status_code, 200)
                payload = json.loads(response.body.decode())
                self.assertEqual(payload["closed"], True)
                self.assertEqual(payload["estimated_pnl"], 5.0)

                args = send_close.await_args.args
                self.assertEqual(args[1], "LONG")
        finally:
            main.engine.strategies = old_strategies


if __name__ == "__main__":
    unittest.main()
