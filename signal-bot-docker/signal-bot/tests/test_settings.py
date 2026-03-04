import unittest

import config.settings as settings


class TestValidateRuntimeSettings(unittest.TestCase):
    def test_validate_runtime_settings_returns_missing_keys(self):
        original = {
            "TELEGRAM_TOKEN": settings.TELEGRAM_TOKEN,
            "CHANNEL_ID": settings.CHANNEL_ID,
            "MY_CHAT_ID": settings.MY_CHAT_ID,
            "BINGX_API_KEY": settings.BINGX_API_KEY,
            "BINGX_API_SECRET": settings.BINGX_API_SECRET,
        }
        try:
            settings.TELEGRAM_TOKEN = ""
            settings.CHANNEL_ID = ""
            settings.MY_CHAT_ID = ""
            settings.BINGX_API_KEY = ""
            settings.BINGX_API_SECRET = ""

            missing = settings.validate_runtime_settings()
            self.assertEqual(
                missing,
                [
                    "TELEGRAM_TOKEN",
                    "CHANNEL_ID",
                    "MY_CHAT_ID",
                    "BINGX_API_KEY",
                    "BINGX_API_SECRET",
                ],
            )
        finally:
            settings.TELEGRAM_TOKEN = original["TELEGRAM_TOKEN"]
            settings.CHANNEL_ID = original["CHANNEL_ID"]
            settings.MY_CHAT_ID = original["MY_CHAT_ID"]
            settings.BINGX_API_KEY = original["BINGX_API_KEY"]
            settings.BINGX_API_SECRET = original["BINGX_API_SECRET"]

    def test_validate_runtime_settings_ok_when_all_present(self):
        original = {
            "TELEGRAM_TOKEN": settings.TELEGRAM_TOKEN,
            "CHANNEL_ID": settings.CHANNEL_ID,
            "MY_CHAT_ID": settings.MY_CHAT_ID,
            "BINGX_API_KEY": settings.BINGX_API_KEY,
            "BINGX_API_SECRET": settings.BINGX_API_SECRET,
        }
        try:
            settings.TELEGRAM_TOKEN = "token"
            settings.CHANNEL_ID = "-100123"
            settings.MY_CHAT_ID = "123"
            settings.BINGX_API_KEY = "key"
            settings.BINGX_API_SECRET = "secret"

            missing = settings.validate_runtime_settings()
            self.assertEqual(missing, [])
        finally:
            settings.TELEGRAM_TOKEN = original["TELEGRAM_TOKEN"]
            settings.CHANNEL_ID = original["CHANNEL_ID"]
            settings.MY_CHAT_ID = original["MY_CHAT_ID"]
            settings.BINGX_API_KEY = original["BINGX_API_KEY"]
            settings.BINGX_API_SECRET = original["BINGX_API_SECRET"]


if __name__ == "__main__":
    unittest.main()
