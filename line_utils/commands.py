"""把一段文字解析成指令。只做字串處理，不查任何資料。

## 格式（案主 2026-10-04 定）

用空白隔開，最多三段：

    台積電                股價
    00918                 股價（用代號）
    台積電 利率           配息方式、預估年利率
    台積電 配息 100000    手上有價值 100000 元的這支股票，下次配多少
    台積電 配息 500股
    台積電 配息 3張
    台積電 走勢           五年走勢圖

第一段有中文字就當名稱，全是英數字就當代號。
"""

from __future__ import annotations

import re

# 解析結果的種類。
PRICE = "price"
RATE = "rate"
DIVIDEND = "dividend"
TREND = "trend"
UNKNOWN = "unknown"            # 看不懂 → 回 USAGE
BAD_DIVIDEND = "bad_dividend"  # 「配息」後面沒給或給錯 → 回 DIVIDEND_USAGE

# 文字是案主 2026-10-04 定的，跟 LINE 後台「加入好友的歡迎訊息」是同一份。
# 改這裡的時候那邊也要改，兩邊不會自己同步。
USAGE = """【查股價】
台積電
00918

【查配息方式與預估年利率】
台積電 利率

【查下次配息會領多少】
台積電 配息 1000（價值1千元的台積電）
台積電 配息 500股
台積電 配息 3張

【看五年走勢圖】
台積電 走勢

小提醒：
・股票名稱要打完整，「台積」查不到，要打「台積電」
・兩個詞中間要加一個空格
・在群組裡要先 @我 才會回"""

# 沒開通的人不管傳什麼都回這一句。
NO_PERMISSION = "這個帳號沒有使用權限。"

DIVIDEND_USAGE = """【查下次配息會領多少】
台積電 配息 1000（價值1千元的台積電）
台積電 配息 500股
台積電 配息 3張"""

_CJK = re.compile(r"[一-鿿]")
_CODE = re.compile(r"[0-9A-Za-z]+")
_HOLDING = re.compile(r"(\d+(?:\.\d+)?)(股|張)?")


def parse(text: str) -> tuple[str, dict]:
    """文字 → (種類, 內容)。

    內容：
        symbol   {'name': '台積電'} 或 {'code': '2330'}
        holding  只有配息有：('money', 100000.0)、('shares', 500.0)

    ★ 段數不對（多打一段）也當看不懂。寬鬆地忽略多出來的字，使用者會以為
      那些字有被讀進去。
    """
    parts = text.split()   # 不帶參數的 split 連全形空白都會切
    if not parts:
        return UNKNOWN, {}

    first = parts[0]
    if _CJK.search(first):
        # ★ 有中文字就當名稱，不要求「完全沒有英數字」：很多 ETF 的名稱帶數字
        #   （元大台灣50、國泰20年美債、群益ESG投等債20+）。
        symbol = {"name": first}
    elif _CODE.fullmatch(first):
        symbol = {"code": first.upper()}
    else:
        return UNKNOWN, {}

    if len(parts) == 1:
        return PRICE, {"symbol": symbol}

    action = parts[1]
    if action == "利率" and len(parts) == 2:
        return RATE, {"symbol": symbol}
    if action == "走勢" and len(parts) == 2:
        return TREND, {"symbol": symbol}
    if action == "配息":
        m = _HOLDING.fullmatch(parts[2]) if len(parts) == 3 else None
        amount = float(m.group(1)) if m else 0
        if amount <= 0:
            return BAD_DIVIDEND, {"symbol": symbol}
        unit = m.group(2)
        if unit == "張":
            holding = ("shares", amount * 1000)
        elif unit == "股":
            holding = ("shares", amount)
        else:
            holding = ("money", amount)
        return DIVIDEND, {"symbol": symbol, "holding": holding}
    return UNKNOWN, {}


def strip_mentions(text: str, mentionees: list[dict]) -> str:
    """把 @某某 那幾段從訊息裡挖掉，剩下的才是指令。

    ★ LINE 給的 index／length 是 **UTF-16 編碼單位**的偏移量，不是 Python
      的字元數。中文在 BMP 內兩者相同，但只要訊息裡有一個 emoji（代理對，
      佔 2 個 UTF-16 單位、1 個 Python 字元），直接拿去切字串就會偏移，
      而且只有在那種訊息上才會錯——最難重現的那一類 bug。

      所以先轉成 utf-16-le、照位元組切、再轉回來。
    """
    if not mentionees:
        return text.strip()
    buf = text.encode("utf-16-le")
    spans = sorted(
        ((int(m["index"]) * 2, (int(m["index"]) + int(m["length"])) * 2)
         for m in mentionees if "index" in m and "length" in m),
        reverse=True,
    )
    for start, end in spans:
        buf = buf[:start] + buf[end:]
    return buf.decode("utf-16-le", errors="replace").strip()
