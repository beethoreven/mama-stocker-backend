"""把五年的每月均價畫成一張 PNG 折線圖。

只畫、不查資料——要畫的東西由 images.py 備好傳進來。給一組假資料就測得
起來、看得到圖。

下面的尺寸都是邏輯 px，最後乘上 SCALE——跟手機螢幕是 2～3 倍一樣的道理，
放大看才不會糊。

## 字型

Noto Sans TC，檔案在 fonts/，OFL 授權；OFL 要求字型檔跟授權書一起散布，
所以授權書放在同一個目錄。

★ 這是可變字型，**預設粗細是 100（Thin）不是 400**。每個字型物件都要明確
  設粗細，漏掉的話字會細得幾乎看不見，而且不會有任何錯誤。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).resolve().parent / "fonts" / "NotoSansTC-Variable.ttf"

SCALE = 2

BG = "#ffffff"
INK = "#141413"
INK_FAINT = "#6b6a66"
GRID = "#e6e5e3"
LINE = "#cf5148"

W, H = 720, 460
LEFT, RIGHT = 64, 24        # 圖表區左右留白；左邊要放得下「2,500」
TOP, BOTTOM = 84, 96        # 上面放標題，下面放年份、「尚未上市」與「截至」


def _font(size: float, weight: int = 400) -> ImageFont.FreeTypeFont:
    # 每張圖各開一組，不跨請求共用：FreeType 的字型物件畫字時會改自己的
    # 狀態，兩個執行緒同時拿同一個去畫會互相踩。
    font = ImageFont.truetype(str(FONT_PATH), round(size * SCALE))
    font.set_variation_by_axes([weight])     # 見開頭：預設是 Thin
    return font


def trend_png(name: str, code: str, months: list[tuple[int, int, float | None]], *,
              now: datetime) -> bytes:
    """months 是 [(年, 月, 均價)]，由舊到新；均價 None 代表那個月沒有資料。

    最前面連續沒有資料的月份 = 還沒上市：照案主的規格畫成 0 元，並在那一段
    X 軸底下加一條黑線。後面沒有資料的月份（這個月還沒有交易日、或那個月
    停牌）不畫成 0，直接把前後連起來——畫成 0 會像是股價歸零。
    """
    s = SCALE
    img = Image.new("RGB", (W * s, H * s), BG)
    d = ImageDraw.Draw(img)

    listed_from = next((i for i, m in enumerate(months) if m[2] is not None), len(months))
    values = [0.0] * listed_from + [m[2] for m in months[listed_from:]]

    top = _top_of(max((v for v in values if v), default=0))
    x0, x1 = LEFT, W - RIGHT
    y0, y1 = TOP, H - BOTTOM
    n = len(months)

    def px(i: int) -> float:
        return (x0 + (x1 - x0) * i / max(n - 1, 1)) * s

    def py(v: float) -> float:
        return (y1 - (y1 - y0) * v / top) * s

    # 標題
    d.text((x0 * s, 22 * s), f"{name}（{code}）", font=_font(22, 700), fill=INK)
    d.text((x0 * s, 54 * s), "近五年股價走勢・每月平均價", font=_font(13), fill=INK_FAINT)

    # Y 軸：四格，五條線
    label = _font(12)
    for k in range(5):
        v = top * k / 4
        y = py(v)
        d.line([(x0 * s, y), (x1 * s, y)], fill=INK if k == 0 else GRID, width=s)
        d.text(((x0 - 8) * s, y), _axis_num(v), font=label, fill=INK_FAINT, anchor="rm")

    # X 軸：每年一月一個刻度
    for i, (year, month, _) in enumerate(months):
        if month == 1:
            x = px(i)
            d.line([(x, y1 * s), (x, (y1 + 5) * s)], fill=INK, width=s)
            d.text((x, (y1 + 8) * s), str(year), font=label, fill=INK_FAINT, anchor="ma")

    # 還沒上市的那一段：X 軸底下一條黑線
    if listed_from > 0:
        a, b = px(0), px(listed_from - 1)
        yb = (y1 + 34) * s
        d.line([(a, yb), (b, yb)], fill=INK, width=4 * s)
        d.text((a, yb + 5 * s), "尚未上市", font=label, fill=INK, anchor="la")

    # 折線
    points = [(px(i), py(v)) for i, v in enumerate(values) if v is not None]
    if len(points) >= 2:
        d.line(points, fill=LINE, width=3 * s, joint="curve")
    if points:
        lx, ly = points[-1]
        r = 4 * s
        d.ellipse([lx - r, ly - r, lx + r, ly + r], fill=LINE)

    d.text((x0 * s, (H - 26) * s), f"截至 {now:%Y/%m/%d %H:%M}・資料來源：證交所、櫃買中心",
           font=label, fill=INK_FAINT)

    # 先轉成 256 色再存：存檔是最花時間的一步，轉了之後快一倍、檔案小三倍。
    buf = io.BytesIO()
    img.quantize(256, method=Image.Quantize.FASTOCTREE).save(buf, "PNG")
    return buf.getvalue()


def _top_of(highest: float) -> float:
    """Y 軸的頂：比最高價大一點、能平分成四格的整齊數字。"""
    if highest <= 0:
        return 4.0
    unit = 1.0
    while unit * 10 * 4 < highest:
        unit *= 10
    while unit > highest:
        unit /= 10
    for k in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if k * unit * 4 >= highest:
            return k * unit * 4
    return highest


def _axis_num(v: float) -> str:
    s = f"{v:,.2f}"
    return s.rstrip("0").rstrip(".")
