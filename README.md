# mama-stocker-backend

LINE bot：傳股票名稱或代號給它，回股價、配息、五年走勢圖。
資料全部來自證交所、櫃買中心、公開資訊觀測站的公開端點，不需要金鑰，也沒有資料庫。

## 指令

| 傳什麼 | 回什麼 |
|---|---|
| `台積電` 或 `2330` | 盤中即時價，或收盤後的收盤價 |
| `台積電 利率` | 配息方式、預估年利率 |
| `台積電 配息 100000` | 手上有價值 100000 元的這支股票，下次配多少、哪天入帳、最晚哪天前要買 |
| `台積電 配息 500股`、`台積電 配息 3張` | 同上，用股數或張數 |
| `台積電 走勢` | 五年走勢圖 |

名稱要完全符合。私訊直接傳；群組裡要 tag bot。看不懂的一律回格式列表。

## 本機啟動

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # 填 LINE 的兩個憑證
./venv/bin/python app.py
```

預設 port 5001。`GET /health` 回 `{"ok": true}`。

## 正式環境

```bash
gunicorn app:app --workers 1 --threads 8
```

★ **一定要單一 worker**。快取與畫好的走勢圖都放在記憶體裡，多個 worker 各有
一份，等於每個 worker 都各自去打一次證交所。

## LINE 那邊要設定的東西

步驟 1、2、5、6、7 的做法與選項名稱在 2026-10-04 對過 LINE 官方文件；後台改版
頻繁，位置對不上時以畫面為準。

1. **建官方帳號**。2024-09 起不能直接在 LINE Developers Console 建 Messaging API
   channel，要先有 LINE 官方帳號：用 LINE Business ID 登入、填申請表建立帳號，
   建好後會出現在 [LINE Official Account Manager](https://manager.line.biz/)。
2. **啟用 Messaging API**。在 Official Account Manager 進該帳號的設定 →
   Messaging API → 啟用，過程中選（或新建）一個 Provider。之後用同一個帳號登入
   [LINE Developers Console](https://developers.line.biz/console/)，那個 Provider
   底下就會有這個 channel。
3. **Channel secret**：Developers Console → channel 的 **Basic settings** 分頁
   → 填進 `LINE_CHANNEL_SECRET`。
4. **Channel access token**：**Messaging API** 分頁最下面，長效 token 按 Issue
   → 填進 `LINE_CHANNEL_ACCESS_TOKEN`。
5. **Webhook**：後端要先部署到有 HTTPS 的公開網址（憑證要是瀏覽器信任的，自簽
   不行）。**Messaging API** 分頁 → Webhook URL 按 Edit，填
   `https://<你的網址>/line/webhook` → 按 **Verify** 要顯示 Success → 打開
   **Use webhook**。
6. **關掉罐頭回覆**：Official Account Manager 的 Messaging API 相關設定裡，把
   **Auto-reply messages**（自動回應訊息）設成停用；**Greeting messages**
   （加入好友的歡迎訊息）隨意。不關的話每則訊息都會多一句罐頭回覆。
7. **要在群組用**：Developers Console → **Messaging API** 分頁 → 打開
   **Allow bot to join group chats**（預設關）。一個群組只能有一個官方帳號。
8. **測試**：用 Messaging API 分頁的 QR code 加好友，私訊傳 `台積電`。

### 沒反應時怎麼查

看後端 log：

| 看到什麼 | 代表 |
|---|---|
| 開機那行是「LINE bot 停用」 | 兩個環境變數沒設好 |
| 完全沒有 `/line/webhook` 的請求 | LINE 沒送過來：Webhook URL 填錯，或 Use webhook 沒開 |
| `/line/webhook` 回 401 | Channel secret 填錯（或填成別的 channel 的） |
| 回 200 但沒收到回覆，log 有「LINE reply 失敗 401」 | Channel access token 填錯 |
| 群組裡沒反應 | 沒有 tag bot。LINE 電腦版的 @ 選單不列官方帳號，電腦上請用私訊 |

### 額度

這個 bot 只用 Reply（回覆收到的那則），**不計入免費額度**。沒有用到 Push。

## 結構

```
app.py              路由：/health、/line/webhook、/line/img/<token>.png
line_utils/
  signature.py      驗 webhook 簽章
  client.py         呼叫 LINE Reply API
  commands.py       文字 → 指令
  handler.py        指令 → 回覆的那幾行字
  images.py         走勢圖的簽名網址與快取
  draw.py           用 Pillow 畫折線圖
stock_utils/
  sources.py        一支函式對一個公開端點（含快取）
  market.py         把端點資料拼成股價、配息、殖利率、五年走勢
```
