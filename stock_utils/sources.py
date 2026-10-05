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

這些資料一天只變一次（收盤後）或偶爾才有新公告，所以整包抓下來放記憶體，
最多放一小時（_TTL）。也就是說：收盤價表、殖利率、配息公告最慢一小時跟上
官方；盤中即時價不快取，每次都是當下的。

★ 過期就當場重抓，抓不到就明確失敗，**不回舊資料**（理由見 _cached）。

## 每一次抓資料都有總期限

★ requests 的 timeout 不是總期限，它只管「多久沒收到任何東西」。對方慢慢吐
  資料時它永遠不會觸發。2026-10-04 部署到 Render 後就是這樣：某一支在開機
  預抓時卡住不回來，握著那份資料的鎖；之後每個請求都排在那把鎖後面，八條
  執行緒全部佔滿，連 /health 都不回應，只能重新部署。

  所以這裡有三道：下載放到另一條執行緒、等不到就放棄（_DEADLINE）；等鎖也有
  期限；剛失敗過的 30 秒內直接回失敗，不要每個請求都再等一次。
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
# 連線 4 秒、每次讀取 10 秒。這不是總期限，總期限見 _DEADLINE。
_TIMEOUT = (4, 10)

# 抓下來的資料放多久。這就是「回出去的數字最多落後官方多久」，不要為了快而
# 調大。
_TTL = 3600

# 抓一包資料最多等幾秒（整包，不是每次讀取）。
_DEADLINE = 8
# 抓失敗之後多久內不再試。webhook 只有 5 秒，不能每一則訊息都去等一次逾時。
_RETRY_AFTER = 30

_cache: dict[tuple, tuple[float, object]] = {}
_failed: dict[tuple, tuple[float, Exception]] = {}
_locks: dict[tuple, threading.Lock] = {}
# 每一包最近一次抓取的結果，給 stats() 看。
_last: dict[tuple, dict] = {}
_locks_guard = threading.Lock()


def _with_deadline(load, seconds: float):
    """在另一條執行緒跑 load()，超過 seconds 就不等了。

    放棄的那條執行緒還會在背景跑到自己結束（Python 沒辦法從外面殺執行緒），
    但它是 daemon，而且不握任何鎖——卡著的只有它自己。
    """
    box: dict = {}

    def run():
        try:
            box["value"] = load()
        except Exception as exc:  # noqa: BLE001 - 帶回主執行緒再拋
            box["error"] = exc

    t = threading.Thread(target=run, name="source-load", daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"超過 {seconds:g} 秒沒有抓完")
    if "error" in box:
        raise box["error"]
    return box["value"]


# 預抓的執行緒把這個設成 True：資料還剩不到 _REFRESH_AHEAD 秒就過期時，提前重抓。
_ahead = threading.local()
_REFRESH_AHEAD = 900


class refresh_ahead:
    """`with refresh_ahead():` 裡面的讀取會提前更新快過期的資料。

    給 app.py 的預抓用：它每隔一陣子在背景把常用的那幾包重抓一次，使用者問的
    時候資料永遠還在期限內，不必等。這**不是**回舊資料——回出去的東西一樣
    不會超過 _TTL，只是重抓的那幾秒不落在使用者頭上。預抓沒跑到的時候
    （剛開機、背景執行緒沒了），照樣是過期就當場抓。
    """

    def __enter__(self):
        _ahead.on = True

    def __exit__(self, *exc):
        _ahead.on = False


def _cached(key: tuple, ttl: float, load, *, deadline: float | None = None):
    """key 對應的資料：還在期限內就直接回，過期就當場重抓、抓到才回。

    ★ **過期的資料一律不回**，抓不到就拋錯（使用者會看到「現在查不到」）。
      案主 2026-10-04：「我可以等，但資料要正確。」曾經做過「先回舊的、背景
      重抓」與「抓失敗就沿用舊的」，都拿掉了——那兩種都會在使用者不知情的
      情況下給出舊數字。
    """
    if getattr(_ahead, "on", False):
        ttl = max(ttl - _REFRESH_AHEAD, 0)
    deadline = deadline or _DEADLINE
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    # 等鎖也有期限：前一個人最多抓 _DEADLINE 秒，再多就是出事了。
    if not lock.acquire(timeout=deadline + 2):
        raise TimeoutError(f"{key} 等不到前一次抓取結束")
    try:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        failed = _failed.get(key)
        if failed and time.time() - failed[0] < _RETRY_AFTER:
            raise failed[1]
        started = time.perf_counter()
        try:
            value = _with_deadline(load, deadline)
        except Exception as exc:  # noqa: BLE001 - 記下來再拋，見 _RETRY_AFTER
            _failed[key] = (time.time(), exc)
            _last[key] = {"at": time.time(), "took": round(time.perf_counter() - started, 1),
                          "error": f"{type(exc).__name__}: {exc}"[:200]}
            print(f"[source] {key} 失敗（{time.perf_counter() - started:.1f} s）："
                  f"{type(exc).__name__}: {exc}"[:300], flush=True)
            raise
        took = time.perf_counter() - started
        if took > 2:
            # 平常每一支都在 1 秒內。慢的要看得見——那是逾時的前兆。
            print(f"[source] {key} 抓了 {took:.1f} s", flush=True)
        _failed.pop(key, None)
        _cache[key] = (time.time(), value)
        _last[key] = {"at": time.time(), "took": round(took, 1), "error": None}
        return value
    finally:
        lock.release()


def stats() -> dict[str, dict]:
    """每一包資料現在的狀態：幾秒前抓的、那次花多久、最近一次失敗是什麼。"""
    now = time.time()
    out = {}
    for key in sorted(set(_cache) | set(_last), key=str):
        last = _last.get(key, {})
        out["/".join(str(k) for k in key)] = {
            "cached_age": round(now - _cache[key][0]) if key in _cache else None,
            "last_took": last.get("took"),
            "last_error": last.get("error"),
            "last_attempt_age": round(now - last["at"]) if last else None,
        }
    return out


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
    return _cached(("twse_quotes",), _TTL, load)


def tpex_quotes() -> dict[str, dict]:
    """上櫃。{代號: {name, close}}，含 ETF（也含權證，沒有濾掉——沒人會問）。"""
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
        return {r["SecuritiesCompanyCode"].strip():
                {"name": r["CompanyName"].strip(), "close": number(r["Close"])}
                for r in rows}
    return _cached(("tpex_quotes",), _TTL, load)


# ── 全市場：官方殖利率（只有個股，ETF 不在裡面）─────────────────

def twse_yields() -> dict[str, float]:
    def load():
        rows = _get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
        return {r["Code"].strip(): y for r in rows
                if (y := number(r["DividendYield"])) is not None}
    return _cached(("twse_yields",), _TTL, load)


def tpex_yields() -> dict[str, float]:
    def load():
        rows = _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis")
        return {r["SecuritiesCompanyCode"].strip(): y for r in rows
                if (y := number(r["YieldRatio"])) is not None}
    return _cached(("tpex_yields",), _TTL, load)


# ── 盤中即時 ─────────────────────────────────────────────────────

def realtime(market: str, code: str) -> dict | None:
    """證交所行情網站自己用的那一支。market 是 'tse' 或 'otc'。不快取。

    回傳 {date, last, prev_close}：
        date        這筆資料是哪個交易日的（休市日會是上一個交易日）
        last        今天最後一筆成交價；今天還沒有成交是 None
        prev_close  昨收

    ★ **不能只看 z**。z 是「這一瞬間的快照剛好是一筆成交」才有值，盤中絕大多數
      時候是 '-'（2026-10-05 開盤時實測：連續十幾次都是 '-'）。最後一筆成交在
      trade 裡面：{"t": "11:48:45", "z": "2565.0000"}。第一版只看 z，z 是 '-'
      就退回昨收，結果開盤第一天就把昨天的收盤價標成「盤中即時價」回出去。

    ★ 同一個查詢字串，行情網站會回快取住的結果，實測可以到一分鐘左右沒變。
      所以這裡的「即時」是一分鐘內，不是逐筆。
    """
    def load():
        return _get_json(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            ex_ch=f"{market}_{code}.tw", json=1, delay=0,
        )
    # 不走 _cached，所以期限自己套。從 Render 連過去偶爾會慢，失敗就再試一次——
    # 這一支問不到，使用者就只能看到「查不到」。
    try:
        data = _with_deadline(load, 4)
    except Exception as exc:  # noqa: BLE001
        print(f"[source] 即時行情 {code} 第一次失敗，重試：{type(exc).__name__}: {exc}"[:200],
              flush=True)
        data = _with_deadline(load, 4)
    rows = data.get("msgArray") or []
    if not rows:
        return None
    r = rows[0]
    d = r.get("d") or ""
    return {
        "date": date(int(d[:4]), int(d[4:6]), int(d[6:8])) if len(d) == 8 else None,
        "last": number(r.get("z")) or number((r.get("trade") or {}).get("z")),
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
    # 去年那一張已經不會再有新公告，放一天；今年的才需要跟著官方。
    # ★ 這一支的期限比別人長。公開資訊觀測站上班時間很慢：2026-10-05 中午從
    #   本機抓一張要 3～6 秒（前一晚是 0.8 秒），Render 上更久，8 秒的期限會
    #   讓「利率」「配息」整個回「查不到」。平常是預抓在背景付這個時間。
    return _cached(("mops", market, year), _TTL if year >= date.today().year else 86400, load,
                   deadline=20)


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
    return _cached(("twse_etf", code), _TTL, load)


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
    return _cached(("tpex_past",), _TTL, load)


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
    return _cached(("tpex_upcoming",), _TTL, load)


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
    return _cached(("tpex_etf_pay", code), _TTL, load)


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
    # 過去的年份不會再變，放一天。
    ttl = _TTL if year >= date.today().year else 86400
    return _cached(("monthly", market, code, year), ttl, load)
