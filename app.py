"""
mama-stocker 後端。

健康檢查，以及 LINE bot 的 webhook 與走勢圖。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date

from dotenv import load_dotenv

# 一定要在 import 任何會讀 os.environ 的模組之前載入。line_utils.client 在
# import 當下就會讀 LINE 的憑證，晚一步就會被當成沒設定。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, jsonify, request, url_for  # noqa: E402

from line_utils import client as line_client  # noqa: E402
from line_utils import handler as line_handler  # noqa: E402
from line_utils import images as line_images  # noqa: E402
from line_utils import signature as line_signature  # noqa: E402
from db import connection as db_connection  # noqa: E402
from stock_utils import sources  # noqa: E402

app = Flask(__name__)

# LINE 沒設定時整包停用，而「停用」在外面看起來跟「正常」一樣安靜，
# 所以啟動時講清楚現在是哪一種。
if line_client.enabled():
    # 印 pid 是為了跟 gunicorn 的「Booting worker with pid」比：一樣就是 worker
    # 自己載入的，不一樣就是主行程載入後才 fork（見 _start_warm_up 的說明）。
    print(f"[info] LINE bot 已啟用（載入於 pid {os.getpid()}）", flush=True)
else:
    print(
        "[warn] LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN 未設定，"
        "LINE bot 停用——webhook 會回 503。",
        flush=True,
    )

# 沒有資料庫就查不到誰有權限，等於沒有人能用——這也要講清楚。
if not db_connection.enabled():
    print("[warn] DATABASE_URL 未設定：查不到使用者名單，所有人都會被當成沒有權限。", flush=True)


def _warm_up() -> None:
    """把常用的那幾包抓進快取，快過期的提前重抓——使用者問的時候不必等。

    抓失敗沒關係，真的有人問的時候會再抓一次。
    """
    year = date.today().year
    loads = [
        ("twse_quotes", sources.twse_quotes), ("tpex_quotes", sources.tpex_quotes),
        ("twse_yields", sources.twse_yields), ("tpex_yields", sources.tpex_yields),
        # 配息公告：第一次問「利率」「配息」要用到今年與去年兩份，各要下載並解析
        # 一張六百 KB 的網頁表格。在 Render 的 0.1 顆 CPU 上那是三到五秒——
        # 2026-10-04 實測，沒預抓時第一次問上櫃股票的利率花了 5.2 秒。
        *((f"mops_{m}_{y}", lambda m=m, y=y: sources.mops_dividends(m, y))
          for m in ("tse", "otc") for y in (year, year - 1)),
        ("tpex_past_dividends", lambda: sources.tpex_past_dividends(date.today())),
        ("tpex_upcoming_dividends", sources.tpex_upcoming_dividends),
    ]
    with sources.refresh_ahead():
        for name, load in loads:
            started = time.perf_counter()
            try:
                n = len(load())
                took = time.perf_counter() - started
                if took > 0.05:
                    # 真的有去抓才印（還在期限內的不印）。這台主機連不連得到
                    # 資料來源、各要多久，log 就看得出來。
                    print(f"[warm] {name} {n} 筆，{took:.1f} s", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 預抓 {name} 失敗（{time.perf_counter() - started:.1f} s）："
                      f"{type(exc).__name__}: {exc}"[:300], flush=True)


# 多久預抓一次。要比「資料期限 − 提前量」（一小時 − 十五分鐘）短，資料才不會
# 在兩次預抓之間過期。
_WARM_EVERY = 600
_warm_lock = threading.Lock()
_warm_next = 0.0


@app.before_request
def _start_warm_up():
    """有請求進來時，如果距離上次預抓超過十分鐘，就在背景再抓一次。

    靠請求來觸發，不是一條一直睡著等的執行緒：保活每幾分鐘會打一次 /health，
    所以它實際上是定時的；而容器被凍結再醒來時，也不必指望背景執行緒還活著。

    ★ **不能在 import 的時候就開執行緒**。2026-10-04 上線第一天就是這樣壞的：
      在模組層開執行緒去預抓時，Render 上 worker 裡每一次對外連線都永遠卡住，
      連 requests 的 timeout 都不會觸發——回覆 LINE、查股價、畫圖全部沒有
      反應，只有不對外連線的 /health 正常。本機完全測不出來。改成這裡之後
      當場就好了（實測 webhook 0.6 秒）。

      **為什麼**會卡，沒有查到底。當時的推論是 gunicorn 先在主行程載入再 fork，
      預抓那條執行緒握著的鎖被複製進 worker 後沒有人放。但依據只有 log 的
      先後順序，而 stdout 與 stderr 在 Render 上是合併顯示的，順序不可靠；
      boo-king-king 在同一個平台上有相反的證據（每個 worker 各自載入）。
      要確認就比對開機那行印的 pid 與「Booting worker with pid」。

      規則：這個檔在載入階段不啟動任何執行緒、不對外連線。
    """
    global _warm_next
    if time.monotonic() < _warm_next or not _warm_lock.acquire(blocking=False):
        return
    _warm_next = time.monotonic() + _WARM_EVERY

    def run():
        try:
            _warm_up()
        finally:
            _warm_lock.release()

    threading.Thread(target=run, name="warm-up", daemon=True).start()


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/line/webhook")
def line_webhook():
    """LINE Messaging API 的 webhook。

    ## 這支沒有登入，簽章就是把關

    網址是公開的、沒有 session。唯一能證明「這包資料真的來自 LINE」的是
    X-Line-Signature。驗不過一律 401。

    ## 一律回 200（除了驗簽失敗）

    ★ 處理單一事件失敗時**不要**回 5xx。LINE 會重送整包，而那包裡可能
      有已經處理成功的事件——重送會重新回覆一次，聊天室裡就會出現兩句
      一樣的話。失敗記 log，回 200。
    """
    if not line_client.enabled():
        return jsonify({"error": "LINE 未設定"}), 503

    raw = request.get_data()
    # ★ 一定要用原始 bytes。先 json.loads 再 dumps 會改變空白與鍵序，
    #   雜湊就對不上——症狀是「設定明明都對卻一直 401」。
    if not line_signature.verify(
        raw, request.headers.get("X-Line-Signature"), line_client.channel_secret()
    ):
        return jsonify({"error": "簽章驗證失敗"}), 401

    # force：不看 Content-Type。簽章已經證明這包是 LINE 送的，沒有理由因為
    # 標頭寫法不同就把事件安靜地丟掉。
    body = request.get_json(force=True, silent=True) or {}
    for event in body.get("events") or []:
        try:
            _handle_line_event(event)
        except Exception as exc:  # noqa: BLE001 - 見上面「一律回 200」
            app.logger.warning("LINE 事件處理失敗：%s", exc, exc_info=True)
    return jsonify({"ok": True})


def _handle_line_event(event: dict) -> None:
    try:
        line_handler.note_group_member(event)
    except Exception as exc:  # noqa: BLE001 - 記不到名單不該擋住回覆
        app.logger.warning("記錄群組成員失敗：%s", exc)

    text = line_handler.text_to_me(event)
    if text is None:
        # 不是在跟我講話就完全不反應——不回覆、不記 log。群組裡大部分
        # 訊息都跟 bot 無關，每一則都留一筆 log 只會把真的錯誤淹掉。
        return
    reply_token = event.get("replyToken")
    if not reply_token:
        return
    sender = (event.get("source") or {}).get("userId")

    # 拿自己的 userId。開通要先有這個 id，而它只有在對方跟 bot 有互動之後才
    # 拿得到。任何人都能問——回的是「你自己的 id」，不是誰的秘密。
    if line_handler.is_my_id(event, text):
        if sender:
            line_client.reply(reply_token, f"你的 LINE userId：\n{sender}")
        return

    private = (event.get("source") or {}).get("type") == "user"
    line_client.reply(
        reply_token,
        _line_messages(line_handler.handle_command(text, sender=sender, private=private)),
    )


def _line_messages(reply):
    """handler 回的東西 → line_client.reply 收的東西。

    多半是幾行字，原樣交出去。走勢回的是 ImageReply：這裡組出這個後端自己的
    圖片網址，LINE 再照網址來抓（見 line_image）。webhook 這一趟只簽一個
    網址，不畫圖——畫圖是 LINE 來抓的那一趟的事，不佔這裡 5 秒的預算。

    ★ https 寫死：LINE 只收 https。TLS 在 Render 前端就解掉了，這個 process
      看到的 request.scheme 可能是 http——照它組的話網址會是 http://，LINE 不收。
    ★ 預覽圖與原圖用同一個網址：預覽上限 1MB，這裡的圖才幾十 KB，不必另外
      畫一張小的。
    """
    if not isinstance(reply, line_images.ImageReply):
        return reply
    url = url_for(
        "line_image",
        token=line_images.sign(reply.spec, line_client.channel_secret()),
        _external=True,
        _scheme="https",
    )
    return [{"type": "image", "originalContentUrl": url, "previewImageUrl": url}]


@app.get("/line/img/<token>.png")
def line_image(token: str):
    """LINE 照 _line_messages 發出去的網址來抓圖。

    來抓的是看訊息的那台裝置上的 LINE App，不會帶任何登入資訊。網址上的
    簽名證明這是 bot 自己回出去的（為什麼要擋，見 line_utils/images.py）。
    驗不過一律 404。
    """
    if not line_client.enabled():
        return jsonify({"error": "LINE 未設定"}), 503
    spec = line_images.verify(token, line_client.channel_secret())
    started = time.perf_counter()
    png = line_images.render(spec) if spec else None
    if png is None:
        return jsonify({"error": "網址無效"}), 404
    # LINE 什麼時候、由誰來抓圖，官方文件沒寫；boo-king-king 是靠這一行 log
    # 量出來的。圖出不來的時候，這一行是唯一能分辨「沒來抓」與「畫太久」的線索。
    print(
        f"[line-img] {spec}｜{(time.perf_counter() - started) * 1000:.0f} ms"
        f"｜{len(png) // 1024} KB｜UA: {request.headers.get('User-Agent', '')[:120] or '(無)'}",
        flush=True,
    )
    resp = app.response_class(png, mimetype="image/png")
    # 網址含發出時間，每一次查詢都是新的網址，所以快取再久也不會讓明天的人
    # 看到今天的圖。留得愈久，LINE 愈不需要回來抓。
    resp.headers["Cache-Control"] = "public, max-age=31536000"
    return resp


if __name__ == "__main__":
    # 預設 5001 不是 5000：macOS 的 AirPlay 接收器佔用 5000，
    # 會回 403 而不是連線被拒，症狀看起來像應用程式壞了。
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or 5001))
