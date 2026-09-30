import unittest
from unittest.mock import patch

from shizumu_services import get_weather_forecast_rows, get_weather_info_text


def weather_location(temperature="25", rain="30", weather="多雲"):
    elements = [{"Time": [{"ElementValue": [{}]}]} for _ in range(13)]
    elements[0]["Time"][0]["ElementValue"][0]["Temperature"] = temperature
    elements[11]["Time"][0]["ElementValue"][0]["ProbabilityOfPrecipitation"] = rain
    elements[12]["Time"][0]["ElementValue"][0]["Weather"] = weather
    return {"WeatherElement": elements}


class WeatherServiceTests(unittest.TestCase):
    def setUp(self):
        self.locations = [weather_location() for _ in range(20)]
        self.locations[16] = weather_location("24", "20", "晴時多雲")
        self.locations[19] = weather_location("28", "40", "短暫雨")

    def test_city_weather_and_unknown_city_fallback(self):
        with patch("shizumu_services._get_weather_locations", return_value=self.locations):
            self.assertEqual(get_weather_info_text("臺中"), "臺中天氣：短暫雨，氣溫 28°C，降雨機率 40%")
            self.assertEqual(get_weather_info_text("未知"), "臺北天氣：晴時多雲，氣溫 24°C，降雨機率 20%")

    def test_forecast_rows_keep_city_order(self):
        with patch("shizumu_services._get_weather_locations", return_value=self.locations):
            rows = get_weather_forecast_rows()
        self.assertEqual([row[0] for row in rows], ["臺北", "臺中", "嘉義", "高雄", "花蓮"])
        self.assertEqual(rows[0], ("臺北", "24", "20", "晴時多雲"))


if __name__ == "__main__":
    unittest.main()
