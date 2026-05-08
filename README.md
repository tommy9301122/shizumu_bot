# Shizumu Bot
🍱 Shizumu Bot 是晚餐結社的 Discord 機器人，她的名子是小寒。

## 主要功能

*   **AI (Powered by Gemini)**: 具備上下文記憶、個人對話摘要與群體共享記憶功能。
*   **晚餐推薦**:
    *   整合 Google Maps API 尋找附近高評價餐廳。
    *   支援指定餐別（早/午/晚餐）與料理類型（中式/台式/日式/美式）。
    *   AI 可使用的 function call 能力。
*   **每日頭條新聞**: 定期抓取並顯示 Google 新聞的焦點新聞。
*   **即時天氣與地震資訊**:
    *   串接中央氣象署 (CWA) API 取得最新天氣預報與地震報告。
*   **記憶系統**: 
    *   短期對話歷史追蹤。
    *   個人長期對話摘要。
    *   全伺服器適用的共享記憶（管理員可新增/刪除）。

## 指令列表

*   **對話指令**:
    *   `小寒 [訊息]` 或 `shizumu_doro [訊息]`: 呼叫 AI 進行對話。
    *   `重置記憶`: 清除你與小寒的對話歷史。
*   **實用指令**:
    *   `新聞`: 獲取本日頭條新聞。
    *   `地震`: 獲取最新地震圖文資訊。
    *   `晚餐吃什麼 [類型] [地點]` 或 `午餐吃什麼 [類型] [地點]`: 提供餐飲建議（參數可省略）。
    *   `早餐吃什麼`: 提供早餐建議。
*   **管理員指令** (僅限指定 ID):
    *   `共享記憶 [內容]` / `記住這個 [內容]`: 新增伺服器共享記憶。
    *   `清除共享記憶 [編號]`: 刪除特定共享記憶。
    *   `共享記憶列表`: 查看所有共享記憶。
    *   `shizumu_bot_status`: 檢視 API 額度與記憶體狀態。

---

## 程式架構

|  層  | 行數範圍  | 職責 |
|  ---- | ----  | ----  |
| 設定 / 常數 | 1–185 | 環境變數、各種閾值、全域狀態變數 |
| 用量限制 | 57–119 | 個人每日上限、冷卻、頻道每日上限 |
| Gemini AI | 540–663 | get_gemini_response（個人）、get_gemini_channel_response（群聊） |
| Function Calling | 664–869 | 工具定義、3 個工具執行函式、_handle_function_calls |
| 記憶 | 187–437 | 持久化讀寫、共享/個人/頻道記憶、濃縮排程 |
| Discord 指令 | 936–1,445 | 11 個指令、on_raw_reaction_add、on_member_join |
| 訊息入口 | 1,142–1,514 | _handle_ai_chat、_handle_channel_chat、on_message |



#### 流程圖
```
graph TD
    subgraph Entry["入口 / Discord 事件"]
        OM[on_message]
        ORA[on_raw_reaction_add]
        OMJ[on_member_join]
        OR[on_ready]
    end

    subgraph Commands["指令層（@bot.command）"]
        C1[小寒 / shizumu_doro]
        C2[新聞]
        C3[地震]
        C4[晚餐/午餐吃什麼]
        C5[早餐吃什麼]
        C6[色色 NSFW]
        C7[重置記憶]
        C8[add/list/clear 共享記憶]
        C9[頻道記憶 / reset_channel]
        C10[shizumu_bot_status]
        C11[shizumu說]
    end

    subgraph Handlers["處理器層"]
        HAI[_handle_ai_chat\n個人記憶模式]
        HCC[_handle_channel_chat\n群聊頻道模式]
        HPR[_handle_passive_reactions\n問候/emoji]
    end

    subgraph RateLimit["用量限制層"]
        CAL[check_api_limit\n個人每日+冷卻]
        CCL[check_channel_limit\n頻道每日]
        RAU[record_api_usage]
        RCU[record_channel_usage]
    end

    subgraph GeminiLayer["Gemini AI 層（同步，走 executor）"]
        GR[get_gemini_response\n個人對話]
        GCR[get_gemini_channel_response\n群聊對話]
        HFC[_handle_function_calls\nFunction Calling 迴圈]
        BCC[build_channel_context\n組裝 history]
    end

    subgraph Tools["Function Calling 工具"]
        TF[get_food_recommendation]
        TE[get_earthquake_info]
        TW[get_weather_info]
        GM[googlemaps_search_food]
        TF --> GM
    end

    subgraph Memory["記憶層（持久化 memory.json）"]
        LM[load_memories]
        SM[save_memories\natom write + _memory_lock]
        ASF[add_shared_fact]
        SPS[save_personal_summary]
        GPS[get_personal_summary]
        GSMP[get_shared_memory_prompt]
        subgraph ChannelMem["頻道記憶"]
            RCM[_record_channel_message]
            MSC[_maybe_summarize_channel_async\nasyncio.Lock + executor]
            TSC[_try_summarize_channel\n同步 Gemini 呼叫]
            SR[should_respond\n規則+機率]
            MSC --> TSC
        end
        subgraph PersonalMem["個人記憶（per user）"]
            CH["chat_histories\ndeque maxlen=24\n_chat_histories_lock"]
        end
    end

    OM -->|群聊頻道| RCM
    OM -->|群聊頻道| SR
    SR -->|should=True| HCC
    OM -->|群聊頻道結尾| MSC
    OM -->|bot自身訊息| RCM
    OM -->|bot自身訊息| MSC
    OM --> HPR
    OM -->|mention| HAI
    C1 --> HAI
    HAI --> CAL
    HAI --> GR
    HAI -->|成功後| RAU
    HCC --> CCL
    HCC --> GCR
    HCC -->|成功後| RCU
    GR --> HFC
    GCR --> BCC
    GCR --> HFC
    HFC --> TF
    HFC --> TE
    HFC --> TW
    GR <-->|快照/寫回| CH
    OR --> LM
```