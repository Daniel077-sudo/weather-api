import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives import hashes
from fastapi.testclient import TestClient

import auth
import disaster_service
import event_service
import main
from chat_service import CHAT_LOGS_TABLE, LOCAL_PENDING_EVENTS, build_clarify_response, build_local_fallback, create_event_from_chat, normalize_chat_response, prepare_chat_response, sanitize_event_title
import gemini_service
from gemini_service import parse_json_object
from transport_service import build_tdx_status
from weather_service import CWA_SSL_ERROR_MARKERS, compare_weather_snapshots, parse_weather_periods, resolve_event_location_parts


def make_test_jwt(user_id: str, secret: str = "test-secret", lifetime_seconds: int = 3600) -> str:
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode({"sub": user_id, "exp": int(time.time()) + lifetime_seconds})
    signed = f"{header}.{payload}".encode("ascii")
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()).decode("ascii").rstrip("=")
    return f"{header}.{payload}.{signature}"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_test_es256_jwt(user_id: str, private_key, kid: str = "test-kid", lifetime_seconds: int = 3600) -> str:
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return b64url(raw)

    header = encode({"alg": "ES256", "kid": kid, "typ": "JWT"})
    payload = encode({"sub": user_id, "exp": int(time.time()) + lifetime_seconds})
    signed = f"{header}.{payload}".encode("ascii")
    der_signature = private_key.sign(signed, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{payload}.{b64url(raw_signature)}"


def public_key_to_jwk(private_key, kid: str = "test-kid") -> dict:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }


class CoreLogicTests(unittest.TestCase):
    def test_event_description_is_returned_by_normalizer(self):
        event = event_service.normalize_event({
            "id": 1,
            "title": "開會",
            "description": "攜帶簡報",
            "start_time": "2026-09-24T15:00:00+08:00",
            "end_time": "2026-09-24T16:00:00+08:00",
        })
        self.assertEqual(event["description"], "攜帶簡報")

    def test_create_event_compatibility_fallback_keeps_description(self):
        inserted = []

        class FakeResult:
            def __init__(self, data):
                self.data = data

        class FakeQuery:
            def __init__(self, payload):
                self.payload = dict(payload)

            def execute(self):
                inserted.append(self.payload)
                if "location" in self.payload:
                    raise RuntimeError("simulated incompatible location column")
                return FakeResult([{**self.payload, "id": 321}])

        class FakeTable:
            def insert(self, payload):
                return FakeQuery(payload)

        class FakeSupabase:
            def table(self, _name):
                return FakeTable()

        async def fake_enrich(payload, **_kwargs):
            return {**payload, "weather_checked_at": "2026-09-24T12:00:00+08:00"}

        event = main.EventCreate(
            user_id="00000000-0000-0000-0000-000000000001",
            title="開會",
            description="攜帶簡報",
            start_time="2026-09-24T15:00:00+08:00",
            end_time="2026-09-24T16:00:00+08:00",
            city="臺南市",
            district="東區",
            location="臺南市東區",
        )
        with patch.object(main, "supabase", FakeSupabase()), patch.object(
            main, "enrich_event_payload_with_risk", new=AsyncMock(side_effect=fake_enrich)
        ), patch.object(main, "persist_event_risk_fields", return_value={}):
            response = asyncio.run(main.create_event(event, main.BackgroundTasks()))

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["data"]["description"], "攜帶簡報")
        self.assertEqual(inserted[-1]["description"], "攜帶簡報")

    def test_parse_weather_periods(self):
        dist_data = {
            "weatherElement": [
                {
                    "elementName": "Wx",
                    "time": [
                        {
                            "startTime": "2026-06-04T09:00:00+08:00",
                            "endTime": "2026-06-04T12:00:00+08:00",
                            "elementValue": [{"value": "多雲"}],
                        }
                    ],
                },
                {
                    "elementName": "PoP",
                    "time": [
                        {
                            "startTime": "2026-06-04T09:00:00+08:00",
                            "endTime": "2026-06-04T12:00:00+08:00",
                            "elementValue": [{"value": "80"}],
                        }
                    ],
                },
            ]
        }
        parsed = parse_weather_periods(dist_data)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["description"], "多雲")
        self.assertEqual(parsed[0]["pop"], 80)

    def test_compare_weather_snapshots_detects_rain_increase(self):
        old_snapshot = {"weather": {"description": "晴", "pop": 10, "temp": 28}, "risk_level": "low", "risk_tags": []}
        new_snapshot = {"weather": {"description": "大雨", "pop": 80, "temp": 27}, "risk_level": "high", "risk_tags": ["heavy_rain"]}
        comparison = compare_weather_snapshots(old_snapshot, new_snapshot)
        self.assertTrue(comparison["should_notify"])
        self.assertIn("降雨機率增加 70%", comparison["reasons"])

    def test_gemini_json_fallback(self):
        parsed = parse_json_object("not json")
        self.assertEqual(parsed, {})

    def test_tdx_fallback_when_not_configured(self):
        status = asyncio.run(build_tdx_status("tra"))
        self.assertIn(status["tdx_status"], ["not_configured", "error", "success"])

    def test_timetree_missing_token(self):
        client = TestClient(main.app)
        response = client.post("/api/integrations/timetree/sync")
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.json()["status"], ["not_configured", "partial_success", "success"])

    def test_api_smoke_health(self):
        client = TestClient(main.app)
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_api_chat_contract_add_event(self):
        client = TestClient(main.app)
        response = client.post(
            "/api/chat",
            json={"user_id": "test-user", "message": "我這週末想要去阿里山露營！"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        expected_keys = {
            "reply",
            "has_alert",
            "alert_title",
            "alert_url",
            "action_type",
            "event_title",
            "event_start",
            "event_end",
            "event_id_to_delete",
        }
        self.assertTrue(expected_keys.issubset(set(body.keys())))
        self.assertIn(body["action_type"], ["ADD_EVENT", "CREATE_EVENT", "DELETE_EVENT", "CLARIFY", "NONE"])
        if body["action_type"] == "ADD_EVENT":
            self.assertTrue(body["event_title"])
            self.assertIn("+08:00", body["event_start"])
            self.assertIn("+08:00", body["event_end"])

    def test_api_chat_includes_timing_contract(self):
        client = TestClient(main.app)
        response = client.post(
            "/api/chat",
            json={"user_id": "timing-user", "message": "來一個防災小遊戲"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("x-process-time", response.headers)
        body = response.json()
        self.assertIn("timing", body)
        self.assertIn("total_ms", body["timing"])

    def test_chat_deferred_persist_keeps_pending_event_available(self):
        user_id = "pending-user"
        LOCAL_PENDING_EVENTS.pop(user_id, None)
        clarify = build_clarify_response({"event_title": "爬山"}, ["location"])
        response = prepare_chat_response(user_id, "我禮拜六想去爬山", clarify, defer_persist=True)
        self.assertEqual(response["action_type"], "CLARIFY")
        self.assertIn(user_id, LOCAL_PENDING_EVENTS)
        self.assertIn("_persist_chat_turn", response)
        LOCAL_PENDING_EVENTS.pop(user_id, None)

    def test_chat_local_fallback_detects_complete_event(self):
        body = build_local_fallback("test-user", "明天下午三點我要去台南市東區的公園跑步")
        self.assertEqual(body["action_type"], "ADD_EVENT")
        self.assertTrue(body["event_title"])
        self.assertIn("+08:00", body["event_start"])
        self.assertIn("15:00:00", body["event_start"])

    def test_chat_detects_absolute_date_event(self):
        body = build_local_fallback("absolute-date-user", "9月12號早上十點高雄前鎮區路跑")
        self.assertEqual(body["action_type"], "ADD_EVENT")
        self.assertEqual(body["event_title"], "路跑")
        self.assertEqual(body["event_city"], "高雄市")
        self.assertEqual(body["event_district"], "前鎮區")
        self.assertIn("10:00:00", body["event_start"])

    def test_event_title_sanitizer_removes_quotes_and_spaces(self):
        self.assertEqual(sanitize_event_title(" 「 路跑 」 "), "路跑")
        fallback = {"action_type": "ADD_EVENT", "event_title": "「 路跑 」", "event_start": "2027-09-12T10:00:00+08:00", "event_end": "2027-09-12T12:00:00+08:00"}
        body = normalize_chat_response({"action_type": "ADD_EVENT", "event_title": " 「 路跑 」 "}, fallback)
        self.assertEqual(body["event_title"], "路跑")

    def test_chat_create_event_reply_includes_year(self):
        inserted_payloads = []

        class FakeResult:
            def __init__(self, data):
                self.data = data

        class FakeQuery:
            def __init__(self, action, payload=None):
                self.action = action
                self.payload = payload or {}

            def select(self, *_args, **_kwargs):
                return self

            def eq(self, *_args, **_kwargs):
                return self

            def limit(self, *_args, **_kwargs):
                return self

            def insert(self, payload):
                return FakeQuery("insert", payload)

            def execute(self):
                if self.action == "insert":
                    inserted_payloads.append(dict(self.payload))
                    return FakeResult([{**self.payload, "id": 999}])
                return FakeResult([])

        class FakeSupabase:
            def table(self, _table_name):
                return FakeQuery("select")

        import chat_service

        original_supabase = chat_service.supabase
        try:
            chat_service.supabase = FakeSupabase()
            response = asyncio.run(create_event_from_chat("not-a-uuid", {
                "event_title": " 「 路跑 」 ",
                "event_start": "2027-09-12T10:00:00+08:00",
                "event_end": "2027-09-12T12:00:00+08:00",
                "event_city": "高雄市",
                "event_district": "前鎮區",
                "event_location": "高雄市前鎮區",
            }, defer_risk=True))
            self.assertEqual(response["action_type"], "CREATE_EVENT")
            self.assertEqual(inserted_payloads[0]["title"], "路跑")
            self.assertEqual(response["event_title"], "路跑")
            self.assertIn("2027-09-12 10:00", response["reply"])
        finally:
            chat_service.supabase = original_supabase

    def test_chat_clarifies_destination_phrase_without_history(self):
        body = build_local_fallback("creek-user", "我要去溪邊")
        self.assertEqual(body["action_type"], "CLARIFY")
        self.assertIn("time", body["missing_slots"])
        self.assertTrue(body["event_title"])

    def test_chat_new_complete_message_ignores_old_pending_title(self):
        user_id = "pending-pollution-user"
        LOCAL_PENDING_EVENTS[user_id] = {
            "event_title": "墾丁玩",
            "event_city": "屏東縣",
            "event_district": "恆春鎮",
            "event_location": "屏東縣恆春鎮",
        }
        try:
            body = build_local_fallback(user_id, "明天早上八點台中西屯區運動")
            self.assertEqual(body["action_type"], "ADD_EVENT")
            self.assertEqual(body["event_title"], "運動")
            self.assertEqual(body["event_city"], "臺中市")
            self.assertEqual(body["event_district"], "西屯區")
        finally:
            LOCAL_PENDING_EVENTS.pop(user_id, None)

    def test_chat_clarifies_city_only_event_location(self):
        body = build_local_fallback("test-user", "我明天下午三點要去台南爬山")
        self.assertEqual(body["action_type"], "CLARIFY")
        self.assertIn("district", body["missing_slots"])
        self.assertEqual(body["event_city"], "臺南市")
        self.assertEqual(body["event_district"], "")
        self.assertIn("行政區", body["reply"])

    def test_location_resolver_does_not_mix_city_with_default_district(self):
        parts = resolve_event_location_parts({"city": "臺南市", "district": "", "location": "臺南市"})
        self.assertEqual(parts["city"], "臺南市")
        self.assertEqual(parts["district"], "")

    def test_location_resolver_does_not_default_empty_event_to_taipei(self):
        parts = resolve_event_location_parts({"title": "沒有地點的行程"})
        self.assertEqual(parts["city"], "")
        self.assertEqual(parts["district"], "")

    def test_chat_local_fallback_answers_disaster_qa(self):
        body = build_local_fallback("test-user", "地震來的時候應該怎麼辦")
        self.assertEqual(body["action_type"], "DISASTER_GUIDE")
        self.assertIn("趴下", body["reply"])
        self.assertNotIn("目前不會更動行事曆", body["reply"])

    def test_chat_normalizer_does_not_downgrade_local_event(self):
        fallback = build_local_fallback("test-user", "明天下午三點我要去台南市東區的公園跑步")
        body = normalize_chat_response({"action_type": "NONE", "reply": "一般對話"}, fallback)
        self.assertEqual(body["action_type"], "ADD_EVENT")

    def test_api_smoke_chat_history(self):
        client = TestClient(main.app)
        response = client.get("/api/chat/history?user_id=test-user&limit=5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("errors", body)
        self.assertNotEqual(body.get("source"), "chat_messages")

    def test_chat_history_uses_existing_chat_logs_table(self):
        self.assertEqual(CHAT_LOGS_TABLE, "chat_logs")

    def test_cwa_ssl_fallback_is_scoped_to_certificate_errors(self):
        self.assertIn("CERTIFICATE_VERIFY_FAILED", CWA_SSL_ERROR_MARKERS)
        self.assertIn("Missing Subject Key Identifier", CWA_SSL_ERROR_MARKERS)

    def test_api_smoke_chat_memory(self):
        client = TestClient(main.app)
        response = client.get("/api/chat/memory?user_id=test-user")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("data", body)

    def test_api_smoke_debug_status(self):
        client = TestClient(main.app)
        response = client.get("/api/debug/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "partial_success", "error"])
        self.assertIn("data", body)

    def test_api_smoke_sync_logs(self):
        client = TestClient(main.app)
        response = client.get("/api/sync-logs?limit=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("errors", body)

    def test_api_smoke_disaster_alerts(self):
        client = TestClient(main.app)
        response = client.get("/api/disaster-alerts?city=臺南市&limit=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("errors", body)

    def test_disaster_alerts_filter_by_affected_areas(self):
        calls = []

        class FakeResult:
            def __init__(self, data):
                self.data = data

        class FakeQuery:
            def select(self, *args, **kwargs):
                calls.append(("select", args, kwargs))
                return self

            def gte(self, *args, **kwargs):
                calls.append(("gte", args, kwargs))
                return self

            def order(self, *args, **kwargs):
                calls.append(("order", args, kwargs))
                return self

            def limit(self, *args, **kwargs):
                calls.append(("limit", args, kwargs))
                return self

            def eq(self, *args, **kwargs):
                calls.append(("eq", args, kwargs))
                raise AssertionError("city/district DB filters should not be used")

            def execute(self):
                return FakeResult([
                    {
                        "title": "臺南市大雨特報",
                        "severity": "medium",
                        "type": "rain",
                        "starts_at": "2026-09-17T10:00:00+08:00",
                        "expires_at": "2099-01-01T00:00:00+08:00",
                        "affected_areas": [{"city": "臺南市", "district": ""}],
                    },
                    {
                        "title": "臺北市強風特報",
                        "severity": "medium",
                        "type": "wind",
                        "starts_at": "2026-09-17T10:00:00+08:00",
                        "expires_at": "2099-01-01T00:00:00+08:00",
                        "affected_areas": [{"city": "臺北市", "district": ""}],
                    },
                ])

        class FakeSupabase:
            def table(self, table_name):
                self.table_name = table_name
                return FakeQuery()

        original_supabase = disaster_service.supabase
        try:
            disaster_service.supabase = FakeSupabase()
            result = disaster_service.get_active_disaster_alerts(city="臺南市", limit=10)
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(result["data"]), 1)
            self.assertEqual(result["data"][0]["city"], "臺南市")
            ordered_columns = [call[1][0] for call in calls if call[0] == "order"]
            self.assertIn("starts_at", ordered_columns)
            self.assertNotIn("started_at", ordered_columns)
        finally:
            disaster_service.supabase = original_supabase

    def test_cwa_alert_normalizes_to_current_schema(self):
        payload = disaster_service.normalize_cwa_alert(
            "臺南市",
            {"info": {"phenomena": "大雨", "significance": "特報", "effectiveTime": "2026-09-17T10:00:00+08:00"}},
            {"locationName": "臺南市"},
        )
        self.assertIn("starts_at", payload)
        self.assertIn("affected_areas", payload)
        self.assertNotIn("started_at", payload)
        self.assertNotIn("city", payload)
        self.assertEqual(payload["affected_areas"][0]["city"], "臺南市")

    def test_api_smoke_area_status(self):
        client = TestClient(main.app)
        response = client.get("/api/area/status?city=臺南市&district=東區")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("data", body)
        if body["status"] == "success":
            self.assertIn("traffic_risk", body["data"])
            self.assertIn("booking_links", body["data"])

    def test_api_smoke_watch_areas(self):
        client = TestClient(main.app)
        response = client.get("/api/watch-areas?user_id=test-user&limit=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("errors", body)

    def test_api_smoke_watch_area_statuses(self):
        client = TestClient(main.app)
        response = client.get("/api/watch-areas/status?user_id=test-user&limit=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "partial_success", "error"])
        self.assertIn("errors", body)

    def test_api_smoke_area_alert_notifications(self):
        client = TestClient(main.app)
        response = client.get("/api/area-alert-notifications?user_id=test-user&limit=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("errors", body)

    def test_api_smoke_notifications_summary(self):
        client = TestClient(main.app)
        response = client.get("/api/notifications/summary?user_id=test-user")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "partial_success"])
        self.assertIn("data", body)
        self.assertIn("latest", body["data"])

    def test_api_smoke_cleanup_disaster_alerts(self):
        client = TestClient(main.app)
        response = client.post("/api/cron/cleanup-disaster-alerts")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("errors", body)

    def test_api_smoke_monitor_watch_areas(self):
        client = TestClient(main.app)
        response = client.post("/api/cron/monitor-watch-areas?limit=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "partial_success", "error", "processing"])
        self.assertIn("errors", body)

    def test_api_smoke_disaster_pipeline(self):
        client = TestClient(main.app)
        response = client.post("/api/cron/disaster-pipeline?watch_area_limit=1&hours_ahead=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "partial_success", "error", "processing"])
        self.assertIn("errors", body)

    def test_api_smoke_events_query(self):
        client = TestClient(main.app)
        response = client.get("/api/events?limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.json()["status"], ["success", "error"])

    def test_api_smoke_vision_invalid_base64(self):
        client = TestClient(main.app)
        response = client.post(
            "/api/emergency-kit/vision-check",
            json={"image_base64": "not-base64", "mime_type": "image/jpeg"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["source"], "validation")

    def test_auth_contract_endpoint(self):
        client = TestClient(main.app)
        response = client.get("/api/auth/contract")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["data"]["unauthorized_response"]["http_status"], 401)
        self.assertIn("POST /api/chat", body["data"]["protected_when_auth_required"])
        self.assertIn("POST /api/chat/memory/reset", body["data"]["protected_when_auth_required"])
        self.assertIn("PATCH /api/events/{event_id}", body["data"]["protected_when_auth_required"])

    def test_supabase_jwt_uses_sub_as_user_id(self):
        original_secret = auth.SUPABASE_JWT_SECRET
        try:
            auth.SUPABASE_JWT_SECRET = "test-secret"
            claims = auth.verify_supabase_jwt(make_test_jwt("jwt-user"))
            context = auth.AuthContext(user_id=claims["sub"], claims=claims, authenticated=True)
            self.assertEqual(auth.resolve_user_id(context, "body-user"), "jwt-user")
        finally:
            auth.SUPABASE_JWT_SECRET = original_secret

    def test_supabase_es256_jwt_uses_jwks(self):
        original_cache = dict(auth.JWKS_CACHE)
        try:
            private_key = ec.generate_private_key(ec.SECP256R1())
            auth.JWKS_CACHE["keys"] = [public_key_to_jwk(private_key, kid="es-test")]
            auth.JWKS_CACHE["fetched_at"] = int(time.time())
            claims = auth.verify_supabase_jwt(make_test_es256_jwt("es-user", private_key, kid="es-test"))
            context = auth.AuthContext(user_id=claims["sub"], claims=claims, authenticated=True)
            self.assertEqual(auth.resolve_user_id(context, "body-user"), "es-user")
        finally:
            auth.JWKS_CACHE.clear()
            auth.JWKS_CACHE.update(original_cache)

    def test_auth_required_rejects_missing_token(self):
        original_required = auth.AUTH_REQUIRED
        try:
            auth.AUTH_REQUIRED = True
            client = TestClient(main.app)
            response = client.get("/api/chat/history?user_id=test-user&limit=1")
            self.assertEqual(response.status_code, 401)
            body = response.json()
            self.assertEqual(body["detail"]["status"], "error")
            self.assertEqual(body["detail"]["errors"][0]["code"], "missing_token")
        finally:
            auth.AUTH_REQUIRED = original_required

    def test_invalid_bearer_token_returns_401(self):
        original_secret = auth.SUPABASE_JWT_SECRET
        try:
            auth.SUPABASE_JWT_SECRET = "test-secret"
            client = TestClient(main.app)
            response = client.get("/api/chat/history?user_id=test-user&limit=1", headers={"Authorization": "Bearer invalid"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"]["source"], "auth")
        finally:
            auth.SUPABASE_JWT_SECRET = original_secret

    def test_chat_memory_reset_requires_auth_when_enabled(self):
        original_required = auth.AUTH_REQUIRED
        try:
            auth.AUTH_REQUIRED = True
            client = TestClient(main.app)
            response = client.post("/api/chat/memory/reset?user_id=test-user")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"]["errors"][0]["code"], "missing_token")
        finally:
            auth.AUTH_REQUIRED = original_required

    def test_gemini_cached_fallback_reports_missing_key(self):
        original_key = gemini_service.GEMINI_API_KEY
        try:
            gemini_service.GEMINI_API_KEY = ""
            response = asyncio.run(
                gemini_service.call_gemini_json_cached(
                    "請回 JSON",
                    {"risk_summary": "fallback", "recommended_action": "fallback"},
                    "unit_test",
                    "missing-key",
                    {"case": "missing-key"},
                )
            )
            self.assertEqual(response["suggestion_source"], "local_rules")
            self.assertFalse(response["gemini_configured"])
            self.assertFalse(response["gemini_attempted"])
            self.assertFalse(response["gemini_response_valid"])
            self.assertEqual(response["gemini_error"], "missing_api_key")
        finally:
            gemini_service.GEMINI_API_KEY = original_key

    def test_event_risk_uses_current_weather_not_full_json_keywords(self):
        class FakeResult:
            def __init__(self, data):
                self.data = data

        class FakeQuery:
            def __init__(self, table_name):
                self.table_name = table_name

            def select(self, *_args, **_kwargs):
                return self

            def eq(self, *_args, **_kwargs):
                return self

            def execute(self):
                if self.table_name == "weather_cache":
                    return FakeResult([
                        {
                            "weather_data": {
                                "current": {"description": "晴", "pop": 0, "wind_ms": 1, "app_temp": 28, "wbgt": 0},
                                "forecast": [{"description": "大雨強風", "pop": 90}],
                            }
                        }
                    ])
                return FakeResult([])

        class FakeSupabase:
            def table(self, table_name):
                return FakeQuery(table_name)

        async def fake_ai(prompt, fallback, prompt_type, cache_subject, context):
            return {**fallback, "suggestion_source": "local_rules", "cache_hit": False}

        async def fake_traffic(_weather, _transport):
            return {"tdx_status": "not_configured", "tdx_message": ""}

        original_supabase = event_service.supabase
        original_alerts = event_service.get_active_disaster_alerts
        original_ai = event_service.call_gemini_json_cached
        original_traffic = event_service.build_traffic_risk_async
        try:
            event_service.supabase = FakeSupabase()
            event_service.get_active_disaster_alerts = lambda *_args, **_kwargs: {"status": "success", "data": []}
            event_service.call_gemini_json_cached = fake_ai
            event_service.build_traffic_risk_async = fake_traffic
            result = asyncio.run(
                event_service.build_event_risk(
                    main.EventRiskCheckRequest(
                        title="臺東室內會議",
                        city="臺東縣",
                        district="臺東市",
                        location="臺東縣臺東市",
                        activity="開會",
                    )
                )
            )
            self.assertEqual(result["risk_level"], "low")
            self.assertNotIn("heavy_rain", result["risk_tags"])
            self.assertNotIn("strong_wind", result["risk_tags"])
        finally:
            event_service.supabase = original_supabase
            event_service.get_active_disaster_alerts = original_alerts
            event_service.call_gemini_json_cached = original_ai
            event_service.build_traffic_risk_async = original_traffic

    def test_weather_suggestion_uses_gemini_when_available(self):
        async def fake_ai(prompt, fallback, prompt_type, cache_subject, context):
            return {
                "suggestion": "Gemini 建議改成室內備案。",
                "suggestion_source": "gemini",
                "gemini_configured": True,
                "gemini_attempted": True,
                "gemini_response_valid": True,
                "gemini_error": "",
                "cache_hit": False,
            }

        original_ai = main.call_gemini_json_cached
        try:
            main.call_gemini_json_cached = fake_ai
            client = TestClient(main.app)
            response = client.post(
                "/api/weather/suggestion",
                json={
                    "city": "臺南市",
                    "district": "東區",
                    "message": "下午要打球",
                    "weather_data": {"current": {"description": "晴", "pop": 0, "app_temp": 31}},
                },
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()["data"]
            self.assertEqual(data["suggestion_source"], "gemini")
            self.assertTrue(data["gemini_used"])
            self.assertEqual(data["suggestion"], "Gemini 建議改成室內備案。")
        finally:
            main.call_gemini_json_cached = original_ai

    def test_update_event_falls_back_when_risk_columns_missing(self):
        stored_event = {
            "id": "215",
            "user_id": "jwt-user",
            "title": "原標題",
            "start_time": "2026-09-18T10:00:00+08:00",
            "end_time": "2026-09-18T11:00:00+08:00",
            "city": "臺北市",
            "district": "大安區",
            "location": "臺北市大安區",
            "has_weather_risk": False,
            "ai_suggestion": "",
        }
        attempted_updates = []

        class FakeResult:
            def __init__(self, data):
                self.data = data

        class FakeQuery:
            def __init__(self, action, payload=None):
                self.action = action
                self.payload = payload or {}

            def select(self, *_args, **_kwargs):
                return self

            def update(self, payload):
                return FakeQuery("update", payload)

            def eq(self, *_args, **_kwargs):
                return self

            def limit(self, *_args, **_kwargs):
                return self

            def execute(self):
                if self.action == "select":
                    return FakeResult([stored_event])
                attempted_updates.append(dict(self.payload))
                if "risk_level" in self.payload:
                    raise Exception("{'message': 'column \"risk_level\" does not exist', 'code': '42703'}")
                stored_event.update(self.payload)
                return FakeResult([stored_event])

        class FakeSupabase:
            def table(self, _table_name):
                return FakeQuery("select")

        async def fake_enrich(payload, **_kwargs):
            return {
                **payload,
                "risk_level": "low",
                "risk_tags": [],
                "has_weather_risk": False,
                "weather_alert_status": "checked",
                "ai_suggestion": "ok",
                "recommended_action": "ok",
            }

        original_supabase = main.supabase
        original_enrich = main.enrich_event_payload_with_risk
        try:
            main.supabase = FakeSupabase()
            main.enrich_event_payload_with_risk = fake_enrich
            response = asyncio.run(
                main.update_event_by_id(
                    "215",
                    main.EventUpdate(title="新標題", city="高雄市", district="鹽埕區", location="高雄市鹽埕區"),
                    auth.AuthContext(user_id="jwt-user", authenticated=True),
                )
            )
            self.assertEqual(response["status"], "success")
            self.assertGreaterEqual(len(attempted_updates), 2)
            self.assertEqual(stored_event["title"], "新標題")
            self.assertEqual(stored_event["city"], "高雄市")
            self.assertEqual(stored_event["district"], "鹽埕區")
            self.assertNotEqual(stored_event["district"], "中正區")
        finally:
            main.supabase = original_supabase
            main.enrich_event_payload_with_risk = original_enrich


if __name__ == "__main__":
    unittest.main()
