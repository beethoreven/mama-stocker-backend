"""走勢圖：網址怎麼簽、LINE 來抓的時候怎麼畫。

    指令 → ImageReply（要畫哪一支）→ 簽名網址 → 回覆給 LINE
    LINE 照網址來抓 → 驗簽 → 查五年資料 → draw 畫成 PNG

## 為什麼回的是網址

LINE 的圖片訊息**只收網址**（HTTPS），不收檔案本身。所以 bot 回的是一個
指向這個後端的網址，看訊息的那台裝置上的 LINE 再來抓（路由在 app.py 的
/line/img/...）。

這也剛好解決 5 秒的問題：五年資料要打六次證交所（一年一次），webhook 裡
等不起。webhook 只簽一個網址就回，慢的那一段發生在 LINE 來抓圖的時候。

## 網址帶簽名

這裡的資料都是公開的，簽名不是為了保密，是為了不讓這支路由變成「任何人
給個代號就替他去打六次證交所」的公開代理——證交所會擋打太兇的 IP，被擋了
整個 bot 就查不到股價。只有 bot 自己回出去的網址畫得出圖。

## 同一支股票一天只畫一次

LINE 的預覽圖與原圖是同一個網址，會抓兩次；群組裡每個人的裝置也各抓一次。
每月均價一天只變一次，所以畫好的圖照「代號＋日期」放在記憶體裡。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import threading
import time
from dataclasses import dataclass

from stock_utils import market


@dataclass(frozen=True)
class ImageReply:
    """handler 回給 app.py 的「這次要回一張圖」。

    handler 不碰 HTTP，不知道自己的網址長什麼樣，所以只講要畫什麼；
    網址由 app.py 在請求裡組（見 sign）。

    spec  t2330    2330 的五年走勢
    """
    spec: str

    @classmethod
    def trend(cls, code: str) -> ImageReply:
        return cls(f"t{code}")


# ── 簽名 ─────────────────────────────────────────────────────────

_TOKEN = re.compile(r"(t[0-9A-Z]{1,10})\.([0-9]{1,12})\.([A-Za-z0-9_-]{22})")


def _key(secret: str) -> bytes:
    """從 channel secret 導出一把只給圖片網址用的鑰匙。

    同一把 secret 也拿來驗 webhook 的簽章。分開導出，兩種簽名就不可能互相
    冒用——就算哪天格式碰巧長得一樣。
    """
    return hmac.new(secret.encode(), b"mama-stocker/line-image", hashlib.sha256).digest()


def _mac(secret: str, body: str) -> str:
    raw = hmac.new(_key(secret), body.encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign(spec: str, secret: str, *, issued: int | None = None) -> str:
    """spec → 放進網址的那一段：'t2330.1790922600.<22 碼簽名>'。

    ★ 帶發出時間是為了讓每次查詢的網址都不一樣：LINE 會照網址快取圖片，
      網址固定的話，明天再問同一支股票會看到今天的圖。
    """
    body = f"{spec}.{int(time.time()) if issued is None else issued}"
    return f"{body}.{_mac(secret, body)}"


def verify(token: str, secret: str) -> str | None:
    """驗簽。對的話回 spec，不對回 None。"""
    m = _TOKEN.fullmatch(token)
    if not m:
        return None
    spec, issued, sig = m.groups()
    if not hmac.compare_digest(sig, _mac(secret, f"{spec}.{issued}")):
        return None
    return spec


# ── 來抓的時候 ───────────────────────────────────────────────────

_drawn: dict[tuple, bytes] = {}
_drawn_lock = threading.Lock()


def render(spec: str) -> bytes | None:
    """spec → PNG。代號查不到就回 None。"""
    sym = market.by_code(spec[1:])
    if sym is None:
        return None
    now = market.now()
    key = (sym.code, now.date())
    # ★ 整段鎖起來：預覽圖與原圖幾乎同時來抓，不鎖的話兩邊各打六次證交所。
    with _drawn_lock:
        png = _drawn.get(key)
        if png is None:
            # 在這裡才 import：Pillow 只有畫圖用得到，問股價的請求不必載它。
            from line_utils import draw

            png = draw.trend_png(sym.name, sym.code, market.five_year_monthly(sym), now=now)
            # 換日之後昨天的圖都用不到了，順手清掉。
            for old in [k for k in _drawn if k[1] != now.date()]:
                del _drawn[old]
            _drawn[key] = png
        return png
