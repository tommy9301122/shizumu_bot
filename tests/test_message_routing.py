import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import shizumu_bot as app


class MessageRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = app.MemoryStore(Path(directory.name) / "memory.json")
        self.history = store.channel_history
        self.channel_chat = AsyncMock()
        self.process_commands = AsyncMock()
        self.passive = AsyncMock()
        self.summarize = AsyncMock()
        self.should_respond = Mock(return_value=(True, "name_called"))
        patches = [
            patch.object(app, "memory_store", store),
            patch.object(app, "Google_AI_API_key", "test-key"),
            patch.object(app, "channel_history", self.history),
            patch.object(app, "_channel_summary", {"summary": "先前大家決定週末聚餐"}),
            patch.object(app, "get_shared_memory_prompt", return_value=""),
            patch.object(app, "_handle_channel_chat", self.channel_chat),
            patch.object(app, "_handle_passive_reactions", self.passive),
            patch.object(app, "_maybe_summarize_channel_async", self.summarize),
            patch.object(app, "should_respond", self.should_respond),
            patch.object(app.bot, "process_commands", self.process_commands),
            patch.object(app.bot._connection, "user", SimpleNamespace(id=999)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def message(self, content, channel_id=None):
        return SimpleNamespace(
            _state=app.bot._connection,
            content=content,
            author=SimpleNamespace(id=123, display_name="使用者", bot=False),
            channel=SimpleNamespace(id=channel_id or app.CHAT_CHANNEL_ID),
            mentions=[],
        )

    async def test_chat_commands_reply_once_with_channel_context(self):
        for name in ("小寒", "shizumu_doro", "shizumudoro"):
            with self.subTest(command=name):
                self.history.clear()
                self.channel_chat.reset_mock()
                previous = self.message("大家想吃火鍋")
                app._record_channel_message(previous, is_bot=False)
                message = self.message(f"{name} 晚安，剛剛大家聊了什麼？")

                async def check_context(target):
                    context = app.build_channel_context({"content": target.content})
                    text = "\n".join(item["parts"] for item in context)
                    self.assertIn("大家想吃火鍋", text)
                    self.assertIn("先前大家決定週末聚餐", text)
                    self.assertIn(message.content, text)

                self.channel_chat.side_effect = check_context
                await app.on_message(message)

                self.channel_chat.assert_awaited_once_with(message)
                self.process_commands.assert_not_awaited()
                self.passive.assert_not_awaited()
                self.should_respond.assert_not_called()
                self.assertEqual(len(self.history), 2)
        self.assertEqual(self.summarize.await_count, 3)

    async def test_other_channel_keeps_command_dispatch(self):
        message = self.message("小寒 你好", channel_id=42)
        await app.on_message(message)
        self.process_commands.assert_awaited_once_with(message)
        self.channel_chat.assert_not_awaited()
        self.assertEqual(len(self.history), 0)

    async def test_other_commands_still_dispatch_in_chat_channel(self):
        self.should_respond.return_value = (False, "skip")
        message = self.message("reset_memory")
        await app.on_message(message)
        self.process_commands.assert_awaited_once_with(message)
        self.channel_chat.assert_not_awaited()
        self.summarize.assert_awaited_once()

    async def test_normal_chat_keeps_automatic_reply(self):
        message = self.message("我想問小寒一個問題")
        await app.on_message(message)
        self.should_respond.assert_called_once_with(message)
        self.channel_chat.assert_awaited_once_with(message)

    async def test_missing_api_key_keeps_existing_command_dispatch(self):
        message = self.message("小寒 你好")
        with patch.object(app, "Google_AI_API_key", None):
            await app.on_message(message)
        self.process_commands.assert_awaited_once_with(message)
        self.channel_chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
