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


def _warm_up() -> None:
    """開機時先把全市場的那幾包抓進快取，第一個使用者不必在 5 秒裡等它們。

    放在模組層啟動而不是 __main__：正式環境用 gunicorn 啟動時不會執行
    __main__。抓失敗沒關係，真的有人問的時候會再抓一次。
    """
    for load in (sources.twse_quotes, sources.tpex_quotes,
                 sources.twse_yields, sources.tpex_yields):
        try:
            load()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 預抓 {load.__name__} 失敗：{exc}", flush=True)


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
    if reply_token:
        line_client.reply(reply_token, _line_messages(line_handler.handle_command(text)))


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
