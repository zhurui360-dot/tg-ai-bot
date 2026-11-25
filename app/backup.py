"""GitHub 备份：把配置、机器人、智能体、长期记忆打包成一个 JSON，提交到私有仓库。

为什么备份不含对话记录：
对话记录动辄上万条、体积大，而且里面全是聊天原文，推到 GitHub 风险最高
（哪怕私有仓库，泄露一次就很难收回）。真正难重建的是智能体配置和长期记忆，
这些加起来通常只有几十 KB，所以只备份它们。

为什么备份不含密钥：
bot token / AI api_key / 面板密码一律不导出，只导出「是否已设置」。
备份文件会进 git 历史，一旦写进去基本等于永久泄露。恢复后用户重填一次即可。

为什么用 Contents API 而不是 git clone：
容器里不一定有 git，也没有必要为一次备份拉整个仓库；Contents API 一次 PUT
就是一个 commit，还能拿到返回的 commit sha，实现上最省事。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger("tg-ai-bot.backup")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "backup-state.json"

# 备份内容里出现这些键就抹掉值，只留「有没有设置」的标记。
# 注意 ai_agents 表的 api_key 和 bots 表的 token 在下面单独处理。
# backup_token 必须在列表里：否则会把 GitHub Token 自己备份进仓库，
# 这个 Token 有 Contents 写权限，等于把仓库钥匙一并交出去。
_SECRET_SETTING_KEYS = {
    "panel_password", "bot_token", "ai_api_key", "ai_proxy", "backup_token",
}

# Contents API 单文件上限 100MB，但这么大的 JSON 说明过滤逻辑坏了，
# 这里直接设一个远低于上限的阈值，超了就报错而不是硬传。
_MAX_BYTES = 8 * 1024 * 1024

STATE_LOCK = threading.Lock()

FORMAT_VERSION = 1


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def write_state(state: dict[str, Any]) -> None:
    with STATE_LOCK:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except OSError as exc:
            log.warning("写备份状态失败：%s", exc)


# ---------------- 配置项：存在 settings 表里，不是环境变量 ----------------
# 备份仓库、分支、Token 都从这里读，允许用户在面板里改，不用重启容器。

def cfg(key: str, default: str = "") -> str:
    return (db.get_setting(key) or default).strip()


def repo() -> str:
    """目标仓库，形如 user/repo。"""
    return cfg("backup_repo") or os.getenv("BACKUP_REPO", "").strip()


def branch() -> str:
    return cfg("backup_branch") or os.getenv("BACKUP_BRANCH", "").strip() or "main"


def path() -> str:
    """仓库里的文件路径。"""
    return cfg("backup_path") or "tg-ai-bot-backup.json"


def token() -> str:
    return cfg("backup_token") or os.getenv("BACKUP_TOKEN", "").strip()


def auto_enabled() -> bool:
    return cfg("backup_auto") == "1"


def interval_minutes() -> int:
    """自动备份间隔（分钟），最小 10 分钟。"""
    raw = cfg("backup_interval") or "1440"
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = 1440
    return max(10, value)


def _proxy() -> str:
    return (cfg("backup_proxy")
            or os.getenv("UPDATE_PROXY", "").strip()
            or os.getenv("AI_PROXY", "").strip())


def _opener() -> urllib.request.OpenerDirector:
    proxy = _proxy()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def _request(url: str, method: str = "GET", body: dict[str, Any] | None = None,
             timeout: int = 30) -> tuple[int, dict[str, Any]]:
    """调 GitHub API。返回 (状态码, 解析后的 JSON)。"""
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tg-ai-bot-backup",
    }
    tok = token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"message": raw[:300]}
        return exc.code, payload


# ---------------- 打包 / 还原 ----------------

def build_payload() -> dict[str, Any]:
    """把要备份的东西打包。密钥一律不带值。"""
    settings = db.get_settings()
    safe_settings: dict[str, str] = {}
    for key, value in settings.items():
        if key in _SECRET_SETTING_KEYS:
            safe_settings[f"{key}__set"] = "1" if str(value or "").strip() else "0"
            continue
        safe_settings[key] = value

    bots = []
    for item in db.list_bots():
        data = {k: v for k, v in item.items() if k != "token"}
        data["token_set"] = bool(str(item.get("token") or "").strip())
        # update_offset 是 Telegram 的偏移游标，换了环境没有意义，还会导致漏消息
        data.pop("update_offset", None)
        bots.append(data)

    agents = []
    for item in db.list_agents():
        data = {k: v for k, v in item.items() if k != "api_key"}
        data["api_key_set"] = bool(str(item.get("api_key") or "").strip())
        agents.append(data)

    notes = db.list_notes(None, None, None, 5000)

    return {
        "format": FORMAT_VERSION,
        "app": "tg-ai-bot",
        "exported_at": _now(),
        "note": "密钥不在备份里，恢复后需要在面板重新填写 Bot Token 与 AI Key。",
        "settings": safe_settings,
        "bots": bots,
        "agents": agents,
        "notes": [
            {
                "agent_id": n.get("agent_id"),
                "scope": n.get("scope"),
                "scope_key": n.get("scope_key"),
                "content": n.get("content"),
                "source": n.get("source"),
                "created_at": n.get("created_at"),
            }
            for n in notes
        ],
    }


def _encode(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    blob = text.encode("utf-8")
    if len(blob) > _MAX_BYTES:
        raise ValueError(f"备份体积 {len(blob) // 1024}KB 超过上限，请减少记忆条数")
    return base64.b64encode(blob).decode("ascii")


def _api_base() -> str:
    return f"https://api.github.com/repos/{repo()}/contents/{urllib.parse.quote(path())}"


def _remote_sha() -> str:
    """取远端文件当前的 sha（更新已有文件时必须带上，否则 422）。"""
    status, data = _request(_api_base())
    if status == 200:
        return str(data.get("sha", "") or "")
    if status == 404:
        return ""
    if status == 401:
        raise PermissionError("GitHub Token 无效或已过期")
    if status == 403:
        raise PermissionError("Token 没有该仓库的写入权限")
    raise RuntimeError(f"读取远端文件失败（HTTP {status}）")


def push(message: str | None = None) -> dict[str, Any]:
    """把当前配置推到 GitHub。"""
    result: dict[str, Any] = {"ok": False, "at": _now()}
    if not repo():
        result["error"] = "还没填备份仓库"
        result["hint"] = "在「GitHub 备份」里填 owner/repo，或者点「一键创建」自动建一个私有仓库。"
        return result
    if not token():
        result["error"] = "还没填 GitHub Token"
        result["hint"] = "点「一键直达 GitHub」创建带权限的 Token，粘贴回来即可。"
        return result

    try:
        payload = build_payload()
        content = _encode(payload)
    except Exception as exc:
        result["error"] = f"打包失败：{exc}"
        return result

    try:
        sha = _remote_sha()
    except PermissionError as exc:
        result["error"] = str(exc)
        result["hint"] = "重新生成一个带 Contents: Read and write 权限的 Token。"
        return result
    except Exception as exc:
        result["error"] = f"连不上 GitHub：{type(exc).__name__}"
        result["hint"] = "容器出网受限时，在「代理」里填 HTTP 代理地址。"
        log.warning("备份读取远端失败：%s", exc)
        return result

    body: dict[str, Any] = {
        "message": message or f"backup: {payload['exported_at']}",
        "content": content,
        "branch": branch(),
    }
    if sha:
        body["sha"] = sha

    try:
        status, data = _request(_api_base(), method="PUT", body=body, timeout=60)
    except Exception as exc:
        result["error"] = f"上传失败：{type(exc).__name__}"
        result["hint"] = "检查容器出网，或配置代理。"
        log.warning("备份上传失败：%s", exc)
        return result

    if status in (200, 201):
        commit = (data.get("commit") or {}).get("sha", "")
        result.update({
            "ok": True,
            "commit": commit[:8],
            "url": (data.get("content") or {}).get("html_url", ""),
            "counts": {
                "bots": len(payload["bots"]),
                "agents": len(payload["agents"]),
                "notes": len(payload["notes"]),
            },
            "bytes": len(content) * 3 // 4,
            "message": "已备份到 GitHub。",
        })
        state = read_state()
        state.update({
            "last_push_at": result["at"],
            "last_commit": commit[:8],
            "last_ok": True,
            "repo": repo(),
            "branch": branch(),
        })
        write_state(state)
        log.info("备份已推送：%s %s", repo(), commit[:8])
        return result

    detail = str(data.get("message", "") or "")[:200]
    result["error"] = f"GitHub 拒绝写入（HTTP {status}）"
    result["hint"] = detail or "确认 Token 有 Contents 写权限，且仓库/分支存在。"
    return result


def fetch_remote() -> dict[str, Any]:
    """从 GitHub 取回备份内容（不落盘）。"""
    if not repo() or not token():
        return {"ok": False, "error": "备份仓库或 Token 未配置"}
    try:
        status, data = _request(_api_base())
    except Exception as exc:
        return {"ok": False, "error": f"连不上 GitHub：{type(exc).__name__}"}
    if status == 404:
        return {"ok": False, "error": "远端还没有备份文件"}
    if status != 200:
        return {"ok": False, "error": f"读取失败（HTTP {status}）"}
    raw = str(data.get("content", "") or "").replace("\n", "")
    try:
        payload = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"备份文件不是合法 JSON：{exc}"}
    if not isinstance(payload, dict) or payload.get("app") != "tg-ai-bot":
        return {"ok": False, "error": "这个文件不是本项目的备份"}
    return {
        "ok": True,
        "payload": payload,
        "counts": {
            "bots": len(payload.get("bots") or []),
            "agents": len(payload.get("agents") or []),
            "notes": len(payload.get("notes") or []),
        },
        "exported_at": payload.get("exported_at", ""),
    }


def restore(payload: dict[str, Any]) -> dict[str, Any]:
    """把备份写回本地。

    只恢复「配置 / 智能体 / 记忆」，不动现有机器人记录里的 token：
    备份里没有 token，若整条覆盖会把用户已填的 token 清空。
    """
    applied = {"settings": 0, "agents": 0, "notes": 0, "bots": 0}
    try:
        for key, value in (payload.get("settings") or {}).items():
            if key.endswith("__set") or key in _SECRET_SETTING_KEYS:
                continue  # 密钥类跳过，不让备份覆盖本地凭据
            db.set_setting(key, value)
            applied["settings"] += 1

        existing_agents = {str(a.get("id")): a for a in db.list_agents()}
        for agent in (payload.get("agents") or []):
            data = {k: v for k, v in agent.items() if k not in ("id", "api_key_set", "key_set")}
            old_id = str(agent.get("id"))
            if old_id in existing_agents:
                db.update_agent(int(old_id), data)
                applied["agents"] += 1
            else:
                data.pop("created_at", None)
                db.add_agent(data)
                applied["agents"] += 1

        existing_notes = {
            (str(n.get("agent_id")), str(n.get("scope")), str(n.get("scope_key")), str(n.get("content")))
            for n in db.list_notes(None, None, None, 5000)
        }
        for note in (payload.get("notes") or []):
            key = (str(note.get("agent_id")), str(note.get("scope")),
                   str(note.get("scope_key")), str(note.get("content")))
            if key in existing_notes:
                continue  # 幂等：重复恢复不会堆出一堆重复记忆
            db.add_note(
                int(note.get("agent_id") or 0),
                str(note.get("scope") or "global"),
                str(note.get("scope_key") or ""),
                str(note.get("content") or ""),
                source=str(note.get("source") or "auto"),
            )
            applied["notes"] += 1

        for bot in (payload.get("bots") or []):
            if not bot.get("id"):
                continue
            data = {k: v for k, v in bot.items() if k not in ("id", "token", "token_set", "created_at")}
            if db.update_bot(int(bot["id"]), data):
                applied["bots"] += 1
    except Exception as exc:
        log.exception("恢复备份失败")
        return {"ok": False, "error": f"恢复失败：{exc}", "applied": applied}
    return {"ok": True, "applied": applied, "message": "已从备份恢复，密钥需重新填写。"}


# ---------------- Token 创建链接 ----------------

def token_url() -> str:
    """生成「已勾好权限」的 GitHub Token 创建链接。

    GitHub 的 fine-grained token 页面支持用 URL 参数预填字段（官方文档
    「在 URL 中预填细粒度个人访问令牌详情」）。备份只需要一个权限：
    Contents = Read and write（读写仓库文件）。Metadata 由 GitHub 自动附带。
    仓库选择没法用 URL 指定，所以 description 里写清楚该选哪个仓库。
    """
    target = ""
    if repo() and "/" in repo():
        target = repo().split("/", 1)[0]

    desc = "TG AI 机器人配置备份，只需要 Contents 读写权限"
    if repo():
        desc = f"TG AI 机器人配置备份（{repo()}），只需要 Contents 读写权限"
    params = {
        "name": "tg-ai-bot-backup",
        "description": desc,
        "expires_in": "90",
        "contents": "write",
    }
    if target:
        params["target_name"] = target
    return "https://github.com/settings/personal-access-tokens/new?" + urllib.parse.urlencode(params)


def verify_token() -> dict[str, Any]:
    """校验 Token 是否可用、有没有写权限，并返回账号信息。"""
    if not token():
        return {"ok": False, "error": "还没填 Token"}
    try:
        status, data = _request("https://api.github.com/user", timeout=20)
    except Exception as exc:
        return {"ok": False, "error": f"连不上 GitHub：{type(exc).__name__}"}
    if status == 401:
        return {"ok": False, "error": "Token 无效或已过期"}
    if status != 200:
        return {"ok": False, "error": f"校验失败（HTTP {status}）"}

    info: dict[str, Any] = {
        "ok": True,
        "login": data.get("login", ""),
        "name": data.get("name", ""),
        "scopes": [],
    }
    if not repo():
        info["hint"] = "Token 有效。接下来填备份仓库（owner/repo），或点「创建私有仓库」。"
        return info

    try:
        status, rdata = _request(f"https://api.github.com/repos/{repo()}")
        if status == 404:
            info["repo_ok"] = False
            info["hint"] = "Token 有效，但访问不到这个仓库。确认仓库名正确、Token 有该仓库权限。"
            return info
        if status != 200:
            info.update({"repo_ok": False, "hint": f"读取仓库失败（HTTP {status}）"})
            return info
        info["repo_ok"] = True
        info["repo_private"] = bool(rdata.get("private"))
        info["can_push"] = bool((rdata.get("permissions") or {}).get("push"))
        if info["repo_private"] is False:
            info["warn"] = "这个仓库是公开的，备份内容（含聊天记忆）会被人看到，建议改成私有。"
        elif not info["can_push"]:
            info["warn"] = "Token 对这个仓库似乎没有写权限，备份会失败。"
    except Exception as exc:
        info["hint"] = f"检查仓库时出错：{type(exc).__name__}"
    return info


def create_repo(name: str, private: bool = True) -> dict[str, Any]:
    """建一个私有仓库，省得用户跳出去手动建。"""
    if not token():
        return {"ok": False, "error": "需要先填 Token"}
    if not name:
        return {"ok": False, "error": "仓库名不能为空"}
    name = name.strip()
    if "/" in name:
        return {"ok": False, "error": "只填仓库名就行，不要带 owner/"}
    body = {
        "name": name,
        "private": bool(private),
        "auto_init": True,  # 带 README，这样后面 PUT 文件时分支一定存在
        "description": "TG AI 机器人配置备份（自动生成）",
    }
    try:
        status, data = _request("https://api.github.com/user/repos", method="POST", body=body, timeout=45)
    except Exception as exc:
        return {"ok": False, "error": f"连不上 GitHub：{type(exc).__name__}"}
    if status == 201:
        full = str(data.get("full_name", "") or "")
        if full:
            db.set_setting("backup_repo", full)
            db.set_setting("backup_branch", str(data.get("default_branch") or "main"))
        return {"ok": True, "repo": full, "url": data.get("html_url", ""), "private": bool(data.get("private"))}
    msg = str(data.get("message", "") or "")[:160]
    if status == 422:
        return {"ok": False, "error": "仓库已存在，或名字不合法", "hint": msg}
    return {"ok": False, "error": f"创建失败（HTTP {status}）", "hint": msg}


def status() -> dict[str, Any]:
    """面板展示用。"""
    state = read_state()
    return {
        "configured": bool(repo() and token()),
        "has_token": bool(token()),
        "repo": repo(),
        "branch": branch(),
        "path": path(),
        "auto": auto_enabled(),
        "interval": interval_minutes(),
        "proxy": cfg("backup_proxy"),
        "last_push_at": state.get("last_push_at", ""),
        "last_commit": state.get("last_commit", ""),
        "token_url": token_url(),
    }


# ---------------- 自动备份 ----------------

class AutoBackup:
    """后台线程：按间隔自动推一次。失败不重试，等下一轮。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auto-backup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 启动后先等一会儿，别和机器人启动抢资源
        if self._stop.wait(30):
            return
        # 以启动时刻为起点：否则 time.time() - 0 恒大于间隔，一启动就会推一次
        self._last = time.time()
        while not self._stop.is_set():
            try:
                if auto_enabled() and token() and repo():
                    due = time.time() - self._last
                    if due >= interval_minutes() * 60:
                        result = push("backup: auto")
                        self._last = time.time()
                        if result.get("ok"):
                            log.info("自动备份完成：%s", result.get("commit", ""))
                        else:
                            log.warning("自动备份失败：%s", result.get("error"))
            except Exception as exc:
                log.warning("自动备份异常：%s", exc)
            # 每分钟醒一次判断是否到期，够用且开销可忽略
            if self._stop.wait(60):
                return


auto = AutoBackup()
