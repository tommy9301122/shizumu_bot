import unittest
from unittest.mock import AsyncMock

from shizumu_bot import _send_message_chunks


class BotHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_messages_are_split_at_discord_limit(self):
        destination = AsyncMock()
        content = "x" * 4001

        await _send_message_chunks(destination, content)

        self.assertEqual(destination.send.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in destination.send.await_args_list],
            ["x" * 2000, "x" * 2000, "x"],
        )


if __name__ == "__main__":
    unittest.main()
