import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from chat_contract_v2 import AMBIGUOUS_DRAFT_TIMES, build_chat_v2_response, extract_location_entities, validate_chat_v2_response


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
        if key == "intent_in":
            if actual.get("intent") not in wanted:
                errors.append(f"intent {actual.get('intent')!r} not in {wanted!r}")
            continue
        if key == "draft_event_absent":
            draft = actual.get("draft_event") or {}
            filled = {field: draft.get(field) for field in wanted if draft.get(field) not in (None, "")}
            if filled:
                errors.append(f"draft_event unexpectedly filled {filled}")
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
    def setUp(self):
        main.app.dependency_overrides[main.get_required_auth_context] = lambda: main.AuthContext(
            user_id="test-user",
            claims={"sub": "test-user"},
            authenticated=True,
        )

    def tearDown(self):
        main.app.dependency_overrides.pop(main.get_required_auth_context, None)

    def payload_for(self, case):
        return {
            "user_id": "test-user",
            "contract_version": 2,
            "message": case["input"],
            "client_now": case.get("client_now", SUITE["client_now"]),
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
            if not result.get("reply") and result.get("intent") not in {"QUERY_EVENT", "QUERY_WEATHER", "EVENT_WEATHER"}:
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
        self.assertNotIn("event_id", body)
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
        async def successful_reply(*_args, **_kwargs):
            main.set_timing("gemini_status", "ok")
            return "我是晴晴。辛苦了，今天可以先讓自己喘口氣。"

        with patch.object(main, "get_chat_history", return_value={"status": "success", "data": []}), patch.object(
            main,
            "generate_general_chat_reply",
            new=AsyncMock(side_effect=successful_reply),
        ) as generate_reply, patch.object(main, "persist_chat_turn", return_value=None):
            response = client.post("/api/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["intent"], "GENERAL_CHAT")
        self.assertEqual(body["assistant_name"], "晴晴")
        self.assertIn("晴晴", body["reply"])
        self.assertFalse(body["ai_failed"])
        generate_reply.assert_awaited_once()

    def test_general_chat_ai_failure_is_explicit_and_has_no_fake_reply(self):
        body = main.apply_general_chat_result(
            {"intent": "GENERAL_CHAT", "reply": "原始回覆"},
            "我是小藍。AI 暫時忙不過來。",
            "timeout",
        )
        self.assertEqual(body["gemini_status"], "timeout")
        self.assertTrue(body["ai_failed"])
        self.assertEqual(body["reply"], "AI 暫時忙不過來，請稍後再試。")

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

    def test_ambiguous_clock_time_requires_period(self):
        payload = self.payload_for({"input": "明天三點去台南辦護照"})
        result = build_chat_v2_response(payload)

        self.assertTrue(result["needs_clarification"])
        self.assertIn("start_time", result["missing_fields"])
        self.assertIsNone(result["draft_event"]["start_time"])
        self.assertFalse(result["draft_event"]["all_day"])
        self.assertEqual(result["reply"], "是上午還是下午？")

    def test_period_only_follow_up_resolves_remembered_ambiguous_clock(self):
        first = build_chat_v2_response({
            "message": "明天三點開會", "contract_version": 2,
            "client_now": "2026-11-23T10:00:00+08:00",
        })
        second = build_chat_v2_response({
            "message": "下午", "contract_version": 2,
            "client_now": "2026-11-23T10:01:00+08:00",
            "intent_hint": "CREATE_EVENT", "draft_id": first["draft_id"],
            "draft_event": first["draft_event"],
        })

        self.assertEqual(second["intent"], "CREATE_EVENT")
        self.assertEqual(second["draft_event"]["title"], "開會")
        self.assertEqual(second["draft_event"]["start_time"], "15:00")
        self.assertNotIn("start_time", second["missing_fields"])

        AMBIGUOUS_DRAFT_TIMES.clear()
        restored = build_chat_v2_response({
            "message": "下午", "contract_version": 2,
            "client_now": "2026-11-23T10:02:00+08:00",
            "intent_hint": "CREATE_EVENT", "draft_id": first["draft_id"],
            "draft_event": first["draft_event"], "_ambiguous_times": ["03:00"],
        })
        self.assertEqual(restored["draft_event"]["start_time"], "15:00")

    def test_compound_day_period_words_set_both_date_and_clock_period(self):
        result = build_chat_v2_response({
            "message": "明晚七點開會", "contract_version": 2,
            "client_now": "2026-11-23T10:00:00+08:00",
        })
        self.assertEqual(result["draft_event"]["date"], "2026-11-24")
        self.assertEqual(result["draft_event"]["start_time"], "19:00")

    def test_complete_station_names_are_preserved(self):
        cases = {
            "明天下午3點臺南火車站開會": "臺南火車站",
            "明天09:05到高鐵臺中站搭車": "高鐵臺中站",
            "後天晚上七點在捷運西門站集合": "捷運西門站",
            "12/15下午三點板橋站搭車": "板橋站",
            "明天在臺北車站開會": "臺北車站",
            "明天到新左營站搭車": "新左營站",
            "明天到臺北101/世貿站集合": "臺北101/世貿站",
            "明天到高鐵左營站搭車": "高鐵左營站",
            "明天到左營高鐵站搭車": "左營高鐵站",
            "明天在捷運西門站集合": "捷運西門站",
            "明天在西門捷運站集合": "西門捷運站",
        }
        for message, station in cases.items():
            with self.subTest(message=message):
                self.assertEqual(extract_location_entities(message)["location"], station)
        self.assertIsNone(extract_location_entities("明天在網站開會")["location"])

    def test_event_operation_questions_return_help_without_creating_drafts(self):
        cases = {
            "怎麼新增行程": ["整理成草稿", "確認後才加入"],
            "如何建立行程？": ["臺南火車站", "全天草稿"],
            "新增行程要怎麼說": ["明天下午三點", "確認後才加入"],
            "怎麼修改行程": ["修改草稿", "確認後才更新"],
            "怎麼刪除行程": ["找出符合的行程", "確認後才刪除"],
            "怎麼查行程": ["明天有什麼行程", "後天下午"],
        }
        for message, fragments in cases.items():
            with self.subTest(message=message):
                result = build_chat_v2_response({
                    "message": message,
                    "contract_version": 2,
                    "client_now": "2026-11-23T10:00:00+08:00",
                })
                self.assertEqual(result["intent"], "GENERAL_CHAT")
                self.assertIsNone(result["draft_event"])
                self.assertEqual(result["missing_fields"], [])
                self.assertTrue(result.get("_skip_gemini"))
                for fragment in fragments:
                    self.assertIn(fragment, result["reply"])

    def test_cancel_prefix_station_title_and_typhoon_safety_regressions(self):
        now = "2026-11-23T10:00:00+08:00"
        for message in ["取消明天的開會", "取消明天的行程", "取消明天的提醒"]:
            with self.subTest(message=message):
                result = build_chat_v2_response({"message": message, "contract_version": 2, "client_now": now})
                self.assertEqual(result["intent"], "DELETE_EVENT")

        meeting = build_chat_v2_response({
            "message": "明天下午三點在臺北車站跟朋友見面",
            "contract_version": 2,
            "client_now": now,
        })
        self.assertEqual(meeting["intent"], "CREATE_EVENT")
        self.assertEqual(meeting["draft_event"]["location"], "臺北車站")
        self.assertEqual(meeting["draft_event"]["title"], "跟朋友見面")

        typhoon = build_chat_v2_response({
            "message": "颱風天可以去海邊看浪嗎？",
            "contract_version": 2,
            "client_now": now,
        })
        self.assertEqual(typhoon["intent"], "DISASTER_INFO")
        self.assertIn("不要前往海邊", typhoon["reply"])
        self.assertIn("長浪", typhoon["reply"])

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

    def test_chain_landmark_keeps_location_and_requires_city(self):
        payload = self.payload_for({"input": "明天下午三點開會，地點在新光三越"})
        result = build_chat_v2_response(payload)

        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertIn("city", result["missing_fields"])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["draft_event"]["location"], "新光三越")

    def test_passport_phrase_without_hint_creates_all_day_draft(self):
        payload = self.payload_for({"input": "明天辦護照"})
        payload["intent_hint"] = None
        result = build_chat_v2_response(payload)

        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertTrue(result["needs_clarification"])
        self.assertIn("city", result["missing_fields"])
        self.assertEqual(result["draft_event"]["title"], "辦護照")
        self.assertTrue(result["draft_event"]["all_day"])
        self.assertIsNone(result["draft_event"]["start_time"])

    def test_time_only_follow_up_preserves_title_and_still_asks_period(self):
        first = self.payload_for({"input": "等一下2點45分提醒我去國防期中考"})
        first_result = build_chat_v2_response(first)
        self.assertEqual(first_result["intent"], "CREATE_EVENT")
        self.assertEqual(first_result["draft_event"]["title"], "國防期中考")
        self.assertIn("start_time", first_result["missing_fields"])

        follow_up = self.payload_for({"input": "2點45分"})
        follow_up.update({
            "intent_hint": "CREATE_EVENT",
            "draft_id": first_result["draft_id"],
            "draft_event": first_result["draft_event"],
        })
        second_result = build_chat_v2_response(follow_up)
        self.assertEqual(second_result["draft_event"]["title"], "國防期中考")
        self.assertNotEqual(second_result["draft_event"]["title"], "分")
        self.assertIn("start_time", second_result["missing_fields"])
        self.assertIsNone(second_result["draft_event"]["start_time"])
        self.assertEqual(second_result["reply"], "是上午還是下午？")

    def test_weather_queries_extract_alias_and_week_range(self):
        alias = self.payload_for({"input": "阿里山現在氣溫多少"})
        alias_result = build_chat_v2_response(alias)
        self.assertEqual(alias_result["intent"], "QUERY_WEATHER")
        self.assertEqual(alias_result["entities"]["city"], "嘉義縣")
        self.assertEqual(alias_result["entities"]["district"], "阿里山鄉")
        self.assertEqual(alias_result["entities"]["start_date"], "2026-09-23")
        self.assertTrue(alias_result["reply"])

        week = self.payload_for({"input": "未來一週天氣"})
        week_result = build_chat_v2_response(week)
        self.assertEqual(week_result["intent"], "QUERY_WEATHER")
        self.assertEqual(week_result["entities"]["start_date"], "2026-09-23")
        self.assertEqual(week_result["entities"]["end_date"], "2026-09-29")

        city_only = build_chat_v2_response(self.payload_for({"input": "嘉義市天氣如何"}))
        self.assertEqual(city_only["entities"]["city"], "嘉義市")
        self.assertIsNone(city_only["entities"]["district"])

    def test_weather_question_variants_do_not_fall_into_general_chat(self):
        for message in [
            "什麼時候會回溫", "下次變冷是什麼時候", "適合曬衣服嗎", "空氣品質好嗎",
            "現在濕度多少", "明天日出幾點", "今天日落時間",
        ]:
            with self.subTest(message=message):
                result = build_chat_v2_response(self.payload_for({"input": message}))
                self.assertEqual(result["intent"], "QUERY_WEATHER")
                expected_start = "2026-09-24" if "明天" in message else "2026-09-23"
                self.assertEqual(result["entities"]["start_date"], expected_start)
                if "什麼時候" in message or "下次" in message:
                    self.assertEqual(result["entities"]["end_date"], "2026-09-29")

    def test_current_location_is_not_copied_and_only_chore_locations_are_optional(self):
        for message in ["今天晚上八點要倒垃圾", "明天早上九點去銀行", "明天下午三點開會"]:
            with self.subTest(message=message):
                result = build_chat_v2_response(self.payload_for({"input": message}))
                self.assertEqual(result["intent"], "CREATE_EVENT")
                self.assertIsNone(result["draft_event"]["city"])
                if "倒垃圾" in message:
                    self.assertNotIn("city", result["missing_fields"])
                    self.assertEqual(result["draft_event"]["title"], "倒垃圾")
                else:
                    self.assertIn("city", result["missing_fields"])

    def test_unrelated_message_does_not_continue_existing_draft(self):
        payload = self.payload_for({"input": "我今天心情不錯"})
        payload.update({
            "intent_hint": "CREATE_EVENT",
            "draft_event": {"title": "開會", "date": "2026-09-24", "start_time": "15:00"},
        })
        result = build_chat_v2_response(payload)
        self.assertEqual(result["intent"], "GENERAL_CHAT")
        self.assertIsNone(result["draft_event"])

    def test_location_only_follow_up_preserves_draft_title(self):
        payload = self.payload_for({"input": "地點在臺南大學"})
        payload.update({
            "intent_hint": "CREATE_EVENT",
            "draft_event": {"title": "開會", "date": "2026-09-24", "start_time": "15:00"},
        })
        result = build_chat_v2_response(payload)
        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertEqual(result["draft_event"]["title"], "開會")
        self.assertEqual(result["draft_event"]["location"], "臺南大學")

    def test_create_event_supports_at_location_and_duration(self):
        result = build_chat_v2_response(self.payload_for({"input": "週六下午兩點開會兩小時＠信義區"}))
        self.assertEqual(result["intent"], "CREATE_EVENT")
        self.assertEqual(result["draft_event"]["title"], "開會")
        self.assertEqual(result["draft_event"]["start_time"], "14:00")
        self.assertEqual(result["draft_event"]["end_time"], "16:00")
        self.assertEqual(result["draft_event"]["location"], "信義區")

    def test_20261123_acceptance_examples(self):
        now = "2026-11-23T10:00:00+08:00"
        cases = [
            ("12/15下午三點到三點十五拍🎓學士團體照", "CREATE_EVENT", {"date": "2026-12-15", "start_time": "15:00", "end_time": "15:15"}, ["city"]),
            ("明天下午三點到五點去成大開會", "CREATE_EVENT", {"title": "開會", "date": "2026-11-24", "start_time": "15:00", "end_time": "17:00", "city": "臺南市", "location": "成大"}, []),
            ("2點到晚上10點半上班", "CREATE_EVENT", {"title": "上班", "start_time": "14:00", "end_time": "22:30"}, ["city"]),
            ("上午十一點到下午一點吃飯", "CREATE_EVENT", {"title": "吃飯", "start_time": "11:00", "end_time": "13:00"}, ["city"]),
            ("晚上十一點到凌晨一點值班", "CREATE_EVENT", {"title": "值班", "start_time": "23:00", "end_time": "01:00"}, ["city"]),
            ("明天三點開會", "CREATE_EVENT", {"title": "開會", "date": "2026-11-24", "start_time": None}, ["start_time", "city"]),
            ("下午三點十五看牙醫", "CREATE_EVENT", {"title": "看牙醫", "start_time": "15:15"}, ["city"]),
            ("明天早上九點零五分交報告", "CREATE_EVENT", {"title": "交報告", "date": "2026-11-24", "start_time": "09:05"}, []),
            ("提醒我繳電費", "CREATE_EVENT", {"title": "繳電費", "date": "2026-11-23", "all_day": True, "is_reminder": True}, []),
            ("明天下午開會", "CREATE_EVENT", {"title": "開會", "date": "2026-11-24", "start_time": None}, ["start_time", "city"]),
            ("明天下午3點 臺南火車站 開會", "CREATE_EVENT", {"title": "開會", "city": "臺南市", "location": "臺南火車站"}, []),
            ("週五晚上去全聯", "CREATE_EVENT", {"title": "去全聯", "date": "2026-11-27", "location": "全聯"}, ["start_time", "city"]),
            ("下週五早上八點去我公司", "CREATE_EVENT", {"title": "去公司", "date": "2026-12-04", "start_time": "08:00", "location": "我公司"}, ["city"]),
        ]
        for message, intent, expected_draft, expected_missing in cases:
            with self.subTest(message=message):
                result = build_chat_v2_response({"message": message, "contract_version": 2, "client_now": now})
                self.assertEqual(result["intent"], intent)
                for key, value in expected_draft.items():
                    self.assertEqual(result["draft_event"].get(key), value, key)
                self.assertEqual(result["missing_fields"], expected_missing)

        query_event = build_chat_v2_response({"message": "後天有行程嗎", "contract_version": 2, "client_now": now})
        self.assertEqual(query_event["intent"], "QUERY_EVENT")
        self.assertEqual(query_event["entities"]["start_date"], "2026-11-25")
        query_weather = build_chat_v2_response({"message": "明天台南會下雨嗎", "contract_version": 2, "client_now": now})
        self.assertEqual(query_weather["intent"], "QUERY_WEATHER")
        self.assertEqual(query_weather["entities"]["city"], "臺南市")
        event_weather = build_chat_v2_response({"message": "明天去台南開會會下雨嗎", "contract_version": 2, "client_now": now})
        self.assertEqual(event_weather["intent"], "EVENT_WEATHER")
        self.assertEqual(event_weather["entities"]["activity"], "開會")
        self.assertEqual(build_chat_v2_response({"message": "我昨天去看牙醫了", "client_now": now})["intent"], "GENERAL_CHAT")

        titled_delete = build_chat_v2_response({"message": "新增【測試】請刪除 明天下午三點", "client_now": now})
        self.assertEqual(titled_delete["intent"], "CREATE_EVENT")
        self.assertEqual(titled_delete["draft_event"]["title"], "【測試】請刪除")
        update = build_chat_v2_response({"message": "把明天開會改到下午四點", "client_now": now})
        self.assertEqual(update["intent"], "UPDATE_EVENT")
        self.assertEqual(update["changes"]["start_time"], "16:00")
        delete = build_chat_v2_response({"message": "把那個取消", "client_now": now})
        self.assertEqual(delete["intent"], "DELETE_EVENT")
        self.assertEqual(delete["missing_fields"], ["target"])

        follow = build_chat_v2_response({
            "message": "小西門", "client_now": now, "intent_hint": "CREATE_EVENT", "draft_id": "draft-1",
            "draft_event": {"title": "上班", "date": "2026-11-24", "start_time": "14:00", "all_day": False},
        })
        self.assertEqual(follow["draft_event"]["title"], "上班")
        self.assertEqual(follow["draft_event"]["location"], "小西門")
        date_follow = build_chat_v2_response({
            "message": "12/15", "client_now": now, "intent_hint": "CREATE_EVENT", "draft_id": "draft-2",
            "draft_event": {"title": "上班", "date": None, "start_time": "14:00", "city": "臺南市", "location": "成大"},
        })
        self.assertEqual(date_follow["draft_event"]["date"], "2026-12-15")
        self.assertEqual(date_follow["draft_event"]["title"], "上班")
        weekend = build_chat_v2_response({"message": "這週末去爬山", "client_now": now})
        self.assertEqual(weekend["intent"], "CREATE_EVENT")
        self.assertEqual(weekend["draft_event"]["date"], "2026-11-28")

    def test_contract_validator_blocks_invalid_mobile_payload(self):
        invalid = {
            "status": "success",
            "contract_version": 2,
            "intent": "NOT_A_REAL_INTENT",
            "reply": "",
            "missing_fields": ["unknown"],
            "draft_event": {"date": "2026-99-99", "start_time": "35:88", "city": "不存在市"},
        }
        result = validate_chat_v2_response(invalid, "小藍")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["intent"], "GENERAL_CHAT")
        self.assertFalse(result["needs_clarification"])
        self.assertTrue(result["reply"])
        self.assertTrue(result["_skip_gemini"])
        self.assertIn("invalid_intent", result["validation_errors"])


if __name__ == "__main__":
    unittest.main()
