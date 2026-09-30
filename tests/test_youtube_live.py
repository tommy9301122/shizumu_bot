import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from youtube_live import APIError, DESTINATION, LiveMonitor, State, TAIPEI, UTC, YouTubeAPI, create_monitor, dense_window


def local(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=TAIPEI)


class ScheduleTests(unittest.TestCase):
    def test_boundaries_and_thursday(self):
        for day, start in ((28, 20), (29, 20), (30, 20), (25, 20), (27, 20), (26, 18)):
            with self.subTest(day=day):
                self.assertFalse(dense_window(local(day, start, 44)))
                self.assertTrue(dense_window(local(day, start, 45)))
                self.assertTrue(dense_window(local(day, start + 1, 59)))
                self.assertFalse(dense_window(local(day, start + 2)))
        self.assertFalse(dense_window(local(24, 21)))
        self.assertTrue(dense_window(local(28, 21).astimezone(UTC)))

    def test_quota_persists_and_resets_at_pacific_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            state = State(path)
            now = local(30, 14, 59)  # 23:59 previous day in Pacific daylight time
            self.assertTrue(all(state.reserve_search(now) for _ in range(95)))
            self.assertFalse(state.reserve_search(now))
            state.db.close()
            state = State(path)
            self.assertFalse(state.reserve_search(now))
            self.assertTrue(state.reserve_search(now + timedelta(minutes=1)))
            state.db.close()

    def test_disabled_or_missing_key(self):
        with patch.dict("os.environ", {"YOUTUBE_API_KEY": "", "YOUTUBE_LIVE_ENABLED": "1"}):
            self.assertIsNone(create_monitor(Mock()))
        with patch.dict("os.environ", {"YOUTUBE_API_KEY": "unused", "YOUTUBE_LIVE_ENABLED": "0"}):
            self.assertIsNone(create_monitor(Mock()))

    def test_api_errors_do_not_expose_key(self):
        import requests
        api = YouTubeAPI("secret-api-key")
        with patch("youtube_live.requests.get", side_effect=requests.ConnectionError("URL?key=secret-api-key")):
            with self.assertRaises(APIError) as error:
                api.request("search")
            self.assertNotIn("secret-api-key", str(error.exception))
        response = Mock(status_code=403)
        response.json.return_value = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
        with patch("youtube_live.requests.get", return_value=response):
            with self.assertRaisesRegex(APIError, "^quotaExceeded$"):
                api.request("search")


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = local(30, 21)
        self.history_messages = []
        self.channel = Mock()
        self.channel.guild.emojis = []
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, read_message_history=True, send_messages=True, mention_everyone=True
        )

        async def history(**kwargs):
            for message in self.history_messages:
                yield message

        self.channel.history = history
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=123))
        self.bot = Mock()
        self.bot.user.id = 42
        self.bot.get_channel.return_value = self.channel
        self.monitor = LiveMonitor(self.bot, "unused", Path(self.directory.name) / "state.db")
        self.monitor.state.put("channel_id", "UCtarget")
        self.video = {
            "id": "video1", "snippet": {"channelId": "UCtarget", "liveBroadcastContent": "live"},
            "liveStreamingDetails": {"actualStartTime": self.now.isoformat()},
        }
        self.monitor.api_call = AsyncMock(side_effect=self.api_response)

    async def asyncTearDown(self):
        self.monitor.state.db.close()
        self.directory.cleanup()

    async def api_response(self, resource, **params):
        if resource == "search":
            return {"items": [{"id": {"videoId": "video1"}}]}
        if resource == "videos":
            return {"items": [self.video]}
        return {"items": [{"id": "UCtarget"}]}

    async def test_live_message_mentions_and_dedup_after_restart(self):
        await self.monitor.tick(self.now)
        self.channel.send.assert_awaited_once()
        args = self.channel.send.call_args
        self.assertEqual(args.args[0], "@everyone 靜靜子直播開始了！晚餐們一起來看台:shizumu_splash:\nhttps://www.youtube.com/watch?v=video1")
        mentions = args.kwargs["allowed_mentions"].to_dict()
        self.assertEqual(mentions["parse"], ["everyone"])
        self.bot.get_channel.assert_called_with(DESTINATION)
        self.monitor.state.db.close()
        self.monitor.state = State(Path(self.directory.name) / "state.db")
        self.monitor.startup = True
        await self.monitor.tick(self.now + timedelta(minutes=1))
        self.channel.send.assert_awaited_once()

    async def test_custom_emoji(self):
        emoji = Mock(name="emoji")
        emoji.name = "shizumu_splash"
        emoji.is_usable.return_value = True
        emoji.__str__ = Mock(return_value="<:shizumu_splash:12345>")
        self.channel.guild.emojis = [emoji]
        await self.monitor.tick(self.now)
        self.assertIn("<:shizumu_splash:12345>", self.channel.send.call_args.args[0])

    async def test_no_results_upcoming_ended_or_other_channel(self):
        self.monitor.api_call = AsyncMock(return_value={"items": []})
        await self.monitor.tick(self.now)
        self.channel.send.assert_not_awaited()
        self.monitor.state.discover(["video1"])
        self.video["liveStreamingDetails"] = {"scheduledStartTime": self.now.isoformat()}
        await self.monitor.notify(self.video, self.now)
        self.video["liveStreamingDetails"] = {"actualEndTime": self.now.isoformat()}
        await self.monitor.notify(self.video, self.now)
        self.assertEqual(self.monitor.state.pending(), [])
        self.video["liveStreamingDetails"] = {"actualStartTime": self.now.isoformat()}
        self.video["snippet"]["channelId"] = "UCother"
        await self.monitor.notify(self.video, self.now)
        self.channel.send.assert_not_awaited()

    async def test_startup_off_hours_then_two_hour_interval(self):
        now = local(24, 10)  # Thursday
        self.monitor.api_call = AsyncMock(return_value={"items": []})
        await self.monitor.tick(now)
        self.assertEqual(self.monitor.api_call.await_count, 1)
        await self.monitor.tick(now + timedelta(minutes=119))
        self.assertEqual(self.monitor.api_call.await_count, 1)
        await self.monitor.tick(now + timedelta(hours=2))
        self.assertEqual(self.monitor.api_call.await_count, 2)

    async def test_dense_search_then_slow_after_notification(self):
        self.monitor.api_call = AsyncMock(return_value={"items": []})
        await self.monitor.tick(self.now)
        await self.monitor.tick(self.now + timedelta(seconds=59))
        self.assertEqual(self.monitor.api_call.await_count, 1)
        await self.monitor.tick(self.now + timedelta(seconds=60))
        self.assertEqual(self.monitor.api_call.await_count, 2)
        self.monitor.state.discover(["video1"])
        self.monitor.state.finish("video1", 123, self.now)
        await self.monitor.tick(self.now + timedelta(minutes=2))
        self.assertEqual(self.monitor.api_call.await_count, 2)

    async def test_send_timeout_recovers_from_history(self):
        self.channel.send.side_effect = TimeoutError()
        await self.monitor.tick(self.now)
        self.assertEqual(self.monitor.state.pending(), ["video1"])
        self.history_messages.append(SimpleNamespace(
            author=SimpleNamespace(id=42), content="@everyone\nhttps://www.youtube.com/watch?v=video1",
            id=987, created_at=self.now,
        ))
        await self.monitor.tick(self.now + timedelta(minutes=1))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.monitor.state.pending(), [])

    async def test_failed_send_retries_and_history_failure_defers(self):
        original_history = self.channel.history
        self.channel.send.side_effect = RuntimeError()
        await self.monitor.tick(self.now)
        self.channel.send.side_effect = None
        self.channel.history = Mock(side_effect=RuntimeError())
        await self.monitor.tick(self.now + timedelta(minutes=1))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.monitor.state.pending(), ["video1"])
        self.channel.history = original_history
        await self.monitor.tick(self.now + timedelta(minutes=2))
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(self.monitor.state.pending(), [])

    async def test_missing_permission_does_not_send(self):
        self.channel.permissions_for.return_value.mention_everyone = False
        await self.monitor.tick(self.now)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.monitor.state.pending(), ["video1"])

    async def test_quota_failure_blocks_until_next_quota_day(self):
        self.monitor.api_call = AsyncMock(side_effect=APIError("quotaExceeded"))
        with self.assertRaises(APIError):
            await self.monitor.tick(self.now)
        await self.monitor.tick(self.now + timedelta(minutes=1))
        self.assertEqual(self.monitor.api_call.await_count, 1)
        self.monitor.api_call = AsyncMock(return_value={"items": []})
        await self.monitor.tick(self.now + timedelta(days=1))
        self.monitor.api_call.assert_awaited_once()

    async def test_local_quota_limit_prevents_search(self):
        for _ in range(95):
            self.monitor.state.reserve_search(self.now)
        await self.monitor.tick(self.now)
        self.monitor.api_call.assert_not_awaited()

    async def test_resolves_handle_and_fetches_uncached_discord_channel(self):
        self.monitor.state.put("channel_id", "")
        self.bot.get_channel.return_value = None
        self.bot.fetch_channel = AsyncMock(return_value=self.channel)
        await self.monitor.tick(self.now)
        self.monitor.api_call.assert_any_await("channels", part="id", forHandle="@shizumushizumu")
        self.bot.fetch_channel.assert_awaited_once_with(DESTINATION)
        self.channel.send.assert_awaited_once()

    async def test_search_network_error_does_not_lose_pending(self):
        self.monitor.state.discover(["video1"])

        async def response(resource, **params):
            if resource == "search":
                raise APIError("YouTube network/response error")
            return {"items": [self.video]}

        self.monitor.api_call.side_effect = response
        await self.monitor.tick(self.now)
        self.channel.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
