"""資料庫連線層。每次呼叫各自開一條連線，用號誌限制同時上限。

    with pool.connection() as conn:
        with conn.cursor() as cur:
            ...

## 為什麼不是連線池

照搬 boo-king-king 2026-08-31 實測後的結論：psycopg3 的 ConnectionPool 在
Render 免費方案上會**永久卡死**。Render 閒置時凍結容器，喚醒後連線池賴以
建立連線的背景執行緒沒有跟著恢復，之後每一次存取都等到逾時。每次請求
自己開、自己關，就沒有東西會「壞掉之後一直壞著」。

代價是每次多一次連線握手（同區約 0.2～0.6 秒），所以 Render 與資料庫要開在
同一區；也因此權限查詢有快取（見 db/users.py），不是每則訊息都連一次。

## 沒設定 DATABASE_URL

enabled() 回 False。這時沒有人查得到權限——等於沒有人能用（見 db/users.py）。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# 同時可以有幾條連線。這是反壓上限，不是效能旋鈕。
_MAX_CONCURRENT = 3
# 等不到名額最多等多久。超過就明確失敗，不要無限期排隊。
_SLOT_WAIT = 5
# 單次連線的握手上限。webhook 只有 5 秒，握手不能把它吃完。
_CONNECT_TIMEOUT = 4

_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)


class DatabaseBusy(Exception):
    """同時連線數已達上限，而且等不到名額。"""


def enabled() -> bool:
    return bool(DATABASE_URL)


class _Connections:
    @contextmanager
    def connection(self):
        """開一條連線，用完就關。正常離開就 commit，拋例外就 rollback。"""
        if not _slots.acquire(timeout=_SLOT_WAIT):
            raise DatabaseBusy(f"資料庫同時連線已達上限（{_MAX_CONCURRENT}）")
        try:
            # 在這裡才 import：沒設 DATABASE_URL 的本機開發不必裝得起 psycopg。
            import psycopg

            conn = psycopg.connect(DATABASE_URL, connect_timeout=_CONNECT_TIMEOUT)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        finally:
            _slots.release()


pool = _Connections()
