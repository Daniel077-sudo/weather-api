import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import gemini_service


def run_immediate(coroutine):
    """Drive coroutines whose awaited dependencies are fully mocked and immediate."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("mocked coroutine unexpectedly suspended")


class GeminiReliabilityTests(unittest.TestCase):
    def test_text_call_retries_transient_503_once_and_uses_low_temperature(self):
        unavailable = MagicMock(status_code=503, headers={})
        success = MagicMock(status_code=200, headers={})
        success.raise_for_status.return_value = None
        success.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "完成"}]}}]
        }
        client = MagicMock()
        client.post = AsyncMock(side_effect=[unavailable, success])
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)

        with patch.object(gemini_service, "GEMINI_API_KEY", "test-key"), patch.object(
            gemini_service.httpx, "AsyncClient", return_value=context
        ), patch.object(gemini_service.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = run_immediate(gemini_service.call_gemini_raw("hello"))

        self.assertEqual(result, "完成")
        self.assertEqual(client.post.await_count, 2)
        sleep.assert_awaited_once()
        payload = client.post.await_args_list[0].kwargs["json"]
        self.assertEqual(payload["generationConfig"]["temperature"], 0.2)

    def test_json_call_sends_schema_and_returns_valid_object(self):
        fallback = {"intent": "GENERAL_CHAT", "reply": "", "missing_fields": []}
        model_output = {"intent": "GENERAL_CHAT", "reply": "你好", "missing_fields": []}
        mocked = AsyncMock(return_value=json.dumps(model_output, ensure_ascii=False))

        with patch.object(gemini_service, "call_gemini_raw", new=mocked):
            result = run_immediate(gemini_service.call_gemini_json("prompt", fallback))

        self.assertEqual(result, model_output)
        kwargs = mocked.await_args.kwargs
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["response_schema"]["type"], "OBJECT")
        self.assertIn("intent", kwargs["response_schema"]["required"])

    def test_invalid_json_is_retried_once_then_falls_back(self):
        fallback = {"intent": "GENERAL_CHAT", "reply": "請再說一次", "missing_fields": []}
        mocked = AsyncMock(side_effect=["not-json", "still-not-json"])

        with patch.object(gemini_service, "call_gemini_raw", new=mocked):
            result = run_immediate(gemini_service.call_gemini_json("prompt", fallback))

        self.assertEqual(result, fallback)
        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(mocked.await_args_list[1].kwargs["max_retries"], 0)


if __name__ == "__main__":
    unittest.main()
