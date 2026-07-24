import asyncio
import unittest

from fastapi.testclient import TestClient

import main
from gemini_service import parse_json_object
from transport_service import build_tdx_status
from weather_service import compare_weather_snapshots, parse_weather_periods


class CoreLogicTests(unittest.TestCase):
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
        self.assertEqual(set(body.keys()), expected_keys)
        self.assertIn(body["action_type"], ["ADD_EVENT", "DELETE_EVENT", "NONE"])
        if body["action_type"] == "ADD_EVENT":
            self.assertTrue(body["event_title"])
            self.assertIn("+08:00", body["event_start"])
            self.assertIn("+08:00", body["event_end"])

    def test_api_smoke_chat_history(self):
        client = TestClient(main.app)
        response = client.get("/api/chat/history?user_id=test-user&limit=5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["status"], ["success", "error"])
        self.assertIn("errors", body)

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


if __name__ == "__main__":
    unittest.main()
