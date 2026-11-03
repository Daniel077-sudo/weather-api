import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from chat_contract_v2 import build_chat_v2_response


ROOT = Path(__file__).resolve().parents[1]
SUITE = json.loads((ROOT / "docs" / "intent_test_cases.json").read_text(encoding="utf-8"))


def partial_diff(expected, actual, path=""):
    errors = []
    for key, wanted in expected.items():
        if key.startswith("_"):
            continue
        if key == "missing_fields_include":
            missing = [item for item in wanted if item not in (actual.get("missing_fields") or [])]
            if missing:
                errors.append(f"missing_fields lacks {missing}")
            continue
        got = actual.get(key)
        here = f"{path}.{key}" if path else key
        if isinstance(wanted, dict):
            if not isinstance(got, dict):
                errors.append(f"{here} is not an object")
            else:
                errors.extend(partial_diff(wanted, got, here))
        elif isinstance(wanted, str) and isinstance(got, str):
            if wanted.replace("台", "臺") != got.replace("台", "臺"):
                errors.append(f"{here}: expected {wanted!r}, got {got!r}")
        elif wanted != got:
            errors.append(f"{here}: expected {wanted!r}, got {got!r}")
    return errors


class ChatContractV2Tests(unittest.TestCase):
    def payload_for(self, case):
        return {
            "user_id": "test-user",
            "contract_version": 2,
            "message": case["input"],
            "client_now": SUITE["client_now"],
            "current_location": SUITE["current_location"],
            "intent_hint": case.get("intent_hint"),
            "draft_id": "draft-test",
            "draft_event": case.get("draft_event"),
        }

    def test_all_golden_intent_cases(self):
        failures = []
        for case in SUITE["cases"]:
            result = build_chat_v2_response(self.payload_for(case))
            errors = partial_diff(case["expect"], result)
            if not result.get("reply"):
                errors.append("reply is empty")
            if bool(result.get("needs_clarification")) != bool(result.get("missing_fields")):
                errors.append("clarification fields are inconsistent")
            if errors:
                failures.append(f"{case['id']}: {errors}")
        self.assertFalse(failures, "\n".join(failures))

    def test_v2_endpoint_bypasses_legacy_mutating_flow(self):
        client = TestClient(main.app)
        with patch.object(main, "persist_chat_turn", return_value=None):
            response = client.post("/api/chat", json=self.payload_for(SUITE["cases"][0]))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["intent"], "CREATE_EVENT")
        self.assertIsNone(body["event_id"])
        self.assertIsNone(body["event_created"])

    def test_p0_draft_mode_does_not_call_legacy_create_flow(self):
        client = TestClient(main.app)
        payload = self.payload_for(SUITE["cases"][0])
        payload.pop("contract_version")
        payload["draft_mode"] = True
        with patch.object(main, "persist_chat_turn", return_value=None):
            response = client.post("/api/chat", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["action_type"], "CREATE_EVENT")
        self.assertIsNone(body["event_id"])
        self.assertIsNone(body["event_created"])
        self.assertIn("確認後", body["reply"])

    def test_v2_requires_client_now_instead_of_using_server_clock(self):
        client = TestClient(main.app)
        payload = self.payload_for(SUITE["cases"][0])
        payload.pop("client_now")
        response = client.post("/api/chat", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["contract_version"], 2)
        self.assertNotIn("client_now", body["reply"])

    def test_v2_keeps_original_location_and_handles_unlisted_activity(self):
        payload = self.payload_for({"input": "明天下午三點台南看電影"})
        result = build_chat_v2_response(payload)
        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertEqual(result["draft_event"]["title"], "看電影")
        self.assertEqual(result["draft_event"]["city"], "臺南市")
        self.assertEqual(result["draft_event"]["location"], "台南")

    def test_typhoon_knowledge_explains_warning_thresholds(self):
        payload = self.payload_for({"input": "颱風警報怎麼分？"})
        result = build_chat_v2_response(payload)
        self.assertEqual(result["intent"], "DISASTER_INFO")
        self.assertIn("24 小時", result["reply"])
        self.assertIn("18 小時", result["reply"])
        self.assertIn("七級風暴風", result["reply"])

    def test_p0_delete_only_reads_target_and_never_calls_legacy_flow(self):
        client = TestClient(main.app)
        payload = {
            "user_id": "test-user",
            "message": "刪掉明天的開會",
            "draft_mode": True,
            "client_now": SUITE["client_now"],
        }
        matched = {"id": 123, "title": "開會", "start_time": "2026-09-24T15:00:00+08:00"}
        with patch.object(main, "find_event_for_draft", return_value=matched), patch.object(
            main, "persist_chat_turn", return_value=None
        ):
            response = client.post("/api/chat", json=payload)
        body = response.json()
        self.assertEqual(body["action_type"], "DELETE_EVENT")
        self.assertEqual(body["event_id_to_delete"], "123")
        self.assertIn("確認後", body["reply"])

    def test_p0_update_returns_requested_change_without_writing(self):
        client = TestClient(main.app)
        payload = {
            "user_id": "test-user",
            "message": "把明天開會改成下午四點",
            "draft_mode": True,
            "client_now": SUITE["client_now"],
        }
        matched = {"id": 456, "title": "開會", "start_time": "2026-09-24T15:00:00+08:00"}
        with patch.object(main, "find_event_for_draft", return_value=matched), patch.object(
            main, "persist_chat_turn", return_value=None
        ):
            response = client.post("/api/chat", json=payload)
        body = response.json()
        self.assertEqual(body["action_type"], "UPDATE_EVENT")
        self.assertEqual(body["event_id"], "456")
        self.assertIn("16:00:00+08:00", body["event_start"])
        self.assertIn("確認後", body["reply"])

    def test_unversioned_chat_contract_is_retired(self):
        client = TestClient(main.app)
        response = client.post("/api/chat", json={"user_id": "test-user", "message": "明天下午三點去台南開會"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["action_type"], "NONE")
        self.assertIsNone(body["event_id"])
        self.assertIn("contract_version: 2", body["reply"])

    def test_general_chat_uses_gemini_and_custom_assistant_name(self):
        client = TestClient(main.app)
        payload = {
            "user_id": "test-user",
            "message": "我今天心情有點累",
            "assistant_name": "晴晴",
            "contract_version": 2,
            "client_now": SUITE["client_now"],
        }
        with patch.object(main, "get_chat_history", return_value={"status": "success", "data": []}), patch.object(
            main,
            "generate_general_chat_reply",
            new=AsyncMock(return_value="我是晴晴。辛苦了，今天可以先讓自己喘口氣。"),
        ) as generate_reply, patch.object(main, "persist_chat_turn", return_value=None):
            response = client.post("/api/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["intent"], "GENERAL_CHAT")
        self.assertEqual(body["assistant_name"], "晴晴")
        self.assertIn("晴晴", body["reply"])
        generate_reply.assert_awaited_once()

    def test_cancel_general_chat_stays_deterministic_without_gemini(self):
        client = TestClient(main.app)
        payload = {
            "user_id": "test-user",
            "message": "不用了",
            "assistant_name": "阿晴",
            "contract_version": 2,
            "client_now": SUITE["client_now"],
        }
        with patch.object(main, "generate_general_chat_reply", new=AsyncMock()) as generate_reply, patch.object(
            main, "persist_chat_turn", return_value=None
        ):
            response = client.post("/api/chat", json=payload)

        body = response.json()
        self.assertEqual(body["reply"], "好的，這次不建立草稿。")
        self.assertEqual(body["assistant_name"], "阿晴")
        generate_reply.assert_not_awaited()

    def test_date_only_follow_up_completes_existing_create_draft(self):
        first = self.payload_for({"input": "我下午兩點要去台北吃冰"})
        first_result = build_chat_v2_response(first)
        self.assertEqual(first_result["intent"], "CREATE_EVENT")
        self.assertEqual(first_result["draft_event"]["date"], "2026-09-23")
        self.assertTrue(first_result["draft_id"].startswith("draft-"))
        self.assertEqual(first_result["draft_event"]["title"], "吃冰")

        follow_up = self.payload_for({"input": "10/4"})
        follow_up.update({
            "intent_hint": "CREATE_EVENT",
            "draft_id": first_result["draft_id"],
            "draft_event": first_result["draft_event"],
        })
        second_result = build_chat_v2_response(follow_up)
        self.assertEqual(second_result["intent"], "CREATE_EVENT")
        self.assertEqual(second_result["draft_id"], first_result["draft_id"])
        self.assertEqual(second_result["draft_event"]["date"], "2026-10-04")
        self.assertEqual(second_result["draft_event"]["title"], "吃冰")
        self.assertFalse(second_result["needs_clarification"])

    def test_create_without_time_becomes_all_day_and_defaults_to_today(self):
        payload = self.payload_for({"input": "幫我新增台南辦護照的行程"})
        result = build_chat_v2_response(payload)

        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["draft_event"]["date"], "2026-09-23")
        self.assertTrue(result["draft_event"]["all_day"])
        self.assertIsNone(result["draft_event"]["start_time"])
        self.assertIsNone(result["draft_event"]["end_time"])

    def test_ambiguous_clock_time_still_requires_clarification(self):
        payload = self.payload_for({"input": "明天三點去台南辦護照"})
        result = build_chat_v2_response(payload)

        self.assertTrue(result["needs_clarification"])
        self.assertIn("start_time", result["missing_fields"])
        self.assertFalse(result["draft_event"]["all_day"])
        self.assertIsNone(result["draft_event"]["start_time"])

    def test_period_without_clock_time_still_requires_clarification(self):
        payload = self.payload_for({"input": "明天晚上去台南辦護照"})
        result = build_chat_v2_response(payload)

        self.assertTrue(result["needs_clarification"])
        self.assertIn("start_time", result["missing_fields"])
        self.assertFalse(result["draft_event"]["all_day"])

    def test_remind_me_creates_location_optional_event_draft(self):
        payload = self.payload_for({"input": "下午一點提醒我準備期中考"})
        result = build_chat_v2_response(payload)

        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["draft_event"]["title"], "準備期中考")
        self.assertEqual(result["draft_event"]["start_time"], "13:00")
        self.assertTrue(result["draft_event"]["is_reminder"])
        self.assertIsNone(result["draft_event"]["location"])

    def test_named_landmark_satisfies_location_without_city(self):
        payload = self.payload_for({"input": "明天下午三點開會，地點在新光三越"})
        result = build_chat_v2_response(payload)

        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertNotIn("city", result["missing_fields"])
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["draft_event"]["location"], "新光三越")


if __name__ == "__main__":
    unittest.main()
