import datetime
import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
