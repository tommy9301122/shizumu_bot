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

## Nightbot / YouTube 聊天室 API

小寒會在同一個 Railway process 內同時啟動 Discord Bot 與 FastAPI，提供純文字 API 給 Nightbot 的 `$(urlfetch ...)` 使用。AI 對話、群聊模式與記憶系統目前不開放 API，只先提供無狀態的實用指令。

### API 端點

| 端點 | 說明 | 範例 |
| ---- | ---- | ---- |
| `/healthz` | 健康檢查 | `/healthz` |
| `/api/food` | 早餐/午餐/晚餐推薦 | `/api/food?meal_type=dinner&food_class=日式&location=台北車站` |
| `/api/weather` | 天氣查詢 | `/api/weather?city=臺北` |
| `/api/earthquake` | 最新地震資訊 | `/api/earthquake` |

`/api/food` 參數：

| 參數 | 必填 | 說明 |
| ---- | ---- | ---- |
| `meal_type` | 否 | `breakfast`、`lunch`、`dinner`，預設 `dinner` |
| `food_class` | 否 | `中式`、`台式`、`日式`、`美式` |
| `location` | 否 | 地點名稱，例如 `台北車站`；有填入時會使用 Google Maps 查附近餐廳 |
| `token` | 否 | 若有設定 `NIGHTBOT_API_TOKEN`，Nightbot URL 需帶此參數 |

### Nightbot 範例

```text
!晚餐 -> $(urlfetch https://你的-railway-domain/api/food?meal_type=dinner)
!午餐 -> $(urlfetch https://你的-railway-domain/api/food?meal_type=lunch&food_class=日式)
!台北天氣 -> $(urlfetch https://你的-railway-domain/api/weather?city=臺北)
!地震 -> $(urlfetch https://你的-railway-domain/api/earthquake)
```

若有設定 `NIGHTBOT_API_TOKEN`：

```text
!晚餐 -> $(urlfetch https://你的-railway-domain/api/food?meal_type=dinner&token=你的token)
```

### Railway 設定

目前 [Procfile](Procfile) 使用：

```text
web: python shizumu_bot.py
```

程式啟動後會：

1. 在背景啟動 FastAPI，綁定 `0.0.0.0:$PORT`。
2. 繼續啟動 Discord Bot。

可用環境變數：

| 變數 | 說明 | 預設 |
| ---- | ---- | ---- |
| `SHIZUMU_API_ENABLED` | 是否啟用 API，設為 `0` 可停用 | `1` |
| `SHIZUMU_API_HOST` | API bind host | `0.0.0.0` |
| `SHIZUMU_API_PORT` | 本機 API port；Railway 會優先使用 `$PORT` | `8000` |
| `NIGHTBOT_API_TOKEN` | 可選 API token；有設定時 endpoint 需要帶 `token` 或 `x-shizumu-api-token` header | 空白 |

---

## YouTube 直播通知

監看 `https://www.youtube.com/@shizumushizumu`，在 Discord 頻道 `1310279691382558771` 通知正在進行的直播：

```text
@everyone 靜靜子直播開始了！晚餐們一起來看台:shizumu_splash:
https://www.youtube.com/watch?v=直播影片ID
```

程式會使用目標伺服器中名稱為 `shizumu_splash` 且機器人可用的自訂表情；找不到時保留 `:shizumu_splash:` 文字。同一影片只通知一次；啟動時若已在直播且尚未通知，也會補發。尚未開始與已結束的直播不發送通知。

### 搜尋時間（台灣時間）

| 日期 | 每分鐘搜尋時段（結束時間不包含） |
| ---- | ---- |
| 週一、二、三、五、日 | 20:45–22:00 |
| 週六 | 18:45–20:00 |
| 週四 | 無 |

其他時間每兩小時搜尋一次；當天成功通知後改為每兩小時搜尋。啟動會先補查，但距前次搜尋不足 60 秒時稍後再查。時段外臨時開台可能延遲最多約兩小時才被發現，短場直播可能漏掉，YouTube 搜尋索引也可能延遲。

每日最多使用 95 次 `search.list`（含啟動、失敗請求及分頁），以美國太平洋時間午夜重置並自動處理夏令時間。官方預設搜尋額度為每日 100 次；如果同一 Google Cloud 專案有其他使用者共用額度，可能提早耗盡。額度耗盡時暫停到下一配額日。參考 [YouTube 搜尋 API](https://developers.google.com/youtube/v3/docs/search/list)。

### 設定與部署

1. 在 Google Cloud 專案啟用 **YouTube Data API v3**，建立 API 金鑰並限制其只可使用該 API。無需登入 YouTube 頻道或取得頻道擁有者授權。
2. 將金鑰設為 Railway 環境變數 `YOUTUBE_API_KEY`，不要寫入程式或 Git。此設定獨立於 Gemini 的金鑰。
3. 掛載 Railway 持久化 Volume（例如 `/data`），將 `YOUTUBE_LIVE_STATE_PATH` 設為 `/data/youtube_live.sqlite3`。只執行一個 bot 實例；本機與 Railway 不要同時開啟直播通知，以免分別發送。
4. 在通知頻道授予機器人「查看頻道」、「讀取訊息歷史」、「傳送訊息」、「提及 @everyone、@here 和所有身分組」權限。
5. 安裝 `requirements.txt` 並依原本方式啟動。日誌會顯示缺少金鑰、權限不足、API 錯誤或通知成功等狀態。

| 環境變數 | 說明 | 預設 |
| ---- | ---- | ---- |
| `YOUTUBE_API_KEY` | 已啟用 YouTube Data API v3 的 API 金鑰；缺少時停用監看 | 空白 |
| `YOUTUBE_LIVE_ENABLED` | `0`、`false` 或 `no` 停用通知 | `1` |
| `YOUTUBE_LIVE_STATE_PATH` | SQLite 狀態檔路徑，部署時應放持久化 Volume | `youtube_live.sqlite3` |

SQLite 保存通知紀錄、搜尋次數與排程狀態。發送前也會核對該場開播後的機器人歷史訊息，協助處理「已發送但還沒存檔就中斷」的情況；無法讀取歷史時暫緩通知。不要刪除狀態檔或已發送的通知，以免失去去重依據。Discord 與資料庫無法共同交易，因此無法保證任何故障情境下都絕不重複；本功能以單一實例與歷史核對降低風險。

測試（使用模擬 API 與 Discord，不發送真實訊息）：

```text
python -m unittest discover -s tests -v
```
