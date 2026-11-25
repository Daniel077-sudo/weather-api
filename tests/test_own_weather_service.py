import unittest
from datetime import timedelta
from types import SimpleNamespace

from data import TAIWAN_LOCATIONS
from own_weather_service import CITY_WEATHER_DEFAULT_DISTRICTS, TABLE_COLUMNS, build_own_weather_answer, load_own_weather_rows, weather_default_district
from utils import taipei_now


class OwnWeatherServiceTests(unittest.TestCase):
    def tearDown(self):
        TABLE_COLUMNS.clear()

    def test_all_city_weather_default_districts_are_valid(self):
        self.assertEqual(len(CITY_WEATHER_DEFAULT_DISTRICTS), 22)
        for city, district in CITY_WEATHER_DEFAULT_DISTRICTS.items():
            with self.subTest(city=city):
                self.assertIn(city, TAIWAN_LOCATIONS)
                self.assertIn(district, TAIWAN_LOCATIONS[city])

    def test_current_answer_uses_observation_humidity_and_astro(self):
        today = taipei_now().date().isoformat()
        cache_rows = [{
            "updated_at": "2026-11-16T14:05:02.437Z",
            "valid_until": (taipei_now() + timedelta(minutes=30)).isoformat(),
            "uvi": "0",
            "aqi": "58",
            "aqi_status": "普通",
            "weather_data": {
                "current": {"description": "多雲", "pop": "20", "hum": "88", "time": "2026-11-16T02:00:00Z"},
                "obs": {
                    "station": "中西", "time": taipei_now().isoformat(), "temp": 27.4, "hum": 83,
                    "pressure": 1015.8, "rain_today": 0.5, "wind_ms": 1.2, "wind_dir": "東北風",
                    "gust_ms": 3.4, "high": 28.1, "low": 21.5, "visibility": ">30", "sunshine": 6.2,
                },
                "astro": {"sunrise": "06:12", "sunset": "17:18"},
            }
        }]

        result = build_own_weather_answer(
            "臺南市", "東區", "現在濕度多少", today, today, cache_rows, []
        )

        self.assertIsNotNone(result)
        self.assertIn("83%", result["reply"])
        self.assertEqual(result["weather_summary"]["current"]["rh"], 83)
        self.assertEqual(result["weather_summary"]["current"]["hum"], 83)
        self.assertEqual(result["weather_summary"]["current"]["humidity_source"], "observation")
        self.assertEqual(result["weather_summary"]["current"]["visibility"], ">30")
        self.assertEqual(result["weather_summary"]["current"]["fetched_at"], "2026-11-16T14:05:02.437Z")
        self.assertNotIn("forecast_at", result["weather_summary"]["current"])
        self.assertEqual(result["weather_summary"]["current"]["uvi"], "0")
        self.assertEqual(result["weather_summary"]["current"]["aqi"], "58")
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

        self.assertIn("日出 06:05", result["reply"])
        self.assertNotIn("阿里山鄉", result["reply"])

        both = build_own_weather_answer(
            "嘉義縣", "阿里山鄉", "今天日出日落幾點", today, today,
            [{"weather_data": {"astro": {
                "sunrise": "06:05", "sunset": "17:16", "dawn": "05:42", "dusk": "17:39",
            }}}], [],
        )
        self.assertIn("日出 06:05", both["reply"])
        self.assertIn("日落 17:16", both["reply"])

    def test_future_answer_uses_daily_humidity_and_apparent_range(self):
        tomorrow = (taipei_now().date() + timedelta(days=1)).isoformat()
        daily_rows = [{
            "forecast_date": tomorrow,
            "description": "晴時多雲",
            "min_t": 22,
            "max_t": 29,
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

    def test_three_day_rain_probability_lists_each_day_without_current_temperature(self):
        today = taipei_now().date()
        daily_rows = [{
            "daily": [
                {"date": (today + timedelta(days=index)).isoformat(), "description": "多雲", "min_t": 22, "max_t": 29, "pop": pop}
                for index, pop in enumerate([20, 40, 70])
            ],
            "updated_at": taipei_now().isoformat(),
        }]
        start = today.isoformat()
        end = (today + timedelta(days=2)).isoformat()

        result = build_own_weather_answer(
            "臺南市", "中西區", "未來三天的降雨機率", start, end,
            [{"valid_until": (taipei_now() + timedelta(minutes=30)).isoformat(), "weather_data": {
                "current": {"temp": 28, "pop": 10, "description": "晴"},
            }}],
            daily_rows,
        )

        self.assertIn("每日降雨機率", result["reply"])
        self.assertIn("20%", result["reply"])
        self.assertIn("40%", result["reply"])
        self.assertIn("70%", result["reply"])
        self.assertNotIn("氣溫 28", result["reply"])
        self.assertEqual(len(result["weather_summary"]["daily"]), 3)

    def test_daily_array_is_expanded(self):
        tomorrow = (taipei_now().date() + timedelta(days=1)).isoformat()
        rows = [{
            "city": "臺南市",
            "district": "東區",
            "daily": [{
                "date": tomorrow,
                "description": "多雲",
                "temp_min": 21,
                "temp_max": 28,
                "rh": 79,
                "app_min": 22,
                "app_max": 31,
            }],
        }]
        result = build_own_weather_answer("臺南市", "東區", "明天天氣", tomorrow, tomorrow, [], rows)
        self.assertIn("相對濕度約 79%", result["reply"])
        self.assertEqual(result["weather_summary"]["daily"][0]["app_min"], 22)

    def test_tomorrow_astro_and_beyond_tomorrow(self):
        today = taipei_now().date().isoformat()
        tomorrow = (taipei_now().date() + timedelta(days=1)).isoformat()
        later = (taipei_now().date() + timedelta(days=2)).isoformat()
        cache = [{"weather_data": {"astro": {
            "date": today,
            "sunrise": "06:15",
            "sunset": "17:11",
            "tomorrow": {"date": tomorrow, "sunrise": "06:16", "sunset": "17:11", "dawn": "05:53"},
        }}}]
        tomorrow_result = build_own_weather_answer("臺南市", "", "明天日出幾點", tomorrow, tomorrow, cache, [])
        self.assertIn("日出 06:16", tomorrow_result["reply"])
        self.assertIn("天亮 05:53", tomorrow_result["reply"])

        later_result = build_own_weather_answer("臺南市", "", "後天日出幾點", later, later, cache, [])
        self.assertIn("只有臺南市今天和明天", later_result["reply"])

        empty_moonset_cache = [{"weather_data": {"astro": {
            "date": today,
            "moonrise": "12:16",
            "moonset": "23:48",
            "tomorrow": {"date": tomorrow, "moonrise": "12:49", "moonset": ""},
        }}}]
        moonset_result = build_own_weather_answer(
            "臺南市", "", "明天月落幾點", tomorrow, tomorrow, empty_moonset_cache, [],
        )
        self.assertIn("沒有月落", moonset_result["reply"])
        self.assertNotIn("月出 12:49", moonset_result["reply"])

    def test_stale_observation_is_not_used(self):
        stale_time = (taipei_now() - timedelta(minutes=91)).isoformat()
        cache = [{"weather_data": {"obs": {"time": stale_time, "station": "舊站", "hum": 99}}}]
        result = build_own_weather_answer("臺南市", "東區", "現在濕度", "", "", cache, [])
        self.assertNotIn("99%", result["reply"])
        self.assertEqual(result["weather_summary"]["observation_status"], "stale")
        self.assertEqual(result["weather_summary"]["obs"], {})

    def test_expired_cache_and_daily_rows_are_not_presented_as_current(self):
        old = (taipei_now() - timedelta(hours=8)).isoformat()
        expired = (taipei_now() - timedelta(minutes=1)).isoformat()
        cache = [{
            "updated_at": old,
            "valid_until": expired,
            "weather_data": {"current": {"temp": 35, "hum": "99"}},
        }]
        daily = [{
            "updated_at": old,
            "daily": [{"date": taipei_now().date().isoformat(), "min_t": 20, "max_t": 30}],
        }]
        result = build_own_weather_answer("臺南市", "中西區", "現在天氣", "", "", cache, daily)
        self.assertEqual(result["weather_summary"]["cache_status"], "stale")
        self.assertEqual(result["weather_summary"]["daily_status"], "stale")
        self.assertNotIn("35", result["reply"])
        self.assertEqual(result["weather_summary"]["daily"], [])

    def test_unknown_time_and_missing_value_sentinels_are_not_used(self):
        unknown_time = [{"weather_data": {"obs": {"station": "舊站", "hum": 99}}}]
        result = build_own_weather_answer("臺南市", "東區", "現在濕度", "", "", unknown_time, [])
        self.assertNotIn("99%", result["reply"])
        self.assertEqual(result["weather_summary"]["observation_status"], "unknown_time")

        sentinel = [{"weather_data": {"obs": {
            "time": taipei_now().isoformat(), "station": "中西", "visibility": "-99", "hum": -99,
        }}}]
        result = build_own_weather_answer("臺南市", "東區", "能見度如何", "", "", sentinel, [])
        self.assertNotIn("-99", result["reply"])
        self.assertNotIn("visibility", result["weather_summary"]["current"])

    def test_hourly_detail_answers(self):
        active_start = (taipei_now() - timedelta(hours=1)).isoformat()
        cache = [{"weather_data": {
            "current": {"temp": 23, "app_temp": 25, "dew": 22, "comfort_desc": "舒適", "pop": 10},
            "hourly_3h": [
                {"time": active_start, "temp": 24, "humidity": 70, "app_temp": 26, "dew_point": 20},
                {"humidity": 96, "app_temp": 29, "dew_point": 23},
            ],
        }}]
        humidity = build_own_weather_answer("臺南市", "東區", "現在濕度", "", "", cache, [])
        self.assertIn("70–96%", humidity["reply"])
        feels = build_own_weather_answer("臺南市", "東區", "現在體感", "", "", cache, [])
        self.assertIn("26–29°C", feels["reply"])
        dew = build_own_weather_answer("臺南市", "東區", "露點多少", "", "", cache, [])
        self.assertIn("露點 20°C", dew["reply"])
        self.assertEqual(dew["weather_summary"]["current_hourly"]["temp"], 24)

    def test_observation_specific_answers_preserve_visibility_text(self):
        cache = [{"weather_data": {"obs": {
            "station": "中西", "time": taipei_now().isoformat(), "pressure": 1015.8,
            "rain_today": 0.5, "visibility": ">30", "vis_station": "臺南",
            "wind_ms": 1.2, "wind_dir": "東北風", "gust_ms": 3.4,
            "high": 28.1, "low": 21.5,
        }}}]
        pressure = build_own_weather_answer("臺南市", "中西區", "氣壓多少", "", "", cache, [])
        self.assertIn("1015.8 hPa", pressure["reply"])
        rainfall = build_own_weather_answer("臺南市", "中西區", "下了多少雨", "", "", cache, [])
        self.assertIn("0.5 mm", rainfall["reply"])
        visibility = build_own_weather_answer("臺南市", "中西區", "能見度如何", "", "", cache, [])
        self.assertIn(">30 公里", visibility["reply"])

        wind = build_own_weather_answer("臺南市", "中西區", "現在風速和陣風", "", "", cache, [])
        self.assertIn("東北風 1.2 m/s", wind["reply"])
        self.assertIn("陣風 3.4 m/s", wind["reply"])

        detail = build_own_weather_answer("臺南市", "中西區", "詳細天氣資料", "", "", cache, [])
        self.assertIn("氣壓 1015.8 hPa", detail["reply"])
        self.assertIn("最高 28.1°C", detail["reply"])

    def test_no_rows_returns_explicit_no_data_reply(self):
        result = build_own_weather_answer("臺北市", "", "天氣", "", "", [], [])
        self.assertIn("目前讀不到", result["reply"])
        self.assertEqual(result["weather_summary"]["observation_status"], "missing")

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

            def ilike(self, key, value):
                self.filters.append((key, value.rstrip("%")))
                return self

            def order(self, *_args, **_kwargs):
                return self

            def limit(self, value):
                self.row_limit = value
                return self

            def execute(self):
                rows = [
                    row for row in self.rows
                    if all(
                        str(row.get(key) or "").startswith(value)
                        if key == "city_name" and value.endswith(("市", "縣"))
                        else row.get(key) == value
                        for key, value in self.filters
                    )
                ]
                return SimpleNamespace(data=rows[:self.row_limit])

        class Client:
            tables = {
                "weather_cache_own": [{
                    "city_name": "臺南市東區",
                    "updated_at": "2026-11-16T10:00:00+08:00",
                    "weather_data": {"obs": {"rh": 80}},
                }, {
                    "city_name": "臺南市安平區",
                    "updated_at": "2026-11-16T10:00:00+08:00",
                    "weather_data": {"current": {"hum": "87"}},
                }],
                "weather_daily_own": [{
                    "city_name": "臺南市東區",
                    "forecast_date": "2026-11-17",
                    "rh": 75,
                    "app_min": 24,
                    "app_max": 31,
                }, {
                    "city_name": "臺南市安平區",
                    "forecast_date": "2026-11-17",
                    "rh": 87,
                    "app_min": 23,
                    "app_max": 30,
                }],
            }

            def table(self, name):
                return Query(self.tables[name])

        cache, daily = load_own_weather_rows("臺南市", "東區", Client())
        self.assertEqual(cache[0]["weather_data"]["obs"]["rh"], 80)
        self.assertEqual(daily[0]["app_max"], 31)

        default_cache, default_daily = load_own_weather_rows("臺南市", "", Client())
        self.assertEqual(weather_default_district("臺南市"), "安平區")
        self.assertEqual(default_cache[0]["city_name"], "臺南市安平區")
        self.assertEqual(default_daily[0]["rh"], 87)


if __name__ == "__main__":
    unittest.main()
