"""SQLite 持久化层：配置、智能体、机器人、对话记录、长期记忆。

只保留 AI 机器人需要的东西。没有签到表、没有账号表、没有运行日志表。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

_lock = threading.RLock()

DEFAULTS: dict[str, str] = {
    "timezone": "Asia/Shanghai",
    "panel_password": os.getenv("PANEL_PASSWORD", "admin123"),
    # ---------- Bot ----------
    "bot_token": os.getenv("BOT_TOKEN", "").strip(),
    "bot_username": "",
    "bot_trigger": "mention",
    "bot_enabled": "1" if os.getenv("BOT_TOKEN", "").strip() else "0",
    # ---------- AI ----------
    "ai_base_url": os.getenv("AI_BASE_URL", "").strip() or "https://api.deepseek.com/v1",
    "ai_api_key": os.getenv("AI_API_KEY", "").strip(),
    "ai_model": os.getenv("AI_MODEL", "").strip() or "deepseek-chat",
    # 当前使用的智能体（ai_agents 表的主键）。空表示还没选，回退到下面这组老字段。
    "ai_agent_id": "",
    "ai_system_prompt": "你是一个在 Telegram 里聊天的助手，回答简洁、口语化，用中文回复。不确定的事不要编造。",
    "ai_history_len": "12",
    "ai_max_tokens": "1600",
    "ai_temperature": "0.7",
    "ai_min_interval": "3",
    "ai_proxy": os.getenv("AI_PROXY", "").strip(),
    # 没绑智能体时的图片理解开关（智能体各自的开关在 ai_agents.vision）
    "ai_vision": "0",
    # ---------- 长期记忆 ----------
    "memory_enabled": "1",   # 把长期记忆注入上下文
    "memory_learn": "1",     # 聊完自动提炼值得记住的事
    "memory_max_notes": "8", # 每次最多注入几条
    # ---------- 其它 ----------
    "panel_title": os.getenv("APP_TITLE", "").strip() or "TG AI 机器人",
    "known_chats": "",       # 机器人待过的群（JSON）
    "business_connections": "",
    # ---------- WebDAV 备份 ----------
    "webdav_url": os.getenv("WEBDAV_URL", "").strip(),        # https://dav.jianguoyun.com/dav
    "webdav_user": os.getenv("WEBDAV_USER", "").strip(),
    "webdav_pass": os.getenv("WEBDAV_PASS", "").strip(),      # 坚果云要应用密码
    "webdav_dir": "tg-ai-bot",   # 相对根地址的目录，不存在会自动建
    "backup_path": "tg-ai-bot-backup.json",   # 文件名
    "backup_auto": "0",      # 1 = 定时自动备份
    "backup_interval": "1440",  # 分钟，默认一天
    "backup_proxy": "",
}

# settings 里存的是只有一份的全局配置；这些键在升级后由 _seed_agents 搬进智能体
_LEGACY_AI_KEYS = (
    "ai_base_url", "ai_api_key", "ai_model", "ai_system_prompt",
    "ai_temperature", "ai_max_tokens", "ai_history_len", "ai_min_interval", "ai_proxy",
)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS bots (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL DEFAULT '',
                token                TEXT NOT NULL DEFAULT '',
                username             TEXT NOT NULL DEFAULT '',
                enabled              INTEGER NOT NULL DEFAULT 1,
                trigger              TEXT NOT NULL DEFAULT 'mention',
                update_offset        TEXT NOT NULL DEFAULT '0',
                business_connections TEXT NOT NULL DEFAULT '',
                known_chats          TEXT NOT NULL DEFAULT '',
                ai_agent_id          INTEGER,
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ai_agents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL DEFAULT '',
                base_url      TEXT NOT NULL DEFAULT '',
                api_key       TEXT NOT NULL DEFAULT '',
                model         TEXT NOT NULL DEFAULT '',
                system_prompt TEXT NOT NULL DEFAULT '',
                temperature   TEXT NOT NULL DEFAULT '0.7',
                max_tokens    INTEGER NOT NULL DEFAULT 1600,
                history_len   INTEGER NOT NULL DEFAULT 12,
                min_interval  INTEGER NOT NULL DEFAULT 3,
                proxy         TEXT NOT NULL DEFAULT '',
                vision        INTEGER NOT NULL DEFAULT 0,
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ai_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id     INTEGER NOT NULL DEFAULT 0,
                chat_id    TEXT NOT NULL,
                chat_title TEXT NOT NULL DEFAULT '',
                role       TEXT NOT NULL,
                author     TEXT NOT NULL DEFAULT '',
                -- 发言人 ID。群里要按人隔离上下文，只靠昵称会撞名；
                -- assistant 行记的是「这轮在回谁」，用来把回答归到对应的人。
                author_id  TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL,
                ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ai_chat ON ai_messages(bot_id, chat_id, id DESC);
            -- 按发言人建索引这步放在 _migrate_columns 里：老库此刻还没有 author_id 列，
            -- 写在这里会让整个建表脚本直接失败。
            CREATE TABLE IF NOT EXISTS ai_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id   INTEGER NOT NULL DEFAULT 0,
                scope      TEXT NOT NULL DEFAULT 'global',
                scope_key  TEXT NOT NULL DEFAULT '',
                content    TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                used_at    TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_notes_agent ON ai_notes(agent_id, scope, scope_key);
            -- 每次调用 AI 的账单流水。上游返回 usage 才记，没返回就记 0，
            -- 这样「没记到」和「真的花了 0」能区分开（kind='unknown'）。
            CREATE TABLE IF NOT EXISTS ai_usage (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id   INTEGER NOT NULL DEFAULT 0,
                bot_id     INTEGER NOT NULL DEFAULT 0,
                model      TEXT NOT NULL DEFAULT '',
                kind       TEXT NOT NULL DEFAULT 'chat',
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                ok         INTEGER NOT NULL DEFAULT 1,
                ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON ai_usage(ts);
            """
        )
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
            )
        _migrate_columns(conn)
        _seed_agents(conn)
    _tighten_permissions()


def _tighten_permissions() -> None:
    """库里存着 bot token、AI Key：权限收到 600，目录收到 700。

    Windows 上 chmod 语义有限，失败忽略即可，不影响运行。
    """
    for path, mode in ((DATA_DIR, 0o700), (DB_PATH, 0o600)):
        try:
            if path.exists():
                os.chmod(path, mode)
        except OSError:
            pass


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """老库补列，保证从旧版本升级上来的库不会缺字段。"""
    bots = _columns(conn, "bots")
    for column, ddl in (
        ("ai_agent_id", "INTEGER"),
        ("known_chats", "TEXT NOT NULL DEFAULT ''"),
        ("business_connections", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in bots:
            conn.execute(f"ALTER TABLE bots ADD COLUMN {column} {ddl}")

    agents = _columns(conn, "ai_agents")
    if "vision" not in agents:
        conn.execute("ALTER TABLE ai_agents ADD COLUMN vision INTEGER NOT NULL DEFAULT 0")

    # 老对话没有发言人 ID。历史消息一律留空串，读的时候按「群聊只取有 ID 的」
    # 处理，升级前的老上下文自然被排除——宁可少几轮上文，也不要把 A 的话
    # 当成 B 说的塞进上下文。
    messages = _columns(conn, "ai_messages")
    if "author_id" not in messages:
        conn.execute("ALTER TABLE ai_messages ADD COLUMN author_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_chat_author "
        "ON ai_messages(bot_id, chat_id, author_id, id DESC)"
    )


def _seed_agents(conn: sqlite3.Connection) -> None:
    """把老的单份 AI 配置提升成第一个智能体。

    升级后不会丢配置：接口地址、Key、模型、提示词全部搬进来，
    并把 settings.ai_agent_id 指向它，机器人默认就用它说话。
    """
    if conn.execute("SELECT COUNT(*) AS n FROM ai_agents").fetchone()["n"]:
        return
    rows = conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(_LEGACY_AI_KEYS))})",
        _LEGACY_AI_KEYS,
    ).fetchall()
    data = {row["key"]: str(row["value"]) for row in rows}
    # 完全没配过就别造一个空壳出来，等用户自己新建
    if not (data.get("ai_api_key") or "").strip() and not (data.get("ai_model") or "").strip():
        return
    cur = conn.execute(
        "INSERT INTO ai_agents(name, base_url, api_key, model, system_prompt, "
        "temperature, max_tokens, history_len, min_interval, proxy, enabled) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
        (
            db_name(data),
            data.get("ai_base_url", ""),
            data.get("ai_api_key", ""),
            data.get("ai_model", ""),
            data.get("ai_system_prompt", ""),
            data.get("ai_temperature", "0.7") or "0.7",
            int(float(data.get("ai_max_tokens", "1600") or 1600)),
            int(float(data.get("ai_history_len", "12") or 12)),
            int(float(data.get("ai_min_interval", "3") or 3)),
            data.get("ai_proxy", ""),
        ),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('ai_agent_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(int(cur.lastrowid)),),
    )


def db_name(data: dict[str, str]) -> str:
    """从模型名推一个像样的智能体名字，省得用户看到一堆「默认智能体」。"""
    model = (data.get("ai_model") or "").strip()
    return f"{model} 助手" if model else "默认智能体"


# ---------------- settings ----------------

def get_setting(key: str, default: str = "") -> str:
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def set_setting(key: str, value: Any) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), "" if value is None else str(value)),
        )


def get_settings() -> dict[str, str]:
    with connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def set_settings(data: dict[str, Any]) -> None:
    with connection() as conn:
        for key, value in data.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), "" if value is None else str(value)),
            )


# ---------------- AI 对话记录 ----------------

def add_ai_message(
    chat_id: str,
    chat_title: str,
    role: str,
    author: str,
    content: str,
    bot_id: int = 0,
    author_id: str = "",
) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO ai_messages(bot_id, chat_id, chat_title, role, author, author_id, content) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                int(bot_id or 0),
                str(chat_id),
                chat_title[:120],
                role,
                author[:80],
                str(author_id or "")[:32],
                content[:4000],
            ),
        )
        # 每个会话只保留最近 60 条，避免无限增长（按智能体分开算）
        conn.execute(
            "DELETE FROM ai_messages WHERE bot_id=? AND chat_id=? AND id NOT IN "
            "(SELECT id FROM ai_messages WHERE bot_id=? AND chat_id=? ORDER BY id DESC LIMIT 60)",
            (int(bot_id or 0), str(chat_id), int(bot_id or 0), str(chat_id)),
        )


def ai_history(
    chat_id: str,
    limit: int = 12,
    bot_id: int = 0,
    author_id: str = "",
) -> list[dict[str, Any]]:
    """取最近若干条对话，按时间正序返回，供拼接上下文。

    author_id 非空时只取这个人和机器人的往返：群里张三问的话不会跑到
    李四的上下文里。老数据没有 author_id（空串），会被这个条件排除掉——
    升级后第一轮可能少几行上文，但不会张冠李戴。
    """
    limit = max(1, min(int(limit), 40))
    sql = "SELECT role, author, author_id, content, ts FROM ai_messages WHERE bot_id=? AND chat_id=?"
    params: list[Any] = [int(bot_id or 0), str(chat_id)]
    if author_id:
        sql += " AND author_id=?"
        params.append(str(author_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_ai_messages(limit: int = 60, bot_id: int | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 300))
    sql = "SELECT * FROM ai_messages"
    params: list[Any] = []
    if bot_id is not None:
        sql += " WHERE bot_id=?"
        params.append(int(bot_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def clear_ai_messages(bot_id: int | None = None) -> None:
    with connection() as conn:
        if bot_id is None:
            conn.execute("DELETE FROM ai_messages")
        else:
            conn.execute("DELETE FROM ai_messages WHERE bot_id=?", (int(bot_id),))


def ai_stats(bot_id: int | None = None) -> dict[str, int]:
    where = "" if bot_id is None else " WHERE bot_id=?"
    params: tuple[Any, ...] = () if bot_id is None else (int(bot_id),)
    with connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM ai_messages{where}", params).fetchone()["n"]
        today_row = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_messages "
            # ts 存的是 UTC（CURRENT_TIMESTAMP），先转本地再比「今天」，
            # 否则东八区白天算出来的「今日回复」会落到昨天
            "WHERE role='assistant' AND date(ts,'localtime')=date('now','localtime')"
            + ("" if bot_id is None else " AND bot_id=?"),
            params,
        ).fetchone()
        chats = conn.execute(
            f"SELECT COUNT(DISTINCT chat_id) AS n FROM ai_messages{where}", params
        ).fetchone()["n"]
    return {"total": int(total), "today": int(today_row["n"]), "chats": int(chats)}


# ---------------- AI 用量流水（ai_usage） ----------------
#
# 记流水而不是在 settings 里累加计数器：流水能按天/按智能体/按模型切，
# 计数器不能。表很小（每轮对话一行），保留 90 天后自动清理。

USAGE_KEEP_DAYS = 90


def add_usage(
    agent_id: int,
    bot_id: int,
    model: str,
    kind: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    ok: bool = True,
) -> None:
    """记一条用量。上游没给 usage 时两个 token 数都是 0，kind 标成 unknown。"""
    with connection() as conn:
        conn.execute(
            "INSERT INTO ai_usage(agent_id, bot_id, model, kind, prompt_tokens, "
            "completion_tokens, latency_ms, ok) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(agent_id or 0),
                int(bot_id or 0),
                str(model or "")[:80],
                str(kind or "chat")[:20],
                max(0, int(prompt_tokens or 0)),
                max(0, int(completion_tokens or 0)),
                max(0, int(latency_ms or 0)),
                1 if ok else 0,
            ),
        )
        # 顺手清理过期流水，避免这张表无限长。用本地时间比较，和别处一致。
        conn.execute(
            "DELETE FROM ai_usage WHERE ts < datetime('now','localtime','-%d days')"
            % USAGE_KEEP_DAYS
        )


def usage_summary(days: int = 1) -> dict[str, Any]:
    """最近 N 天的用量汇总：调用次数、token 数、平均延迟、失败数。

    days=1 表示「今天」（按本地日历天），不是「最近 24 小时」。
    """
    days = max(1, min(int(days), 90))
    if days == 1:
        since = "date(ts,'localtime')=date('now','localtime')"
    else:
        since = "ts >= datetime('now','localtime','-%d days')" % days
    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens),0) AS pin,"
            " COALESCE(SUM(completion_tokens),0) AS pout,"
            " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failed,"
            " COALESCE(SUM(CASE WHEN prompt_tokens=0 AND completion_tokens=0 THEN 1 ELSE 0 END),0) AS unmetered,"
            " COALESCE(AVG(CASE WHEN ok=1 THEN latency_ms END),0) AS avg_ms"
            " FROM ai_usage WHERE " + since,
        ).fetchone()
    return {
        "calls": int(row["calls"]),
        "prompt_tokens": int(row["pin"]),
        "completion_tokens": int(row["pout"]),
        "total_tokens": int(row["pin"]) + int(row["pout"]),
        "failed": int(row["failed"]),
        "avg_ms": int(row["avg_ms"] or 0),
        # 上游没返回 usage 的调用数。这部分 token 是笔糊涂账，必须让用户看见，
        # 否则「0 token」会被误解成「没花钱」。
        "unmetered": int(row["unmetered"]),
        "days": days,
    }


# ---------------- 长期记忆（ai_notes） ----------------
# scope: global 全局 / chat 某个会话 / user 某个人。智能体之间互不串味（agent_id 区分）。

def add_note(
    agent_id: int,
    scope: str,
    scope_key: str,
    content: str,
    source: str = "auto",
) -> int:
    with connection() as conn:
        cursor = conn.execute(
            "INSERT INTO ai_notes(agent_id, scope, scope_key, content, source) VALUES(?,?,?,?,?)",
            (int(agent_id or 0), scope, str(scope_key or "")[:120], content[:400], source),
        )
        return int(cursor.lastrowid or 0)


def list_notes(
    agent_id: int | None = None,
    scope: str | None = None,
    scope_key: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ai_notes WHERE 1=1"
    params: list[Any] = []
    if agent_id is not None:
        sql += " AND agent_id=?"
        params.append(int(agent_id))
    if scope:
        sql += " AND scope=?"
        params.append(scope)
    if scope_key is not None:
        sql += " AND scope_key=?"
        params.append(str(scope_key))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with connection() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def note_pool(agent_id: int, chat_id: str, author: str) -> list[dict[str, Any]]:
    """拼上下文时用的候选池：全局 + 当前会话 + 当前这个人。"""
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_notes WHERE agent_id=? AND ("
            "(scope='global') OR (scope='chat' AND scope_key=?) OR (scope='user' AND scope_key=?)"
            ") ORDER BY id DESC LIMIT 300",
            (int(agent_id or 0), str(chat_id), str(author or "")[:120]),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_note(note_id: int) -> bool:
    with connection() as conn:
        cursor = conn.execute("DELETE FROM ai_notes WHERE id=?", (int(note_id),))
        return cursor.rowcount > 0


def clear_notes(agent_id: int, scope: str = "", scope_key: str = "") -> int:
    sql = "DELETE FROM ai_notes WHERE agent_id=?"
    params: list[Any] = [int(agent_id)]
    if scope:
        sql += " AND scope=?"
        params.append(scope)
    if scope_key:
        sql += " AND scope_key=?"
        params.append(scope_key)
    with connection() as conn:
        cursor = conn.execute(sql, params)
        return int(cursor.rowcount or 0)


def touch_notes(note_ids: list[int]) -> None:
    if not note_ids:
        return
    marks = ",".join("?" for _ in note_ids)
    with connection() as conn:
        conn.execute(
            f"UPDATE ai_notes SET used_at=CURRENT_TIMESTAMP WHERE id IN ({marks})",
            [int(i) for i in note_ids],
        )


def note_stats(agent_id: int) -> dict[str, int]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT scope, COUNT(*) AS n FROM ai_notes WHERE agent_id=? GROUP BY scope",
            (int(agent_id),),
        ).fetchall()
    data = {"total": 0, "global": 0, "chat": 0, "user": 0}
    for row in rows:
        data[str(row["scope"])] = int(row["n"])
        data["total"] += int(row["n"])
    return data


# ---------------- 智能体 ----------------

AGENT_FIELDS = (
    "name", "base_url", "api_key", "model", "system_prompt",
    "temperature", "max_tokens", "history_len", "min_interval", "proxy",
    "vision", "enabled",
)


def _agent_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["vision"] = bool(data.get("vision"))
    data["key_set"] = bool(str(data.get("api_key") or "").strip())
    return data


def list_agents() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM ai_agents ORDER BY id").fetchall()
    return [_agent_row(row) for row in rows]


def get_agent(agent_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM ai_agents WHERE id=?", (agent_id,)).fetchone()
    return None if row is None else _agent_row(row)


def add_agent(data: dict[str, Any]) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO ai_agents(name, base_url, api_key, model, system_prompt, "
            "temperature, max_tokens, history_len, min_interval, proxy, vision, enabled) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data.get("name") or "未命名智能体",
                data.get("base_url", ""),
                data.get("api_key", ""),
                data.get("model", ""),
                data.get("system_prompt", ""),
                str(data.get("temperature", "0.7")),
                int(data.get("max_tokens", 1600)),
                int(data.get("history_len", 12)),
                int(data.get("min_interval", 3)),
                data.get("proxy", ""),
                1 if data.get("vision", False) else 0,
                1 if data.get("enabled", True) else 0,
            ),
        )
        return int(cur.lastrowid)


def update_agent(agent_id: int, data: dict[str, Any]) -> bool:
    current = get_agent(agent_id)
    if current is None:
        return False
    merged = {**current, **{k: v for k, v in data.items() if k in AGENT_FIELDS}}
    with connection() as conn:
        cur = conn.execute(
            "UPDATE ai_agents SET name=?, base_url=?, api_key=?, model=?, system_prompt=?, "
            "temperature=?, max_tokens=?, history_len=?, min_interval=?, proxy=?, vision=?, enabled=? "
            "WHERE id=?",
            (
                merged.get("name") or "未命名智能体",
                merged.get("base_url", ""),
                merged.get("api_key", ""),
                merged.get("model", ""),
                merged.get("system_prompt", ""),
                str(merged.get("temperature", "0.7")),
                int(merged.get("max_tokens", 1600)),
                int(merged.get("history_len", 12)),
                int(merged.get("min_interval", 3)),
                merged.get("proxy", ""),
                1 if merged.get("vision", False) else 0,
                1 if merged.get("enabled", True) else 0,
                agent_id,
            ),
        )
        return cur.rowcount > 0


def delete_agent(agent_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM ai_agents WHERE id=?", (agent_id,))
        # 引用了它的机器人退回「跟随默认」，避免留下悬空 ID
        conn.execute("UPDATE bots SET ai_agent_id=NULL WHERE ai_agent_id=?", (agent_id,))
        return cur.rowcount > 0


def default_agent_id() -> int | None:
    raw = get_setting("ai_agent_id", "").strip()
    if raw.isdigit() and get_agent(int(raw)):
        return int(raw)
    enabled = [a for a in list_agents() if a["enabled"]]
    if enabled:
        return int(enabled[0]["id"])
    return None


def agent_for_bot(bot_id: int | None) -> dict[str, Any] | None:
    """解析某个机器人实际该用哪个智能体。

    优先级：机器人自己指定的 → 全局默认 → 第一个启用的 → 空（回退老配置）。
    """
    if bot_id:
        record = get_bot(int(bot_id))
        if record and record.get("ai_agent_id"):
            agent = get_agent(int(record["ai_agent_id"]))
            if agent:
                return agent
    agent_id = default_agent_id()
    return get_agent(agent_id) if agent_id else None


# ---------------- 机器人 ----------------

BOT_FIELDS = (
    "name", "token", "username", "enabled", "trigger",
    "update_offset", "business_connections", "known_chats", "ai_agent_id",
)


def _bot_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["token_set"] = bool(str(data.get("token") or "").strip())
    return data


def list_bots() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
    return [_bot_row(row) for row in rows]


def get_bot(bot_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    return None if row is None else _bot_row(row)


def default_bot() -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM bots WHERE enabled=1 AND token<>'' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM bots ORDER BY id LIMIT 1").fetchone()
    return None if row is None else _bot_row(row)


def add_bot(data: dict[str, Any]) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO bots(name, token, username, enabled, trigger, ai_agent_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                data.get("name") or data.get("username") or "机器人",
                data.get("token", ""),
                data.get("username", ""),
                1 if data.get("enabled", True) else 0,
                data.get("trigger") or "mention",
                data.get("ai_agent_id") or None,
            ),
        )
        return int(cur.lastrowid)


def update_bot(bot_id: int, data: dict[str, Any]) -> bool:
    current = get_bot(bot_id)
    if current is None:
        return False
    merged = {**current, **{k: v for k, v in data.items() if k in BOT_FIELDS}}
    with connection() as conn:
        cur = conn.execute(
            "UPDATE bots SET name=?, token=?, username=?, enabled=?, trigger=?, "
            "update_offset=?, business_connections=?, known_chats=?, ai_agent_id=? WHERE id=?",
            (
                merged.get("name") or "机器人",
                merged.get("token", ""),
                merged.get("username", ""),
                1 if merged.get("enabled") else 0,
                merged.get("trigger") or "mention",
                str(merged.get("update_offset") or "0"),
                merged.get("business_connections", ""),
                merged.get("known_chats", ""),
                merged.get("ai_agent_id") or None,
                bot_id,
            ),
        )
        return cur.rowcount > 0


def delete_bot(bot_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM bots WHERE id=?", (int(bot_id),))
        conn.execute("DELETE FROM ai_messages WHERE bot_id=?", (int(bot_id),))
        return cur.rowcount > 0