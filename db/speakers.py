"""在群組裡說過話或剛加入、還沒開通的人（為什麼要記，見 db/schema.py）。"""

from __future__ import annotations

from db import connection
from db.schema import ensure_once

# 這個行程已經記過的人。群組裡同一個人會講很多句，不必每一句都連一次資料庫；
# 重新部署後會清空，那時每個人再記一次而已。
_recorded: set[str] = set()


def already_recorded(line_user_id: str) -> bool:
    return line_user_id in _recorded


def record(line_user_id: str, group_id: str | None, name: str | None, via: str) -> None:
    """via 是 'message'（說了話）或 'joined'（剛加入）。"""
    with connection.pool.connection() as conn:
        with conn.cursor() as cur:
            ensure_once(cur)
            cur.execute(
                """
                INSERT INTO group_speakers (line_user_id, name, group_id, via)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (line_user_id) DO UPDATE
                   SET name = COALESCE(EXCLUDED.name, group_speakers.name),
                       group_id = EXCLUDED.group_id,
                       seen_at = now()
                """,
                (line_user_id, name, group_id, via),
            )
    _recorded.add(line_user_id)


def pending() -> list[tuple[str | None, str, str]]:
    """還沒開通的人，[(顯示名稱, userId, via)]，最近看到的排前面。

    ★ 「還沒開通」是查的時候才比對 users，不是記的時候：這樣開通之後那個人
      自然就從名單上消失，不必另外去刪。
    """
    with connection.pool.connection() as conn:
        with conn.cursor() as cur:
            ensure_once(cur)
            cur.execute(
                """
                SELECT s.name, s.line_user_id, s.via
                  FROM group_speakers s
                 WHERE NOT EXISTS (
                           SELECT 1 FROM users u WHERE u.line_user_id = s.line_user_id)
                 ORDER BY s.seen_at DESC
                 LIMIT 20
                """
            )
            return cur.fetchall()
