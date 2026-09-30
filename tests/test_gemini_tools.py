import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from shizumu_gemini_tools import handle_function_calls


def response_with(*parts, text=None):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))],
        text=text,
    )


class GeminiToolTests(unittest.TestCase):
    def test_plain_text_response_is_returned(self):
        response = response_with(SimpleNamespace(text="done"), text="完成")
        self.assertEqual(handle_function_calls(Mock(), response), "完成")

    def test_function_result_is_sent_back_to_model(self):
        call = SimpleNamespace(name="echo", args={"value": "hi"})
        initial = response_with(SimpleNamespace(function_call=call))
        chat = Mock()
        chat.send_message.return_value = response_with(SimpleNamespace(text="done"), text="完成")

        result = handle_function_calls(
            chat,
            initial,
            handlers={"echo": lambda args: args["value"].upper()},
        )

        self.assertEqual(result, "完成")
        chat.send_message.assert_called_once_with([{
            "function_response": {
                "name": "echo",
                "response": {"result": "HI"},
            }
        }])

    def test_missing_candidates_has_readable_fallback(self):
        response = SimpleNamespace(candidates=[], text=None)
        self.assertIn("候選結果為空", handle_function_calls(Mock(), response))


if __name__ == "__main__":
    unittest.main()
