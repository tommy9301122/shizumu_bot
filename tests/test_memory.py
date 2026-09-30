import datetime
import json
import tempfile
import unittest
from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from shizumu_memory import MemoryStore, bigram_relevant


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "memory.json"
        self.store = MemoryStore(
            self.path,
            max_shared_facts=2,
            today=lambda: datetime.date(2026, 9, 30),
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_shared_facts_are_capped_filtered_and_persisted(self):
        self.store.add_shared_fact("小寒喜歡遊戲")
        self.store.add_shared_fact("今天適合吃拉麵")
        self.store.add_shared_fact("靜靜子正在直播")

        self.assertEqual(self.store.shared["facts"], ["今天適合吃拉麵", "靜靜子正在直播"])
        self.assertEqual(self.store.shared["updated"], "2026-09-30")
        self.assertIn("靜靜子正在直播", self.store.shared_prompt("靜靜子直播"))
        self.assertNotIn("今天適合吃拉麵", self.store.shared_prompt("靜靜子直播"))

        loaded = MemoryStore(self.path)
        shared_reference = loaded.shared
        loaded.load()
        self.assertIs(loaded.shared, shared_reference)
        self.assertEqual(loaded.shared, self.store.shared)

    def test_personal_and_channel_summary_lifecycle(self):
        self.store.save_personal_summary("42", "  喜歡遊戲  ")
        self.assertEqual(self.store.get_personal_summary("42"), "喜歡遊戲")
        self.store.save_personal_summary("42", "   ")
        self.assertEqual(self.store.get_personal_summary("42"), "喜歡遊戲")
        self.assertTrue(self.store.remove_personal_summary("42"))
        self.assertFalse(self.store.remove_personal_summary("42"))

        self.store.set_channel_summary("熱鬧的頻道")
        self.assertEqual(self.store.channel["summary"], "熱鬧的頻道")
        self.store.clear_channel_summary()
        self.assertEqual(self.store.channel, {"summary": "", "updated": ""})

    def test_invalid_file_resets_existing_state(self):
        self.store.shared["facts"].append("舊資料")
        self.path.write_text("not-json", encoding="utf-8")
        self.store.load()
        self.assertEqual(self.store.shared, {"facts": [], "updated": ""})
        self.assertEqual(self.store.personal, {})
        self.assertEqual(self.store.channel, {"summary": "", "updated": ""})

    def test_remove_and_clear_shared_facts(self):
        self.store.add_shared_fact("第一條")
        self.store.add_shared_fact("第二條")
        self.assertEqual(self.store.remove_shared_fact(0), "第一條")
        self.store.clear_shared_facts()
        self.assertEqual(self.store.shared["facts"], [])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["shared"]["facts"], [])

    def test_bigram_relevance(self):
        self.assertTrue(bigram_relevant("小寒喜歡遊戲", "聊聊小寒吧"))
        self.assertFalse(bigram_relevant("小寒", "天氣"))

    def test_all_memories_survive_restart_and_history_is_bounded(self):
        self.store.path = self.path.parent / "nested" / "memory.json"
        with self.store.transaction():
            self.store.shared["facts"].append("共享")
            self.store.personal["42"] = {"summary": "個人摘要"}
            self.store.channel["summary"] = "頻道摘要"
            self.store.chat_histories["42"] = deque(
                [{"role": "user", "parts": str(i)} for i in range(40)], maxlen=24
            )
            self.store.channel_history.extend({"content": str(i)} for i in range(40))
        loaded = MemoryStore(self.store.path)
        loaded.load()
        self.assertEqual(loaded._state(), self.store._state())
        self.assertEqual(loaded.chat_histories["42"].maxlen, 24)
        self.assertEqual(len(loaded.chat_histories["42"]), 24)
        self.assertEqual(loaded.channel_history.maxlen, 30)
        self.assertEqual(len(loaded.channel_history), 30)

    def test_old_file_without_recent_history_loads(self):
        self.path.write_text(json.dumps({
            "shared": {"facts": ["old"], "updated": ""},
            "personal": {"42": {"summary": "old personal"}},
            "channel": {"summary": "old channel", "updated": ""},
        }), encoding="utf-8")
        self.store.load()
        self.assertEqual(self.store.shared["facts"], ["old"])
        self.assertEqual(self.store.get_personal_summary("42"), "old personal")
        self.assertEqual(self.store.channel["summary"], "old channel")
        self.assertEqual(self.store.chat_histories, {})
        self.assertFalse(self.store.channel_history)

    def test_concurrent_transactions_preserve_every_update(self):
        def add(index):
            with self.store.transaction():
                self.store.chat_histories[str(index)] = deque([
                    {"role": "user", "parts": str(index)}
                ], maxlen=24)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(40)))
        loaded = MemoryStore(self.path)
        loaded.load()
        self.assertEqual(loaded._state(), self.store._state())
        self.assertEqual(len(loaded.chat_histories), 40)

    def test_failed_commit_preserves_disk_and_rolls_back_live_state(self):
        self.store.add_shared_fact("keep")
        before = self.path.read_bytes()
        with patch("shizumu_memory.os.replace", side_effect=OSError("disk failure")):
            with self.assertLogs(level="ERROR"), self.assertRaises(OSError):
                self.store.clear_shared_facts()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.shared["facts"], ["keep"])
        self.assertFalse(list(self.path.parent.glob(".memory.*.json.tmp")))


if __name__ == "__main__":
    unittest.main()
