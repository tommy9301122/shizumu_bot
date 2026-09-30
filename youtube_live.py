"""Scheduled YouTube live notifications; one running bot per state database."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
import requests

log = logging.getLogger(__name__)
UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
PACIFIC = ZoneInfo("America/Los_Angeles")
HANDLE = "@shizumushizumu"
DESTINATION = 1310279691382558771
DAILY_LIMIT = 95


def dense_window(now):
    local = now.astimezone(TAIPEI)
    minute = local.hour * 60 + local.minute
    start = 18 * 60 + 45 if local.weekday() == 5 else 20 * 60 + 45
    return local.weekday() != 3 and start <= minute < start + 75


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS quota (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY, message_id TEXT, notified_at TEXT
            );
        """)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))

    def reserve_search(self, now):
        day = now.astimezone(PACIFIC).date().isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO quota VALUES (?, 0)", (day,))
            cursor = self.db.execute(
                "UPDATE quota SET count=count+1 WHERE day=? AND count<?", (day, DAILY_LIMIT)
            )
        return cursor.rowcount == 1

    def discover(self, ids):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO videos(id) VALUES (?)", [(v,) for v in ids])

    def pending(self):
        return [r[0] for r in self.db.execute("SELECT id FROM videos WHERE message_id IS NULL")]

    def finish(self, video_id, message_id, now):
        with self.db:
            self.db.execute("UPDATE videos SET message_id=?, notified_at=? WHERE id=?",
                            (str(message_id), now.isoformat(), video_id))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('notified_day', ?)",
                            (now.astimezone(TAIPEI).date().isoformat(),))

    def discard(self, video_id):
        with self.db:
            self.db.execute("DELETE FROM videos WHERE id=? AND message_id IS NULL", (video_id,))


class APIError(Exception):
    pass


class YouTubeAPI:
    def __init__(self, key):
        self.key = key

    def request(self, resource, **params):
        # Never log request exceptions/URLs: the query contains the API key.
        try:
            response = requests.get(f"https://www.googleapis.com/youtube/v3/{resource}",
                                    params={"key": self.key, **params}, timeout=(5, 20))
            body = response.json()
        except (requests.RequestException, ValueError):
            raise APIError("YouTube network/response error") from None
        if response.status_code != 200:
            reasons = [e.get("reason") for e in body.get("error", {}).get("errors", [])]
            reason = next((r for r in reasons if r in {
                "quotaExceeded", "dailyLimitExceeded", "keyInvalid", "accessNotConfigured",
                "forbidden", "rateLimitExceeded"
            }), "requestRejected")
            raise APIError(reason)
        return body


class LiveMonitor:
    def __init__(self, bot, key, path):
        self.bot = bot
        self.state = State(path)
        self.api = YouTubeAPI(key)
        self.startup = True
        self.lock = asyncio.Lock()

    async def api_call(self, resource, **params):
        return await asyncio.to_thread(self.api.request, resource, **params)

    def search_due(self, now):
        last = float(self.state.get("last_search", "0"))
        elapsed = now.timestamp() - last
        notified = self.state.get("notified_day") == now.astimezone(TAIPEI).date().isoformat()
        interval = 60 if dense_window(now) and not notified else 7200
        return elapsed >= (60 if self.startup else interval)

    async def search(self, now):
        channel_id = self.state.get("channel_id")
        if not channel_id:
            data = await self.api_call("channels", part="id", forHandle=HANDLE)
            if not data.get("items"):
                raise APIError("YouTube channel handle could not be resolved")
            channel_id = data["items"][0]["id"]
            self.state.put("channel_id", channel_id)
        self.state.put("last_search", now.timestamp())
        self.startup = False
        token = None
        while True:
            if not self.state.reserve_search(now):
                log.warning("YouTube daily search limit reached (95)")
                return
            params = dict(part="snippet", channelId=channel_id, type="video",
                          eventType="live", maxResults=50, safeSearch="none")
            if token:
                params["pageToken"] = token
            result = await self.api_call("search", **params)
            self.state.discover(item["id"]["videoId"] for item in result.get("items", []))
            token = result.get("nextPageToken")
            if not token:
                return

    async def notify(self, video, now):
        video_id = video["id"]
        details = video.get("liveStreamingDetails", {})
        if details.get("actualEndTime"):
            self.state.discard(video_id)
            return
        if (not details.get("actualStartTime") or
                video.get("snippet", {}).get("liveBroadcastContent") != "live"):
            return
        if video.get("snippet", {}).get("channelId") != self.state.get("channel_id"):
            self.state.discard(video_id)
            return
        channel = self.bot.get_channel(DESTINATION) or await self.bot.fetch_channel(DESTINATION)
        permissions = channel.permissions_for(channel.guild.me)
        if not all((permissions.view_channel, permissions.read_message_history,
                    permissions.send_messages, permissions.mention_everyone)):
            log.error("YouTube notification: missing Discord view/history/send/mention-everyone permissions")
            return
        url = f"https://www.youtube.com/watch?v={video_id}"
        started = datetime.fromisoformat(details["actualStartTime"].replace("Z", "+00:00"))
        # Also recovers a successful send followed by a crash before the DB commit.
        async for message in channel.history(limit=None, after=started - timedelta(seconds=1)):
            if message.author.id == self.bot.user.id and url in message.content.split():
                self.state.finish(video_id, message.id, message.created_at)
                return
        emoji = discord.utils.get(channel.guild.emojis, name="shizumu_splash")
        splash = str(emoji) if emoji and emoji.is_usable() else ":shizumu_splash:"
        message = await channel.send(
            f"@everyone 靜靜子直播開始了！晚餐們一起來看台{splash}\n{url}",
            allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False,
                                                     replied_user=False),
        )
        self.state.finish(video_id, message.id, now)
        log.info("YouTube live notification sent: %s", video_id)

    async def tick(self, now=None):
        now = now or datetime.now(UTC)
        async with self.lock:
            if self.state.get("blocked_day") == now.astimezone(PACIFIC).date().isoformat():
                return
            try:
                if self.search_due(now):
                    await self.search(now)
            except APIError as error:
                if str(error) in ("quotaExceeded", "dailyLimitExceeded"):
                    self.state.put("blocked_day", now.astimezone(PACIFIC).date().isoformat())
                    raise
                log.warning("YouTube search failed: %s", error)
            pending = self.state.pending()
            for offset in range(0, len(pending), 50):
                batch = pending[offset:offset + 50]
                result = await self.api_call("videos", part="snippet,liveStreamingDetails",
                                             id=",".join(batch))
                found = {v["id"] for v in result.get("items", [])}
                for missing in set(batch) - found:
                    self.state.discard(missing)
                for video in result.get("items", []):
                    try:
                        await self.notify(video, now)
                    except Exception as error:
                        # Keep pending; next tick checks history before attempting another send.
                        log.warning("YouTube notification deferred (%s)", type(error).__name__)

    @tasks.loop(seconds=60)
    async def run(self):
        try:
            await self.tick()
        except APIError as error:
            if str(error) in ("quotaExceeded", "dailyLimitExceeded"):
                self.state.put("blocked_day", datetime.now(PACIFIC).date().isoformat())
            log.warning("YouTube monitoring failed: %s", error)
        except Exception as error:
            log.error("YouTube monitoring failed (%s)", type(error).__name__)

    @run.before_loop
    async def before_run(self):
        await self.bot.wait_until_ready()


def create_monitor(bot):
    if os.getenv("YOUTUBE_LIVE_ENABLED", "1").lower() in ("0", "false", "no"):
        return None
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        log.warning("YOUTUBE_API_KEY is not configured; live notifications disabled")
        return None
    return LiveMonitor(bot, key, os.getenv("YOUTUBE_LIVE_STATE_PATH", "youtube_live.sqlite3"))
