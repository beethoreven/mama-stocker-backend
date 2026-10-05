"""資料表。每次開機跑一次，全部都是「沒有才建」，重跑不會動到既有資料。"""

from __future__ import annotations

_ready = False


def ensure_once(cur) -> None:
    """這個行程第一次碰資料庫時把表建好。之後呼叫都是空的。"""
    global _ready
    if not _ready:
        ensure_schema(cur)
        _ready = True


def ensure_schema(cur) -> None:
    _ensure_users(cur)
    _ensure_group_speakers(cur)


def _ensure_users(cur) -> None:
    """誰可以用這個 bot。

    ★ 沒有介面，用 SQL 管（這張表只有幾列）：

        -- 開通：對方先私訊 bot「我的ID」拿到 U 開頭的那一串
        INSERT INTO users (name, line_user_id) VALUES ('媽媽', 'Uxxxxxxxx…');

        -- 停用（保留紀錄）／重新啟用
        UPDATE users SET status = 'disabled' WHERE name = '媽媽';
        UPDATE users SET status = 'active'   WHERE name = '媽媽';

    ★ line_user_id 是 LINE Messaging API 給的 userId（U 開頭 33 碼），**不是**
      使用者自己設定、給人加好友用的那個 LINE ID。而且它是 per-channel 的：
      同一個人在別的官方帳號底下是另一串，換 channel 就要全部重新綁。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id           SERIAL PRIMARY KEY,
            name         TEXT NOT NULL,
            line_user_id TEXT,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 已經有 users 表（先建好的）時，上面那句不會動它，欄位在這裡補。
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS line_user_id TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS users_line_user_id_key "
        "ON users (line_user_id) WHERE line_user_id IS NOT NULL"
    )


def _ensure_group_speakers(cur) -> None:
    """在群組裡說過話的人：開通之前，用來查出對方的 userId。

    LINE 不讓一般帳號列出群組成員（那支 API 只給認證帳號），但群組裡每一則
    訊息都會附上發話者的 userId。所以對方只要在群組說一句話，這裡就記得到，
    不必教他私訊「我的ID」——要開通的是長輩。

    一個人一列（重複說話只更新時間）。已經開通的人不記，見 db/speakers.py。
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS group_speakers (
            line_user_id TEXT PRIMARY KEY,
            name         TEXT,
            group_id     TEXT,
            seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
