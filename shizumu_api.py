import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from shizumu_services import (
    ALLOWED_FOOD_CLASSES,
    get_earthquake_info_text,
    get_food_recommendation_text,
    get_weather_info_text,
    one_line_text,
)

load_dotenv()

NIGHTBOT_API_TOKEN = os.getenv("NIGHTBOT_API_TOKEN", "").strip()

app = FastAPI(
    title="Shizumu Bot API",
    description="Plain-text endpoints for Nightbot and stream chat commands.",
    version="1.0.0",
)


def verify_api_token(request: Request, token: str | None) -> None:
    if not NIGHTBOT_API_TOKEN:
        return
    header_token = request.headers.get("x-shizumu-api-token", "")
    if token != NIGHTBOT_API_TOKEN and header_token != NIGHTBOT_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid API token")


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "OK"


@app.get("/api/food", response_class=PlainTextResponse)
async def api_food(
    request: Request,
    meal_type: str = Query("dinner", description="breakfast, lunch, or dinner"),
    food_class: str | None = Query(None, description="中式, 台式, 日式, 美式"),
    location: str | None = Query(None, description="Place name, for example 台北車站"),
    token: str | None = Query(None, description="Optional NIGHTBOT_API_TOKEN"),
) -> str:
    verify_api_token(request, token)
    meal_type = meal_type.strip().lower()
    if meal_type not in ("breakfast", "lunch", "dinner"):
        return "餐別請輸入 breakfast、lunch 或 dinner 喔 (´・ω・`)"

    if food_class and food_class.strip() not in ALLOWED_FOOD_CLASSES:
        return "料理類型請輸入中式、台式、日式或美式喔 (´・ω・`)"

    return one_line_text(get_food_recommendation_text(meal_type, food_class, location))


@app.get("/api/weather", response_class=PlainTextResponse)
async def api_weather(
    request: Request,
    city: str = Query("臺北", description="City name, for example 臺北"),
    token: str | None = Query(None, description="Optional NIGHTBOT_API_TOKEN"),
) -> str:
    verify_api_token(request, token)
    return one_line_text(get_weather_info_text(city))


@app.get("/api/earthquake", response_class=PlainTextResponse)
async def api_earthquake(
    request: Request,
    token: str | None = Query(None, description="Optional NIGHTBOT_API_TOKEN"),
) -> str:
    verify_api_token(request, token)
    return one_line_text(get_earthquake_info_text())
