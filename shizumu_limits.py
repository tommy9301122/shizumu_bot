"""In-memory daily quotas and cooldowns for AI conversations."""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable


class UsageLimits:
    """Track per-user and shared-channel AI usage.

    State remains intentionally in memory, matching the bot's previous behavior.
    Clock providers are injectable so boundary and cooldown behavior can be tested
    without waiting in real time.
    """

    def __init__(
        self,
        max_user_requests: int,
        user_cooldown_seconds: int,
        max_channel_requests: int,
        *,
        today: Callable[[], datetime.date] = datetime.date.today,
        monotonic_time: Callable[[], float] = time.time,
    ) -> None:
        self.max_user_requests = max_user_requests
        self.user_cooldown_seconds = user_cooldown_seconds
        self.max_channel_requests = max_channel_requests
        self._today = today
        self._time = monotonic_time

        self.user_usage: dict[str, dict] = {}
        self.last_request_time: dict[str, float] = {}
        self.channel_usage: dict = {"date": None, "count": 0}

    def _user_record(self, user_id: str) -> dict:
        today = self._today()
        record = self.user_usage.get(user_id)
        if record is None or record["date"] != today:
            record = {"date": today, "count": 0}
            self.user_usage[user_id] = record
        return record

    def _reset_channel_if_needed(self) -> None:
        today = self._today()
        if self.channel_usage["date"] != today:
            self.channel_usage["date"] = today
            self.channel_usage["count"] = 0

    def check_user(self, user_id: str) -> tuple[bool, str]:
        record = self._user_record(user_id)
        if record["count"] >= self.max_user_requests:
            return (
                False,
                f"你今天已經跟我聊了 {self.max_user_requests} 次了，明天再來找我吧 (´・ω・`)",
            )

        elapsed = self._time() - self.last_request_time.get(user_id, 0)
        if elapsed < self.user_cooldown_seconds:
            remaining = int(self.user_cooldown_seconds - elapsed) + 1
            return False, f"請稍等 {remaining} 秒後再傳訊息喔 (｡･∀･)"
        return True, ""

    def record_user(self, user_id: str) -> None:
        self._user_record(user_id)["count"] += 1
        self.last_request_time[user_id] = self._time()

    def check_channel(self) -> bool:
        self._reset_channel_if_needed()
        return self.channel_usage["count"] < self.max_channel_requests

    def record_channel(self) -> None:
        self._reset_channel_if_needed()
        self.channel_usage["count"] += 1

