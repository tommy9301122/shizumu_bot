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

import feedparser
from bs4 import BeautifulSoup
import nekos
import googlemaps
from googletrans import Translator
#import openai
import discord
from discord.ext import commands, tasks
from discord.ext.commands import CommandNotFound

from PTT_jokes import PttJokes
from shizumu_bot_data import food_a, food_j, food_c, shizumu_murmur


Google_Map_API_key = os.getenv("GOOGLE_MAP_API_KEY")
Discord_token = os.getenv("DISCORD_TOKEN")
weather_authorization = os.getenv("WEATHER_AUTHORIZATION")

intents = discord.Intents.default()
intents.members = True
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
    print('目前登入身份：', bot.user)
    #broadcast.start() # 自動推播
    activity_auto_change.start() #自動更新狀態
    
    
# [新進成員]
@bot.event
async def on_member_join(member):
    if member.guild.id == 1292873644950683658:       #伺服器ID
        channel = bot.get_channel(1292873645794005013)    #頻道ID
        await channel.send("https://i.imgur.com/V6kdDTx.jpg")  #又來了一個新人
        

# [指令] 代替呱YA說話
@bot.command()
async def 呱YA說(ctx, *, arg):
    #開發人員使用限定 
    if int(ctx.message.author.id)==378936265657286659 or int(ctx.message.author.id)==343984138983964684:
        await ctx.message.delete()
        await ctx.send(arg)

    
# [指令] 笑話 :
@bot.command()
async def 笑話(ctx):
    ptt = PttJokes(1)
    joke_class_list = ['笑話','猜謎','耍冷','XD']
    error_n=0
    while True:
        try:
            joke_output = ptt.output()
            if joke_output[1:3] in joke_class_list and re.search('http',joke_output) is None:
                joke_output = re.sub('(\\n){4,}','\n\n\n',joke_output)

                joke_title = re.search('.*\\n',joke_output)[0]
                joke_foot = re.search('\\n.*From ptt',joke_output)[0]
                joke_main = joke_output.replace(joke_title,'').replace(joke_foot,'')
                break
        except:
            error_n+=1
            print(error_n)
            if error_n == 5:
                break
            pass
    embed = discord.Embed(title=joke_title, description=joke_main)
    embed.set_footer(text=joke_foot)
    await ctx.send(embed=embed)
    
    
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
    news_message = await ctx.send('呱YA日報 '+(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"), embed=embed)
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
    
    if news_message.content == '呱YA日報 '+(datetime.datetime.utcnow()+datetime.timedelta(hours=8)).strftime("%Y/%m/%d"): # 只對當日新聞指令有效
        
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


# [指令] 翻譯 :
@bot.command(aliases=['translate'])
async def 翻譯(ctx, *args):
    input_text = ' '.join(args)
    
    translator = Translator()
    us_trans = translator.translate(input_text, dest='en').text
    tw_trans = translator.translate(input_text, dest='zh-tw').text
    kr_trans = translator.translate(input_text, dest='ko').text
    jp_trans = translator.translate(input_text, dest='ja').text
    cn_trans = translator.translate(input_text, dest='zh-cn').text
    
    trans_list = [us_trans, tw_trans, kr_trans, jp_trans, cn_trans]
    output_text = ''
    for trans in trans_list:
        if input_text!=trans:
            output_text = output_text+trans+'\n'
            
    embed=discord.Embed(title='🌏 '+input_text, description=output_text, color=0x3884ff)
    await ctx.send(embed=embed)


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
    if message.author == bot.user: #排除自己的訊息，避免陷入無限循環
        return
    
    # 早安、晚安、owo
    if message.content.lower() == '晚安':
        await message.channel.send(f"晚安 <:shizimu_sleep:1356313689019650099> , {message.author.name}")
        
    if message.content.lower() == "早安":
        await message.channel.send(f"早安(｡･∀･)ﾉﾞ, {message.author.name}")

    # 訊息中包含shizimu_cry
    if '<:shizimu_cry:1356313573487284244>' in message.content:
        await message.channel.send('<:shizimu_cry:1356313573487284244>' * 3)
            
    await bot.process_commands(message)
    
bot.run(Discord_token)