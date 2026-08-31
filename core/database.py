from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class DatabaseManager:
    """
    SQLite 数据库管理器
    负责聊天记账插件的所有数据持久化、索引维护与事务操作。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self) -> None:
        """初始化数据表与索引"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. 用户表：记录用户最新昵称
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 2. 交易流水记录表（真实生效的账本，精确到秒）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lender_id TEXT NOT NULL,
                    lender_name TEXT NOT NULL,
                    borrower_id TEXT NOT NULL,
                    borrower_name TEXT NOT NULL,
                    amount REAL NOT NULL,
                    record_type TEXT NOT NULL,  -- 'BORROW' (借出/欠款) 或 'REPAY' (还款/冲抵)
                    note TEXT DEFAULT '',
                    origin_group_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL,   -- 发起申请时间 (YYYY-MM-DD HH:MM:SS)
                    confirmed_at TEXT NOT NULL, -- 双方确认时间 (YYYY-MM-DD HH:MM:SS)
                    status TEXT DEFAULT 'ACTIVE' -- 'ACTIVE', 'REVOKED'
                )
            """)

            # 3. 待确认借还款申请表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    req_code TEXT NOT NULL UNIQUE,
                    proposer_id TEXT NOT NULL,
                    proposer_name TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    lender_id TEXT NOT NULL,
                    borrower_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    record_type TEXT NOT NULL,  -- 'BORROW' 或 'REPAY'
                    note TEXT DEFAULT '',
                    origin_group_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    expire_at TEXT NOT NULL,
                    status TEXT DEFAULT 'PENDING' -- 'PENDING', 'ACCEPTED', 'REJECTED', 'EXPIRED', 'REVOKED'
                )
            """)

            # 4. 用户外号 / 别名表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_aliases (
                    alias TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_by TEXT DEFAULT '',
                    group_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            # 创建索引提升多群检索与账单统计性能
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_lender ON records(lender_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_borrower ON records(borrower_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_confirmed ON records(confirmed_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_target ON pending_requests(target_id, status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_code ON pending_requests(req_code)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_alias_user ON user_aliases(user_id)")
            conn.commit()

    def update_user_name(self, user_id: str, nickname: str) -> None:
        """更新用户最新昵称"""
        if not user_id or not nickname:
            return
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO users (user_id, nickname, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    updated_at = excluded.updated_at
            """, (str(user_id), str(nickname), now_str))
            conn.commit()

    def get_user_name(self, user_id: str, default_name: str = "") -> str:
        """获取用户最新记录的昵称"""
        with self._get_connection() as conn:
            row = conn.execute("SELECT nickname FROM users WHERE user_id = ?", (str(user_id),)).fetchone()
            if row and row["nickname"]:
                return str(row["nickname"])
        return default_name or str(user_id)

    def insert_pending_request(
        self,
        req_code: str,
        proposer_id: str,
        proposer_name: str,
        target_id: str,
        target_name: str,
        lender_id: str,
        borrower_id: str,
        amount: float,
        record_type: str,
        note: str,
        origin_group_id: str,
        created_at: str,
        expire_at: str
    ) -> int:
        """插入待确认申请"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO pending_requests (
                    req_code, proposer_id, proposer_name, target_id, target_name,
                    lender_id, borrower_id, amount, record_type, note,
                    origin_group_id, created_at, expire_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                req_code, str(proposer_id), str(proposer_name), str(target_id), str(target_name),
                str(lender_id), str(borrower_id), float(amount), str(record_type),
                str(note or ""), str(origin_group_id or ""), created_at, expire_at
            ))
            conn.commit()
            return cursor.lastrowid

    def get_pending_request(self, req_code: str) -> Optional[Dict[str, Any]]:
        """通过单号获取待确认申请 (仅限 PENDING 状态)"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM pending_requests WHERE req_code = ? AND status = 'PENDING'",
                (req_code,)
            ).fetchone()
            return dict(row) if row else None

    def get_request_by_code(self, req_code: str) -> Optional[Dict[str, Any]]:
        """通过单号获取申请（无论何种状态）"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM pending_requests WHERE req_code = ?",
                (req_code,)
            ).fetchone()
            return dict(row) if row else None

    def get_latest_pending_for_target(self, target_id: str, group_id: str = "") -> Optional[Dict[str, Any]]:
        """获取等待该目标用户确认的最新一笔申请（优先同群）"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            if group_id:
                row = conn.execute("""
                    SELECT * FROM pending_requests
                    WHERE target_id = ? AND status = 'PENDING' AND expire_at > ? AND origin_group_id = ?
                    ORDER BY id DESC LIMIT 1
                """, (str(target_id), now_str, str(group_id))).fetchone()
                if row:
                    return dict(row)

            # 若同群无，则跨群匹配最新一笔
            row = conn.execute("""
                SELECT * FROM pending_requests
                WHERE target_id = ? AND status = 'PENDING' AND expire_at > ?
                ORDER BY id DESC LIMIT 1
            """, (str(target_id), now_str)).fetchone()
            return dict(row) if row else None

    def get_latest_pending_for_proposer(self, proposer_id: str, group_id: str = "") -> Optional[Dict[str, Any]]:
        """获取该发起者最新发起的一笔未决申请（用于撤销）"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            if group_id:
                row = conn.execute("""
                    SELECT * FROM pending_requests
                    WHERE proposer_id = ? AND status = 'PENDING' AND expire_at > ? AND origin_group_id = ?
                    ORDER BY id DESC LIMIT 1
                """, (str(proposer_id), now_str, str(group_id))).fetchone()
                if row:
                    return dict(row)

            row = conn.execute("""
                SELECT * FROM pending_requests
                WHERE proposer_id = ? AND status = 'PENDING' AND expire_at > ?
                ORDER BY id DESC LIMIT 1
            """, (str(proposer_id), now_str)).fetchone()
            return dict(row) if row else None

    def get_all_pending_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """获取与该用户相关的所有未决申请（待确认+已发起）"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM pending_requests
                WHERE (target_id = ? OR proposer_id = ?) AND status = 'PENDING' AND expire_at > ?
                ORDER BY id DESC
            """, (str(user_id), str(user_id), now_str)).fetchall()
            return [dict(r) for r in rows]

    def update_pending_status(self, req_code: str, new_status: str) -> bool:
        """更新申请状态 (ACCEPTED, REJECTED, EXPIRED, REVOKED)"""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE pending_requests SET status = ? WHERE req_code = ? AND status = 'PENDING'",
                (new_status, req_code)
            )
            conn.commit()
            return cur.rowcount > 0

    def clean_expired_requests(self) -> int:
        """清理已超时的未决申请"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE pending_requests SET status = 'EXPIRED' WHERE status = 'PENDING' AND expire_at <= ?",
                (now_str,)
            )
            conn.commit()
            return cur.rowcount

    def insert_record(
        self,
        lender_id: str,
        lender_name: str,
        borrower_id: str,
        borrower_name: str,
        amount: float,
        record_type: str,
        note: str,
        origin_group_id: str,
        created_at: str,
        confirmed_at: str
    ) -> int:
        """插入真实生效的交易记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO records (
                    lender_id, lender_name, borrower_id, borrower_name,
                    amount, record_type, note, origin_group_id,
                    created_at, confirmed_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
            """, (
                str(lender_id), str(lender_name), str(borrower_id), str(borrower_name),
                float(amount), str(record_type), str(note or ""), str(origin_group_id or ""),
                created_at, confirmed_at
            ))
            conn.commit()
            return cursor.lastrowid

    def get_pair_records(
        self,
        user_a: str,
        user_b: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """获取两人之间的所有真实流水明细（跨所有群，按确认时间升序或降序）"""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM records
                WHERE status = 'ACTIVE'
                  AND ((lender_id = ? AND borrower_id = ?) OR (lender_id = ? AND borrower_id = ?))
                ORDER BY confirmed_at ASC, id ASC
                LIMIT ? OFFSET ?
            """, (str(user_a), str(user_b), str(user_b), str(user_a), limit, offset)).fetchall()
            return [dict(r) for r in rows]

    def get_pair_records_desc(
        self,
        user_a: str,
        user_b: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """获取两人之间最近发生的真实流水明细（按确认时间倒序）"""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM records
                WHERE status = 'ACTIVE'
                  AND ((lender_id = ? AND borrower_id = ?) OR (lender_id = ? AND borrower_id = ?))
                ORDER BY confirmed_at DESC, id DESC
                LIMIT ?
            """, (str(user_a), str(user_b), str(user_b), str(user_a), limit)).fetchall()
            return [dict(r) for r in rows]

    def get_all_active_records_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """获取与该用户相关的所有跨群真实交易流水"""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM records
                WHERE status = 'ACTIVE' AND (lender_id = ? OR borrower_id = ?)
                ORDER BY confirmed_at ASC, id ASC
            """, (str(user_id), str(user_id))).fetchall()
            return [dict(r) for r in rows]

    # ==========================================
    # 外号 / 别名管理
    # ==========================================

    def set_user_alias(self, alias: str, user_id: str, created_by: str = "", group_id: str = "") -> bool:
        """设置或更新用户的外号/别名"""
        alias_clean = alias.strip().lstrip("@")
        if not alias_clean or not user_id:
            return False
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO user_aliases (alias, user_id, created_by, group_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    user_id = excluded.user_id,
                    created_by = excluded.created_by,
                    group_id = excluded.group_id,
                    created_at = excluded.created_at
            """, (alias_clean, str(user_id), str(created_by), str(group_id), now_str))
            return True

    def get_user_id_by_alias(self, alias: str) -> Optional[str]:
        """根据外号查询对应的用户 QQ 号"""
        alias_clean = alias.strip().lstrip("@")
        with self._get_connection() as conn:
            row = conn.execute("SELECT user_id FROM user_aliases WHERE alias = ?", (alias_clean,)).fetchone()
            if row:
                return str(row["user_id"])
            # 如果别名表中没有，尝试从 users 昵称表中精确或模糊查询
            user_row = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (alias_clean,)).fetchone()
            if user_row:
                return str(user_row["user_id"])
            return None

    def get_all_aliases(self, include_external: bool = True) -> Dict[str, str]:
        """获取所有已绑定的外号字典 {alias: user_id}，包含外部天使插件记忆与 users 缓存"""
        alias_map: Dict[str, str] = {}

        # 1. 外部天使插件记忆（天使之忆、天使之眼、画像等）
        if include_external:
            try:
                from .memory_bridge import MemoryBridge
                ext = MemoryBridge.get_all_external_aliases()
                alias_map.update(ext)
            except Exception:
                pass

        with self._get_connection() as conn:
            # 2. 基础昵称映射
            user_rows = conn.execute("SELECT user_id, nickname FROM users WHERE nickname != ''").fetchall()
            for r in user_rows:
                nick = str(r["nickname"]).strip()
                uid = str(r["user_id"]).strip()
                if nick and uid and len(nick) >= 2:
                    alias_map[nick] = uid
            # 3. 显式设置的外号映射（最高优先级）
            alias_rows = conn.execute("SELECT alias, user_id FROM user_aliases").fetchall()
            for r in alias_rows:
                alias = str(r["alias"]).strip()
                uid = str(r["user_id"]).strip()
                if alias and uid:
                    alias_map[alias] = uid
        return alias_map

    def get_aliases_for_user(self, user_id: str) -> List[str]:
        """获取指定用户绑定的所有外号"""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT alias FROM user_aliases WHERE user_id = ? ORDER BY created_at ASC",
                (str(user_id),)
            ).fetchall()
            return [str(r["alias"]) for r in rows]

    def delete_alias(self, alias: str) -> bool:
        """删除指定外号"""
        alias_clean = alias.strip().lstrip("@")
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM user_aliases WHERE alias = ?", (alias_clean,))
            return cur.rowcount > 0
