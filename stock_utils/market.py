"""把各個端點的資料拼成「這支股票現在多少錢、怎麼配息、五年怎麼走」。"""

from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

from stock_utils import sources

# 台北是固定的 UTC+8、沒有日光節約。
TAIPEI = timezone(timedelta(hours=8))

_OPEN, _CLOSE = time(9, 0), time(13, 30)


def now() -> datetime:
    return datetime.now(TAIPEI)


# ── 找股票 ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Symbol:
    code: str
    name: str
    market: str            # 'tse' 上市、'otc' 上櫃
    close: float | None    # 最近一個交易日的收盤價

    @property
    def is_etf(self) -> bool:
        # 台灣的 ETF 代號都是 00 開頭（0050、00878、00679B）。
        return self.code.startswith("00")


def _symbol(market: str, code: str, row: dict) -> Symbol:
    return Symbol(code, row["name"], market, row["close"])


def by_code(code: str) -> Symbol | None:
    code = code.upper()
    for market, table in (("tse", sources.twse_quotes), ("otc", sources.tpex_quotes)):
        row = table().get(code)
        if row:
            return _symbol(market, code, row)
    return None


def by_name(name: str) -> Symbol | None:
    """名稱要**完全符合**。「台積」不會找到台積電——案主 2026-10-04 定的：
    打錯就是錯，不替使用者猜。"""
    for market, table in (("tse", sources.twse_quotes), ("otc", sources.tpex_quotes)):
        for code, row in table().items():
            if row["name"] == name:
                return _symbol(market, code, row)
    return None


# ── 現價 ─────────────────────────────────────────────────────────

def price(sym: Symbol) -> tuple[bool, float | None]:
    """(是不是盤中, 價格)。

    盤中 = 現在是 9:00～13:30，而且行情資料的日期就是今天。假日與颱風假
    不必另外查行事曆：那種日子行情資料停在上一個交易日，日期對不上。

    ★ 收盤後也是問即時行情那一支，不是每日收盤表：收盤表要到下午才更新，
      13:30 剛收盤時那裡還是昨天的價。即時行情問不到才退回收盤表。
    """
    try:
        rt = sources.realtime(sym.market, sym.code)
    except Exception:  # noqa: BLE001 - 行情網站不通時還有收盤表可用
        rt = None
    if not rt:
        return False, sym.close
    t = now()
    if rt["date"] == t.date() and _OPEN <= t.time() < _CLOSE:
        # 盤中某一瞬間可能剛好沒有成交，那時用前一筆。
        return True, rt["last"] or rt["prev_trade"] or rt["prev_close"]
    # 收盤後 last 就是收盤價；開盤前還沒有成交，昨收就是「當前收盤價」。
    return False, rt["last"] or rt["prev_close"] or sym.close


# ── 配息 ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Payout:
    ex_date: date              # 除息日：這天之前買進才領得到
    pay_date: date | None      # 發放日：錢入帳的那天
    cash: float | None         # 每股（每單位）配多少；None = 金額還沒公告
    pay_estimated: bool = False  # 發放日是推估的，不是官方公告的


def payouts(sym: Symbol) -> list[Payout]:
    """這支股票過去一兩年與已公告的配息，照除息日由舊到新。"""
    today = now().date()
    if not sym.is_etf:
        rows = []
        for year in (today.year - 1, today.year):
            rows += sources.mops_dividends(sym.market, year).get(sym.code, [])
        found = [Payout(r["ex_date"], r["pay_date"], r["cash"]) for r in rows]
    elif sym.market == "tse":
        found = [Payout(r["ex_date"], r["pay_date"], r["cash"])
                 for r in sources.twse_etf_dividends(sym.code, today)]
    else:
        found = _tpex_etf_payouts(sym.code, today)
    # 同一次配息可能公告兩次（更正），留後面那一筆。
    return sorted({p.ex_date: p for p in found}.values(), key=lambda p: p.ex_date)


def _tpex_etf_payouts(code: str, today: date) -> list[Payout]:
    """上櫃 ETF：過去的配息、預告表、發放日是三個地方的資料。

    發放日官方只在除息之後才公布（見 sources.tpex_etf_pay_dates）。同一檔
    ETF 從除息到發放隔幾天很固定（實測 00679B 兩次都是 21 天、00950B 是
    26 與 28 天），所以還沒公布的就用上一次的間隔推估，並標成推估。
    """
    by_ex = {r["ex_date"]: r["cash"]
             for r in sources.tpex_past_dividends(today).get(code, [])}
    for r in sources.tpex_upcoming_dividends().get(code, []):
        by_ex.setdefault(r["ex_date"], r["cash"])
    try:
        pay_dates = sources.tpex_etf_pay_dates(code)
    except Exception:  # noqa: BLE001 - 少了發放日還是答得出金額與除息日
        pay_dates = {}
    gap = None
    if pay_dates:
        last_ex = max(pay_dates)
        gap = pay_dates[last_ex] - last_ex
    out = []
    for ex, cash in by_ex.items():
        if ex in pay_dates:
            out.append(Payout(ex, pay_dates[ex], cash))
        elif gap is not None:
            out.append(Payout(ex, ex + gap, cash, pay_estimated=True))
        else:
            out.append(Payout(ex, None, cash))
    return out


def period_months(history: list[Payout]) -> int | None:
    """幾個月配一次：1、3、6、12；完全沒有配息紀錄是 None。

    看的是相鄰兩次除息日隔多久，取中位數——不是數一年配幾次。剛上市半年的
    月配 ETF 只配過五次，用數的會被當成季配。只有一筆紀錄時當年配。
    """
    if not history:
        return None
    gaps = [(b.ex_date - a.ex_date).days for a, b in zip(history, history[1:])]
    if not gaps:
        return 12
    g = median(gaps)
    return 1 if g < 45 else 3 if g < 135 else 6 if g < 270 else 12


def yield_percent(sym: Symbol, history: list[Payout], current: float | None) -> float:
    """殖利率（%）。

    個股用官方的數字（最近一個年度的現金股利 ÷ 收盤價）。
    ETF 官方沒有這個欄位（2026-10-04 查過證交所、櫃買、e添富、ETF 訊息中心），
    所以自己算：過去 365 天實際配的現金 ÷ 現價——Yahoo、CMoney 也是這個算法。
    個股在官方表裡找不到時（例如剛上市）也退回這個算法。
    """
    if not sym.is_etf:
        table = sources.twse_yields if sym.market == "tse" else sources.tpex_yields
        official = table().get(sym.code)
        if official is not None:
            return official
    if not current:
        return 0.0
    today = now().date()
    paid = sum(p.cash or 0 for p in history
               if today - timedelta(days=365) < p.ex_date <= today)
    return paid / current * 100


def next_payout(history: list[Payout]) -> Payout | None:
    """已經公告、錢還沒入帳的那一次；沒有就是 None。

    ★ 看的是發放日，不是除息日：除息日過了但錢還沒發的那一次，對手上有這支
      股票的人來說仍然是「下次配息」。
    """
    today = now().date()
    for p in history:
        if (p.pay_date or p.ex_date) >= today:
            return p
    return None


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def guess_next_pay_date(history: list[Payout], months: int) -> date | None:
    """還沒公告時猜下一次發放日：上一次的發放日往後推一個週期。

    推出來的日子已經過了就再推一個週期——公司比往年晚公告時，不該回一個
    過去的日期。
    """
    past = [p for p in history if p.pay_date or p.ex_date]
    if not past:
        return None
    last = past[-1]
    guess = add_months(last.pay_date or last.ex_date, months)
    today = now().date()
    while guess < today:
        guess = add_months(guess, months)
    return guess


# ── 五年走勢 ─────────────────────────────────────────────────────

def five_year_monthly(sym: Symbol) -> list[tuple[int, int, float | None]]:
    """最近 60 個月的每月均價，[(年, 月, 價)]，由舊到新。還沒上市的月份價是 None。

    ★ 股價沒有還原：遇到分割（例如 0050 在 2025 年一股拆四股）圖上會是一個
      斷崖。官方的每月均價就是當時的實際成交價。
    """
    today = now().date()
    other = "otc" if sym.market == "tse" else "tse"
    years = list(range(today.year - 5, today.year + 1))
    # ★ 六年一起問，不要一年一年排隊：證交所一支要 0.7 秒左右，排隊就是四五秒，
    #   而這段時間 LINE 正等著抓圖。實測六支同時打約 1 秒，沒有被擋。
    by_year = _monthly_many(sym.market, sym.code, years)
    # 這幾年內從上櫃轉上市（或反過來）的股票，轉之前的資料在另一邊。
    empty = [y for y in years if not by_year.get(y)]
    if empty:
        by_year.update(_monthly_many(other, sym.code, empty))
    out = []
    for i in range(59, -1, -1):
        y, m = divmod(today.year * 12 + today.month - 1 - i, 12)
        out.append((y, m + 1, by_year.get(y, {}).get(m + 1)))
    return out


# 一批每月均價最多等幾秒。
_MONTHLY_DEADLINE = 10


def _monthly_many(market: str, code: str, years: list[int]) -> dict[int, dict[int, float]]:
    """同時問好幾年。**到時間就不等了**，沒回來的那幾年當作沒有資料。

    ★ 不能用 `with ThreadPoolExecutor()`：它離開時會等每一支都結束，其中一支
      不回來就整個請求陪它等。2026-10-04 在 Render 上第一次畫圖就掛了五十分鐘
      （本機 1 秒），後面每一張圖都跟著排隊。當時沒有 log，卡在哪一支是推測；
      requests 的 timeout 只管「多久沒收到任何東西」，對方慢慢吐資料時不會觸發。
    """
    pool = ThreadPoolExecutor(len(years))
    futures = {pool.submit(_monthly, market, code, y): y for y in years}
    done, late = wait(futures, timeout=_MONTHLY_DEADLINE)
    pool.shutdown(wait=False, cancel_futures=True)
    if late:
        print(f"[trend] {code} {market} 有 {len(late)} 年超過 {_MONTHLY_DEADLINE} 秒沒回來："
              f"{sorted(futures[f] for f in late)}", flush=True)
    return {futures[f]: f.result() for f in done}


def _monthly(market: str, code: str, year: int) -> dict[int, float]:
    try:
        return sources.monthly_prices(market, code, year)
    except Exception as exc:  # noqa: BLE001 - 少一年就畫少一年，不要整張圖沒了
        print(f"[trend] {code} {market} {year} 抓不到：{type(exc).__name__}: {exc}"[:300], flush=True)
        return {}
