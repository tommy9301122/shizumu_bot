import asyncio
import time
from io import BytesIO
import os
import datetime
from datetime import date
import re
import random
import json
import requests
from pathlib import Path

import feedparser
from bs4 import BeautifulSoup
import nekos
import googlemaps
from dotenv import load_dotenv
#import openai
import discord
from discord.ext import commands, tasks
from discord.ext.commands import CommandNotFound
from shizumu_bot_data import food_a, food_j, food_c, shizumu_murmur

# 嘗試載入 Letta (可選功能)
try:
    from letta import create_client
    LETTA_AVAILABLE = True
except ImportError:
    LETTA_AVAILABLE = False
    print("警告: letta 套件未安裝,AI 對話功能將被停用")

# ================================
# 環境變數載入
# ================================
load_dotenv()

Google_Map_API_key = os.getenv("GOOGLE_MAP_API_KEY")
Discord_token = os.getenv("DISCORD_TOKEN")
weather_authorization = os.getenv("WEATHER_AUTHORIZATION")

# Letta 配置
LETTA_API_URL = os.getenv("LETTA_API_URL", "http://localhost:8283")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
ENABLE_LETTA = os.getenv("ENABLE_LETTA", "false").lower() == "true"

# ================================
# Letta Agent 配置
# ================================

# 預設 Agent 人設 (符合 shizumu 的性格)
DEFAULT_PERSONA = """妳是 Shizumu doro,綽號是小寒,一個可愛、友善但有點懶散的 Discord 機器人助手。
妳的個性溫和,喜歡用顏文字。
妳的創造者(爸爸)是地瓜YA,外觀形象(媽媽)是靜靜子,是個可憐的社畜,常常想加薪。
妳興趣是玩遊戲與動漫,擁有各項ACG知識。
妳能記住與每位使用者的對話。
妳會用繁體中文(台灣)進行對話。"""

DEFAULT_HUMAN = """這是一位 Discord 使用者,正在與你進行對話。"""

# 用戶對應檔案路徑
USER_AGENTS_FILE = Path(__file__).parent / "user_agents.json"

# Letta 客戶端 (全域變數)
letta_client = None

# ================================
# Letta 用戶對應機制
# ================================

def load_user_agents() -> dict:
    """載入用戶-Agent 對應表"""
    if USER_AGENTS_FILE.exists():
        try:
            with open(USER_AGENTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"警告: {USER_AGENTS_FILE} 格式錯誤,將建立新檔案")
            return {}
    return {}


def save_user_agents(user_agents: dict) -> None:
    """儲存用戶-Agent 對應表"""
    with open(USER_AGENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(user_agents, f, ensure_ascii=False, indent=2)


def get_or_create_agent(discord_user_id: str, discord_user_name: str) -> str:
    """取得或建立對應該使用者的 Letta Agent"""
    if not letta_client:
        raise Exception("Letta 客戶端未初始化")
    
    user_agents = load_user_agents()
    
    # 檢查是否已有 Agent
    if discord_user_id in user_agents:
        agent_id = user_agents[discord_user_id]
        print(f"找到現有 Agent: {agent_id} (使用者: {discord_user_name})")
        return agent_id
    
    # 建立新 Agent
    try:
        print(f"為使用者 {discord_user_name} ({discord_user_id}) 建立新 Agent...")
        
        agent_name = f"discord_user_{discord_user_id}"
        
        agent = letta_client.create_agent(
            name=agent_name,
            memory={
                "persona": DEFAULT_PERSONA,
                "human": DEFAULT_HUMAN.replace("Discord 使用者", f"Discord 使用者 {discord_user_name}")
            },
            llm_config={
                "model": GEMINI_MODEL,
                "model_endpoint_type": "google_ai",
                "model_endpoint": "https://generativelanguage.googleapis.com"
            },
            embedding_config={
                "embedding_model": "text-embedding-004",
                "embedding_endpoint_type": "google_ai",
                "embedding_endpoint": "https://generativelanguage.googleapis.com"
            }
        )
        
        agent_id = agent.id
        print(f"成功建立 Agent: {agent_id}")
        
        user_agents[discord_user_id] = agent_id
        save_user_agents(user_agents)
        
        return agent_id
        
    except Exception as e:
        print(f"建立 Agent 時發生錯誤: {e}")
        raise

# ================================
# Discord Bot 設定
# ================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # 需要讀取訊息內容
bot = commands.Bot(command_prefix='', intents=intents, help_command=None)


# Google map推薦餐廳
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

    # 如果有超過 4 分的就選其中一間，否則隨便選一間
    high_rated = [r for r in results if r['rating'] > 4]

    if high_rated:
        selected = random.choice(high_rated)
    elif results:
        selected = random.choice(results)
    else:
        return None, None, None, None, None, None  # 找不到結果時返回空值

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
    await bot.change_presence(status= status_w, activity=activity_w)


# [啟動]
@bot.event
async def on_ready():
    global letta_client
    
    print('目前登入身份：', bot.user)
    
    # 初始化 Letta 客戶端 (如果啟用)
    if ENABLE_LETTA and LETTA_AVAILABLE:
        try:
            print(f"正在連接到 Letta Server: {LETTA_API_URL}")
            letta_client = create_client(base_url=LETTA_API_URL)
            print("✅ 成功連接到 Letta Server - AI 對話功能已啟用")
        except Exception as e:
            print(f"⚠️ 無法連接到 Letta Server: {e}")
            print("AI 對話功能將被停用,其他功能正常運作")
            letta_client = None
    else:
        if ENABLE_LETTA and not LETTA_AVAILABLE:
            print("⚠️ ENABLE_LETTA=true 但 letta 套件未安裝")
        print("AI 對話功能已停用")
    
    #broadcast.start() # 自動推播
    activity_auto_change.start() #自動更新狀態
    
    
# [新進成員]
@bot.event
async def on_member_join(member):
    if member.guild.id == 1292873644950683658:       #伺服器ID
        channel = bot.get_channel(1292873645794005013)    #頻道ID
        await channel.send("https://i.imgur.com/V6kdDTx.jpg")  #又來了一個新人
        await channel.send(f"{member.mention} 歡迎~麻煩剛加入的晚餐們，要記得幫忙把DC的ID改成跟YT一樣的喔，這樣好讓我們認識您，謝謝唷!")
        

# [指令]
@bot.command()
async def shizumu說(ctx, *, arg):
    #開發人員使用限定 
    if int(ctx.message.author.id)==378936265657286659 or int(ctx.message.author.id)==343984138983964684:
        await ctx.message.delete()
        await ctx.send(arg)
    
    
# [指令] 新聞 :
@bot.command()
async def 新聞(ctx):
    d = feedparser.parse('https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant')
    n_title = [i.title for i in d.entries]
    source_name_list = [i.source.title for i in d.entries]
    title_list = [t.replace(' - '+s,'') for t,s in zip(n_title,source_name_list)] # 標題去除來源
    #published_list = [i.published for i in d.entries] #日期
    url_list = [i.link for i in d.entries]
    embed = discord.Embed(title=('頭條新聞'), description=(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x7e6487)
    for title, url, source in zip(title_list[:5], url_list[:5], source_name_list[:5] ):
        embed.add_field(name=title, value='['+source+']('+url+')', inline=False)
    news_message = await ctx.send('晚餐日報 '+(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), embed=embed)
    emojis = ['📰', '🎮', '🌤']
    for emoji in emojis:
        await news_message.add_reaction(emoji)
        
@bot.event
async def on_raw_reaction_add(payload):
    if payload.member.bot: # 機器人自身不算
        return
    channel = bot.get_channel(payload.channel_id)
    news_message = await channel.fetch_message(payload.message_id)    
    emoji = payload.emoji
    
    if news_message.content == '晚餐日報 '+(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"): # 只對當日新聞指令有效
        
        if emoji.name == "📰":
            d = feedparser.parse('https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant')
            n_title = [i.title for i in d.entries]
            source_name_list = [i.source.title for i in d.entries]
            title_list = [t.replace(' - '+s,'') for t,s in zip(n_title,source_name_list)]
            url_list = [i.link for i in d.entries]
            google_embed = discord.Embed(title=('頭條新聞'), description=(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x598ad9)
            for title, url, source in zip(title_list[:5], url_list[:5], source_name_list[:5] ):
                google_embed.add_field(name=title, value='['+source+']('+url+')', inline=False)
            await news_message.edit(embed=google_embed)

        elif emoji.name == "🎮":
            d = feedparser.parse('https://gnn.gamer.com.tw/rss.xml')
            title_list = [i.title for i in d.entries]
            url_list = [i.link for i in d.entries]
            gnn_embed = discord.Embed(title=('巴哈姆特 GNN 新聞'), description=(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x598ad9)
            for title, url in zip(title_list[:5], url_list[:5]):
                gnn_embed.add_field(name=title, value='[巴哈姆特]('+url+')', inline=False)
            await news_message.edit(embed=gnn_embed)

        elif emoji.name == "🌤":
            # 取得台灣各縣市天氣
            url = 'https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-091?Authorization='+weather_authorization
            r = requests.get(url)
            data = r.json()['records']['Locations'][0]['Location']
            weather_embed = discord.Embed(title=('天氣預報 '), description=(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), color=0x598ad9)
            for loc_num, loc_name in zip([16,19,15,17,11], ['臺北','臺中','嘉義','高雄','花蓮']):
                weather_data = data[loc_num]['WeatherElement']
                temp = weather_data[0]['Time'][0]['ElementValue'][0]['Temperature']
                rain = weather_data[11]['Time'][0]['ElementValue'][0]['ProbabilityOfPrecipitation']
                weat = weather_data[12]['Time'][0]['ElementValue'][0]['Weather']
                weather_embed.add_field(name=loc_name ,value='☂'+rain+'%  🌡'+temp+'°C  ⛅'+weat, inline=False)
                print(loc_name, temp, rain, weat)
            
            await news_message.edit(embed=weather_embed)
            
            
# [指令] 地震 :
@bot.command()
async def 地震(ctx, *args):
    
    url = 'https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001?Authorization='+weather_authorization
    eq_data = requests.get(url).json()
    eq_content = eq_data['records']['Earthquake'][0]['ReportContent']
    eq_image = eq_data['records']['Earthquake'][0]['ShakemapImageURI']
    ed_url = eq_data['records']['Earthquake'][0]['Web']
    
    embed=discord.Embed(title=eq_content, url=ed_url, color=0x636363)
    embed.set_image(url=eq_image)
    await ctx.send(embed=embed)


# [指令] 午/晚餐吃什麼:
@bot.command(aliases=['午餐吃什麼'])
async def 晚餐吃什麼(ctx, *args):
    ending_list = ['怎麼樣?','好吃',' 98','?','']
    # 沒有選類別的話就全部隨機: 吃土 2%  中式/台式 49%  日式/美式/意式 49%
    if len(args)==0:
        eat_dust = random.randint(1, 100)
        if eat_dust <= 2:
            await ctx.send('還是吃土?')
        else:
            eat_class = random.randint(1, 2)
            if eat_class == 1:
                await ctx.send(random.choice(food_c)+random.choice(ending_list))
            if eat_class == 2:
                await ctx.send(random.choice(food_j+food_a)+random.choice(ending_list))
    # 只輸入類別
    elif len(args)==1 and '式' in args[0]:
        food_class = args[0]
        if food_class=='中式' or food_class=='台式':
            await ctx.send(random.choice(food_c)+random.choice(ending_list))
        elif food_class=='日式' :
            await ctx.send(random.choice(food_j)+random.choice(ending_list))
        elif food_class=='美式' :
            await ctx.send(random.choice(food_a)+random.choice(ending_list))
        else:
            await ctx.send('我不知道'+food_class+'料理有哪些，請輸入中/台式、日式或美式 º﹃º')
    # 只輸入地點
    elif len(args)==1 and '式' not in args[0]:
        search_food = random.choice(food_j+food_a+food_c)
        search_place = args[0]
        try:
            restaurant = googlemaps_search_food(search_food, search_place)
            embed = discord.Embed(title=restaurant[0], 
                                  description='⭐'+str(restaurant[2])+'  👄'+str(restaurant[3])+'  🕓'+str(restaurant[4])+'  '+'💵'*int(restaurant[5]), 
                                  url='https://www.google.com/maps/search/?api=1&query='+search_food+'&query_place_id='+restaurant[1])
            embed.set_author(name = search_food+random.choice(ending_list))
            await ctx.send(embed=embed)
        except:
            await ctx.send('在'+search_place+'找不到適合的'+search_food+'餐廳，請再重新輸入一遍或換個地點名稱><')
    # 輸入類別和地點
    elif len(args)==2 and ('中式' in args[0] or '台式' in args[0] or '日式' in args[0] or '美式' in args[0]):
        food_class = args[0]
        search_place = args[1]
        if food_class=='中式' or food_class=='台式':
            search_food = random.choice(food_c)
        elif food_class=='日式' :
            search_food = random.choice(food_j)
        elif food_class=='美式' :
            search_food = random.choice(food_a)
        try:
            restaurant = googlemaps_search_food(search_food, search_place)
            embed = discord.Embed(title=restaurant[0], 
                                  description='⭐'+str(restaurant[2])+'  👄'+str(restaurant[3])+'  🕓'+str(restaurant[4])+'  '+'💵'*int(restaurant[5]), 
                                  url='https://www.google.com/maps/search/?api=1&query='+search_food+'&query_place_id='+restaurant[1])
            embed.set_author(name = search_food+random.choice(ending_list))
            await ctx.send(embed=embed)
        
        except:
            await ctx.send('在'+search_place+'找不到適合的'+search_food+'餐廳，請再重新輸入一遍或換個地點名稱><')
    # 格式打錯
    else:
        await ctx.send('確認一下指令是否正確: ```午餐吃什麼 [中式/台式/日式/美式] [地點]``` 參數皆可省略')


# [指令] 早餐吃什麼:
@bot.command()
async def 早餐吃什麼(ctx, *args):
    ending_list = ['怎麼樣?','好吃',' 98','?','']
    if len(args)==0:
        eat_dust = random.randint(1, 100)
        if eat_dust <= 2:
            await ctx.send('早餐不要吃土，再骰一次!')
        else:
            await ctx.send(random.choice(food_b)+random.choice(ending_list))


# [NSFW指令] 色色
class_list_nsfw = ['waifu','neko', 'blowjob']
@commands.is_nsfw()
@bot.command(aliases=['hentai','エロ'])
async def 色色(ctx):
    random_nsfw_class = random.choice(class_list_nsfw)
    nsfw_res = requests.get('https://api.waifu.pics/nsfw/'+random_nsfw_class, headers={"User-Agent":"Defined"}, verify=False)
    nsfw_pic = json.loads(nsfw_res.text)['url']
    embed=discord.Embed(color=0xf1c40f)
    embed.set_image(url=nsfw_pic)
    await ctx.send(embed=embed)


# ================================
# Letta AI 對話指令
# ================================

# [指令] 聊天 - 與 Letta Agent 對話
@bot.command(aliases=['ai_chat', 'chat'])
async def 聊天(ctx, *, message_content: str):
    """使用 Letta AI 進行對話"""
    if not letta_client:
        await ctx.send("❌ 對話功能目前未啟用,問問看地瓜YA怎麼了")
        return
    
    await _handle_ai_chat(ctx, message_content)


async def _handle_ai_chat(ctx, message_content: str):
    """處理 AI 對話的核心邏輯"""
    async with ctx.typing():
        try:
            discord_user_id = str(ctx.author.id)
            discord_user_name = ctx.author.name
            
            # 取得或建立 Agent
            agent_id = get_or_create_agent(discord_user_id, discord_user_name)
            
            # 發送訊息給 Letta
            response = letta_client.send_message(
                agent_id=agent_id,
                message=message_content,
                role="user"
            )
            
            # 解析回應
            reply_messages = []
            for msg in response.messages:
                if hasattr(msg, 'message_type') and msg.message_type == 'user_message':
                    content = getattr(msg, 'message', getattr(msg, 'content', str(msg)))
                    reply_messages.append(content)
            
            if reply_messages:
                full_reply = '\n'.join(reply_messages)
                
                # Discord 訊息長度限制
                if len(full_reply) > 2000:
                    chunks = [full_reply[i:i+2000] for i in range(0, len(full_reply), 2000)]
                    for chunk in chunks:
                        await ctx.send(chunk)
                else:
                    await ctx.send(full_reply)
            else:
                await ctx.send("抱歉,我現在無法回應 😢")
                
        except Exception as e:
            print(f"AI 對話錯誤: {e}")
            await ctx.send(f"欸欸地瓜,有bug你看一下! {str(e)}")


# [指令] 重置記憶
@bot.command(aliases=['reset_memory', '重置記憶'])
async def reset_memory(ctx):
    """重置您的 AI Agent 對話記憶"""
    if not letta_client:
        await ctx.send("❌ AI 對話功能目前未啟用")
        return
    
    discord_user_id = str(ctx.author.id)
    user_agents = load_user_agents()
    
    if discord_user_id in user_agents:
        agent_id = user_agents[discord_user_id]
        
        try:
            letta_client.delete_agent(agent_id)
            del user_agents[discord_user_id]
            save_user_agents(user_agents)
            
            await ctx.send("✅ 已重置您的對話記憶，下次對話將建立新的 Agent")
        except Exception as e:
            await ctx.send(f"❌ 重置失敗: {str(e)}")
    else:
        await ctx.send("您目前沒有對應的 Agent")


# [指令] AI狀態
@bot.command(aliases=['ai_status', 'ai狀態'])
async def shizumu_bot_status(ctx):
    """查看 AI 系統狀態"""
    discord_user_id = str(ctx.author.id)
    user_agents = load_user_agents()
    
    embed = discord.Embed(title="🤖 AI 系統狀態", color=0x7e6487)
    
    if letta_client:
        embed.add_field(name="系統狀態", value="✅ 運行中", inline=False)
        embed.add_field(name="Letta Server", value=LETTA_API_URL, inline=False)
        embed.add_field(name="使用模型", value=GEMINI_MODEL, inline=False)
        embed.add_field(name="總用戶數", value=f"{len(user_agents)} 位", inline=False)
        
        if discord_user_id in user_agents:
            agent_id = user_agents[discord_user_id]
            embed.add_field(name="您的 Agent", value=f"🆔 `{agent_id}`\n✅ 已建立", inline=False)
        else:
            embed.add_field(name="您的 Agent", value="📝 尚未建立 (使用 `AI聊天` 指令自動建立)", inline=False)
    else:
        embed.add_field(name="系統狀態", value="❌ 未啟用", inline=False)
        embed.add_field(name="說明", value="AI 對話功能目前未啟用，請聯絡管理員", inline=False)
    
    await ctx.send(embed=embed)
    

# [忽略error / NSFW警告] : 忽略所有前綴造成的指令錯誤、指令變數輸入錯誤、NSFW警告
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return
    if isinstance(error, commands.errors.NSFWChannelRequired):
        embed=discord.Embed(title="🔞這個頻道不可以色色!!", color=0xe74c3c)
        embed.set_image(url='https://media.discordapp.net/attachments/848185934187855872/1046623635395313664/d2fc6feb-a48e-4ff6-8cd9-689a0cb43ff5.png')
        return await ctx.send(embed=embed)
    raise error
    

# on_message
@bot.event
async def on_message(message):
    if message.author == bot.user: #排除自己的訊息,避免陷入無限循環
        return
    
    # 檢查是否有 @ 標記 bot (透過 mentions 檢查)
    if bot.user in message.mentions and letta_client:
        # 移除 @ 標記後的訊息內容
        message_content = message.content
        # 移除所有 @ 標記
        for mention in message.mentions:
            message_content = message_content.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        message_content = message_content.strip()
        
        # 如果有訊息內容,就進行 AI 對話
        if message_content:
            ctx = await bot.get_context(message)
            await _handle_ai_chat(ctx, message_content)
            return  # 處理完 AI 對話後就返回,不再處理其他邏輯
    
    # 早安、晚安、owo
    if '晚安' in message.content:
        await message.channel.send(f"晚安 <:shizimu_sleep:1356313689019650099> , {message.author.name}")
        
    if "早安" in message.content:
        await message.channel.send(f"早安(｡･∀･)ﾉﾞ, {message.author.name}")

    if "午安" in message.content:
        await message.channel.send(f"午安(｡･∀･)ﾉﾞ, {message.author.name}")

    # 訊息中包含shizimu_cry
    if '<:shizimu_cry:1356313573487284244>' in message.content:
        await message.channel.send('<:shizimu_cry:1356313573487284244>' * 3)
            
    await bot.process_commands(message)
    
bot.run(Discord_token)
