import asyncio
import os
import datetime
import random
import json
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
from shizumu_bot_data import food_a, food_j, food_c, food_b, shizumu_murmur

# ================================
# 環境變數載入
# ================================
load_dotenv()

Google_Map_API_key = os.getenv("GOOGLE_MAP_API_KEY")
Discord_token = os.getenv("DISCORD_TOKEN")
weather_authorization = os.getenv("WEATHER_AUTHORIZATION")
Google_AI_API_key = os.getenv("GOOGLE_AI_API_KEY")
GEMINI_MODEL = "gemini-3.1-flash-lite-preview"

# ================================
# API 用量限制設定
# ================================
MAX_REQUESTS_PER_DAY = 100   # 每位使用者每日上限
COOLDOWN_SECONDS = 5            # 每次請求冷卻秒數

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
妳的創造者(爸爸)是地瓜YA，外觀形象(媽媽)是靜靜子。
妳的個性溫和，喜歡用顏文字。興趣是玩遊戲與動漫，擁有各項ACG知識。
妳會用繁體中文(台灣)進行對話。
回覆時不要過於冗長、長度維持在簡短的一句話，保持自然的對話節奏。"""

# 設定觸發「記憶濃縮」的對話輪數（例如 10 輪，即 20 條訊息）
SUMMARY_THRESHOLD = 10

# 對話歷史儲存：{ user_id: deque([{"role": "user/model", "parts": "..."}]) }
chat_histories: dict[str, deque] = {}


def get_gemini_response(user_id: str, user_name: str, message: str) -> str:
    """
    取得 Gemini 回應，並結合「動態對話摘要」來節省 Token。
    """
    genai.configure(api_key=Google_AI_API_key)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT
    )

    # 1. 初始化使用者記憶
    if user_id not in chat_histories:
        # maxlen 設為 50 作為安全底線，但主要靠 SUMMARY_THRESHOLD 控管
        chat_histories[user_id] = deque(maxlen=50)

    history = chat_histories[user_id]

    # 2. 檢查是否需要進行「記憶濃縮 (摘要)」
    if len(history) >= SUMMARY_THRESHOLD * 2:
        try:
            # 建立一個暫時的對話實例來產出摘要，不影響原本的對話流
            temp_chat = model.start_chat(history=list(history))
            summary_prompt = "【系統指令】請用繁體中文，將我們以上的對話總結成約 200 字內的精簡摘要。務必保留使用者的名字、喜好或重要的上下文資訊。"
            summary_response = temp_chat.send_message(summary_prompt)
            summary_text = summary_response.text

            # 清空原本冗長的對話，替換成濃縮後的摘要
            history.clear()
            history.append({"role": "user", "parts": f"【系統提示：這是我們之前的對話摘要，請記住這些資訊】\n{summary_text}"})
            history.append({"role": "model", "parts": "好的，我已經牢牢記住這些摘要資訊了！(｡･∀･)ﾉﾞ 請問接下來要聊什麼呢？"})
            #print(f"[{user_name}] 的對話已觸發記憶濃縮！")
            
        except Exception as e:
            print(f"記憶濃縮失敗: {e}")
            # 如果因為網路或 API 問題摘要失敗，就把最舊的一輪對話(2條訊息)刪除，避免卡死
            history.popleft()
            history.popleft()

    # 3. 進行正常的對話回覆
    chat = model.start_chat(history=list(history))

    # 第一次對話時附上使用者名稱
    is_new_chat = len(history) == 0
    full_message = f"（使用者名稱：{user_name}）\n{message}" if is_new_chat else message

    response = chat.send_message(full_message)
    reply = response.text

    # 4. 儲存這輪對話
    history.append({"role": "user", "parts": full_message})
    history.append({"role": "model", "parts": reply})

    return reply


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
    """清除您與小寒的對話歷史"""
    user_id = str(ctx.author.id)
    if user_id in chat_histories:
        chat_histories.pop(user_id)
        await ctx.send("已清除對話記憶，下次聊天將重新開始 (｡･∀･)ﾉﾞ")
    else:
        await ctx.send("你還沒跟我說過話喔 (´・ω・`)")


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

        if user_id in chat_histories:
            history = chat_histories[user_id]
            msg_count = len(history) // 2  # 每輪對話有 2 條訊息

            # 嘗試找出摘要內容（摘要會被存在第一條 user 訊息中）
            summary_text = None
            if history:
                first_msg = history[0]
                if first_msg.get("role") == "user" and first_msg.get("parts", "").startswith("【系統提示"):
                    # 擷取摘要本體，去掉前綴說明行
                    lines = first_msg["parts"].splitlines()
                    summary_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else first_msg["parts"]

            if summary_text:
                # 限制顯示長度，避免超出 embed 欄位上限 1024 字
                display = summary_text[:1000] + "..." if len(summary_text) > 1000 else summary_text
                embed.add_field(name=f"對話記憶摘要（共 {msg_count} 輪）", value=display, inline=False)
            else:
                embed.add_field(name="對話記憶", value=f"📝 共 {msg_count} 輪對話（尚未觸發記憶濃縮）", inline=False)
        else:
            embed.add_field(name="對話記憶", value="📝 尚無記錄，使用 `小寒` 指令開始聊天", inline=False)
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
