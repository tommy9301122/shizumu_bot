import asyncio
import os
import datetime
import random
import json
import pathlib
import requests
import tempfile
import threading
import time
from collections import deque

from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from discord.ext.commands import CommandNotFound
import google.generativeai as genai
from google.generativeai import types as genai_types
from shizumu_bot_data import SHIZUMU_MURMUR, INTEREST_KEYWORDS
from shizumu_services import (
    ALLOWED_FOOD_CLASSES,
    FOOD_ENDINGS,
    FoodRecommendation,
    get_earthquake_info_text,
    get_earthquake_report,
    get_food_recommendation,
    get_food_recommendation_text,
    get_headline_articles,
    get_weather_forecast_rows,
    get_weather_info_text,
    today_taipei,
)

# ================================
# 環境變數載入
# ================================
load_dotenv()

Google_Map_API_key = os.getenv("GOOGLE_MAP_API_KEY")
Discord_token = os.getenv("DISCORD_TOKEN")
weather_authorization = os.getenv("WEATHER_AUTHORIZATION")
Google_AI_API_key = os.getenv("GOOGLE_AI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
SHIZUMU_API_ENABLED = os.getenv("SHIZUMU_API_ENABLED", "1").lower() not in ("0", "false", "no")
SHIZUMU_API_HOST = os.getenv("SHIZUMU_API_HOST", "0.0.0.0")
SHIZUMU_API_PORT = int(os.getenv("PORT", os.getenv("SHIZUMU_API_PORT", "8000")))

# 管理員 ID 列表（可以使用特殊指令）
ADMIN_IDS = [378936265657286659, 343984138983964684]

# 群聊模式啟用的頻道 ID（只有此頻道會啟用 channel-centric 記憶與被動回應）
CHAT_CHANNEL_ID = 1319351567421280387

# ================================
# 群聊行為參數
# ================================
CHAT_BASE_RATE = 0.01          # 基礎回應機率
CHAT_QUESTION_BONUS = 0.20     # 是問句時的加成
CHAT_KEYWORD_BONUS = 0.25      # 命中興趣關鍵字的加成
CHAT_RECENT_BONUS = 0.15       # 小寒最近講過話的加成
CHAT_MAX_RATE = 0.6            # 機率上限
CHANNEL_REPLY_COOLDOWN = 8     # 被動回覆之間的最短間隔（秒）
CHANNEL_HISTORY_MAXLEN = 30    # 頻道短期歷史最大長度
CHANNEL_SUMMARY_THRESHOLD = 20 # 達到此筆數即觸發濃縮
CHANNEL_SUMMARY_KEEP = 10      # 濃縮後保留最新幾筆
#INTEREST_KEYWORDS             # 興趣關鍵字 定義於 shizumu_bot_data.py

# ================================
# API 用量限制設定
# ================================
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_AI_REQUESTS_PER_DAY", 50))                # 每位使用者每日上限
COOLDOWN_SECONDS = int(os.getenv("AI_COOLDOWN_SECONDS", 5))                         # 每次請求冷卻秒數
CHANNEL_MAX_AI_CALLS_PER_DAY = int(os.getenv("CHANNEL_MAX_AI_CALLS_PER_DAY", 300))  # 群聊頻道每日 AI 呼叫上限

# 每位使用者的每日計數器：{ user_id: {"date": date, "count": int} }
_user_api_usage: dict[str, dict] = {}
# 每位使用者的上次請求時間：{ user_id: float }
_last_request_time: dict[str, float] = {}
# 群聊頻道每日 AI 用量
_channel_ai_usage: dict = {"date": None, "count": 0}


def check_channel_limit() -> bool:
    """檢查群聊頻道 AI 呼叫的每日上限（與使用者配額無關）"""
    today = datetime.date.today()
    if _channel_ai_usage["date"] != today:
        _channel_ai_usage["date"] = today
        _channel_ai_usage["count"] = 0
    return _channel_ai_usage["count"] < CHANNEL_MAX_AI_CALLS_PER_DAY


def record_channel_usage():
    today = datetime.date.today()
    if _channel_ai_usage["date"] != today:
        _channel_ai_usage["date"] = today
        _channel_ai_usage["count"] = 0
    _channel_ai_usage["count"] += 1


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
妳的創造者(爸爸)是地瓜YA，外觀形象(媽媽)是靜靜子。
地瓜YA爸爸是帥氣的工程師。靜靜子媽媽是美麗可愛的vtuber，是可憐的社畜。
妳興趣是玩遊戲與動漫，擁有各項ACG知識。
妳會用繁體中文(台灣)進行對話。
回覆時不要過於冗長，回話長度大約維持在簡短的一至兩句之間，保持自然的對話節奏。"""

# 特殊成員
SPECIAL_MEMBERS = {
    "378936265657286659": "把拔",    # 地瓜YA 的 ID
    "343984138983964684": "馬麻",    # 靜靜子 的 ID
}

def get_member_identity(user_id: str) -> str | None:
    """
    根據 Discord ID 獲取成員身份標籤
    回傳：身份標籤（如 "把拔"、"馬麻"），若非特殊成員則回傳 None
    """
    return SPECIAL_MEMBERS.get(user_id)

# 設定觸發「記憶濃縮」的對話輪數（例如 10 輪，即 20 條訊息）
SUMMARY_THRESHOLD = 10
# 個人短期歷史 deque 容量；必須大於 SUMMARY_THRESHOLD*2，預留 buffer 給「正在進行的本輪」
PERSONAL_HISTORY_SAFE_BUFFER = 4
PERSONAL_HISTORY_MAXLEN = SUMMARY_THRESHOLD * 2 + PERSONAL_HISTORY_SAFE_BUFFER
MAX_SHARED_FACTS = 50  # 共享記憶的最大條數，超過時會刪除最舊的

# 短期對話歷史（記憶體）
chat_histories: dict[str, deque] = {}

# 並發保護鎖
_chat_histories_lock = threading.Lock()
_memory_lock = threading.Lock()

# 個人摘要失敗冷卻：{ user_id: timestamp }
_last_personal_summary_fail_at: dict[str, float] = {}
PERSONAL_SUMMARY_FAIL_COOLDOWN = 60  # 秒

# 持久化記憶檔案
MEMORY_FILE = pathlib.Path("memory.json")

# 共享記憶（持久化）
_shared_memory: dict = {"facts": [], "updated": ""}

# 個人長期摘要（持久化）
_personal_summaries: dict[str, dict] = {}

# 頻道短期記憶（記憶體，僅針對 CHAT_CHANNEL_ID）
# 每筆元素為 dict：{author_id, author_name, content, is_bot, timestamp}
channel_history: deque = deque(maxlen=CHANNEL_HISTORY_MAXLEN)

# 頻道長期摘要（持久化）
_channel_summary: dict = {"summary": "", "updated": ""}

# 群聊頻道被動回覆冷卻
_last_channel_reply_time: float = 0.0

# 頻道濃縮排程旗標 + lock（避免阻塞 event loop / 重入）
_channel_summary_pending: bool = False
_channel_summary_async_lock: asyncio.Lock | None = None  # on_ready 時建立


# ================================
# 記憶管理
# ================================

def load_memories():
    """Bot 啟動時從 JSON 載入所有持久化記憶"""
    global _shared_memory, _personal_summaries, _channel_summary
    if MEMORY_FILE.exists():
        try:
            raw = MEMORY_FILE.read_text(encoding="utf-8")
            # 若檔案為空或只含空白，視為無效 JSON
            if raw.strip():
                data = json.loads(raw)
            else:
                raise json.JSONDecodeError("Empty memory file", raw, 0)
        except (json.JSONDecodeError, OSError) as e:
            # 檔案損毀、讀取失敗或 JSON 格式錯誤時，回退到預設記憶結構並記錄警告
            print(f"[記憶][警告] 載入記憶檔失敗 ({e!r})，將使用預設記憶結構。")
            _shared_memory = {"facts": [], "updated": ""}
            _personal_summaries = {}
            _channel_summary = {"summary": "", "updated": ""}
            return
        else:
            _shared_memory = data.get("shared", {"facts": [], "updated": ""})
            _personal_summaries = data.get("personal", {})
            _channel_summary = data.get("channel", {"summary": "", "updated": ""})
            print(
                f"[記憶] 已載入共享記憶 {len(_shared_memory['facts'])} 條，"
                f"個人摘要 {len(_personal_summaries)} 位，"
                f"頻道摘要 {'有' if _channel_summary.get('summary') else '無'}"
            )
    else:
        # 未找到記憶檔，保留預設結構
        print("[記憶] 未找到記憶檔，將使用預設記憶結構。")


def save_memories():
    """將記憶持久化寫入 JSON（atomic write + lock）"""
    with _memory_lock:
        data = {
            "shared": _shared_memory,
            "personal": _personal_summaries,
            "channel": _channel_summary,
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)

        target = MEMORY_FILE
        target_dir = str(target.parent) if str(target.parent) else "."
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".memory.", suffix=".json.tmp", dir=target_dir
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise


def add_shared_fact(fact: str):
    """新增一條共享記憶，超過上限時移除最舊的"""
    with _memory_lock:
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
    # 在鎖內快照，避免讀寫競態
    with _memory_lock:
        facts = list(_shared_memory["facts"])
    if not facts:
        return ""

    if user_message:
        selected = [f for f in facts if _bigram_relevant(f, user_message)]
    else:
        selected = facts

    if not selected:
        return ""

    total = len(facts)
    injected = len(selected)
    facts_text = "\n".join(f"- {f}" for f in selected)
    suffix = f"（已依相關性篩選 {injected}/{total} 條）" if user_message else f"（共 {total} 條）"
    return f"【共享記憶：這是所有使用者共同建立的資訊{suffix}，請記住】\n{facts_text}"


def save_personal_summary(user_id: str, summary: str):
    """儲存個人長期摘要；空字串會被拒絕，避免洗掉舊摘要"""
    if not summary or not summary.strip():
        print(f"[記憶][警告] 嘗試以空字串覆寫使用者 {user_id} 的個人摘要，已忽略。")
        return
    with _memory_lock:
        _personal_summaries[user_id] = {
            "summary": summary.strip(),
            "updated": str(datetime.date.today())
        }
    save_memories()


def get_personal_summary(user_id: str) -> str | None:
    """取得個人長期摘要"""
    with _memory_lock:
        return _personal_summaries.get(user_id, {}).get("summary")


# ================================
# 頻道群聊模式 - 記憶與決策
# ================================

CHANNEL_MODE_APPENDIX = """
【群聊情境補充】
妳目前身處一個多人 Discord 聊天頻道，會看到不同使用者交錯對話。
- 對話會以「[時間] 名字：內容」的腳本格式提供給妳作為上下文。
- 不要每則訊息都回，自然地像群裡的一個朋友插話即可。
- 若覺得這則訊息不需要妳開口，請只回覆 [SKIP] 三個字，不要附加任何其他文字。
- 不要動不動就點名某個人，避免每句都加「@」或對方名字。
- 不要重複別人剛講過的話，保持簡短自然，符合妳一貫的個性。
"""


def _now_hhmm() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%H:%M")


def _record_channel_message(message: discord.Message, is_bot: bool):
    """將一則訊息記入頻道短期歷史。會清掉 mention 標籤、限制長度。不在這裡同步呼叫濃縮。"""
    global _channel_summary_pending
    content = message.content or ""
    # 將 <@id> 換成 @display_name，避免上下文出現原始 ID 字串
    for mention in message.mentions:
        tag_a = f'<@{mention.id}>'
        tag_b = f'<@!{mention.id}>'
        replaced = f'@{mention.display_name}'
        content = content.replace(tag_a, replaced).replace(tag_b, replaced)
    content = content.strip()
    if not content:
        return

    channel_history.append({
        "author_id": str(message.author.id),
        "author_name": message.author.display_name,
        "content": content[:300],  # 限長避免單則訊息撐爆上下文
        "is_bot": is_bot,
        "timestamp": _now_hhmm(),
    })

    # 只標記，交給背景任務出去跑
    if len(channel_history) >= CHANNEL_SUMMARY_THRESHOLD:
        _channel_summary_pending = True


async def _maybe_summarize_channel_async():
    """背景執行頻道濃縮，避免阻塞 event loop 並防重入。"""
    global _channel_summary_pending, _channel_summary_async_lock
    if not _channel_summary_pending:
        return
    if _channel_summary_async_lock is None:
        _channel_summary_async_lock = asyncio.Lock()
    if _channel_summary_async_lock.locked():
        return
    async with _channel_summary_async_lock:
        if not _channel_summary_pending:
            return
        _channel_summary_pending = False
        try:
            await asyncio.get_event_loop().run_in_executor(None, _try_summarize_channel)
        except Exception as e:
            print(f"[頻道記憶][警告] 濃縮失敗：{e}")


def _try_summarize_channel():
    """將頻道短期歷史中較舊的部分濃縮進 _channel_summary，保留最新 N 筆。"""
    if not Google_AI_API_key:
        return
    if len(channel_history) < CHANNEL_SUMMARY_THRESHOLD:
        return

    # 切出要被濃縮的舊訊息
    older = list(channel_history)[: len(channel_history) - CHANNEL_SUMMARY_KEEP]
    if not older:
        return
    newer = list(channel_history)[len(channel_history) - CHANNEL_SUMMARY_KEEP:]

    lines = []
    for m in older:
        speaker = "你（小寒）" if m["is_bot"] else m["author_name"]
        lines.append(f"[{m['timestamp']}] {speaker}：{m['content']}")
    script = "\n".join(lines)

    existing = _channel_summary.get("summary", "")
    if existing:
        prompt = (
            "【系統指令】以下是這個 Discord 群聊頻道的舊摘要與最新對話腳本。\n"
            "請將兩者合併，整理成一份新的「頻道氛圍摘要」，格式：\n"
            "- 常出現的成員與其特徵：\n"
            "- 最近聊過的主要話題：\n"
            "- 群組整體氛圍/常用梗：\n"
            f"【舊摘要】\n{existing}\n\n"
            f"【新對話腳本】\n{script}\n\n"
            "總長嚴格控制在 600 字以內，請直接輸出摘要本身，不要加客套話。"
        )
    else:
        prompt = (
            "【系統指令】以下是這個 Discord 群聊頻道近期的對話腳本，請整理成「頻道氛圍摘要」，格式：\n"
            "- 常出現的成員與其特徵：\n"
            "- 最近聊過的主要話題：\n"
            "- 群組整體氛圍/常用梗：\n\n"
            f"{script}\n\n"
            "總長嚴格控制在 600 字以內，請直接輸出摘要本身，不要加客套話。"
        )

    genai.configure(api_key=Google_AI_API_key)
    summary_model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT
    )
    resp = summary_model.generate_content(prompt)
    summary_text = (getattr(resp, "text", None) or "").strip()
    if not summary_text:
        return
    if len(summary_text) > 600:
        summary_text = summary_text[:600]

    _channel_summary["summary"] = summary_text
    _channel_summary["updated"] = str(datetime.date.today())
    save_memories()

    # 重置歷史，只保留最新 N 筆
    channel_history.clear()
    for m in newer:
        channel_history.append(m)
    print(f"[頻道記憶] 已濃縮，摘要長度 {len(summary_text)} 字，保留最新 {len(newer)} 筆")

def should_respond(message: discord.Message) -> tuple[bool, str]:
    """
    純規則 + 機率的群聊回應決策器。
    回傳：(是否回應, 原因)
    """
    content = (message.content or "").strip()

    # === 必回情境 ===
    if bot.user in message.mentions:
        return True, "mention"
    if message.reference is not None:
        try:
            ref = message.reference.resolved
            if ref and getattr(ref, "author", None) == bot.user:
                return True, "reply_to_bot"
        except Exception:
            pass
    if "小寒" in content or "shizumu" in content.lower():
        return True, "name_called"

    # === 不回情境 ===
    if not content:
        return False, "empty"
    if len(content) < 3:
        return False, "too_short"
    if content.startswith(("!", "/", ".")):
        return False, "command_like"
    # 純連結
    if content.startswith("http") and " " not in content:
        return False, "pure_url"

    # === 機率累加 ===
    rate = CHAT_BASE_RATE
    if any(content.endswith(q) for q in ["?", "？", "嗎", "呢"]):
        rate += CHAT_QUESTION_BONUS
    if any(kw in content for kw in INTEREST_KEYWORDS):
        rate += CHAT_KEYWORD_BONUS
    # 小寒最近 5 則內講過話 → 視為對話延續
    recent_bot = sum(1 for m in list(channel_history)[-5:] if m.get("is_bot"))
    if recent_bot > 0:
        rate += CHAT_RECENT_BONUS

    rate = min(rate, CHAT_MAX_RATE)
    if rate <= 0:
        return False, "rate=0"
    if random.random() < rate:
        return True, f"prob({rate:.2f})"
    return False, "skip"


def build_channel_context(target_message: dict) -> list[dict]:
    """組裝頻道群聊模式的 Gemini history（共享記憶 + 頻道摘要 + 腳本式近期對話）。"""
    injected: list[dict] = []

    # 1. 共享記憶（依目標訊息篩選）
    shared = get_shared_memory_prompt(user_message=target_message.get("content", ""))
    if shared:
        injected.append({"role": "user", "parts": shared})
        injected.append({"role": "model", "parts": "好的，我記住這些共享資訊了 (｡･∀･)"})

    # 2. 頻道長期摘要
    summary_text = _channel_summary.get("summary", "")
    if summary_text:
        injected.append({
            "role": "user",
            "parts": f"【這個聊天頻道的氛圍與歷史摘要】\n{summary_text}"
        })
        injected.append({"role": "model", "parts": "嗯嗯我記得這個頻道的氛圍 (｡･∀･)"})

    # 3. 頻道近期訊息（腳本格式）
    if channel_history:
        lines = []
        for m in channel_history:
            speaker = "你（小寒）" if m["is_bot"] else m["author_name"]
            lines.append(f"[{m['timestamp']}] {speaker}：{m['content']}")
        script = "【目前群聊頻道的最近對話】\n" + "\n".join(lines)
        injected.append({"role": "user", "parts": script})
        injected.append({"role": "model", "parts": "我有跟上對話 (｡･∀･)"})

    return injected


def get_gemini_channel_response(target: dict) -> str:
    """頻道群聊模式的 Gemini 呼叫。target = {author_name, content}"""
    genai.configure(api_key=Google_AI_API_key)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT + "\n" + CHANNEL_MODE_APPENDIX,
        tools=_TOOLS,
    )
    history = build_channel_context(target)
    chat = model.start_chat(history=history)

    prompt = (
        f"【最新訊息】\n{target['author_name']}：{target['content']}\n"
        f"↑ 請判斷是否要接話。若不需要請只回覆 [SKIP]。"
    )
    response = chat.send_message(prompt)
    return _handle_function_calls(chat, response)


def get_gemini_response(user_id: str, user_name: str, message: str, identity: str = None) -> str:
    """
    取得 Gemini 回應。
    上下文注入順序：共享記憶 → 個人長期摘要 → 近期對話
    
    參數：
    - identity: 用戶身份標籤（如 "爸爸"、"媽媽"），若無則為 None
    """
    genai.configure(api_key=Google_AI_API_key)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT,
        tools=_TOOLS  # ← 注入工具定義
    )

    # 1. 初始化個人短期對話歷史 + 取得歷史快照（在鎖內）
    with _chat_histories_lock:
        if user_id not in chat_histories:
            chat_histories[user_id] = deque(maxlen=PERSONAL_HISTORY_MAXLEN)
        history_snapshot = list(chat_histories[user_id])

    # 2. 觸發個人記憶濃縮（用 snapshot 計算，不直接動共用 deque）
    summarized_history: list | None = None  # 若濃縮成功，這份取代原本 deque 的內容
    if len(history_snapshot) >= SUMMARY_THRESHOLD * 2:
        last_fail = _last_personal_summary_fail_at.get(user_id, 0)
        if (time.time() - last_fail) < PERSONAL_SUMMARY_FAIL_COOLDOWN:
            print(f"[記憶] 跳過個人摘要（冷卻中）user={user_id}")
        else:
            try:
                summary_model = genai.GenerativeModel(
                    model_name=GEMINI_MODEL,
                    system_instruction=SYSTEM_PROMPT
                )
                temp_chat = summary_model.start_chat(history=history_snapshot)

                existing_summary = get_personal_summary(user_id)
                if existing_summary:
                    summary_prompt = (
                        "【系統指令】以下是這位使用者的舊摘要與最新對話紀錄。\n"
                        "請將兩者合併，整理成一份新的結構化摘要，格式如下：\n"
                        "- 使用者名稱：\n"
                        "- 使用者喜好/特徵：\n"
                        "- 重要話題摘要：\n"
                        f"【舊摘要】\n{existing_summary}\n\n"
                        "請注意：最終摘要必須嚴格控制在 500 字以內，刪去不重要的細節，保留關鍵資訊。請直接輸出摘要內容，不要添加任何其他文字、說明或客套話。只需輸出摘要本身。"
                    )
                else:
                    summary_prompt = (
                        "【系統指令】請用繁體中文，將以上對話整理成結構化摘要，格式如下：\n"
                        "- 使用者名稱：\n"
                        "- 使用者喜好/特徵：\n"
                        "- 重要話題摘要：\n"
                        "全部控制在 500 字內。請直接輸出摘要內容，不要添加任何其他文字、說明或客套話。只需輸出摘要本身。"
                    )

                summary_response = temp_chat.send_message(summary_prompt)
                summary_text = (getattr(summary_response, "text", None) or "").strip()
                if not summary_text:
                    raise RuntimeError("摘要結果為空（可能被 safety filter 擋下）")

                if len(summary_text) > 500:
                    summary_text = summary_text[:500]

                save_personal_summary(user_id, summary_text)

                # 構造新的歷史：摘要 + 「正在進行的本輪」之前還沒處理
                summarized_history = [
                    {"role": "user", "parts": f"【系統提示：這是我們之前的對話摘要，請記住這些資訊】\n{summary_text}"},
                    {"role": "model", "parts": "好的，我已經牢牢記住這些摘要資訊了！(｡･∀･)ﾉﾞ 請問接下來要聊什麼呢？"},
                ]
                history_snapshot = summarized_history

            except Exception as e:
                _last_personal_summary_fail_at[user_id] = time.time()
                print(f"記憶濃縮失敗（將於 {PERSONAL_SUMMARY_FAIL_COOLDOWN}s 後重試）: {e}")
                # 不刪 history，下一輪重試

    # 3. 組合注入 prompt
    injected_history = []

    shared_prompt = get_shared_memory_prompt(user_message=message)
    if shared_prompt:
        injected_history.append({"role": "user", "parts": shared_prompt})
        injected_history.append({"role": "model", "parts": "好的，我記住這些共享資訊了 (｡･∀･)"})

    if len(history_snapshot) == 0:
        personal_summary = get_personal_summary(user_id)
        if personal_summary:
            injected_history.append({"role": "user", "parts": f"【系統提示：這是我們之前的對話摘要，請記住這些資訊】\n{personal_summary}"})
            injected_history.append({"role": "model", "parts": "好的，我記住你的個人資訊了 (｡･∀･)ﾉﾞ"})

    injected_history.extend(history_snapshot)

    # 4. 進行對話
    chat = model.start_chat(history=injected_history)
    is_new_chat = len(history_snapshot) == 0

    # 組合特殊成員資訊
    if is_new_chat:
        if identity:
            full_message = f"（使用者名稱：{user_name}，身份：我的{identity}）\n{message}"
        else:
            full_message = f"（使用者名稱：{user_name}）\n{message}"
    else:
        full_message = message

    response = chat.send_message(full_message)

    # 5. 處理 Function Calling
    reply = _handle_function_calls(chat, response)

    # 6. 寫回短期記憶（在鎖內）；若有濃縮就以 summarized_history 重建 deque
    with _chat_histories_lock:
        dq = chat_histories.setdefault(user_id, deque(maxlen=PERSONAL_HISTORY_MAXLEN))
        if summarized_history is not None:
            dq.clear()
            for item in summarized_history:
                dq.append(item)
        dq.append({"role": "user", "parts": full_message})
        dq.append({"role": "model", "parts": reply})

    return reply


# ================================
# Function Calling 工具定義
# ================================

_TOOLS = [{
    "function_declarations": [
        {
            "name": "get_food_recommendation",
            "description": "推薦餐點或餐廳。當使用者詢問吃什麼、推薦食物、早餐、午餐、晚餐時，呼叫此工具。",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "meal_type": {
                        "type": "STRING",
                        "description": "餐别：breakfast（早餐）、lunch（午餐）、dinner（晚餐）"
                    },
                    "food_class": {
                        "type": "STRING",
                        "description": "料理類型：中式、台式、日式、美式，若使用者未主動指定則省略此參數。"
                    },
                    "location": {
                        "type": "STRING",
                        "description": "地點名稱，若使用者有明確指定地點才填入，例如：台北車站、公館，若無提及請直接省略。"
                    }
                },
                "required": ["meal_type"]
            }
        },
        {
            "name": "get_earthquake_info",
            "description": "取得最新地震資訊。當使用者詢問地震、有沒有在搖、有沒有地震時使用。",
            "parameters": {
                "type": "OBJECT",
                "properties": {}
            }
        },
        {
            "name": "get_weather_info",
            "description": "取得天氣預報。當使用者詢問天氣、下雨、溫度、要不要帶傘時使用。",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "city": {
                        "type": "STRING",
                        "description": "城市名稱，例如：臺北、臺中、嘉義、高雄、花蓮，若未指定預設臺北"
                    }
                }
            }
        }
    ]
}]


# ================================
# Function Calling 執行邏輯
# ================================

def _execute_get_food_recommendation(meal_type: str, food_class: str = None, location: str = None) -> str:
    return get_food_recommendation_text(meal_type, food_class, location)


def _execute_get_earthquake_info() -> str:
    return get_earthquake_info_text()


def _execute_get_weather_info(city: str = "臺北") -> str:
    return get_weather_info_text(city)


_TOOL_HANDLERS = {
    "get_food_recommendation": lambda args: _execute_get_food_recommendation(**args),
    "get_earthquake_info":     lambda args: _execute_get_earthquake_info(),
    "get_weather_info":        lambda args: _execute_get_weather_info(**args),
}


def _handle_function_calls(chat, response) -> str:
    """
    處理 Gemini 的 Function Call 回應。
    Gemini 可能連續要求多次 function call，迴圈處理直到得到純文字回覆。
    如果 Gemini 回應被安全機制阻擋或沒有候選，會回傳可讀錯誤訊息，
    而不是直接觸發 IndexError/AttributeError。
    """
    MAX_ROUNDS = 5

    for _ in range(MAX_ROUNDS):
        # 防禦性檢查：確保有候選與內容可用
        candidates = getattr(response, "candidates", None)
        if not candidates:
            fallback_text = getattr(response, "text", None)
            return fallback_text or "無法取得模型回應（候選結果為空或缺失）。"

        first_candidate = candidates[0]
        content = getattr(first_candidate, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if not parts:
            fallback_text = getattr(response, "text", None)
            return fallback_text or "無法取得模型回應內容（content.parts 為空或缺失）。"

        fn_calls = [
            part.function_call
            for part in parts
            if hasattr(part, "function_call") and part.function_call.name
        ]

        if not fn_calls:
            fallback_text = getattr(response, "text", None)
            return fallback_text or "未偵測到可用的函式呼叫，且無可用文字回覆。"

        fn_results = []
        for fn_call in fn_calls:
            fn_name = fn_call.name
            fn_args = dict(fn_call.args)
            print(f"[Function Call] {fn_name}({fn_args})")

            handler = _TOOL_HANDLERS.get(fn_name)
            result = handler(fn_args) if handler else f"未知的工具：{fn_name}"
            print(f"[Function Result] {result}")

            # 構造 function response 物件（相容於 0.7.2 版本）
            # 使用字典格式而非 Part.from_function_response() 以避免 AttributeError
            fn_results.append({
                "function_response": {
                    "name": fn_name,
                    "response": {"result": result}
                }
            })

        # 發送 function call 結果給模型，並獲取後續回應
        # 使用字典格式直接發送，兼容所有版本
        response = chat.send_message(
            [{"function_response": part["function_response"]} for part in fn_results]
        )

    # 超過 MAX_ROUNDS 仍未取得純文字回應時，回傳最後一個 response 的文字或錯誤訊息
    fallback_text = getattr(response, "text", None)
    return fallback_text or "反覆處理函式呼叫後仍無法取得模型文字回應。"


# ================================
# Discord Bot 設定
# ================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='', intents=intents, help_command=None)


#################################################################################################################################################


_api_server_thread: threading.Thread | None = None


def start_api_server_if_enabled():
    global _api_server_thread
    if not SHIZUMU_API_ENABLED:
        print("Shizumu API 已停用（SHIZUMU_API_ENABLED=0）")
        return
    if _api_server_thread and _api_server_thread.is_alive():
        return

    def run_api_server():
        import uvicorn

        uvicorn.run(
            "shizumu_api:app",
            host=SHIZUMU_API_HOST,
            port=SHIZUMU_API_PORT,
            log_level=os.getenv("SHIZUMU_API_LOG_LEVEL", "info"),
        )

    _api_server_thread = threading.Thread(target=run_api_server, daemon=True, name="shizumu-api")
    _api_server_thread.start()
    print(f"Shizumu API 啟動中：http://{SHIZUMU_API_HOST}:{SHIZUMU_API_PORT}")


# [自動更新狀態]
@tasks.loop(seconds=15)
async def activity_auto_change():
    status_w = discord.Status.online
    activity_w = discord.Activity(type=discord.ActivityType.playing, name=random.choice(SHIZUMU_MURMUR))
    await bot.change_presence(status=status_w, activity=activity_w)


# [啟動]
@bot.event
async def on_ready():
    print('目前登入身份：', bot.user)

    if Google_AI_API_key:
        print(f"Gemini AI 已啟用，模型：{GEMINI_MODEL}")
    else:
        print("GOOGLE_AI_API_KEY 未設定，AI 對話功能將停用")

    if CHAT_CHANNEL_ID:
        print(f"群聊頻道模式已啟用，頻道 ID：{CHAT_CHANNEL_ID}")
    else:
        print("未設定 SHIZUMU_CHAT_CHANNEL_ID，群聊頻道模式停用")

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
    embed = discord.Embed(title='頭條新聞', description=today_taipei(), color=0x7e6487)
    for article in get_headline_articles():
        embed.add_field(name=article.title, value=f'[{article.source}]({article.url})', inline=False)
    news_message = await ctx.send('晚餐日報 ' + today_taipei(), embed=embed)
    for emoji in ['📰', '🎮', '🌤']:
        await news_message.add_reaction(emoji)


@bot.event
async def on_raw_reaction_add(payload):
    if payload.member.bot:
        return
    channel = bot.get_channel(payload.channel_id)
    news_message = await channel.fetch_message(payload.message_id)
    emoji = payload.emoji

    if news_message.content == '晚餐日報 ' + today_taipei():
        if emoji.name == "📰":
            google_embed = discord.Embed(title='頭條新聞', description=today_taipei(), color=0x598ad9)
            for article in get_headline_articles():
                google_embed.add_field(name=article.title, value=f'[{article.source}]({article.url})', inline=False)
            await news_message.edit(embed=google_embed)

        elif emoji.name == "🎮":
            gnn_embed = discord.Embed(title='巴哈姆特 GNN 新聞', description=today_taipei(), color=0x598ad9)
            for article in get_headline_articles('https://gnn.gamer.com.tw/rss.xml'):
                gnn_embed.add_field(name=article.title, value='[巴哈姆特](' + article.url + ')', inline=False)
            await news_message.edit(embed=gnn_embed)

        elif emoji.name == "🌤":
            weather_embed = discord.Embed(title='天氣預報 ', description=today_taipei(), color=0x598ad9)
            for loc_name, temp, rain, weat in get_weather_forecast_rows():
                weather_embed.add_field(name=loc_name, value='☂' + rain + '%  🌡' + temp + '°C  ⛅' + weat, inline=False)
            await news_message.edit(embed=weather_embed)


# [指令] 地震
@bot.command()
async def 地震(ctx, *args):
    report = get_earthquake_report()
    if report.web_url or report.image_url:
        embed = discord.Embed(title=report.text, url=report.web_url, color=0x636363)
        if report.image_url:
            embed.set_image(url=report.image_url)
        await ctx.send(embed=embed)
    else:
        await ctx.send(report.text)


async def send_food_recommendation(ctx, result: FoodRecommendation):
    if result.restaurant:
        restaurant = result.restaurant
        embed = discord.Embed(
            title=restaurant.name,
            description=(
                '⭐' + str(restaurant.rating) +
                '  👄' + str(restaurant.user_ratings_total) +
                '  🕓' + str(restaurant.open_now) +
                '  ' + '💵' * int(restaurant.price_level)
            ),
            url=result.maps_url
        )
        embed.set_author(name=(result.search_food or '推薦餐廳') + random.choice(FOOD_ENDINGS))
        await ctx.send(embed=embed)
        return
    await ctx.send(result.message)


# [指令] 午/晚餐吃什麼
@bot.command(aliases=['午餐吃什麼'])
async def 晚餐吃什麼(ctx, *args):
    if len(args) == 0:
        await send_food_recommendation(ctx, get_food_recommendation("lunch" if ctx.invoked_with == "午餐吃什麼" else "dinner"))
    elif len(args) == 1 and args[0] in ALLOWED_FOOD_CLASSES:
        food_class = args[0]
        await send_food_recommendation(ctx, get_food_recommendation("lunch" if ctx.invoked_with == "午餐吃什麼" else "dinner", food_class=food_class))
    elif len(args) == 1 and '式' in args[0]:
        await ctx.send('我不知道' + args[0] + '料理有哪些，請輸入中/台式、日式或美式 º﹃º')
    elif len(args) == 1 and '式' not in args[0]:
        await send_food_recommendation(ctx, get_food_recommendation("lunch" if ctx.invoked_with == "午餐吃什麼" else "dinner", location=args[0]))
    elif len(args) == 2 and args[0] in ALLOWED_FOOD_CLASSES:
        food_class = args[0]
        search_place = args[1]
        await send_food_recommendation(ctx, get_food_recommendation("lunch" if ctx.invoked_with == "午餐吃什麼" else "dinner", food_class=food_class, location=search_place))
    else:
        await ctx.send('確認一下指令是否正確: ```午餐吃什麼 [中式/台式/日式/美式] [地點]``` 參數皆可省略')


# [指令] 早餐吃什麼
@bot.command()
async def 早餐吃什麼(ctx, *args):
    if len(args) == 0:
        await send_food_recommendation(ctx, get_food_recommendation("breakfast"))


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
            member_identity = get_member_identity(user_id)

            reply = await asyncio.get_event_loop().run_in_executor(
                None,
                get_gemini_response,
                user_id,
                ctx.author.display_name,
                message_content,
                member_identity
            )
            # 成功取得回覆才記一次配額 / cooldown
            record_api_usage(user_id)

            if len(reply) > 2000:
                for chunk in [reply[i:i+2000] for i in range(0, len(reply), 2000)]:
                    await ctx.send(chunk)
            else:
                await ctx.send(reply)

        except Exception as e:
            print(f"AI 對話錯誤: {e}")
            await ctx.send("欸欸地瓜，有bug你看一下！(´・ω・`)")


# [指令] 小寒 - 與 Gemini 對話
@bot.command(aliases=['shizumu_doro', 'shizumudoro'])
async def 小寒(ctx, *, message_content: str):
    await _handle_ai_chat(ctx, message_content)


async def _handle_channel_chat(message: discord.Message):
    """處理「群聊頻道」中的被動 / 主動觸發回應"""
    global _last_channel_reply_time

    # 使用「頻道級」每日上限，不再倉用觸發者個人配額
    if not check_channel_limit():
        return

    target = {
        "author_name": message.author.display_name,
        "content": message.content or "",
    }

    try:
        async with message.channel.typing():
            reply = await asyncio.get_event_loop().run_in_executor(
                None, get_gemini_channel_response, target
            )

            if not reply:
                return
            stripped = reply.strip()
            # Gemini 自己選擇不回應
            if stripped in ("[SKIP]", "[skip]") or stripped.startswith("[SKIP"):
                return

            # 成功才計用量 + 冷卻
            record_channel_usage()
            _last_channel_reply_time = time.time()

            # 寫入頻道由 on_message 中 bot self branch 統一處理，這裡不重複記錄
            if len(reply) > 2000:
                for chunk in [reply[i:i+2000] for i in range(0, len(reply), 2000)]:
                    await message.channel.send(chunk)
            else:
                await message.channel.send(reply)
    except Exception as e:
        print(f"[群聊 AI] 錯誤：{e}")


async def _handle_passive_reactions(message: discord.Message):
    """處理問候語、emoji 等被動反應（與 AI 無關）"""
    if '晚安' in message.content and random.randint(1, 100) <= 15:
        await message.channel.send(f"晚安 <:shizimu_sleep:1356313689019650099> , {message.author.display_name}")

    if '早安' in message.content and random.randint(1, 100) <= 15:
        await message.channel.send(f"早安(｡･∀･)ﾉﾞ, {message.author.display_name}")

    if '午安' in message.content and random.randint(1, 100) <= 15:
        await message.channel.send(f"午安(｡･∀･)ﾉﾞ, {message.author.display_name}")

    if '<:shizimu_cry:1356313573487284244>' in message.content:
        await message.channel.send('<:shizimu_cry:1356313573487284244>' * 3)


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


# [指令] 頻道記憶狀態
@bot.command(aliases=['頻道記憶'])
async def channel_memory_status(ctx):
    """查看群聊頻道的記憶狀態"""
    embed = discord.Embed(title="🗨️ 群聊頻道記憶", color=0x7e6487)
    if CHAT_CHANNEL_ID:
        embed.add_field(name="啟用頻道", value=f"<#{CHAT_CHANNEL_ID}>", inline=False)
    else:
        embed.add_field(name="啟用頻道", value="未設定（請設定環境變數 SHIZUMU_CHAT_CHANNEL_ID）", inline=False)

    embed.add_field(
        name="短期歷史",
        value=f"{len(channel_history)} 則（上限 {CHANNEL_HISTORY_MAXLEN}）",
        inline=False
    )

    summary = _channel_summary.get("summary") or "尚無"
    updated = _channel_summary.get("updated") or "未更新"
    display = summary[:1000] + "..." if len(summary) > 1000 else summary
    embed.add_field(name=f"長期摘要（上次更新：{updated}）", value=display, inline=False)

    cooldown_left = max(0, CHANNEL_REPLY_COOLDOWN - (time.time() - _last_channel_reply_time))
    embed.set_footer(text=f"被動回覆冷卻剩餘：{cooldown_left:.1f} 秒")
    await ctx.send(embed=embed)


# [指令] 重置頻道記憶（限管理員）
@bot.command(aliases=['重置頻道記憶'])
async def reset_channel_memory(ctx):
    """清除群聊頻道的短期歷史與長期摘要（限管理員）"""
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("只有管理員才能清除頻道記憶喔 (´・ω・`)")
        return
    channel_history.clear()
    _channel_summary["summary"] = ""
    _channel_summary["updated"] = ""
    save_memories()
    await ctx.send("已清除群聊頻道的所有記憶 (｡･∀･)ﾉﾞ")


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

        # 群聊頻道狀態
        if CHAT_CHANNEL_ID:
            ch_summary = _channel_summary.get("summary") or "尚無"
            ch_updated = _channel_summary.get("updated") or "未更新"
            ch_summary_short = ch_summary[:200] + "..." if len(ch_summary) > 200 else ch_summary
            embed.add_field(
                name="🗨️ 群聊頻道記憶",
                value=(
                    f"頻道：<#{CHAT_CHANNEL_ID}>\n"
                    f"短期歷史：{len(channel_history)} 則 ／ 上限 {CHANNEL_HISTORY_MAXLEN}\n"
                    f"長期摘要（{ch_updated}）：{ch_summary_short}"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="🗨️ 群聊頻道記憶", value="未設定 SHIZUMU_CHAT_CHANNEL_ID", inline=False)

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
        # 把自己的訊息記入頻道歷史（唯一記錄點）。
        # 包含被動回覆、指令回覆、新進成員歡迎訊息等，全部走這一條路。
        if CHAT_CHANNEL_ID and message.channel.id == CHAT_CHANNEL_ID:
            _record_channel_message(message, is_bot=True)
            await _maybe_summarize_channel_async()
        return

    # === 群聊頻道專屬邏輯 ===
    if CHAT_CHANNEL_ID and message.channel.id == CHAT_CHANNEL_ID and Google_AI_API_key:
        # 1. 不論是否回應，都先記入頻道歷史
        _record_channel_message(message, is_bot=False)

        # 2. 決策是否回應
        should, reason = should_respond(message)
        if should:
            # 冷卻：點名類觸發不受冷卻限制
            bypass_cooldown = reason in ("mention", "reply_to_bot", "name_called")
            if not bypass_cooldown and (time.time() - _last_channel_reply_time) < CHANNEL_REPLY_COOLDOWN:
                print(f"[群聊] 冷卻中，跳過（reason={reason}）")
            else:
                print(f"[群聊] 回應觸發：{reason}")
                await _handle_channel_chat(message)

        # 不論有沒有回應，都走原本的問候/emoji 反應，並讓指令仍可被觸發
        await _handle_passive_reactions(message)
        await bot.process_commands(message)
        # 背景排程頻道濃縮（如有需要）
        await _maybe_summarize_channel_async()
        return

    # === 其他頻道：保留原本行為 ===
    # @ 標記 bot 時觸發 AI 對話（個人記憶模式）
    if bot.user in message.mentions and Google_AI_API_key:
        message_content = message.content
        for mention in message.mentions:
            message_content = message_content.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        message_content = message_content.strip()
        if message_content:
            ctx = await bot.get_context(message)
            await _handle_ai_chat(ctx, message_content)
            return

    await _handle_passive_reactions(message)
    await bot.process_commands(message)



start_api_server_if_enabled()
bot.run(Discord_token)
