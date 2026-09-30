import tempfile
import asyncio
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import shizumu_bot as app


class MemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = app.MemoryStore(Path(directory.name) / "memory.json")
        for name, value in {
            "memory_store": self.store,
            "chat_histories": self.store.chat_histories,
            "channel_history": self.store.channel_history,
            "_personal_summaries": self.store.personal,
            "_channel_summary": self.store.channel,
            "_chat_histories_lock": self.store.lock,
            "_memories_loaded": False,
            "_channel_memory_generation": 0,
            "_channel_summary_pending": False,
            "Google_AI_API_key": "test",
        }.items():
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def reloaded(self):
        store = app.MemoryStore(self.store.path)
        store.load()
        return store

    def seed(self):
        with self.store.transaction():
            self.store.chat_histories["42"] = deque([
                {"role": "user", "parts": "hello"}
            ], maxlen=24)
            self.store.personal["42"] = {"summary": "personal"}
            self.store.channel["summary"] = "channel"
            self.store.channel_history.append({"content": "hi"})

    async def test_resets_clear_disk_and_live_memory(self):
        self.seed()
        ctx = SimpleNamespace(author=SimpleNamespace(id=42), send=AsyncMock())
        await app.reset_memory.callback(ctx)
        loaded = self.reloaded()
        self.assertFalse(loaded.personal)
        self.assertFalse(loaded.chat_histories)
        self.assertTrue(loaded.channel_history)
        ctx.author.id = app.ADMIN_IDS[0]
        await app.reset_channel_memory.callback(ctx)
        loaded = self.reloaded()
        self.assertFalse(loaded.channel_history)
        self.assertEqual(loaded.channel["summary"], "")

    async def test_failed_resets_report_failure_and_preserve_memory(self):
        self.seed()
        for command, user_id in ((app.reset_memory, 42), (app.reset_channel_memory, app.ADMIN_IDS[0])):
            ctx = SimpleNamespace(author=SimpleNamespace(id=user_id), send=AsyncMock())
            with patch("shizumu_memory.os.replace", side_effect=OSError("full")):
                with self.assertLogs(level="ERROR"):
                    await command.callback(ctx)
            self.assertIn("未完成重置", ctx.send.call_args.args[0])
            self.assertEqual(self.store._state(), self.reloaded()._state())
            self.assertTrue(self.store.chat_histories)
            self.assertTrue(self.store.channel_history)

    async def test_reconnect_loads_only_once(self):
        self.seed()
        task = Mock()
        task.is_running.return_value = True
        with patch.object(self.store, "load", wraps=self.store.load) as load:
            with patch.object(app, "activity_auto_change", task), patch.object(
                app, "youtube_live_monitor", SimpleNamespace(run=task)
            ):
                await app.on_ready()
                self.store.channel["summary"] = "live state"
                await app.on_ready()
            load.assert_called_once()
        self.assertEqual(self.store.channel["summary"], "live state")

    async def test_personal_conversation_is_saved_and_restored(self):
        model = Mock()
        with patch.object(app.genai, "configure"), patch.object(
            app.genai, "GenerativeModel", return_value=model
        ), patch.object(app, "_handle_function_calls", return_value="answer"):
            app.get_gemini_response("42", "Alice", "question")
        history = self.reloaded().chat_histories["42"]
        self.assertEqual(len(history), 2)
        self.assertIn("question", history[0]["parts"])
        self.assertEqual(history[1]["parts"], "answer")

    async def test_channel_summary_preserves_new_messages(self):
        with self.store.transaction():
            self.store.channel_history.extend({
                "content": str(i), "author_name": "Alice", "is_bot": False,
                "timestamp": "12:00",
            } for i in range(20))
        def generate(prompt):
            with self.store.transaction():
                self.store.channel_history.append({"content": "arrived during summary"})
            return SimpleNamespace(text="new summary")
        model = Mock()
        model.generate_content.side_effect = generate
        with patch.object(app.genai, "configure"), patch.object(
            app.genai, "GenerativeModel", return_value=model
        ):
            app._try_summarize_channel()
        loaded = self.reloaded()
        self.assertEqual(loaded.channel["summary"], "new summary")
        self.assertEqual(len(loaded.channel_history), 11)
        self.assertEqual(loaded.channel_history[-1]["content"], "arrived during summary")

    async def test_inflight_reply_cannot_restore_reset_personal_memory(self):
        def reply(chat, response):
            with self.store.transaction():
                self.store.chat_histories.pop("42", None)
                self.store.personal.pop("42", None)
            return "late reply"
        with patch.object(app.genai, "configure"), patch.object(app.genai, "GenerativeModel"), patch.object(
            app, "_handle_function_calls", side_effect=reply
        ):
            app.get_gemini_response("42", "Alice", "hello")
        self.assertFalse(self.reloaded().chat_histories)

    async def test_inflight_channel_summary_cannot_restore_reset_memory(self):
        with self.store.transaction():
            self.store.channel_history.extend({
                "content": str(i), "author_name": "Alice", "is_bot": False,
                "timestamp": "12:00",
            } for i in range(20))
        started, resume = threading.Event(), threading.Event()
        def generate(prompt):
            started.set()
            if not resume.wait(5):
                raise TimeoutError("test did not resume summary")
            return SimpleNamespace(text="stale summary")
        model = Mock()
        model.generate_content.side_effect = generate
        with patch.object(app.genai, "configure"), patch.object(
            app.genai, "GenerativeModel", return_value=model
        ):
            pending = asyncio.create_task(asyncio.to_thread(app._try_summarize_channel))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                ctx = SimpleNamespace(author=SimpleNamespace(id=app.ADMIN_IDS[0]), send=AsyncMock())
                await app.reset_channel_memory.callback(ctx)
            finally:
                resume.set()
                await pending
        loaded = self.reloaded()
        self.assertFalse(loaded.channel_history)
        self.assertEqual(loaded.channel["summary"], "")

    async def test_channel_message_is_saved_before_summary(self):
        message = SimpleNamespace(
            author=SimpleNamespace(id=42, display_name="Alice"),
            content="new message", mentions=[],
        )
        app._record_channel_message(message, is_bot=False)
        loaded = self.reloaded()
        self.assertEqual(loaded.channel_history[0]["content"], "new message")
        self.assertEqual(loaded.channel["summary"], "")
