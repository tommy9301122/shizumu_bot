import datetime
import unittest

from shizumu_limits import UsageLimits


class UsageLimitsTests(unittest.TestCase):
    def setUp(self):
        self.today = [datetime.date(2026, 9, 30)]
        self.now = [100.0]
        self.limits = UsageLimits(
            max_user_requests=2,
            user_cooldown_seconds=5,
            max_channel_requests=2,
            today=lambda: self.today[0],
            monotonic_time=lambda: self.now[0],
        )

    def test_user_quota_cooldown_and_daily_reset(self):
        self.assertEqual(self.limits.check_user("42"), (True, ""))
        self.limits.record_user("42")

        allowed, message = self.limits.check_user("42")
        self.assertFalse(allowed)
        self.assertIn("6 秒", message)

        self.now[0] += 5
        self.assertEqual(self.limits.check_user("42"), (True, ""))
        self.limits.record_user("42")
        allowed, message = self.limits.check_user("42")
        self.assertFalse(allowed)
        self.assertIn("今天已經跟我聊了 2 次", message)

        self.today[0] += datetime.timedelta(days=1)
        allowed, message = self.limits.check_user("42")
        self.assertFalse(allowed)
        self.assertIn("6 秒", message)
        self.now[0] += 5
        self.assertEqual(self.limits.check_user("42"), (True, ""))
        self.assertEqual(self.limits.user_usage["42"]["count"], 0)

    def test_channel_quota_resets_each_day(self):
        self.assertTrue(self.limits.check_channel())
        self.limits.record_channel()
        self.limits.record_channel()
        self.assertFalse(self.limits.check_channel())

        self.today[0] += datetime.timedelta(days=1)
        self.assertTrue(self.limits.check_channel())
        self.assertEqual(self.limits.channel_usage["count"], 0)


if __name__ == "__main__":
    unittest.main()
