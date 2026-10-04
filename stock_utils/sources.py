"""一支函式對一個公開端點：打、解析成簡單的 dict／list、快取。

## 來源（2026-10-04 逐支實測過）

    證交所 OpenAPI      openapi.twse.com.tw       收盤價、殖利率
    證交所行情          mis.twse.com.tw           盤中即時價（上市、上櫃都在這）
    證交所網站          www.twse.com.tw/rwd       ETF 配息、每月均價
    櫃買中心 OpenAPI    www.tpex.org.tw/openapi   收盤價、殖利率、除權息預告
    櫃買中心網站        www.tpex.org.tw/www       除權息結果、每月均價
    櫃買 ETF 訊息中心   info.tpex.org.tw          上櫃 ETF 的發放日
    公開資訊觀測站      mopsov.twse.com.tw        個股的除息日、發放日、金額

## 快取

這些資料一天只變一次（收盤後），所以整包抓下來放記憶體，過期才重抓。
webhook 有 5 秒的預算，而這裡每一支實測都在 1 秒內；app.py 開機時會先在
背景把全市場的那幾包抓好，第一個使用者不必等。

★ 抓失敗時**舊資料繼續用**，不把例外往外拋——證交所偶爾會擋連線，
  回昨天的數字比回「查不到」有用。從來沒抓成功過才會拋。
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (mama-stocker)"}
_TIMEOUT = 8

# 全市場的每日資料放多久。收盤後幾點更新各端點不一樣，一小時夠新了。
_DAILY_TTL = 3600
# 配息公告放多久。公告隨時可能出來，但不差這幾小時。
_DIVIDEND_TTL = 6 * 3600

_cache: dict[tuple, tuple[float, object]] = {}
_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()


def _cached(key: tuple, ttl: float, load):
    """key 對應的資料；過期就用 load() 重抓，重抓失敗就繼續用舊的。"""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            value = load()
        except Exception as exc:  # noqa: BLE001 - 見開頭「舊資料繼續用」
            if hit:
                log.warning("重抓 %s 失敗，沿用舊資料：%s", key, exc)
                return hit[1]
            raise
        _cache[key] = (time.time(), value)
        return value


def _get_json(url: str, **params):
    r = requests.get(url, params=params or None, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post_json(url: str, **data):
    r = requests.post(url, data=data, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── 小工具：這些網站的日期與數字寫法 ─────────────────────────────

def roc_date(s: str | None) -> date | None:
    """民國日期 → date。吃 '1151002'、'115/10/02'、'115年10月02日' 三種寫法。"""
    m = re.search(r"(\d{2,3})\D?(\d{2})\D?(\d{2})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def number(s) -> float | None:
    """'1,505.00' → 1505.0；空白、'-'、'----'、文字一律 None。"""
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ── 全市場：代號、名稱、收盤價 ───────────────────────────────────

def twse_quotes() -> dict[str, dict]:
    """上市。{代號: {name, close}}，含 ETF。"""
    def load():
        rows = _get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
        return {r["Code"].strip(): {"name": r["Name"].strip(), "close": number(r["ClosingPrice"])}
                for r in rows}
    return _cached(("twse_quotes",), _DAILY_TTL, load)


def tpex_quotes() -> dict[str, dict]:
    """上櫃。{代號: {name, close}}，含 ETF（也含權證，沒有濾掉——沒人會問）。"""
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
        return {r["SecuritiesCompanyCode"].strip():
                {"name": r["CompanyName"].strip(), "close": number(r["Close"])}
                for r in rows}
    return _cached(("tpex_quotes",), _DAILY_TTL, load)


# ── 全市場：官方殖利率（只有個股，ETF 不在裡面）─────────────────

def twse_yields() -> dict[str, float]:
    def load():
        rows = _get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
        return {r["Code"].strip(): y for r in rows
                if (y := number(r["DividendYield"])) is not None}
    return _cached(("twse_yields",), _DAILY_TTL, load)


def tpex_yields() -> dict[str, float]:
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis")
        return {r["SecuritiesCompanyCode"].strip(): y for r in rows
                if (y := number(r["YieldRatio"])) is not None}
    return _cached(("tpex_yields",), _DAILY_TTL, load)


# ── 盤中即時 ─────────────────────────────────────────────────────

def realtime(market: str, code: str) -> dict | None:
    """證交所行情網站自己用的那一支。market 是 'tse' 或 'otc'。不快取。

    回傳 {date, last, prev_trade, prev_close}：
        date        這筆資料是哪個交易日的（休市日會是上一個交易日）
        last        最近成交價；當下那一瞬間沒有成交時是 None
        prev_trade  前一筆成交價
        prev_close  昨收
    """
    data = _get_json(
        "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
        ex_ch=f"{market}_{code}.tw", json=1, delay=0,
    )
    rows = data.get("msgArray") or []
    if not rows:
        return None
    r = rows[0]
    d = r.get("d") or ""
    return {
        "date": date(int(d[:4]), int(d[4:6]), int(d[6:8])) if len(d) == 8 else None,
        "last": number(r.get("z")),
        "prev_trade": number(r.get("pz")),
        "prev_close": number(r.get("y")),
    }


# ── 配息：個股（上市、上櫃同一支）────────────────────────────────

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")


def mops_dividends(market: str, year: int) -> dict[str, list[dict]]:
    """公開資訊觀測站「公司股利分派公告資料彙總表」，整個市場一年份。

    market 是 'tse' 或 'otc'，year 是西元年。
    回傳 {代號: [{ex_date, pay_date, cash}]}，只留有配現金、有除息日的那幾筆。

    ★ 這一支回的是網頁表格不是 JSON，欄位靠位置認（2026-10-04 的版面）：
        0 代號  7 盈餘配的現金  8 公積配的現金  9 特別股的現金
        10 除息交易日  11 現金股利發放日
      哪天版面改了，這裡會安靜地解不出東西——所以解出來是空的就當失敗。
    """
    def load():
        r = requests.post(
            "https://mopsov.twse.com.tw/mops/web/ajax_t108sb27",
            data={"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1,
                  "TYPEK": "sii" if market == "tse" else "otc", "year": year - 1911},
            headers=_UA, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        out: dict[str, list[dict]] = {}
        for row in _ROW.findall(r.content.decode("utf-8", "replace")):
            c = [html.unescape(_TAG.sub("", x)).strip() for x in _CELL.findall(row)]
            if len(c) < 12:
                continue
            cash = sum(number(x) or 0 for x in c[7:10])
            ex = roc_date(c[10])
            if cash > 0 and ex:
                out.setdefault(c[0], []).append(
                    {"ex_date": ex, "pay_date": roc_date(c[11]), "cash": cash})
        if not out and year < date.today().year:
            raise ValueError("公開資訊觀測站的表格解不出任何一筆，版面可能改了")
        return out
    return _cached(("mops", market, year), _DIVIDEND_TTL, load)


# ── 配息：上市 ETF ───────────────────────────────────────────────

def twse_etf_dividends(code: str, today: date) -> list[dict]:
    """證交所 ETF 專區的收益分配，往回兩年、往後一年。

    cash 是 None 代表「日期公告了、金額還沒」。
    """
    def load():
        data = _get_json(
            "https://www.twse.com.tw/rwd/zh/ETF/etfDiv", stkNo=code, response="json",
            startDate=f"{today.year - 2}0101", endDate=f"{today.year + 1}1231",
        )
        out = []
        for r in data.get("data") or []:
            ex = roc_date(r[2])
            if ex:
                out.append({"ex_date": ex, "pay_date": roc_date(r[4]), "cash": number(r[5])})
        return out
    return _cached(("twse_etf", code), _DIVIDEND_TTL, load)


# ── 配息：上櫃 ETF（三支拼起來）─────────────────────────────────

def tpex_past_dividends(today: date) -> dict[str, list[dict]]:
    """櫃買「除權除息計算結果表」，全市場過去 400 天。{代號: [{ex_date, cash}]}"""
    def load():
        data = _post_json(
            "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ", response="json",
            startDate=f"{today - timedelta(days=400):%Y/%m/%d}", endDate=f"{today:%Y/%m/%d}",
        )
        out: dict[str, list[dict]] = {}
        for r in data["tables"][0]["data"]:
            ex, cash = roc_date(r[0]), number(r[6])   # 6 是「息值」
            if ex and cash:
                out.setdefault(r[1].strip(), []).append({"ex_date": ex, "cash": cash})
        return out
    return _cached(("tpex_past",), _DIVIDEND_TTL, load)


def tpex_upcoming_dividends() -> dict[str, list[dict]]:
    """櫃買「除權除息預告表」。{代號: [{ex_date, cash}]}，cash 可能是 None。"""
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost")
        out: dict[str, list[dict]] = {}
        for r in rows:
            ex = roc_date(r["ExRrightsExDividendDate"])
            if ex and "息" in r["ExRrightsExDividend"]:
                out.setdefault(r["SecuritiesCompanyCode"].strip(), []).append(
                    {"ex_date": ex, "cash": number(r["CashDividend"]) or None})
        return out
    return _cached(("tpex_upcoming",), _DIVIDEND_TTL, load)


def tpex_etf_pay_dates(code: str) -> dict[date, date]:
    """櫃買 ETF 訊息中心的配息行事曆。{除息日: 發放日}

    ★ 只有最近兩次，而且**除息之後才會出現**——即將到來的那一次查不到發放日
      （2026-10-04 實測：00950B 隔天除息，這裡還只列到上個月）。所以上櫃 ETF
      的下次發放日是用這裡的間隔天數推估的，見 market.py。
    """
    def load():
        rows = _post_json("https://info.tpex.org.tw/api/etfExDivPopup",
                          stkNo=code, lang="zh-tw")
        out = {}
        for r in rows:
            ex, pay = roc_date(r.get("divDate")), roc_date(r.get("inDate"))
            if ex and pay:
                out[ex] = pay
        return out
    return _cached(("tpex_etf_pay", code), _DIVIDEND_TTL, load)


# ── 每月均價（走勢圖用）─────────────────────────────────────────

def monthly_prices(market: str, code: str, year: int) -> dict[int, float]:
    """某一年每個月的平均價。{月: 價}；那一年沒有交易就是空的。

    上市是成交金額 ÷ 成交股數的加權平均，上櫃是收盤價的平均——兩邊官方給的
    算法不同，但畫五年走勢看不出差別。
    """
    def load():
        if market == "tse":
            data = _get_json(
                "https://www.twse.com.tw/rwd/zh/afterTrading/FMSRFK",
                date=f"{year}0101", stockNo=code, response="json",
            )
            rows = data.get("data") or []
        else:
            data = _post_json(
                "https://www.tpex.org.tw/www/zh-tw/statistics/monthlyStock",
                code=code, date=year, response="json",
            )
            tables = data.get("tables") or []
            rows = tables[0].get("data") or [] if tables else []
        return {int(r[1]): p for r in rows if (p := number(r[4]))}
    # 過去的年份不會再變，放一天；今年的照每日資料的節奏。
    ttl = _DAILY_TTL if year >= date.today().year else 86400
    return _cached(("monthly", market, code, year), ttl, load)
