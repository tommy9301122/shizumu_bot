"""Persistent memory storage shared by Discord and Gemini features."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import tempfile
import threading
from collections.abc import Callable


def bigram_relevant(fact: str, user_message: str) -> bool:
    """Return whether two strings share at least one two-character sequence."""

    def bigrams(text: str) -> set[str]:
        return {text[index:index + 2] for index in range(len(text) - 1)}

    return bool(bigrams(fact) & bigrams(user_message))


class MemoryStore:
    """Own and atomically persist shared, personal, and channel memories."""

    def __init__(
        self,
        path: str | pathlib.Path = "memory.json",
        max_shared_facts: int = 50,
        *,
        today: Callable[[], datetime.date] = datetime.date.today,
    ) -> None:
        self.path = pathlib.Path(path)
        self.max_shared_facts = max_shared_facts
        self._today = today
        self._lock = threading.Lock()
        # These dictionaries retain their identity so callers may safely keep a
        # reference while load() refreshes their contents.
        self.shared: dict = {"facts": [], "updated": ""}
        self.personal: dict[str, dict] = {}
        self.channel: dict = {"summary": "", "updated": ""}

    def _replace_state(self, data: dict) -> None:
        self.shared.clear()
        self.shared.update(data.get("shared", {"facts": [], "updated": ""}))
        self.personal.clear()
        self.personal.update(data.get("personal", {}))
        self.channel.clear()
        self.channel.update(data.get("channel", {"summary": "", "updated": ""}))

    def load(self) -> None:
        """Load state from disk, falling back to empty state for invalid files."""
        if not self.path.exists():
            print("[記憶] 未找到記憶檔，將使用預設記憶結構。")
            return

        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                raise json.JSONDecodeError("Empty memory file", raw, 0)
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as error:
            print(f"[記憶][警告] 載入記憶檔失敗 ({error!r})，將使用預設記憶結構。")
            with self._lock:
                self._replace_state({})
            return

        with self._lock:
            self._replace_state(data)
            shared_count = len(self.shared["facts"])
            personal_count = len(self.personal)
            has_channel_summary = bool(self.channel.get("summary"))
        print(
            f"[記憶] 已載入共享記憶 {shared_count} 條，"
            f"個人摘要 {personal_count} 位，"
            f"頻道摘要 {'有' if has_channel_summary else '無'}"
        )

    def save(self) -> None:
        """Write the current state using an atomic file replacement."""
        with self._lock:
            data = {
                "shared": self.shared,
                "personal": self.personal,
                "channel": self.channel,
            }
            payload = json.dumps(data, ensure_ascii=False, indent=2)

            target_dir = str(self.path.parent) if str(self.path.parent) else "."
            temp_fd, temp_path = tempfile.mkstemp(
                prefix=".memory.", suffix=".json.tmp", dir=target_dir
            )
            try:
                with os.fdopen(temp_fd, "w", encoding="utf-8") as file:
                    file.write(payload)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp_path, self.path)
            except Exception:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise

    def add_shared_fact(self, fact: str) -> None:
        with self._lock:
            self.shared["facts"].append(fact)
            if len(self.shared["facts"]) > self.max_shared_facts:
                self.shared["facts"].pop(0)
            self.shared["updated"] = str(self._today())
        self.save()

    def remove_shared_fact(self, index: int) -> str:
        """Remove and return a fact by zero-based index."""
        with self._lock:
            removed = self.shared["facts"].pop(index)
            self.shared["updated"] = str(self._today())
        self.save()
        return removed

    def clear_shared_facts(self) -> None:
        with self._lock:
            self.shared["facts"].clear()
            self.shared["updated"] = str(self._today())
        self.save()

    def shared_prompt(self, user_message: str = "") -> str:
        with self._lock:
            facts = list(self.shared["facts"])
        if not facts:
            return ""

        selected = (
            [fact for fact in facts if bigram_relevant(fact, user_message)]
            if user_message else facts
        )
        if not selected:
            return ""

        facts_text = "\n".join(f"- {fact}" for fact in selected)
        suffix = (
            f"（已依相關性篩選 {len(selected)}/{len(facts)} 條）"
            if user_message else f"（共 {len(facts)} 條）"
        )
        return f"【共享記憶：這是所有使用者共同建立的資訊{suffix}，請記住】\n{facts_text}"

    def save_personal_summary(self, user_id: str, summary: str) -> None:
        if not summary or not summary.strip():
            print(f"[記憶][警告] 嘗試以空字串覆寫使用者 {user_id} 的個人摘要，已忽略。")
            return
        with self._lock:
            self.personal[user_id] = {
                "summary": summary.strip(),
                "updated": str(self._today()),
            }
        self.save()

    def get_personal_summary(self, user_id: str) -> str | None:
        with self._lock:
            return self.personal.get(user_id, {}).get("summary")

    def remove_personal_summary(self, user_id: str) -> bool:
        with self._lock:
            existed = self.personal.pop(user_id, None) is not None
        if existed:
            self.save()
        return existed

    def set_channel_summary(self, summary: str) -> None:
        with self._lock:
            self.channel["summary"] = summary
            self.channel["updated"] = str(self._today())
        self.save()

    def clear_channel_summary(self) -> None:
        with self._lock:
            self.channel["summary"] = ""
            self.channel["updated"] = ""
        self.save()

