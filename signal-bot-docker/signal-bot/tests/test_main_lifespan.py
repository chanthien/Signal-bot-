import unittest
from unittest.mock import patch

import main


class TestLifespanValidation(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_raises_when_missing_settings(self):
        with patch("main.validate_runtime_settings", return_value=["TELEGRAM_TOKEN"]), \
             patch.object(main.log, "error"):
            with self.assertRaises(RuntimeError):
                async with main.lifespan(main.app):
                    pass


if __name__ == "__main__":
    unittest.main()
