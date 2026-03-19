import asyncio
import os
import datetime
import random
import json
import pathlib
import requests
import time
from collections import deque

import feedparser
import googlemaps
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from discord.ext.commands import CommandNotFound
import google.generativeai as genai
from google.generativeai import types as genai_types
from shizumu_bot_data import food_a, food_j, food_c, food_b, shizumu_murmur

# ================================
# 環境變數載入
# ================================
load_dotenv()

Google_Map_API_key = os.getenv("GOOGLE_MAP_API_KEY")
Discord_token = os.getenv("DISCORD_TOKEN")
weather_authorization = os.getenv("WEATHER_AUTHORIZATION")
Google_AI_API_key = os.getenv("GOOGLE_AI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# ================================
# API 用量限制設定
# ================================
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_AI_REQUESTS_PER_DAY", 50))   # 每位使用者每日上限
COOLDOWN_SECONDS = int(os.getenv("AI_COOLDOWN_SECONDS", 5))             # 每次請求冷卻秒數

# 每位使用者的每日計數器：{ user_id: {"date": date, "count": int} }
_user_api_usage: dict[str, dict] = {}
# 每位使用者的上次請求時間：{ user_id: float }
_last_request_time: dict[str, float] = {}


def check_api_limit(user_id: str) -> tuple[bool, str]:
    """
    檢查該使用者是否超過用量限制。
    回傳 (是否允許, 錯誤訊息)
    """
    today = datetime.date.today()

    # 初始化或每日重置
    if user_id not in _user_api_usage or _user_api_usage[user_id]["date"] != today:
        _user_api_usage[user_id] = {"date": today, "count": 0}

    # 檢查每日個人上限
    if _user_api_usage[user_id]["count"] >= MAX_REQUESTS_PER_DAY:
        return False, f"你今天已經跟我聊了 {MAX_REQUESTS_PER_DAY} 次了，明天再來找我吧 (´・ω・`)"

    # 檢查冷卻時間
    last_time = _last_request_time.get(user_id, 0)
    elapsed = time.time() - last_time
    if elapsed < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - elapsed) + 1
        return False, f"請稍等 {remaining} 秒後再傳訊息喔 (｡･∀･)"

    return True, ""


def record_api_usage(user_id: str):
    """記錄一次 API 使用"""
    today = datetime.date.today()
    if user_id not in _user_api_usage or _user_api_usage[user_id]["date"] != today:
        _user_api_usage[user_id] = {"date": today, "count": 0}
    _user_api_usage[user_id]["count"] += 1
    _last_request_time[user_id] = time.time()


# ================================
# Gemini AI 設定
# ================================

SYSTEM_PROMPT = """妳是 Shizumu doro，綽號是小寒，一個可愛、友善但有點懶散的 Discord 機器人助手。
妳的個性溫和，喜歡用顏文字。
妳的創造者(爸爸)是地瓜YA，外觀形象(媽媽)是靜靜子，是個可憐的社畜，常常想加薪。
妳興趣是玩遊戲與動漫，擁有各項ACG知識。
妳會用繁體中文(台灣)進行對話。
回覆時不要過於冗長，回話長度大約維持在簡短的一至兩句之間，保持自然的對話節奏。"""

# 設定觸發「記憶濃縮」的對話輪數（例如 10 輪，即 20 條訊息）
SUMMARY_THRESHOLD = 10
MAX_SHARED_FACTS = 50  # 共享記憶的最大條數，超過時會刪除最舊的

# 短期對話歷史（記憶體）
chat_histories: dict[str, deque] = {}

# 持久化記憶檔案
MEMORY_FILE = pathlib.Path("memory.json")

# 共享記憶（持久化）
_shared_memory: dict = {"facts": [], "updated": ""}

# 個人長期摘要（持久化）
_personal_summaries: dict[str, dict] = {}


# ================================
# 記憶管理
# ================================

def load_memories():
    """Bot 啟動時從 JSON 載入所有持久化記憶"""
    global _shared_memory, _personal_summaries
    if MEMORY_FILE.exists():
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        _shared_memory = data.get("shared", {"facts": [], "updated": ""})
        _personal_summaries = data.get("personal", {})
        print(f"[記憶] 已載入共享記憶 {len(_shared_memory['facts'])} 條，個人摘要 {len(_personal_summaries)} 位")


def save_memories():
    """將記憶持久化寫入 JSON"""
    data = {
        "shared": _shared_memory,
        "personal": _personal_summaries
    }
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_shared_fact(fact: str):
    """新增一條共享記憶，超過上限時移除最舊的"""
    _shared_memory["facts"].append(fact)
    if len(_shared_memory["facts"]) > MAX_SHARED_FACTS:
        _shared_memory["facts"].pop(0)
    _shared_memory["updated"] = str(datetime.date.today())
    save_memories()


def _bigram_relevant(fact: str, user_message: str) -> bool:
    """用 bigram 判斷共享記憶條目是否與使用者訊息相關"""
    def bigrams(text: str) -> set:
        return {text[i:i+2] for i in range(len(text) - 1)}
    return bool(bigrams(fact) & bigrams(user_message))


def get_shared_memory_prompt(user_message: str = "") -> str:
    """將共享記憶組合成注入 prompt 的字串，若提供 user_message 則只注入相關條目"""
    if not _shared_memory["facts"]:
        return ""
    
    if user_message:
        selected = [f for f in _shared_memory["facts"] if _bigram_relevant(f, user_message)]
    else:
        selected = _shared_memory["facts"]
    
    if not selected:
        return ""
    
    total = len(_shared_memory["facts"])
    injected = len(selected)
    facts_text = "\n".join(f"- {f}" for f in selected)
    suffix = f"（已依相關性篩選 {injected}/{total} 條）" if user_message else f"（共 {total} 條）"
    return f"【共享記憶：這是所有使用者共同建立的資訊{suffix}，請記住】\n{facts_text}"


def save_personal_summary(user_id: str, summary: str):
    """儲存個人長期摘要"""
    _personal_summaries[user_id] = {
        "summary": summary,
        "updated": str(datetime.date.today())
    }
    save_memories()


def get_personal_summary(user_id: str) -> str | None:
    """取得個人長期摘要"""
    return _personal_summaries.get(user_id, {}).get("summary")


def get_gemini_response(user_id: str, user_name: str, message: str) -> str:
    """
    取得 Gemini 回應。
    上下文注入順序：共享記憶 → 個人長期摘要 → 近期對話
    """
    genai.configure(api_key=Google_AI_API_key)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT,
        tools=_TOOLS  # ← 注入工具定義
    )

    # 1. 初始化個人短期對話歷史
    if user_id not in chat_histories:
        chat_histories[user_id] = deque(maxlen=50)

    history = chat_histories[user_id]

    # 2. 觸發個人記憶濃縮
    if len(history) >= SUMMARY_THRESHOLD * 2:
        try:
            summary_model = genai.GenerativeModel(
                model_name=GEMINI_MODEL,
                system_instruction=SYSTEM_PROMPT
            )
            temp_chat = summary_model.start_chat(history=list(history))

            existing_summary = get_personal_summary(user_id)
            if existing_summary:
                summary_prompt = (
                    "【系統指令】以下是這位使用者的舊摘要與最新對話紀錄。\n"
                    "請將兩者合併，整理成一份新的結構化摘要，格式如下：\n"
                    "- 使用者名稱：\n"
                    "- 使用者喜好/特徵：\n"
                    "- 重要話題摘要：\n"
                    f"【舊摘要】\n{existing_summary}\n\n"
                    "請注意：最終摘要必須嚴格控制在 500 字以內，刪去不重要的細節，保留關鍵資訊。"
                )
            else:
                summary_prompt = (
                    "【系統指令】請用繁體中文，將以上對話整理成結構化摘要，格式如下：\n"
                    "- 使用者名稱：\n"
                    "- 使用者喜好/特徵：\n"
                    "- 重要話題摘要：\n"
                    "全部控制在 500 字內。"
                )

            summary_response = temp_chat.send_message(summary_prompt)
            summary_text = summary_response.text

            if len(summary_text) > 500:
                summary_text = summary_text[:500]

            save_personal_summary(user_id, summary_text)

            history.clear()
            history.append({"role": "user", "parts": f"【系統提示：這是我們之前的對話摘要，請記住這些資訊】\n{summary_text}"})
            history.append({"role": "model", "parts": "好的，我已經牢牢記住這些摘要資訊了！(｡･∀･)ﾉﾞ 請問接下來要聊什麼呢？"})

        except Exception as e:
            print(f"記憶濃縮失敗: {e}")
            history.popleft()
            history.popleft()

    # 3. 組合注入 prompt
    injected_history = []

    shared_prompt = get_shared_memory_prompt(user_message=message)
    if shared_prompt:
        injected_history.append({"role": "user", "parts": shared_prompt})
        injected_history.append({"role": "model", "parts": "好的，我記住這些共享資訊了 (｡･∀･)"})

    if len(history) == 0:
        personal_summary = get_personal_summary(user_id)
        if personal_summary:
            injected_history.append({"role": "user", "parts": f"【系統提示：這是我們之前的對話摘要，請記住這些資訊】\n{personal_summary}"})
            injected_history.append({"role": "model", "parts": "好的，我記住你的個人資訊了 (｡･∀･)ﾉﾞ"})

    injected_history.extend(list(history))

    # 4. 進行對話
    chat = model.start_chat(history=injected_history)
    is_new_chat = len(history) == 0
    full_message = f"（使用者名稱：{user_name}）\n{message}" if is_new_chat else message

    response = chat.send_message(full_message)

    # 5. 處理 Function Calling
    reply = _handle_function_calls(chat, response)

    # 6. 儲存本輪對話到短期記憶
    history.append({"role": "user", "parts": full_message})
    history.append({"role": "model", "parts": reply})

    return reply


# ================================
# Function Calling 工具定義
# ================================

_TOOLS = [
    genai_types.Tool(function_declarations=[
        genai_types.FunctionDeclaration(
            name="get_food_recommendation",
            description="推薦餐點或餐廳。當使用者詢問吃什麼、推薦食物、早餐、午餐、晚餐時使用。",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "meal_type": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="餐別：breakfast（早餐）、lunch（午餐）、dinner（晚餐）",
                        enum=["breakfast", "lunch", "dinner"]
                    ),
                    "food_class": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="料理類型：中式、台式、日式、美式，若使用者未指定則省略此參數",
                        enum=["中式", "台式", "日式", "美式"]
                    ),
                    "location": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="地點名稱，若使用者有明確指定地點才填入，例如：台北、信義區"
                    )
                },
                required=["meal_type"]
            )
        ),
        genai_types.FunctionDeclaration(
            name="get_earthquake_info",
            description="取得最新地震資訊。當使用者詢問地震、有沒有在搖、有沒有地震時使用。",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={}
            )
        ),
        genai_types.FunctionDeclaration(
            name="get_weather_info",
            description="取得天氣預報。當使用者詢問天氣、下雨、溫度、要不要帶傘時使用。",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "city": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="城市名稱，例如：臺北、臺中、嘉義、高雄、花蓮，若未指定預設臺北"
                    )
                }
            )
        )
    ])
]


# ================================
# Function Calling 執行邏輯
# ================================

def _execute_get_food_recommendation(meal_type: str, food_class: str = None, location: str = None) -> str:
    ending_list = ['怎麼樣?', '好吃', ' 98', '?', '']

    if meal_type == "breakfast":
        if random.randint(1, 100) < 2:
            return "早餐不要吃土，再骰一次!"
        return f"推薦早餐：{random.choice(food_b)}{random.choice(ending_list)}"

    # 2% 機率吃土
    if random.randint(1, 100) <= 2:
        return "還是吃土?"

    if food_class in ("中式", "台式"):
        candidates = food_c
    elif food_class == "日式":
        candidates = food_j
    elif food_class == "美式":
        candidates = food_a
    else:
        candidates = food_j + food_a + food_c

    search_food = random.choice(candidates)

    if location:
        try:
            name, place_id, rating, total, open_now, price = googlemaps_search_food(search_food, location)
            if name:
                maps_url = f"https://www.google.com/maps/search/?api=1&query={search_food}&query_place_id={place_id}"
                return (
                    f"在「{location}」附近找到一間不錯的餐廳！\n"
                    f"🍽️ {name}\n"
                    f"⭐ {rating}　👄 {total} 則評論　🕓 {open_now}　{'💵' * int(price)}\n"
                    f"類型：{search_food}\n"
                    f"地圖連結：{maps_url}"
                )
            else:
                return f"在「{location}」附近找不到適合的 {search_food} 餐廳，要不要換個地點試試？"
        except Exception as e:
            return f"查詢餐廳時發生錯誤：{e}"

    return f"推薦吃：{search_food}{random.choice(ending_list)}"


def _execute_get_earthquake_info() -> str:
    try:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001?Authorization={weather_authorization}"
        eq_data = requests.get(url).json()
        eq = eq_data['records']['Earthquake'][0]
        return (
            f"{eq['ReportContent']}\n"
            f"詳細資訊：{eq['Web']}"
        )
    except Exception as e:
        return f"查詢地震資訊失敗：{e}"


def _execute_get_weather_info(city: str = "臺北") -> str:
    city_index_map = {"臺北": 16, "台北": 16, "臺中": 19, "台中": 19, "嘉義": 15, "高雄": 17, "花蓮": 11}
    try:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-091?Authorization={weather_authorization}"
        data = requests.get(url).json()['records']['Locations'][0]['Location']
        loc_num = city_index_map.get(city)
        if loc_num is None:
            # 若找不到對應城市，使用臺北作為預設城市，避免出現「顯示城市 != 實際資料來源」的情況
            loc_num = 16
            city = "臺北"
        weather_data = data[loc_num]['WeatherElement']
        temp = weather_data[0]['Time'][0]['ElementValue'][0]['Temperature']
        rain = weather_data[11]['Time'][0]['ElementValue'][0]['ProbabilityOfPrecipitation']
        weat = weather_data[12]['Time'][0]['ElementValue'][0]['Weather']
        return f"{city}天氣：{weat}，氣溫 {temp}°C，降雨機率 {rain}%"
    except Exception as e:
        return f"查詢天氣失敗：{e}"


_TOOL_HANDLERS = {
    "get_food_recommendation": lambda args: _execute_get_food_recommendation(**args),
    "get_earthquake_info":     lambda args: _execute_get_earthquake_info(),
    "get_weather_info":        lambda args: _execute_get_weather_info(**args),
}


def _handle_function_calls(chat, response) -> str:
    """
    處理 Gemini 的 Function Call 回應。
    Gemini 可能連續要求多次 function call，迴圈處理直到得到純文字回覆。
    """
    MAX_ROUNDS = 5

    for _ in range(MAX_ROUNDS):
        fn_calls = [
            part.function_call
            for part in response.candidates[0].content.parts
            if hasattr(part, "function_call") and part.function_call.name
        ]

        if not fn_calls:
            return response.text

        fn_results = []
        for fn_call in fn_calls:
            fn_name = fn_call.name
            fn_args = dict(fn_call.args)
            print(f"[Function Call] {fn_name}({fn_args})")

            handler = _TOOL_HANDLERS.get(fn_name)
            result = handler(fn_args) if handler else f"未知的工具：{fn_name}"
            print(f"[Function Result] {result}")

            fn_results.append(
                genai_types.Part.from_function_response(
                    name=fn_name,
                    response={"result": result}
                )
            )

        response = chat.send_message(fn_results)

    return response.text


# ================================
# Discord Bot 設定
# ================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='', intents=intents, help_command=None)


# ================================
# Google Maps 推薦餐廳
# ================================
def googlemaps_search_food(search_food, search_place):
    gmaps = googlemaps.Client(key=Google_Map_API_key)
    location_info = gmaps.geocode(search_place)
    location_lat = location_info[0]['geometry']['location']['lat']
    location_lng = location_info[0]['geometry']['location']['lng']

    search_place_r = gmaps.places_nearby(
        keyword=search_food,
        location=f"{location_lat},{location_lng}",
        language='zh-TW',
        radius=1000
    )

    results = []
    for place in search_place_r.get('results', []):
        name = place.get('name')
        place_id = place.get('place_id')
        rating = place.get('rating')
        user_ratings_total = place.get('user_ratings_total')
        price_level = place.get('price_level')
        open_now_info = place.get('opening_hours')
        open_now = '營業中' if open_now_info and open_now_info.get('open_now') else '未營業'

        if None not in (name, place_id, rating, user_ratings_total, price_level):
            results.append({
                'name': name,
                'place_id': place_id,
                'rating': rating,
                'user_ratings_total': user_ratings_total,
                'open_now': open_now,
                'price_level': price_level
            })

    high_rated = [r for r in results if r['rating'] > 4]
    selected = random.choice(high_rated) if high_rated else (random.choice(results) if results else None)

    if not selected:
        return None, None, None, None, None, None

    return (
        selected['name'],
        selected['place_id'],
        selected['rating'],
        selected['user_ratings_total'],
        selected['open_now'],
        selected['price_level']
    )


#################################################################################################################################################


# [自動更新狀態]
@tasks.loop(seconds=15)
async def activity_auto_change():
    status_w = discord.Status.online
    activity_w = discord.Activity(type=discord.ActivityType.playing, name=random.choice(shizumu_murmur))
    await bot.change_presence(status=status_w, activity=activity_w)


# [啟動]
@bot.event
async def on_ready():
    print('目前登入身份：', bot.user)

    if Google_AI_API_key:
        print(f"Gemini AI 已啟用，模型：{GEMINI_MODEL}")
    else:
        print("GOOGLE_AI_API_KEY 未設定，AI 對話功能將停用")

    load_memories()
    activity_auto_change.start()


# [新進成員]
@bot.event
async def on_member_join(member):
    if member.guild.id == 1292873644950683658:
        channel = bot.get_channel(1292873645794005013)
        await channel.send("https://i.imgur.com/V6kdDTx.jpg")
        await channel.send(f"{member.mention} 歡迎~麻煩剛加入的晚餐們，要記得幫忙把DC的ID改成跟YT一樣的喔，這樣好讓我們認識您，謝謝唷!")


# [指令]
@bot.command()
async def shizumu說(ctx, *, arg):
    if int(ctx.message.author.id) == 378936265657286659 or int(ctx.message.author.id) == 343984138983964684:
        await ctx.message.delete()
        await ctx.send(arg)


# [指令] 新聞
@bot.command()
async def 新聞(ctx):
    d = feedparser.parse('https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant')
    n_title = [i.title for i in d.entries]
    source_name_list = [i.source.title for i in d.entries]
    title_list = [t.replace(' - ' + s, '') for t, s in zip(n_title, source_name_list)]
    url_list = [i.link for i in d.entries]
    embed = discord.Embed(title='頭條新聞', description=(datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x7e6487)
    for title, url, source in zip(title_list[:5], url_list[:5], source_name_list[:5]):
        embed.add_field(name=title, value='[' + source + '](' + url + ')', inline=False)
    news_message = await ctx.send('晚餐日報 ' + (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), embed=embed)
    for emoji in ['📰', '🎮', '🌤']:
        await news_message.add_reaction(emoji)


@bot.event
async def on_raw_reaction_add(payload):
    if payload.member.bot:
        return
    channel = bot.get_channel(payload.channel_id)
    news_message = await channel.fetch_message(payload.message_id)
    emoji = payload.emoji

    if news_message.content == '晚餐日報 ' + (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d"):
        if emoji.name == "📰":
            d = feedparser.parse('https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant')
            n_title = [i.title for i in d.entries]
            source_name_list = [i.source.title for i in d.entries]
            title_list = [t.replace(' - ' + s, '') for t, s in zip(n_title, source_name_list)]
            url_list = [i.link for i in d.entries]
            google_embed = discord.Embed(title='頭條新聞', description=(datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x598ad9)
            for title, url, source in zip(title_list[:5], url_list[:5], source_name_list[:5]):
                google_embed.add_field(name=title, value='[' + source + '](' + url + ')', inline=False)
            await news_message.edit(embed=google_embed)

        elif emoji.name == "🎮":
            d = feedparser.parse('https://gnn.gamer.com.tw/rss.xml')
            title_list = [i.title for i in d.entries]
            url_list = [i.link for i in d.entries]
            gnn_embed = discord.Embed(title='巴哈姆特 GNN 新聞', description=(datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x598ad9)
            for title, url in zip(title_list[:5], url_list[:5]):
                gnn_embed.add_field(name=title, value='[巴哈姆特](' + url + ')', inline=False)
            await news_message.edit(embed=gnn_embed)

        elif emoji.name == "🌤":
            url = 'https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-091?Authorization=' + weather_authorization
            r = requests.get(url)
            data = r.json()['records']['Locations'][0]['Location']
            weather_embed = discord.Embed(title='天氣預報 ', description=(datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x598ad9)
            for loc_num, loc_name in zip([16, 19, 15, 17, 11], ['臺北', '臺中', '嘉義', '高雄', '花蓮']):
                weather_data = data[loc_num]['WeatherElement']
                temp = weather_data[0]['Time'][0]['ElementValue'][0]['Temperature']
                rain = weather_data[11]['Time'][0]['ElementValue'][0]['ProbabilityOfPrecipitation']
                weat = weather_data[12]['Time'][0]['ElementValue'][0]['Weather']
                weather_embed.add_field(name=loc_name, value='☂' + rain + '%  🌡' + temp + '°C  ⛅' + weat, inline=False)
            await news_message.edit(embed=weather_embed)


# [指令] 地震
@bot.command()
async def 地震(ctx, *args):
    url = 'https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001?Authorization=' + weather_authorization
    eq_data = requests.get(url).json()
    eq_content = eq_data['records']['Earthquake'][0]['ReportContent']
    eq_image = eq_data['records']['Earthquake'][0]['ShakemapImageURI']
    ed_url = eq_data['records']['Earthquake'][0]['Web']
    embed = discord.Embed(title=eq_content, url=ed_url, color=0x636363)
    embed.set_image(url=eq_image)
    await ctx.send(embed=embed)


# [指令] 午/晚餐吃什麼
@bot.command(aliases=['午餐吃什麼'])
async def 晚餐吃什麼(ctx, *args):
    ending_list = ['怎麼樣?', '好吃', ' 98', '?', '']
    if len(args) == 0:
        eat_dust = random.randint(1, 100)
        if eat_dust <= 2:
            await ctx.send('還是吃土?')
        else:
            eat_class = random.randint(1, 2)
            if eat_class == 1:
                await ctx.send(random.choice(food_c) + random.choice(ending_list))
            if eat_class == 2:
                await ctx.send(random.choice(food_j + food_a) + random.choice(ending_list))
    elif len(args) == 1 and '式' in args[0]:
        food_class = args[0]
        if food_class == '中式' or food_class == '台式':
            await ctx.send(random.choice(food_c) + random.choice(ending_list))
        elif food_class == '日式':
            await ctx.send(random.choice(food_j) + random.choice(ending_list))
        elif food_class == '美式':
            await ctx.send(random.choice(food_a) + random.choice(ending_list))
        else:
            await ctx.send('我不知道' + food_class + '料理有哪些，請輸入中/台式、日式或美式 º﹃º')
    elif len(args) == 1 and '式' not in args[0]:
        search_food = random.choice(food_j + food_a + food_c)
        search_place = args[0]
        try:
            restaurant = googlemaps_search_food(search_food, search_place)
            embed = discord.Embed(
                title=restaurant[0],
                description='⭐' + str(restaurant[2]) + '  👄' + str(restaurant[3]) + '  🕓' + str(restaurant[4]) + '  ' + '💵' * int(restaurant[5]),
                url='https://www.google.com/maps/search/?api=1&query=' + search_food + '&query_place_id=' + restaurant[1]
            )
            embed.set_author(name=search_food + random.choice(ending_list))
            await ctx.send(embed=embed)
        except:
            await ctx.send('在' + search_place + '找不到適合的' + search_food + '餐廳，請再重新輸入一遍或換個地點名稱><')
    elif len(args) == 2 and ('中式' in args[0] or '台式' in args[0] or '日式' in args[0] or '美式' in args[0]):
        food_class = args[0]
        search_place = args[1]
        if food_class == '中式' or food_class == '台式':
            search_food = random.choice(food_c)
        elif food_class == '日式':
            search_food = random.choice(food_j)
        elif food_class == '美式':
            search_food = random.choice(food_a)
        try:
            restaurant = googlemaps_search_food(search_food, search_place)
            embed = discord.Embed(
                title=restaurant[0],
                description='⭐' + str(restaurant[2]) + '  👄' + str(restaurant[3]) + '  🕓' + str(restaurant[4]) + '  ' + '💵' * int(restaurant[5]),
                url='https://www.google.com/maps/search/?api=1&query=' + search_food + '&query_place_id=' + restaurant[1]
            )
            embed.set_author(name=search_food + random.choice(ending_list))
            await ctx.send(embed=embed)
        except:
            await ctx.send('在' + search_place + '找不到適合的' + search_food + '餐廳，請再重新輸入一遍或換個地點名稱><')
    else:
        await ctx.send('確認一下指令是否正確: ```午餐吃什麼 [中式/台式/日式/美式] [地點]``` 參數皆可省略')


# [指令] 早餐吃什麼
@bot.command()
async def 早餐吃什麼(ctx, *args):
    ending_list = ['怎麼樣?', '好吃', ' 98', '?', '']
    if len(args) == 0:
        eat_dust = random.randint(1, 100)
        if eat_dust < 2:
            await ctx.send('早餐不要吃土，再骰一次!')
        else:
            await ctx.send(random.choice(food_b) + random.choice(ending_list))


# [NSFW指令] 色色
class_list_nsfw = ['waifu', 'neko', 'blowjob']
@commands.is_nsfw()
@bot.command(aliases=['hentai', 'エロ'])
async def 色色(ctx):
    random_nsfw_class = random.choice(class_list_nsfw)
    nsfw_res = requests.get('https://api.waifu.pics/nsfw/' + random_nsfw_class, headers={"User-Agent": "Defined"}, verify=False)
    nsfw_pic = json.loads(nsfw_res.text)['url']
    embed = discord.Embed(color=0xf1c40f)
    embed.set_image(url=nsfw_pic)
    await ctx.send(embed=embed)


# ================================
# Gemini AI 對話
# ================================

async def _handle_ai_chat(ctx, message_content: str):
    """處理 AI 對話的核心邏輯"""
    if not Google_AI_API_key:
        await ctx.send("對話功能未啟用，問問看地瓜YA怎麼了 (´・ω・`)")
        return

    user_id = str(ctx.author.id)

    # 檢查用量限制
    allowed, error_msg = check_api_limit(user_id)
    if not allowed:
        await ctx.send(error_msg)
        return

    async with ctx.typing():
        try:
            record_api_usage(user_id)
            reply = await asyncio.get_event_loop().run_in_executor(
                None,
                get_gemini_response,
                user_id,
                ctx.author.name,
                message_content
            )
            if len(reply) > 2000:
                for chunk in [reply[i:i+2000] for i in range(0, len(reply), 2000)]:
                    await ctx.send(chunk)
            else:
                await ctx.send(reply)

        except Exception as e:
            print(f"AI 對話錯誤: {e}")
            await ctx.send(f"欸欸地瓜，有bug你看一下！`{str(e)}`")


# [指令] 小寒 - 與 Gemini 對話
@bot.command(aliases=['shizumu_doro', 'shizumudoro'])
async def 小寒(ctx, *, message_content: str):
    await _handle_ai_chat(ctx, message_content)


# [指令] 重置記憶
@bot.command(aliases=['重置記憶'])
async def reset_memory(ctx):
    """清除您與小寒的對話歷史（包含持久化的個人摘要）"""
    user_id = str(ctx.author.id)
    cleared = []
    if user_id in chat_histories:
        chat_histories.pop(user_id)
        cleared.append("短期對話歷史")
    if user_id in _personal_summaries:
        _personal_summaries.pop(user_id)
        save_memories()
        cleared.append("個人長期摘要")
    if cleared:
        await ctx.send(f"已清除：{'、'.join(cleared)}，下次聊天將重新開始 (｡･∀･)ﾉﾞ")
    else:
        await ctx.send("你還沒跟我說過話喔 (´・ω・`)")


# [指令] 新增共享記憶（限管理員）
ADMIN_IDS = [378936265657286659, 343984138983964684]

@bot.command(aliases=['記住這個', '共享記憶'])
async def add_memory(ctx, *, fact: str):
    """新增一條所有人都能用到的共享記憶（限管理員）"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("只有管理員才能新增共享記憶喔 (´・ω・`)")
        return
    add_shared_fact(fact)
    await ctx.send(f"好的，我記住了！目前共享記憶共 {len(_shared_memory['facts'])} 條 (｡･∀･)ﾉﾞ")


# [指令] 查看共享記憶列表
@bot.command(aliases=['共享記憶列表'])
async def list_memory(ctx):
    """查看目前的共享記憶列表"""
    if not _shared_memory["facts"]:
        await ctx.send("目前沒有共享記憶喔 (´・ω・`)")
        return

    facts = _shared_memory["facts"]
    max_fields = 25  # Discord 每個 embed 最多 25 個欄位
    total = len(facts)
    total_pages = (total + max_fields - 1) // max_fields

    for page_index in range(total_pages):
        start = page_index * max_fields
        end = start + max_fields
        embed = discord.Embed(title="📚 共享記憶列表", color=0x7e6487)

        # 全域編號，避免和清除指令的 index 搞混
        for i, fact in enumerate(facts[start:end], start + 1):
            embed.add_field(name=f"#{i}", value=fact, inline=False)

        footer_text = f"最後更新：{_shared_memory.get('updated', '未知')}　｜　上限 {MAX_SHARED_FACTS} 條"
        if total_pages > 1:
            footer_text += f"　｜　頁面 {page_index + 1}/{total_pages}"
        embed.set_footer(text=footer_text)

        await ctx.send(embed=embed)


# [指令] 清除共享記憶（限管理員）
@bot.command(aliases=['清除共享記憶'])
async def clear_shared_memory(ctx, index: int = None):
    """
    清除共享記憶（限管理員）
    - 清除共享記憶 3   → 刪除第 3 條
    - 清除共享記憶      → 清除全部
    """
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("只有管理員才能清除共享記憶喔 (´・ω・`)")
        return

    if not _shared_memory["facts"]:
        await ctx.send("目前沒有共享記憶可以清除喔 (´・ω・`)")
        return

    # 指定編號：刪除單筆
    if index is not None:
        total = len(_shared_memory["facts"])
        if index < 1 or index > total:
            await ctx.send(f"編號不正確喔，請輸入 1 ~ {total} 之間的數字 (´・ω・`)")
            return
        removed = _shared_memory["facts"].pop(index - 1)
        _shared_memory["updated"] = str(datetime.date.today())
        save_memories()
        await ctx.send(f"已刪除第 #{index} 條共享記憶：「{removed}」(｡･∀･)ﾉﾞ\n剩餘 {len(_shared_memory['facts'])} 條")

    # 未傳編號：清除全部
    else:
        _shared_memory["facts"].clear()
        _shared_memory["updated"] = str(datetime.date.today())
        save_memories()
        await ctx.send("已清除所有共享記憶 (｡･∀･)ﾉﾞ")


# [指令] AI狀態
@bot.command(aliases=['ai_status', 'ai狀態'])
async def shizumu_bot_status(ctx):
    """查看 AI 系統狀態"""
    user_id = str(ctx.author.id)
    embed = discord.Embed(title="AI 系統狀態", color=0x7e6487)

    if Google_AI_API_key:
        embed.add_field(name="系統狀態", value="✅ 運行中", inline=False)
        embed.add_field(name="使用模型", value=GEMINI_MODEL, inline=False)
        embed.add_field(name="目前對話中的用戶數", value=f"{len(chat_histories)} 位", inline=False)

        # 共享記憶數量
        shared_count = len(_shared_memory["facts"])
        shared_updated = _shared_memory.get("updated") or "尚無記錄"
        embed.add_field(
            name="共享記憶",
            value=f"{shared_count} 條（上限 {MAX_SHARED_FACTS} 條）　最後更新：{shared_updated}",
            inline=False
        )

        # 個人每日用量
        today = datetime.date.today()
        user_usage = _user_api_usage.get(user_id, {})
        used = user_usage.get("count", 0) if user_usage.get("date") == today else 0
        remaining = MAX_REQUESTS_PER_DAY - used
        embed.add_field(
            name="今日對話次數",
            value=f"已使用 {used} 次 ／ 剩餘 {remaining} 次（上限 {MAX_REQUESTS_PER_DAY} 次）",
            inline=False
        )

        # 個人短期記憶 & 長期摘要
        if user_id in chat_histories:
            history = chat_histories[user_id]
            msg_count = len(history) // 2

            summary_text = None
            if history:
                first_msg = history[0]
                if first_msg.get("role") == "user" and first_msg.get("parts", "").startswith("【系統提示"):
                    lines = first_msg["parts"].splitlines()
                    summary_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else first_msg["parts"]

            if summary_text:
                display = summary_text[:1000] + "..." if len(summary_text) > 1000 else summary_text
                embed.add_field(name=f"🧠 個人對話摘要（共 {msg_count} 輪）", value=display, inline=False)
            else:
                embed.add_field(name="🧠 個人對話記憶", value=f"📝 共 {msg_count} 輪對話（尚未觸發記憶濃縮）", inline=False)
        else:
            # 嘗試顯示持久化的個人長期摘要
            personal_summary = get_personal_summary(user_id)
            if personal_summary:
                updated = _personal_summaries.get(user_id, {}).get("updated", "未知")
                display = personal_summary[:1000] + "..." if len(personal_summary) > 1000 else personal_summary
                embed.add_field(name=f"🧠 個人長期摘要（上次更新：{updated}）", value=display, inline=False)
            else:
                embed.add_field(name="🧠 個人對話記憶", value="📝 尚無記錄，使用 `小寒` 指令開始聊天", inline=False)
    else:
        embed.add_field(name="系統狀態", value="❌ 未啟用（GOOGLE_AI_API_KEY 未設定）", inline=False)

    await ctx.send(embed=embed)


# ================================
# 錯誤處理
# ================================
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return
    if isinstance(error, commands.errors.NSFWChannelRequired):
        embed = discord.Embed(title="🔞這個頻道不可以色色!!", color=0xe74c3c)
        embed.set_image(url='https://media.discordapp.net/attachments/848185934187855872/1046623635395313664/d2fc6feb-a48e-4ff6-8cd9-689a0cb43ff5.png')
        return await ctx.send(embed=embed)
    raise error


# ================================
# on_message
# ================================
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # @ 標記 bot 時觸發 AI 對話
    if bot.user in message.mentions and Google_AI_API_key:
        message_content = message.content
        for mention in message.mentions:
            message_content = message_content.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        message_content = message_content.strip()
        if message_content:
            ctx = await bot.get_context(message)
            await _handle_ai_chat(ctx, message_content)
            return

    if '晚安' in message.content:
        await message.channel.send(f"晚安 <:shizimu_sleep:1356313689019650099> , {message.author.name}")

    if '早安' in message.content:
        await message.channel.send(f"早安(｡･∀･)ﾉﾞ, {message.author.name}")

    if '午安' in message.content:
        await message.channel.send(f"午安(｡･∀･)ﾉﾞ, {message.author.name}")

    if '<:shizimu_cry:1356313573487284244>' in message.content:
        await message.channel.send('<:shizimu_cry:1356313573487284244>' * 3)

    await bot.process_commands(message)


bot.run(Discord_token)
