"""
mama-stocker 後端。

健康檢查，以及 LINE bot 的 webhook 與走勢圖。
"""

from __future__ import annotations

import os
import threading
import time

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
    print("[info] LINE bot 已啟用", flush=True)
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
    """先把全市場的那幾包抓進快取，第一個問股價的人不必在 5 秒裡等它們。

    抓失敗沒關係，真的有人問的時候會再抓一次。
    """
    for load in (sources.twse_quotes, sources.tpex_quotes,
                 sources.twse_yields, sources.tpex_yields):
        started = time.perf_counter()
        try:
            n = len(load())
            # 每一支都印：這台主機連不連得到資料來源，開機 log 就看得出來。
            print(f"[warm] {load.__name__} {n} 筆，{time.perf_counter() - started:.1f} s",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 預抓 {load.__name__} 失敗（{time.perf_counter() - started:.1f} s）："
                  f"{type(exc).__name__}: {exc}"[:300], flush=True)


_warm_started = threading.Event()


@app.before_request
def _start_warm_up():
    """第一個請求進來時才啟動預抓（保活的 /health 也算）。

    ★ **不能在 import 的時候就開執行緒**。2026-10-04 上線第一天就是這樣壞的：
      Render 上 gunicorn 是先在主行程載入這個檔、再 fork 出 worker（log 裡
      「LINE bot 已啟用」印在「Booting worker」之前）。fork 只複製呼叫它的那條
      執行緒，但會把**當下被別條執行緒握著的鎖**原樣複製過去——預抓那條執行緒
      正在連線，握著的鎖到了 worker 裡就再也沒有人會放。結果是 worker 裡每一次
      對外連線都永遠卡住，連 requests 的 timeout 都不會觸發：回覆 LINE、查股價、
      畫圖全部沒有反應，只有不對外連線的 /health 正常。本機不會 fork 在載入
      之後，所以完全測不出來。

      規則：這個檔在載入階段不啟動任何執行緒、不對外連線。
    """
    if not _warm_started.is_set():
        _warm_started.set()
        threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()


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

    line_client.reply(
        reply_token, _line_messages(line_handler.handle_command(text, sender=sender))
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
