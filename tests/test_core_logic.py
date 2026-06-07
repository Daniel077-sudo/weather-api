import asyncio
import unittest

from fastapi.testclient import TestClient

import main
from gemini_service import parse_json_object
from incident_service import classify_incident_type, extract_locations, infer_city_district, incident_keywords
from threads_service import parse_bing_threads_urls, parse_duckduckgo_threads_urls, parse_threads_public_page
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

    def test_incident_location_extraction(self):
        locations = extract_locations(["台中市西屯區逢甲商圈附近濃煙很大", "文華路有消防車"])
        self.assertIn("台中市西屯區", locations)
        self.assertIn("文華路", locations)

    def test_incident_type_classification(self):
        self.assertEqual(classify_incident_type("今天午後下雨"), "rain")
        self.assertEqual(classify_incident_type("外面大太陽很炎熱"), "sun")
        self.assertEqual(classify_incident_type("剛剛看到消防車和濃煙"), "fire")
        self.assertEqual(classify_incident_type("路口發生車禍大塞車"), "traffic_accident")

    def test_incident_keywords_default_scope(self):
        self.assertIn("下雨", incident_keywords())
        self.assertIn("封路", incident_keywords())

    def test_parse_bing_threads_urls(self):
        html = '''
        <a href="https://www.threads.net/@demo/post/abc">result</a>
        <a href="https://example.com/not-thread">skip</a>
        <a href="https://www.threads.net/@demo/post/abc">duplicate</a>
        '''
        urls = parse_bing_threads_urls(html, limit=5)
        self.assertEqual(urls, ["https://www.threads.net/@demo/post/abc"])

    def test_parse_duckduckgo_threads_urls(self):
        html = '''
        <a href="/l/?uddg=https%3A%2F%2Fwww.threads.com%2F%40demo%2Fpost%2Fabc">result</a>
        <a href="/l/?uddg=https%3A%2F%2Fexample.com%2Fskip">skip</a>
        '''
        urls = parse_duckduckgo_threads_urls(html, limit=5)
        self.assertEqual(urls, ["https://www.threads.com/@demo/post/abc"])

    def test_parse_threads_public_page(self):
        html = '''
        <html>
          <head>
            <title>Threads</title>
            <meta property="og:description" content="台中市西屯區文華路附近疑似火災濃煙" />
          </head>
          <body>留言說逢甲商圈附近有消防車</body>
        </html>
        '''
        parsed = parse_threads_public_page(html, "https://www.threads.net/@demo/post/abc", "火災")
        self.assertIn("台中市西屯區", parsed["text"])
        self.assertTrue(parsed["_replies"])

    def test_infer_city_district(self):
        result = infer_city_district(["臺南市永康區附近塞車"], ["永康區"])
        self.assertEqual(result["city"], "臺南市")
        self.assertEqual(result["district"], "永康區")


if __name__ == "__main__":
    unittest.main()
