import datetime
import os
import random
from dataclasses import dataclass
from typing import Literal

import feedparser
import googlemaps
import requests
from dotenv import load_dotenv

from shizumu_bot_data import FOOD_AMERICAN, FOOD_BREAKFAST, FOOD_CHINESE, FOOD_JAPANESE

load_dotenv()

Google_Map_API_key = os.getenv("GOOGLE_MAP_API_KEY")
weather_authorization = os.getenv("WEATHER_AUTHORIZATION")

MealType = Literal["breakfast", "lunch", "dinner"]

FOOD_ENDINGS = ['怎麼樣?', '好吃', ' 98', '?', '']
ALLOWED_FOOD_CLASSES = {"中式", "台式", "日式", "美式"}
CITY_INDEX_MAP = {"臺北": 16, "台北": 16, "臺中": 19, "台中": 19, "嘉義": 15, "高雄": 17, "花蓮": 11}


@dataclass
class RestaurantInfo:
    name: str
    place_id: str
    rating: float
    user_ratings_total: int
    open_now: str
    price_level: int


@dataclass
class FoodRecommendation:
    message: str
    meal_type: str
    food_class: str | None = None
    location: str | None = None
    search_food: str | None = None
    restaurant: RestaurantInfo | None = None
    maps_url: str | None = None


@dataclass
class EarthquakeReport:
    text: str
    web_url: str | None = None
    image_url: str | None = None


@dataclass
class NewsArticle:
    title: str
    url: str
    source: str


def normalize_food_class(food_class: str | None) -> str | None:
    if not food_class:
        return None
    food_class = food_class.strip()
    return food_class if food_class in ALLOWED_FOOD_CLASSES else None


def pick_food(food_class: str | None = None) -> str:
    food_class = normalize_food_class(food_class)
    if food_class in ("中式", "台式"):
        candidates = FOOD_CHINESE
    elif food_class == "日式":
        candidates = FOOD_JAPANESE
    elif food_class == "美式":
        candidates = FOOD_AMERICAN
    else:
        candidates = FOOD_JAPANESE + FOOD_AMERICAN + FOOD_CHINESE
    return random.choice(candidates)


def googlemaps_search_food(search_food: str, search_place: str) -> RestaurantInfo | None:
    if not Google_Map_API_key:
        raise RuntimeError("GOOGLE_MAP_API_KEY 未設定")

    gmaps = googlemaps.Client(key=Google_Map_API_key)
    location_info = gmaps.geocode(search_place)
    if not location_info:
        return None

    location_lat = location_info[0]['geometry']['location']['lat']
    location_lng = location_info[0]['geometry']['location']['lng']

    search_place_r = gmaps.places_nearby(
        keyword=search_food,
        location=f"{location_lat},{location_lng}",
        language='zh-TW',
        radius=1000
    )

    results: list[RestaurantInfo] = []
    for place in search_place_r.get('results', []):
        name = place.get('name')
        place_id = place.get('place_id')
        rating = place.get('rating')
        user_ratings_total = place.get('user_ratings_total')
        price_level = place.get('price_level')
        open_now_info = place.get('opening_hours')
        open_now = '營業中' if open_now_info and open_now_info.get('open_now') else '未營業'

        if None not in (name, place_id, rating, user_ratings_total, price_level):
            results.append(RestaurantInfo(
                name=name,
                place_id=place_id,
                rating=rating,
                user_ratings_total=user_ratings_total,
                open_now=open_now,
                price_level=price_level,
            ))

    high_rated = [r for r in results if r.rating > 4]
    return random.choice(high_rated) if high_rated else (random.choice(results) if results else None)


def get_food_recommendation(meal_type: str, food_class: str | None = None, location: str | None = None) -> FoodRecommendation:
    meal_type = (meal_type or "dinner").strip().lower()
    food_class = normalize_food_class(food_class)
    location = location.strip() if location else None

    if meal_type not in ("breakfast", "lunch", "dinner"):
        return FoodRecommendation(
            message="餐別請輸入 breakfast、lunch 或 dinner 喔 (´・ω・`)",
            meal_type=meal_type,
            food_class=food_class,
            location=location,
        )

    if meal_type == "breakfast":
        if random.randint(1, 100) < 2:
            message = "早餐不要吃土，再骰一次!"
        else:
            message = f"推薦早餐：{random.choice(FOOD_BREAKFAST)}{random.choice(FOOD_ENDINGS)}"
        return FoodRecommendation(message=message, meal_type=meal_type, food_class=food_class, location=location)

    if random.randint(1, 100) <= 2:
        return FoodRecommendation(message="還是吃土?", meal_type=meal_type, food_class=food_class, location=location)

    search_food = pick_food(food_class)
    if location:
        try:
            restaurant = googlemaps_search_food(search_food, location)
        except Exception as exc:
            return FoodRecommendation(
                message=f"查詢餐廳時發生錯誤：{exc}",
                meal_type=meal_type,
                food_class=food_class,
                location=location,
                search_food=search_food,
            )

        if restaurant:
            maps_url = f"https://www.google.com/maps/search/?api=1&query={search_food}&query_place_id={restaurant.place_id}"
            message = (
                f"在「{location}」附近找到一間不錯的餐廳！\n"
                f"🍽️ {restaurant.name}\n"
                f"⭐ {restaurant.rating}　👄 {restaurant.user_ratings_total} 則評論　🕓 {restaurant.open_now}　{'💵' * int(restaurant.price_level)}\n"
                f"類型：{search_food}\n"
                f"地圖連結：{maps_url}"
            )
            return FoodRecommendation(
                message=message,
                meal_type=meal_type,
                food_class=food_class,
                location=location,
                search_food=search_food,
                restaurant=restaurant,
                maps_url=maps_url,
            )

        return FoodRecommendation(
            message=f"在「{location}」附近找不到適合的 {search_food} 餐廳，要不要換個地點試試？",
            meal_type=meal_type,
            food_class=food_class,
            location=location,
            search_food=search_food,
        )

    return FoodRecommendation(
        message=f"推薦吃：{search_food}{random.choice(FOOD_ENDINGS)}",
        meal_type=meal_type,
        food_class=food_class,
        location=location,
        search_food=search_food,
    )


def get_food_recommendation_text(meal_type: str, food_class: str | None = None, location: str | None = None) -> str:
    return get_food_recommendation(meal_type, food_class, location).message


def get_earthquake_report() -> EarthquakeReport:
    try:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001?Authorization={weather_authorization}"
        eq_data = requests.get(url, timeout=10).json()
        eq = eq_data['records']['Earthquake'][0]
        return EarthquakeReport(
            text=eq['ReportContent'],
            web_url=eq.get('Web'),
            image_url=eq.get('ShakemapImageURI'),
        )
    except Exception as exc:
        return EarthquakeReport(text=f"查詢地震資訊失敗：{exc}")


def get_earthquake_info_text() -> str:
    report = get_earthquake_report()
    if report.web_url:
        return f"{report.text}\n詳細資訊：{report.web_url}"
    return report.text


def get_weather_info_text(city: str = "臺北") -> str:
    city = (city or "臺北").strip()
    try:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-091?Authorization={weather_authorization}"
        data = requests.get(url, timeout=10).json()['records']['Locations'][0]['Location']
        loc_num = CITY_INDEX_MAP.get(city)
        if loc_num is None:
            loc_num = 16
            city = "臺北"
        weather_data = data[loc_num]['WeatherElement']
        temp = weather_data[0]['Time'][0]['ElementValue'][0]['Temperature']
        rain = weather_data[11]['Time'][0]['ElementValue'][0]['ProbabilityOfPrecipitation']
        weat = weather_data[12]['Time'][0]['ElementValue'][0]['Weather']
        return f"{city}天氣：{weat}，氣溫 {temp}°C，降雨機率 {rain}%"
    except Exception as exc:
        return f"查詢天氣失敗：{exc}"


def get_weather_forecast_rows() -> list[tuple[str, str, str, str]]:
    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-091?Authorization={weather_authorization}"
    data = requests.get(url, timeout=10).json()['records']['Locations'][0]['Location']
    rows = []
    for loc_num, loc_name in zip([16, 19, 15, 17, 11], ['臺北', '臺中', '嘉義', '高雄', '花蓮']):
        weather_data = data[loc_num]['WeatherElement']
        temp = weather_data[0]['Time'][0]['ElementValue'][0]['Temperature']
        rain = weather_data[11]['Time'][0]['ElementValue'][0]['ProbabilityOfPrecipitation']
        weat = weather_data[12]['Time'][0]['ElementValue'][0]['Weather']
        rows.append((loc_name, temp, rain, weat))
    return rows


def get_headline_articles(feed_url: str = 'https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant', limit: int = 5) -> list[NewsArticle]:
    feed = feedparser.parse(feed_url)
    articles = []
    for entry in feed.entries[:limit]:
        source = getattr(getattr(entry, 'source', None), 'title', '') or ''
        title = entry.title.replace(' - ' + source, '') if source else entry.title
        articles.append(NewsArticle(title=title, url=entry.link, source=source))
    return articles


def today_taipei() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d")


def one_line_text(text: str, max_length: int = 450) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[:max_length - 1] + "…"
