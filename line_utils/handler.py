"""一則 LINE 事件 → 回什麼。

這一層刻意不碰 HTTP 也不碰 requests：路由負責驗簽與取出事件，client 負責
送訊息，資料由 stock_utils 查，這裡只決定「這則訊息代表什麼、該回哪一句」。

回覆的每一行字都是案主 2026-10-04 定的規格，改字之前先確認。
"""

from __future__ import annotations

import logging
from datetime import date

from line_utils import commands
from line_utils.images import ImageReply
from stock_utils import market

log = logging.getLogger(__name__)

_PERIOD_TEXT = {12: "年配", 6: "六個月配", 3: "三個月配", 1: "每個月配"}


def mentionees_of(event: dict) -> list[dict]:
    msg = event.get("message") or {}
    return ((msg.get("mention") or {}).get("mentionees")) or []


def text_to_me(event: dict) -> str | None:
    """這則事件如果是在跟 bot 講話，回傳清乾淨的指令文字；不是就回 None。

        私訊  每一則文字都算
        群組  有 tag 我的才算，@mention 那幾段會先挖掉

    ★ 判斷 tag 用 mentionees[].isSelf，不是比對文字。比對文字會被「訊息
      裡剛好提到 bot 的名字」誤觸，而 isSelf 是 LINE 明確告訴我們的。
    """
    if event.get("type") != "message":
        return None
    msg = event.get("message") or {}
    if msg.get("type") != "text":
        return None
    text = msg.get("text") or ""
    source_type = (event.get("source") or {}).get("type")
    if source_type == "user":
        return text.strip()
    if source_type in ("group", "room"):
        mentionees = mentionees_of(event)
        if any(m.get("isSelf") is True for m in mentionees):
            return commands.strip_mentions(text, mentionees)
    return None


def handle_command(text: str) -> str | ImageReply:
    """處理一段已經清乾淨的指令文字，回傳要回覆的東西。

    多半是幾行字；走勢回的是 ImageReply，由 app.py 換成圖片訊息——網址要在
    請求裡才組得出來，這一層不碰 HTTP。

    ★ 不論看不看得懂都回一句——傳了訊息卻沒有任何反應，使用者無從判斷是
      「格式錯」還是「bot 死了」。資料來源掛掉時也一樣要回。
    """
    kind, payload = commands.parse(text)
    if kind == commands.UNKNOWN:
        return commands.USAGE
    try:
        return _answer(kind, payload)
    except Exception as exc:  # noqa: BLE001 - 見上面：一定要回一句
        log.warning("查詢失敗 %r：%s", text, exc, exc_info=True)
        return "現在查不到資料，可能是證交所或櫃買中心的網站暫時連不上，請稍後再試。"


def _answer(kind: str, payload: dict) -> str | ImageReply:
    wanted = payload["symbol"]
    if "name" in wanted:
        sym = market.by_name(wanted["name"])
    else:
        sym = market.by_code(wanted["code"])
    if sym is None:
        asked = wanted.get("name") or wanted.get("code")
        return f"找不到「{asked}」這支股票。\n\n{commands.USAGE}"

    if kind == commands.BAD_DIVIDEND:
        return commands.DIVIDEND_USAGE
    if kind == commands.TREND:
        return ImageReply.trend(sym.code)

    intraday, current = market.price(sym)
    if kind == commands.PRICE:
        label = "盤中即時價" if intraday else "當前收盤價"
        return f"{label}：{_num(current)}" if current else f"{label}：查不到"

    history = market.payouts(sym)
    months = market.period_months(history)
    rate = market.yield_percent(sym, history, current)

    if kind == commands.RATE:
        how = _PERIOD_TEXT[months] if months else "近兩年沒有配息"
        return f"配息方式：{how}\n預估年利率：{_num(rate)}%"

    return _dividend(sym, payload["holding"], current, history, months, rate)


def _dividend(sym: market.Symbol, holding: tuple[str, float], current: float | None,
              history: list[market.Payout], months: int | None, rate: float) -> str:
    """「配息」的三種回法：都公告了、只公告日期、都還沒公告。"""
    unit, amount = holding
    if unit == "shares":
        shares = amount
    elif current:
        shares = amount / current
    else:
        return "現在查不到股價，沒辦法把金額換算成股數。改用「500股」或「3張」的寫法試試。"

    if not months:
        return "這支股票近兩年沒有配息紀錄。"

    # 還沒公告金額時的估法（案主定）：殖利率是一年的，除以一年配幾次就是
    # 一期的利率，再乘上手上那些股票現在值多少。
    estimate = shares * (current or 0) * rate / 100 / (12 // months)

    nxt = market.next_payout(history)
    if nxt is None:
        guess = market.guess_next_pay_date(history, months)
        return (
            "下次配息日：尚未公告\n"
            f"預期配息金額：{_money(estimate)}\n"
            f"非官方預估配息日：{_date(guess) if guess else '無法預估'}"
        )

    if nxt.pay_date is None:
        first = "下次配息日：尚未公告"
    elif nxt.pay_estimated:
        # 上櫃 ETF 的發放日官方要到除息後才公布，這裡是用上次的間隔推的。
        first = f"ETF推估下次配息日：約 {_date(nxt.pay_date)}"
    else:
        first = f"下次配息日：{_date(nxt.pay_date)}"

    if nxt.cash is None:
        first += "，金額尚未公告"
        second = estimate
    else:
        second = shares * nxt.cash

    last = f"最晚何時前要購入：{_date(nxt.ex_date)}"
    if nxt.ex_date <= market.now().date():
        # 除息日當天買已經領不到。錢還沒入帳所以仍然是「下次配息」，
        # 但要講清楚現在買來不及了。
        last += "（已過）"
    return f"{first}\n預期配息金額：{_money(second)}\n{last}"


def _num(x: float) -> str:
    """2500.0 → '2,500'；57.05 → '57.05'；最多兩位小數，不留尾巴的 0。"""
    s = f"{x:,.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _money(x: float) -> str:
    # 一百元以上的零頭沒有意義；小金額留到角分，不然 0.4 元會變成 0。
    return f"{x:,.0f} 元" if x >= 100 else f"{_num(x)} 元"


def _date(d: date) -> str:
    return f"{d.year}/{d.month}/{d.day}"
