"""WebDAV 备份：把配置、机器人、智能体、长期记忆打包成一个 JSON，传到自己的网盘。

为什么用 WebDAV 而不是 GitHub：
GitHub 那条路要么让用户建仓库、要么生成带 Contents 写权限的 PAT，两步都在把
「仓库钥匙」交给一个聊天机器人；而且国内访问 api.github.com 经常要代理。
WebDAV 是坚果云 / 群晖 / Nextcloud / fnOS 都支持的协议，用户手里基本都有，
一条 PUT 就能落盘，不需要任何第三方账号授权。

为什么备份不含对话记录：
对话记录动辄上万条、体积大，而且里面全是聊天原文，落到网盘风险最高。
真正难重建的是智能体配置和长期记忆，这些加起来通常只有几十 KB，所以只备份它们。

为什么备份不含密钥：
bot token / AI api_key / 面板密码 / WebDAV 密码一律不导出，只导出「是否已设置」。
备份文件是明文 JSON，一旦传上去就基本等于永久泄露。恢复后用户重填一次即可。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
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
#
# panel_secret 是面板会话的 HMAC 签名密钥。它本身不是密码，但拿到它就能
# 伪造任意登录 cookie —— 落到网盘上和把面板密码送出去等价，必须抹掉。
# webdav_pass 同理：备份文件本身就存在这个账号下面，再写密码进去没意义。
_SECRET_SETTING_KEYS = {
    "panel_password", "bot_token", "ai_api_key", "ai_proxy",
    "panel_secret", "webdav_pass", "backup_token",
}

# 上一版「GitHub 备份」留下的设置键。功能已经删了，但老用户的数据库里还在，
# 导出时直接丢掉，免得备份文件里带着一堆看不懂又没用的字段。
_LEGACY_KEYS = {"backup_repo", "backup_branch", "backup_token", "backup_token__set"}

# 打包后的体积上限。超过说明过滤逻辑坏了（比如记忆条数失控），
# 直接报错比闷头上传一个几十 MB 的文件好。
_MAX_BYTES = 8 * 1024 * 1024

STATE_LOCK = threading.Lock()

FORMAT_VERSION = 1

# 有些 WebDAV 服务端（坚果云就是）会挡掉 urllib 默认的 User-Agent
_UA = "tg-ai-bot-backup/1.0"

_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
    b"<d:getcontentlength/></d:prop></d:propfind>"
)


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
# 地址、账号、密码都从这里读，允许用户在面板里改，不用重启容器。

def cfg(key: str, default: str = "") -> str:
    return (db.get_setting(key) or default).strip()


def server_url() -> str:
    """WebDAV 根地址，形如 https://dav.jianguoyun.com/dav/。末尾斜杠会去掉。"""
    url = cfg("webdav_url") or os.getenv("WEBDAV_URL", "").strip()
    return url.rstrip("/")


def username() -> str:
    return cfg("webdav_user") or os.getenv("WEBDAV_USER", "").strip()


def password() -> str:
    return cfg("webdav_pass") or os.getenv("WEBDAV_PASS", "").strip()


def remote_dir() -> str:
    """目标目录，相对根地址。空表示直接放根目录（用户清空时尊重选择）。"""
    raw = cfg("webdav_dir")
    if "webdav_dir" not in db.get_settings():
        raw = "tg-ai-bot"  # 从没设置过才用默认值，设置成空串是有意的
    return "/".join(p for p in raw.strip().strip("/").split("/") if p)


def filename() -> str:
    raw = cfg("backup_path") or "tg-ai-bot-backup.json"
    # 只取最后一段，避免用户填了带目录的路径把 remote_dir 绕过去
    return raw.strip().split("/")[-1] or "tg-ai-bot-backup.json"


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


def ready() -> bool:
    """能不能动手：地址、账号、密码齐全。"""
    return bool(server_url() and username() and password())


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


# ---------------- WebDAV 基础动作 ----------------

def _auth_header() -> str:
    raw = f"{username()}:{password()}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _quote(seg: str) -> str:
    return urllib.parse.quote(seg, safe="")


def _path_url(*segments: str) -> str:
    """拼出某个集合/文件的完整 URL。每段单独转义，中文目录也能用。"""
    base = server_url()
    parts = [_quote(s) for s in segments if s]
    return base + ("/" + "/".join(parts) if parts else "")


def _file_url() -> str:
    return _path_url(remote_dir(), filename())


def _request(url: str, method: str = "GET", data: bytes | None = None,
             headers: dict[str, str] | None = None,
             timeout: int = 30) -> tuple[int, bytes, dict[str, str]]:
    """发一个 WebDAV 请求。返回 (状态码, 响应体, 响应头)。"""
    send: dict[str, str] = {
        "User-Agent": _UA,
        "Authorization": _auth_header(),
    }
    if headers:
        send.update(headers)
    req = urllib.request.Request(url, data=data, headers=send, method=method)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _probe_root() -> tuple[bool, str, str]:
    """探一下根地址是不是真的 WebDAV。返回 (是否可用, 错误, 建议)。

    单独抽出来是因为 push 和 verify 都要用：不做这一步的话，根地址填错会在
    MKCOL 那一步变成「403 没有权限」，把「地址错了」误导成「权限问题」——
    用户会去翻账号设置，而不是改 URL。
    """
    try:
        status, _body, _h = _request(
            server_url(), method="PROPFIND", data=_PROPFIND_BODY,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            timeout=25,
        )
    except Exception as exc:
        return False, f"连不上这个地址：{type(exc).__name__}", \
            "确认地址能从容器里访问到（带 https:// 或 http://），必要时填代理。"

    if status == 401:
        return False, "账号或密码不对（HTTP 401）", "坚果云要用「应用密码」，不是登录密码。"
    if status == 404:
        return False, "地址不存在（HTTP 404）", \
            "坚果云是 https://dav.jianguoyun.com/dav/，末尾的 dav 不能少。"
    if status not in (200, 207):
        return False, f"这个地址不像 WebDAV（HTTP {status}）", \
            "确认填的是 WebDAV 入口，不是网盘网页地址。"
    return True, "", ""


def _ensure_dir() -> None:
    """逐级 MKCOL。已存在返回 405（部分服务端返回 200/301），都算成功。"""
    segments = [s for s in remote_dir().split("/") if s]
    for i in range(1, len(segments) + 1):
        url = _path_url(*segments[:i])
        status, _body, _h = _request(url, method="MKCOL", timeout=20)
        # 201 新建成功；405 已存在；301/302 多数是服务端补尾斜杠
        if status in (200, 201, 204, 301, 302, 405):
            continue
        if status == 401:
            raise PermissionError("账号或密码不对（HTTP 401）")
        if status == 403:
            raise PermissionError("这个账号没有在目标目录创建的权限（HTTP 403）")
        if status == 409:
            raise RuntimeError("上级目录不存在（HTTP 409），检查根地址是否填对")
        if status == 404:
            raise RuntimeError("这个路径在建目录时找不到（HTTP 404），检查根地址是否填对")
        raise RuntimeError(f"创建目录失败（HTTP {status}）")


# ---------------- 打包 / 还原 ----------------

def build_payload() -> dict[str, Any]:
    """把要备份的东西打包。密钥一律不带值。"""
    settings = db.get_settings()
    safe_settings: dict[str, str] = {}
    for key, value in settings.items():
        if key in _LEGACY_KEYS:
            continue
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


def _encode(payload: dict[str, Any]) -> bytes:
    blob = json.dumps(payload, ensure_ascii=False, indent=1,
                      sort_keys=True).encode("utf-8")
    if len(blob) > _MAX_BYTES:
        raise ValueError(f"备份体积 {len(blob) // 1024}KB 超过上限，请减少记忆条数")
    return blob


def _counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "bots": len(payload.get("bots") or []),
        "agents": len(payload.get("agents") or []),
        "notes": len(payload.get("notes") or []),
    }


def push() -> dict[str, Any]:
    """把当前配置传到 WebDAV。"""
    result: dict[str, Any] = {"ok": False, "at": _now()}
    if not server_url():
        result["error"] = "还没填 WebDAV 地址"
        result["hint"] = "坚果云填 https://dav.jianguoyun.com/dav/，群晖/Nextcloud 填对应的 WebDAV 地址。"
        return result
    if not username() or not password():
        result["error"] = "还没填账号或密码"
        result["hint"] = "坚果云要用「应用密码」，不是登录密码；在账户信息 → 安全选项中生成。"
        return result

    try:
        payload = build_payload()
        blob = _encode(payload)
    except Exception as exc:
        result["error"] = f"打包失败：{exc}"
        return result

    # 先确认根地址真的是 WebDAV，否则后面 MKCOL 的 404 会被误读成权限问题
    ok, err, hint = _probe_root()
    if not ok:
        result["error"] = err
        result["hint"] = hint
        return result

    try:
        _ensure_dir()
    except PermissionError as exc:
        result["error"] = str(exc)
        return result
    except RuntimeError as exc:
        # 服务端明确回了个错，不是网络问题，别提示用户去查代理
        result["error"] = str(exc)
        result["hint"] = "确认地址指向 WebDAV 入口，不是网盘首页。"
        return result
    except Exception as exc:
        result["error"] = f"连不上 WebDAV：{type(exc).__name__}"
        result["hint"] = "地址要能直连；容器出网受限时在「代理」里填 HTTP 代理。"
        log.warning("备份建目录失败：%s", exc)
        return result

    url = _file_url()
    try:
        status, body, headers = _request(
            url, method="PUT", data=blob,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=60,
        )
    except Exception as exc:
        result["error"] = f"上传失败：{type(exc).__name__}"
        result["hint"] = "检查容器出网，或配置代理。"
        log.warning("备份上传失败：%s", exc)
        return result

    # 200 覆盖成功 / 201 新建 / 204 无内容返回，都是成功
    if status in (200, 201, 204):
        etag = (headers.get("ETag") or headers.get("etag") or "").strip('"')
        result.update({
            "ok": True,
            "path": f"{remote_dir()}/{filename()}".lstrip("/"),
            "etag": etag[:16],
            "counts": _counts(payload),
            "bytes": len(blob),
            "message": "已备份到 WebDAV。",
        })
        state = read_state()
        state.update({
            "last_push_at": result["at"],
            "last_etag": etag[:16],
            "last_bytes": len(blob),
            "last_ok": True,
            "url": server_url(),
            "dir": remote_dir(),
        })
        write_state(state)
        log.info("备份已上传：%s（%d 字节）", url, len(blob))
        return result

    if status == 401:
        result["error"] = "账号或密码不对（HTTP 401）"
        result["hint"] = "坚果云必须用应用密码；其他网盘确认没开二次验证拦截。"
    elif status == 403:
        result["error"] = "没有写入权限（HTTP 403）"
        result["hint"] = "账号没有这个目录的写权限，换个目录或换个账号。"
    elif status == 507:
        result["error"] = "网盘空间不足（HTTP 507）"
    else:
        detail = body.decode("utf-8", "replace")[:200] if body else ""
        result["error"] = f"上传被拒绝（HTTP {status}）"
        if detail:
            result["hint"] = re.sub(r"<[^>]+>", " ", detail).strip()[:160]
    return result


def fetch_remote() -> dict[str, Any]:
    """从 WebDAV 取回备份内容（不落盘）。"""
    if not ready():
        return {"ok": False, "error": "WebDAV 地址、账号或密码未配置"}
    try:
        status, body, _h = _request(_file_url(), method="GET", timeout=45)
    except Exception as exc:
        return {"ok": False, "error": f"连不上 WebDAV：{type(exc).__name__}"}
    if status == 404:
        return {"ok": False, "error": "网盘上还没有备份文件"}
    if status == 401:
        return {"ok": False, "error": "账号或密码不对（HTTP 401）"}
    if status != 200:
        return {"ok": False, "error": f"读取失败（HTTP {status}）"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"备份文件不是合法 JSON：{exc}"}
    if not isinstance(payload, dict) or payload.get("app") != "tg-ai-bot":
        return {"ok": False, "error": "这个文件不是本项目的备份"}
    return {
        "ok": True,
        "payload": payload,
        "counts": _counts(payload),
        "exported_at": payload.get("exported_at", ""),
        "bytes": len(body),
    }


def restore(payload: dict[str, Any]) -> dict[str, Any]:
    """把备份写回本地。

    只恢复「配置 / 智能体 / 记忆」，不动现有机器人记录里的 token：
    备份里没有 token，若整条覆盖会把用户已填的 token 清空。
    """
    applied = {"settings": 0, "agents": 0, "notes": 0, "bots": 0}
    try:
        for key, value in (payload.get("settings") or {}).items():
            if key.endswith("__set") or key in _SECRET_SETTING_KEYS or key in _LEGACY_KEYS:
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


# ---------------- 连通性校验 ----------------

def verify() -> dict[str, Any]:
    """校验连接：探测根地址 → 建目录 → 写一个探针文件再删掉。

    只做 PROPFIND 不足以说明问题：地址对、密码对，但没有写权限的话，
    备份照样会失败。所以最后要真写一次。
    """
    if not server_url():
        return {"ok": False, "error": "还没填 WebDAV 地址"}
    if not username() or not password():
        return {"ok": False, "error": "还没填账号或密码"}

    steps: list[dict[str, str]] = []

    ok, err, hint = _probe_root()
    if not ok:
        return {"ok": False, "error": err, "hint": hint, "steps": steps}
    steps.append({"step": "连接", "state": "ok", "detail": "WebDAV 可用"})

    try:
        _ensure_dir()
    except PermissionError as exc:
        return {"ok": False, "error": str(exc), "steps": steps}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "steps": steps}
    steps.append({"step": "目录", "state": "ok",
                  "detail": remote_dir() or "（根目录）"})

    # 真写一次，确认有写权限
    probe_url = _path_url(remote_dir(), "." + filename() + ".probe")
    try:
        status, _b, _h = _request(
            probe_url, method="PUT", data=b"ok",
            headers={"Content-Type": "text/plain"}, timeout=25,
        )
    except Exception as exc:
        return {"ok": False, "error": f"写入测试失败：{type(exc).__name__}", "steps": steps}
    if status not in (200, 201, 204):
        if status == 401:
            msg = "账号或密码不对（HTTP 401）"
        elif status == 403:
            msg = "这个账号没有写入权限（HTTP 403）"
        else:
            msg = f"写入测试失败（HTTP {status}）"
        return {"ok": False, "error": msg, "hint": "只能读不能写的账号做不了备份。",
                "steps": steps}
    steps.append({"step": "写入", "state": "ok", "detail": "探针文件已写入"})

    # 删掉探针，不留在用户网盘里
    try:
        _request(probe_url, method="DELETE", timeout=20)
    except Exception:
        pass

    return {
        "ok": True,
        "steps": steps,
        "target": f"{remote_dir()}/{filename()}".lstrip("/"),
        "message": "连接正常，可以写入。",
    }


def status() -> dict[str, Any]:
    """面板展示用。"""
    state = read_state()
    return {
        "configured": ready(),
        "url": server_url(),
        "user": username(),
        "has_password": bool(password()),
        "dir": remote_dir(),
        "path": f"{remote_dir()}/{filename()}".lstrip("/"),
        "auto": auto_enabled(),
        "interval": interval_minutes(),
        "proxy": cfg("backup_proxy"),
        "last_push_at": state.get("last_push_at", ""),
        "last_etag": state.get("last_etag", ""),
        "last_bytes": state.get("last_bytes", 0),
    }


# ---------------- 自动备份 ----------------

class AutoBackup:
    """后台线程：按间隔自动传一次。失败不重试，等下一轮。"""

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
        # 以启动时刻为起点：否则 time.time() - 0 恒大于间隔，一启动就会传一次
        self._last = time.time()
        while not self._stop.is_set():
            try:
                if auto_enabled() and ready():
                    due = time.time() - self._last
                    if due >= interval_minutes() * 60:
                        result = push()
                        self._last = time.time()
                        if result.get("ok"):
                            log.info("自动备份完成：%s", result.get("path", ""))
                        else:
                            log.warning("自动备份失败：%s", result.get("error"))
            except Exception as exc:
                log.warning("自动备份异常：%s", exc)
            # 每分钟醒一次判断是否到期，够用且开销可忽略
            if self._stop.wait(60):
                return


auto = AutoBackup()
