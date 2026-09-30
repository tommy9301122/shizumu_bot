"""Gemini function declarations and function-call dispatching."""

from shizumu_services import (
    get_earthquake_info_text,
    get_food_recommendation_text,
    get_weather_info_text,
)


TOOLS = [{
    "function_declarations": [
        {
            "name": "get_food_recommendation",
            "description": "推薦餐點或餐廳。當使用者詢問吃什麼、推薦食物、早餐、午餐、晚餐時，呼叫此工具。",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "meal_type": {
                        "type": "STRING",
                        "description": "餐别：breakfast（早餐）、lunch（午餐）、dinner（晚餐）",
                    },
                    "food_class": {
                        "type": "STRING",
                        "description": "料理類型：中式、台式、日式、美式，若使用者未主動指定則省略此參數。",
                    },
                    "location": {
                        "type": "STRING",
                        "description": "地點名稱，若使用者有明確指定地點才填入，例如：台北車站、公館，若無提及請直接省略。",
                    },
                },
                "required": ["meal_type"],
            },
        },
        {
            "name": "get_earthquake_info",
            "description": "取得最新地震資訊。當使用者詢問地震、有沒有在搖、有沒有地震時使用。",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "get_weather_info",
            "description": "取得天氣預報。當使用者詢問天氣、下雨、溫度、要不要帶傘時使用。",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "city": {
                        "type": "STRING",
                        "description": "城市名稱，例如：臺北、臺中、嘉義、高雄、花蓮，若未指定預設臺北",
                    }
                },
            },
        },
    ]
}]


TOOL_HANDLERS = {
    "get_food_recommendation": lambda args: get_food_recommendation_text(**args),
    "get_earthquake_info": lambda args: get_earthquake_info_text(),
    "get_weather_info": lambda args: get_weather_info_text(**args),
}


def handle_function_calls(chat, response, handlers=None, max_rounds: int = 5) -> str:
    """Resolve Gemini function calls until the model produces plain text."""
    if handlers is None:
        handlers = TOOL_HANDLERS

    for _ in range(max_rounds):
        candidates = getattr(response, "candidates", None)
        if not candidates:
            fallback_text = getattr(response, "text", None)
            return fallback_text or "無法取得模型回應（候選結果為空或缺失）。"

        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if not parts:
            fallback_text = getattr(response, "text", None)
            return fallback_text or "無法取得模型回應內容（content.parts 為空或缺失）。"

        function_calls = [
            part.function_call
            for part in parts
            if hasattr(part, "function_call") and part.function_call.name
        ]
        if not function_calls:
            fallback_text = getattr(response, "text", None)
            return fallback_text or "未偵測到可用的函式呼叫，且無可用文字回覆。"

        function_results = []
        for function_call in function_calls:
            name = function_call.name
            arguments = dict(function_call.args)
            print(f"[Function Call] {name}({arguments})")
            handler = handlers.get(name)
            result = handler(arguments) if handler else f"未知的工具：{name}"
            print(f"[Function Result] {result}")
            function_results.append({
                "function_response": {
                    "name": name,
                    "response": {"result": result},
                }
            })

        response = chat.send_message(function_results)

    fallback_text = getattr(response, "text", None)
    return fallback_text or "反覆處理函式呼叫後仍無法取得模型文字回應。"
