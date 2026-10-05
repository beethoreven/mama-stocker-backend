"""誰可以用這個 bot：LINE 帳號綁定在 users 表、而且是啟用中的人。

做法同 boo-king-king 的 is_staff：**誰能用是資料，不是程式**。把 userId 寫死
在程式裡的話，每加一個人就要改 code、部署。用一張表，開通就是一句 SQL
（寫法見 db/schema.py）。

## 快取

這張表只有幾列、幾乎不會變，而每一則訊息都要查。所以整張讀進來放一分鐘
——剛開通或剛停用的人，最慢一分鐘生效。

## 查不到的時候

★ **寧可擋錯，不可放錯**：資料庫沒設定、或連不上而且手上沒有舊名單時，
  當作沒有人有權限。連不上但有舊名單時繼續用舊的——資料庫打個嗝不該讓
  已經開通的人突然不能用。
"""

from __future__ import annotations

import logging
import os
import threading
import time

from db import connection
from db.schema import ensure_once

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = float(os.environ.get("AUTH_CACHE_TTL_SECONDS") or "60")

_lock = threading.Lock()
_allowed: frozenset[str] | None = None
_expires_at = 0.0


def _load() -> frozenset[str]:
    with connection.pool.connection() as conn:
        with conn.cursor() as cur:
            ensure_once(cur)
            cur.execute(
                "SELECT line_user_id FROM users "
                " WHERE line_user_id IS NOT NULL AND status = 'active'"
            )
            return frozenset(row[0] for row in cur.fetchall())


def is_allowed(line_user_id: str | None) -> bool:
    """這個 LINE 帳號能不能用 bot（群組與私訊都看這一支）。"""
    global _allowed, _expires_at
    if not line_user_id or not connection.enabled():
        return False
    # 整段在鎖裡：同時進來的幾則訊息只需要查一次。握手有 4 秒上限，不會卡死。
    with _lock:
        if _allowed is None or time.monotonic() >= _expires_at:
            try:
                _allowed = _load()
            except Exception as exc:  # noqa: BLE001 - 見開頭「查不到的時候」
                log.warning("讀取 users 失敗：%s", exc)
                if _allowed is None:
                    return False
            # 失敗時也往後延：資料庫掛著的時候，不要每一則訊息都去等一次逾時。
            _expires_at = time.monotonic() + _CACHE_TTL_SECONDS
        return line_user_id in _allowed
