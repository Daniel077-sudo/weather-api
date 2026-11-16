import unittest
from datetime import timedelta
from types import SimpleNamespace

from own_weather_service import TABLE_COLUMNS, build_own_weather_answer, load_own_weather_rows
from utils import taipei_now


class OwnWeatherServiceTests(unittest.TestCase):
    def tearDown(self):
        TABLE_COLUMNS.clear()

    def test_current_answer_uses_observation_humidity_and_astro(self):
        today = taipei_now().date().isoformat()
        cache_rows = [{
            "weather_data": {
                "current": {"description": "多雲", "pop": 20},
                "obs": {"temp": 27.4, "rh": 83, "app_temp": 30},
                "astro": {"sunrise": "06:12", "sunset": "17:18"},
            }
        }]

        result = build_own_weather_answer(
            "臺南市", "東區", "現在濕度多少", today, today, cache_rows, []
        )

        self.assertIsNotNone(result)
        self.assertIn("83%", result["reply"])
        self.assertEqual(result["weather_summary"]["current"]["rh"], 83)
        self.assertEqual(result["weather_summary"]["obs"]["temp"], 27.4)
        self.assertEqual(result["weather_summary"]["astro"]["sunset"], "17:18")

    def test_sunrise_question_uses_astro(self):
        today = taipei_now().date().isoformat()
        result = build_own_weather_answer(
            "嘉義縣",
            "阿里山鄉",
            "阿里山日出幾點",
            today,
            today,
            [{"weather_data": {"astro": {"sunrise": "06:05", "sunset": "17:16"}}}],
            [],
        )

        self.assertEqual(result["reply"], "嘉義縣阿里山鄉日出時間為 06:05。")

    def test_future_answer_uses_daily_humidity_and_apparent_range(self):
        tomorrow = (taipei_now().date() + timedelta(days=1)).isoformat()
        daily_rows = [{
            "forecast_date": tomorrow,
            "description": "晴時多雲",
            "temp_min": 22,
            "temp_max": 29,
            "rh": 76,
            "app_min": 23,
            "app_max": 32,
            "pop": 20,
        }]

        result = build_own_weather_answer(
            "高雄市", "苓雅區", "明天天氣", tomorrow, tomorrow, [], daily_rows
        )

        self.assertIn("相對濕度約 76%", result["reply"])
        self.assertIn("體感 23–32°C", result["reply"])
        self.assertEqual(result["weather_summary"]["daily"][0]["app_max"], 32)

    def test_no_rows_keeps_existing_entity_only_behavior(self):
        result = build_own_weather_answer("臺北市", "", "天氣", "", "", [], [])
        self.assertIsNone(result)

    def test_table_reader_detects_city_column_variants(self):
        class Query:
            def __init__(self, rows):
                self.rows = list(rows)
                self.filters = []
                self.row_limit = None

            def select(self, *_args):
                return self

            def eq(self, key, value):
                self.filters.append((key, value))
                return self

            def order(self, *_args, **_kwargs):
                return self

            def limit(self, value):
                self.row_limit = value
                return self

            def execute(self):
                rows = [row for row in self.rows if all(row.get(key) == value for key, value in self.filters)]
                return SimpleNamespace(data=rows[:self.row_limit])

        class Client:
            tables = {
                "weather_cache_own": [{
                    "city_name": "臺南市",
                    "district_name": "東區",
                    "updated_at": "2026-11-16T10:00:00+08:00",
                    "weather_data": {"obs": {"rh": 80}},
                }],
                "weather_daily_own": [{
                    "city": "臺南市",
                    "district": "東區",
                    "forecast_date": "2026-11-17",
                    "rh": 75,
                    "app_min": 24,
                    "app_max": 31,
                }],
            }

            def table(self, name):
                return Query(self.tables[name])

        cache, daily = load_own_weather_rows("臺南市", "東區", Client())
        self.assertEqual(cache[0]["weather_data"]["obs"]["rh"], 80)
        self.assertEqual(daily[0]["app_max"], 31)


if __name__ == "__main__":
    unittest.main()
