"""提醒调度：每 20 秒扫一次到期的提醒，发回它被创建时的那个会话。

**为什么不用 APScheduler / crontab**

提醒量级是「几十条、分钟级精度」，引入调度框架等于给自己加一个依赖和一套
生命周期管理。一个 daemon 线程 + `status='pending' AND due_at<=now` 的索引查询
就够了，重启后也能自然恢复（pending 的行还在，下次扫描照样发）。

**几个必须处理的边界**

1. **重启补偿**：服务停了两小时，重启后会一次扫出所有过期提醒。全发是对的——
   提醒的价值就在于「你该做这事了」，哪怕晚了；但要限流，不能一口气刷 50 条。
2. **重试上限**：发送失败（比如机器人被踢出群）不能无限重试，否则数据库里
   永远躺着几条 pending，每次扫描都失败一次。超过 3 次标 failed，留痕不重试。
3. **只在有机器人在跑时发**：STANDBY 模式下不发，否则测试实例会打扰真实用户。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import db

log = logging.getLogger("tg-ai-bot.scheduler")

SCAN_INTERVAL = 20          # 秒
MAX_PER_SCAN = 5            # 一轮最多发几条，避免重启后一次性刷屏
MAX_TRIES = 3               # 同一条最多尝试几次

_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _tick() -> int:
    """扫描并发送一轮，返回成功发送的条数。"""
    # 延迟导入：bot 依赖 scheduler 的启停，scheduler 又用 bot 发消息
    from . import bot as bot_mod

    rows = db.due_reminders(_now_utc(), limit=MAX_PER_SCAN)
    sent = 0
    for row in rows:
        chat_id = row.get("chat_id")
        text = str(row.get("text") or "").strip()
        if not chat_id or not text:
            db.mark_reminder(int(row["id"]), "failed", "会话或内容为空")
            continue
        bot_id = int(row.get("bot_id") or 0) or None
        if not bot_mod.token_of(bot_id):
            # 机器人被删了：标失败别再刷，否则每 20 秒失败一次
            db.mark_reminder(int(row["id"]), "failed", "机器人不存在或没有 Token")
            continue

        who = str(row.get("author") or "").strip()
        title = str(row.get("chat_title") or "").strip()
        # 群聊里点名是谁的提醒；私聊没必要写「提醒你」之外的东西
        is_private = not title or str(title) == str(chat_id)
        body = f"⏰ 提醒{('「' + who + '」') if (who and not is_private) else '你'}：{text}"
        try:
            bot_mod.send_to(chat_id, body, bot_id=bot_id)
            db.mark_reminder(int(row["id"]), "done")
            sent += 1
            log.info("提醒已送达 #%s -> %s", row["id"], chat_id)
        except Exception as exc:
            tries = int(row.get("tries") or 0) + 1
            if tries >= MAX_TRIES:
                db.mark_reminder(int(row["id"]), "failed", str(exc))
                log.warning("提醒 #%s 连续失败 %d 次，放弃：%s", row["id"], tries, exc)
            else:
                db.mark_reminder(int(row["id"]), "pending", str(exc))
                log.info("提醒 #%s 发送失败，下次重试（%d/%d）：%s",
                         row["id"], tries, MAX_TRIES, exc)
    return sent


def _loop() -> None:
    log.info("提醒调度已启动（每 %d 秒扫描一次）", SCAN_INTERVAL)
    while not _stop.is_set():
        try:
            _tick()
        except Exception as exc:  # 扫描本身出错不能让线程死掉
            log.warning("提醒扫描异常：%s", exc)
        # 用 wait 而不是 sleep：stop 时能立刻退出，不用等满 20 秒
        _stop.wait(SCAN_INTERVAL)
    log.info("提醒调度已停止")


def start() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="reminder-scheduler", daemon=True)
        _thread.start()


def stop() -> None:
    global _thread
    with _lock:
        if _thread is None:
            return
        _stop.set()
        _thread.join(timeout=5)
        _thread = None


def status() -> dict[str, Any]:
    with _lock:
        alive = _thread is not None and _thread.is_alive()
    # 用 300 上限取一次再数长度；面板只需要「大概几条待发」，
    # 为了它单独加一个 COUNT 查询不值当。
    pending = db.list_reminders(status="pending", limit=300)
    return {
        "running": alive,
        "pending": len(pending),
        "interval": SCAN_INTERVAL,
    }


def run_once() -> int:
    """手动跑一轮，测试用。"""
    return _tick()
