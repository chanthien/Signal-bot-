import json
import unittest
from unittest.mock import AsyncMock, patch

import main


class TestHealthEndpoints(unittest.IsolatedAsyncioTestCase):
    async def test_live_endpoint(self):
        response = await main.live()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body.decode()), {"status": "alive"})

    async def test_health_degraded_when_balance_fails(self):
        with patch.object(main.gateway, "get_balance", AsyncMock(side_effect=RuntimeError("boom"))):
            response = await main.health()
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode())
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["balance"], 0.0)
        self.assertIn("assets", payload)


if __name__ == "__main__":
    unittest.main()
