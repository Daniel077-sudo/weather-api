import unittest
from unittest.mock import AsyncMock, patch

from gemini_service import generate_general_chat_reply, normalize_assistant_name
from timing_service import get_timing, reset_timing, start_timing


def run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("mocked coroutine unexpectedly suspended")


class GeneralChatReplyTests(unittest.TestCase):
    def test_normalize_assistant_name_defaults_and_limits_length(self):
        self.assertEqual(normalize_assistant_name(None), "小藍")
        self.assertEqual(normalize_assistant_name("  晴\n晴  "), "晴 晴")
        self.assertEqual(len(normalize_assistant_name("很長的助理名字" * 10)), 24)

    def test_non_greeting_reply_does_not_force_self_introduction(self):
        with patch("gemini_service.call_gemini_raw", new=AsyncMock(return_value="今天辛苦了，可以先休息一下。")):
            reply = run_immediate(generate_general_chat_reply("我今天好累", "晴晴", []))
        self.assertEqual(reply, "今天辛苦了，可以先休息一下。")

    def test_gemini_error_uses_named_fallback(self):
        token = start_timing()
        try:
            with patch("gemini_service.call_gemini_raw", new=AsyncMock(return_value="[Gemini HTTP error]: status=503")):
                reply = run_immediate(generate_general_chat_reply("謝謝你", "晴晴", []))
            self.assertEqual(reply, "不客氣，我是晴晴。有需要再告訴我。")
            self.assertEqual(get_timing()["gemini_status"], "error")
        finally:
            reset_timing(token)

    def test_timeout_is_exposed_separately(self):
        token = start_timing()
        try:
            with patch("gemini_service.call_gemini_raw", new=AsyncMock(return_value="[Gemini request error]: ReadTimeout")):
                run_immediate(generate_general_chat_reply("陪我聊聊", "晴晴", []))
            self.assertEqual(get_timing()["gemini_status"], "timeout")
            self.assertIn("ReadTimeout", get_timing()["gemini_error"])
        finally:
            reset_timing(token)

    def test_general_chat_cannot_claim_an_action_was_completed(self):
        with patch(
            "gemini_service.call_gemini_raw",
            new=AsyncMock(return_value="已經幫你設定好提醒了。"),
        ):
            reply = run_immediate(generate_general_chat_reply("提醒我準備考試", "晴晴", []))

        self.assertIn("我沒聽懂", reply)
        self.assertNotIn("設定好提醒", reply)

    def test_general_chat_cannot_claim_a_reminder_was_prepared(self):
        with patch(
            "gemini_service.call_gemini_raw",
            new=AsyncMock(return_value="小藍幫你準備好了『明天去辦護照』的提醒囉。"),
        ):
            reply = run_immediate(generate_general_chat_reply("明天辦護照", "小藍", []))

        self.assertIn("我沒聽懂", reply)
        self.assertNotIn("準備好了", reply)


if __name__ == "__main__":
    unittest.main()
