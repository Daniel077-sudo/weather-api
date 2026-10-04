import unittest
from unittest.mock import AsyncMock, patch

from gemini_service import generate_general_chat_reply, normalize_assistant_name
from timing_service import get_timing, reset_timing, start_timing


class GeneralChatReplyTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_assistant_name_defaults_and_limits_length(self):
        self.assertEqual(normalize_assistant_name(None), "小藍")
        self.assertEqual(normalize_assistant_name("  晴\n晴  "), "晴 晴")
        self.assertEqual(len(normalize_assistant_name("很長的助理名字" * 10)), 24)

    async def test_reply_mentions_name_when_model_omits_it(self):
        with patch("gemini_service.call_gemini_raw", new=AsyncMock(return_value="今天辛苦了，可以先休息一下。")):
            reply = await generate_general_chat_reply("我今天好累", "晴晴", [])
        self.assertEqual(reply, "我是晴晴。今天辛苦了，可以先休息一下。")

    async def test_gemini_error_uses_named_fallback(self):
        token = start_timing()
        try:
            with patch("gemini_service.call_gemini_raw", new=AsyncMock(return_value="[Gemini HTTP error]: status=503")):
                reply = await generate_general_chat_reply("謝謝你", "晴晴", [])
            self.assertEqual(reply, "不客氣，我是晴晴。有需要再告訴我。")
            self.assertEqual(get_timing()["gemini_status"], "error")
        finally:
            reset_timing(token)

    async def test_timeout_is_exposed_separately(self):
        token = start_timing()
        try:
            with patch("gemini_service.call_gemini_raw", new=AsyncMock(return_value="[Gemini request error]: ReadTimeout")):
                await generate_general_chat_reply("陪我聊聊", "晴晴", [])
            self.assertEqual(get_timing()["gemini_status"], "timeout")
        finally:
            reset_timing(token)


if __name__ == "__main__":
    unittest.main()
