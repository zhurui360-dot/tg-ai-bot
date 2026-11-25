"""FastAPI Web 面板与 API —— 只服务 AI 机器人。

接口分四块：面板鉴权 / 概览 / 机器人 / 智能体 + 记忆。
没有签到、账号、备份相关的接口。
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, bot, db, memory, security

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tg-ai-bot")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = security.SESSION_COOKIE

# 值守模式：同一套代码部署多个实例时，备用实例设 STANDBY=1 只开面板、不跑机器人，
# 否则两个进程会抢同一个 bot 的 getUpdates（Conflict）。
STANDBY = os.getenv("STANDBY", "").strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 数据目录里是 bot token、AI Key：能收紧的权限一律收紧
    security.harden_permissions(db.DATA_DIR, db.DB_PATH)
    if security.default_password_in_use():
        log.warning("面板仍在使用默认密码 admin123，任何人都能登录——请立刻在「设置」里改掉")
    if STANDBY:
        log.warning("STANDBY=1：值守模式，只提供面板，不启动机器人")
    else:
        bot.start()
    log.info("TG AI Bot %s 已启动", __version__)
    yield
    if not STANDBY:
        bot.stop()


app = FastAPI(title="TG AI Bot", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    """统一入口守卫：限体积、防 CSRF、补安全响应头、静态资源回源校验。"""
    path = request.url.path
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()

    if request.method in ("POST", "PUT", "PATCH"):
        raw_length = request.headers.get("content-length", "")
        if raw_length.isdigit() and int(raw_length) > security.MAX_BODY:
            return JSONResponse({"detail": "请求体太大，已拒绝"}, status_code=413)
        # 跨站表单在没有 CORS 时也会带上 cookie，这里再挡一层
        if path.startswith("/api/") and not security.same_origin(request.headers):
            return JSONResponse({"detail": "请求来源不可信，已拒绝"}, status_code=403)

    response = await call_next(request)
    security.apply_headers(response, path, https=(scheme == "https"))
    if path.startswith("/static/"):
        # 静态资源每次都回源校验（ETag 命中还是 304，不费流量），
        # 否则反代 / CDN 会把旧 JS/CSS 缓存住，面板更新后看到的还是旧界面
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ---------------- 鉴权 ----------------

def _panel_password() -> str:
    return db.get_setting("panel_password") or os.getenv("PANEL_PASSWORD", "")


def _client_ip(request: Request) -> str:
    """取真实来源 IP：Cloudflare 会把 cf-connecting-ip 写死成访客地址。

    不清洗 X-Forwarded-For —— 那个头客户端可以随便伪造，用来做限速键等于没限。
    """
    for header in ("cf-connecting-ip", "true-client-ip"):
        value = (request.headers.get(header) or "").strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"


def require_auth(request: Request) -> None:
    password = _panel_password()
    if not password:
        # 没设密码时面板等于裸奔。只允许本机直连做初始化，其它一律拒绝。
        if _client_ip(request) not in ("127.0.0.1", "::1"):
            raise HTTPException(
                status_code=401,
                detail="还没有设置面板密码，请先设置 PANEL_PASSWORD 环境变量",
            )
        return
    if not security.verify_session(request.cookies.get(COOKIE_NAME, ""), password):
        raise HTTPException(status_code=401, detail="请先登录管理面板")


# ---------------- 页面 ----------------

def _asset_version() -> str:
    """静态资源版本戳：取 css/js 的修改时间。

    面板更新后，浏览器和反代会继续用旧 JS，功能看着像坏了。
    index.html 里引用资源时带上这个戳，内容一变 URL 就变，天然绕开缓存。
    """
    stamp = 0
    for name in ("app.js", "style.css"):
        try:
            stamp = max(stamp, int((STATIC_DIR / name).stat().st_mtime))
        except OSError:
            continue
    return str(stamp or __version__)


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__ASSET_V__", _asset_version()))


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# ---------------- 面板登录 ----------------

@app.post("/api/panel/login")
def panel_login(body: dict[str, Any], request: Request, response: Response) -> dict[str, Any]:
    ip = _client_ip(request)
    wait = security.login_throttle.wait_seconds(ip)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"密码错误次数过多，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )
    password = str(body.get("password", ""))
    expected = _panel_password()
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="还没有设置面板密码，请先设置 PANEL_PASSWORD 环境变量",
        )
    if not security.password_ok(password, expected):
        security.login_throttle.fail(ip)
        log.warning("面板登录失败，来源 %s", ip)
        raise HTTPException(status_code=401, detail="面板密码错误")
    security.login_throttle.reset(ip)
    https = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    response.set_cookie(
        COOKIE_NAME,
        security.issue_session(expected),
        httponly=True,
        samesite="lax",
        secure=https or request.url.scheme == "https",
        path="/",
        max_age=security.SESSION_TTL,
    )
    return {"ok": True}


@app.post("/api/panel/logout")
def panel_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# ---------------- 概览 ----------------

@app.get("/api/bootstrap")
def bootstrap(_: None = Depends(require_auth)) -> dict[str, Any]:
    settings = db.get_settings()
    agents = db.list_agents()
    agents_public = []
    for item in agents:
        data = {k: v for k, v in item.items() if k != "api_key"}
        agents_public.append(data)
    return {
        "version": __version__,
        "standby": STANDBY,
        "settings": {
            "timezone": settings.get("timezone", "Asia/Shanghai"),
            "panel_title": settings.get("panel_title", "TG AI 机器人"),
            "password_set": bool(_panel_password()),
            "using_default_password": _panel_password() == "admin123",
            "memory_enabled": settings.get("memory_enabled", "1"),
            "memory_learn": settings.get("memory_learn", "1"),
            "memory_max_notes": settings.get("memory_max_notes", "8"),
            "bot_trigger": settings.get("bot_trigger", "mention"),
        },
        "bots": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "username": item.get("username"),
                "enabled": item.get("enabled"),
                "token_set": item.get("token_set"),
                "trigger": item.get("trigger"),
                "ai_agent_id": item.get("ai_agent_id"),
            }
            for item in db.list_bots()
        ],
        "ai_agents": agents_public,
        "ai_agent_id": settings.get("ai_agent_id", ""),
        "bot": bot.status(),
        "memory": {str(item.get("id")): db.note_stats(int(item["id"])) for item in agents},
        "chats": bot.known_chats(None),
    }


# ---------------- 设置 ----------------

SETTING_KEYS = {
    "timezone", "panel_password",
    "bot_enabled", "bot_token", "bot_trigger", "ai_agent_id",
    "ai_base_url", "ai_api_key", "ai_model", "ai_system_prompt",
    "ai_history_len", "ai_max_tokens", "ai_temperature", "ai_min_interval",
    "ai_proxy",
    "memory_enabled", "memory_learn", "memory_max_notes",
}
SECRET_KEYS = {"panel_password", "bot_token", "ai_api_key"}
TOGGLE_KEYS = {"memory_enabled", "memory_learn", "bot_enabled"}
BOT_NUMERIC_KEYS = {"ai_history_len", "ai_max_tokens", "ai_temperature", "ai_min_interval"}


@app.put("/api/settings")
def update_settings(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    changed: set[str] = set()
    for key in SETTING_KEYS:
        if key not in body:
            continue
        value = str(body[key]).strip()
        if key == "timezone":
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(value)
            except Exception:
                raise HTTPException(status_code=400, detail=f"无效时区：{value}")
        if key in BOT_NUMERIC_KEYS and value:
            try:
                number = float(value)
                if number < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{key} 必须是非负数")
        if key in SECRET_KEYS and not value:
            continue  # 留空表示不修改
        if key == "bot_trigger" and value not in ("mention", "all", "private"):
            raise HTTPException(status_code=400, detail="bot_trigger 只能是 mention / all / private")
        if key == "ai_agent_id" and value:
            if not value.isdigit() or not db.get_agent(int(value)):
                raise HTTPException(status_code=400, detail="指定的 AI 智能体不存在")
        if key == "memory_max_notes" and value:
            db.set_setting(key, str(max(1, min(int(float(value)), 20))))
            changed.add(key)
            continue
        if key in TOGGLE_KEYS and value not in ("0", "1"):
            raise HTTPException(status_code=400, detail=f"{key} 只能是 0 或 1")
        db.set_setting(key, value)
        changed.add(key)
    if changed & {"bot_enabled", "bot_token", "bot_trigger"} and not STANDBY:
        bot.restart()
    return {"ok": True}


# ---------------- 机器人 ----------------

@app.get("/api/bot/status")
def bot_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    return bot.status()


@app.get("/api/bot/config")
def bot_config(_: None = Depends(require_auth)) -> dict[str, Any]:
    """面板表单回填用：返回原文，密钥除外。

    token 和 Key 都不回明文：填进去之后面板只显示「已保存」，
    要换就整个重新填一遍。这样即使面板被人看到屏幕，也读不到凭据。
    """
    settings = db.get_settings()
    return {
        "bot_token_set": bool(settings.get("bot_token", "")),
        "ai_base_url": settings.get("ai_base_url", ""),
        "ai_model": settings.get("ai_model", ""),
        "ai_api_key_set": bool(settings.get("ai_api_key", "")),
        "ai_system_prompt": settings.get("ai_system_prompt", ""),
        "ai_history_len": settings.get("ai_history_len", "12"),
        "ai_max_tokens": settings.get("ai_max_tokens", "800"),
        "ai_temperature": settings.get("ai_temperature", "0.7"),
        "ai_min_interval": settings.get("ai_min_interval", "3"),
        "ai_proxy": settings.get("ai_proxy", ""),
    }


@app.get("/api/bot/chats")
def bot_chats(bot_id: int | None = Query(None), _: None = Depends(require_auth)) -> dict[str, Any]:
    """机器人待过的群，面板里可以一键复制群 ID。"""
    if bot_id:
        return {"chats": bot.known_chats(bot_id)}
    seen: dict[Any, dict[str, Any]] = {}
    for record in db.list_bots() or [None]:
        for chat in bot.known_chats((record or {}).get("id")):
            seen.setdefault(chat.get("id"), {**chat, "bot_id": (record or {}).get("id")})
    return {"chats": list(seen.values())}


@app.get("/api/bot/diagnose")
def bot_diagnose(_: None = Depends(require_auth)) -> dict[str, Any]:
    """出网体检：NAS / 内网部署时，先看这一步就知道卡在哪。"""
    return bot.diagnose()


@app.post("/api/bot/verify")
def bot_verify(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """校验 Bot Token，成功则保存到默认机器人。"""
    token = str(body.get("token", "")).strip() or db.get_setting("bot_token", "")
    bot_id = body.get("bot_id")
    try:
        bot_id = int(bot_id) if bot_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bot_id 必须是数字")
    try:
        info = bot.verify_token(token, bot_id)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    record = db.get_bot(bot_id) if bot_id else db.default_bot()
    if record and record.get("id"):
        db.update_bot(int(record["id"]), {"token": token, "username": info.get("username", "")})
    else:
        db.set_setting("bot_token", token)
    bot.restart(bot_id)
    return info


@app.post("/api/bot/send")
def bot_send(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """从面板发一条测试消息到指定会话。"""
    bot_id = body.get("bot_id")
    try:
        bot_id = int(bot_id) if bot_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bot_id 必须是数字")
    try:
        return bot.send_test_message(
            str(body.get("chat_id", "")),
            str(body.get("text", "测试消息")) or "测试消息",
            bot_id,
        )
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/bot/messages")
def bot_messages(
    limit: int = Query(60),
    bot_id: int | None = Query(None),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return {
        "messages": db.list_ai_messages(limit, bot_id),
        "stats": db.ai_stats(bot_id),
    }


@app.delete("/api/bot/messages")
def bot_messages_clear(bot_id: int | None = Query(None), _: None = Depends(require_auth)) -> dict[str, Any]:
    db.clear_ai_messages(bot_id)
    return {"ok": True}


@app.post("/api/bot/restart")
def bot_restart(body: dict[str, Any] | None = None, _: None = Depends(require_auth)) -> dict[str, Any]:
    bot_id = (body or {}).get("bot_id")
    try:
        bot_id = int(bot_id) if bot_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bot_id 必须是数字")
    bot.restart(bot_id)
    return bot.bot_detail(bot_id) if bot_id else bot.status()


# ---------------- 多机器人 ----------------

@app.get("/api/bots")
def list_bots(_: None = Depends(require_auth)) -> dict[str, Any]:
    bots = db.list_bots()
    default = db.default_bot()
    return {
        "bots": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "username": item.get("username"),
                "enabled": item.get("enabled"),
                "token_set": item.get("token_set"),
                "trigger": item.get("trigger"),
                "ai_agent_id": item.get("ai_agent_id"),
                "chats": bot.known_chats(item.get("id")),
                "status": bot.bot_detail(item["id"]) if item.get("id") else {},
                "default": bool(default and default.get("id") == item.get("id")),
            }
            for item in bots
        ],
        "count": len(bots),
    }


def _bot_agent_id(body: dict[str, Any]) -> int | None:
    """机器人绑定哪个智能体；0 / 空表示跟随全局默认。"""
    raw = body.get("ai_agent_id")
    if raw in ("", None, 0, "0"):
        return None
    try:
        agent_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ai_agent_id 必须是数字")
    if not db.get_agent(agent_id):
        raise HTTPException(status_code=400, detail="指定的 AI 智能体不存在")
    return agent_id


@app.post("/api/bots")
def create_bot(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    token = str(body.get("token", "")).strip()
    if not token:
        raise HTTPException(status_code=400, detail="请填写 Bot Token")
    try:
        info = bot.verify_token(token)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bot_id = db.add_bot(
        {
            "name": str(body.get("name", "")).strip() or info.get("name") or info.get("username"),
            "token": token,
            "username": info.get("username", ""),
            "enabled": bool(body.get("enabled", True)),
            "trigger": str(body.get("trigger", "mention") or "mention"),
            "ai_agent_id": _bot_agent_id(body),
        }
    )
    if not STANDBY:
        bot.start(bot_id)
    return {"ok": True, "id": bot_id, "username": info.get("username", ""), "name": info.get("name", "")}


@app.put("/api/bots/{bot_id}")
def edit_bot(bot_id: int, body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.get_bot(bot_id):
        raise HTTPException(status_code=404, detail="机器人不存在")
    fields: dict[str, Any] = {}
    for key in ("name", "username"):
        if key in body:
            fields[key] = str(body[key]).strip()
    if "trigger" in body:
        trigger = str(body["trigger"] or "mention")
        if trigger not in ("mention", "all", "private"):
            raise HTTPException(status_code=400, detail="触发方式只能是 mention / all / private")
        fields["trigger"] = trigger
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    if "ai_agent_id" in body:
        fields["ai_agent_id"] = _bot_agent_id(body)
    token = str(body.get("token", "")).strip()
    if token:
        try:
            info = bot.verify_token(token, bot_id)
        except bot.BotError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        fields["token"] = token
        fields.setdefault("username", info.get("username", ""))
        if not fields.get("name"):
            fields["name"] = info.get("name", "")
    db.update_bot(bot_id, fields)
    bot.restart(bot_id)
    return {"ok": True}


@app.delete("/api/bots/{bot_id}")
def remove_bot(bot_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    bot.stop(bot_id)
    if not db.delete_bot(bot_id):
        raise HTTPException(status_code=404, detail="机器人不存在")
    return {"ok": True}


@app.post("/api/bots/{bot_id}/verify")
def verify_bot(bot_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    record = db.get_bot(bot_id)
    if not record:
        raise HTTPException(status_code=404, detail="机器人不存在")
    try:
        info = bot.verify_token(str(record.get("token") or ""), bot_id)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return info


@app.post("/api/bots/{bot_id}/restart")
def restart_bot(bot_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.get_bot(bot_id):
        raise HTTPException(status_code=404, detail="机器人不存在")
    bot.restart(bot_id)
    return bot.bot_detail(bot_id)


@app.post("/api/bots/{bot_id}/toggle")
def toggle_bot(
    bot_id: int, body: dict[str, Any] | None = None, _: None = Depends(require_auth)
) -> dict[str, Any]:
    """一键停用 / 启用：只动 enabled，不碰 token、智能体绑定这些配置。

    不传 body 就是「取反」，传 {"enabled": true/false} 则设成指定值。
    """
    record = db.get_bot(bot_id)
    if not record:
        raise HTTPException(status_code=404, detail="机器人不存在")
    wanted = (body or {}).get("enabled")
    target = bool(wanted) if wanted is not None else not bool(record.get("enabled"))
    db.update_bot(bot_id, {"enabled": target})
    if not STANDBY:
        if target:
            bot.start(bot_id)
        else:
            bot.stop(bot_id)
    detail = bot.bot_detail(bot_id)
    detail["ok"] = True
    return detail


# ---------------- AI 智能体 ----------------

def _agent_payload(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="给智能体起个名字，方便区分")
    base_url = bot.normalize_base(str(body.get("base_url", "")))
    if not base_url:
        raise HTTPException(status_code=400, detail="请填写接口地址（例如 https://api.deepseek.com/v1）")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="接口地址要以 http:// 或 https:// 开头")
    model = str(body.get("model", "")).strip()
    if not model:
        raise HTTPException(status_code=400, detail="请填写模型名（例如 deepseek-chat）")
    # Key 留空表示不修改，避免面板回填掩码后把真实 Key 覆盖掉
    api_key = str(body.get("api_key", "")).strip()
    if not api_key and current:
        api_key = str(current.get("api_key") or "")
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 API Key")

    def number(key: str, default: float, low: float, high: float) -> float:
        raw = body.get(key, default)
        # 面板留空会传 ""，这不是「填错」，而是「用默认值」
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = default
        try:
            return max(low, min(float(raw), high))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} 必须是数字")

    enabled = body.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("1", "true", "on", "yes")
    return {
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "system_prompt": str(body.get("system_prompt", "")).strip(),
        "temperature": f"{number('temperature', 0.7, 0.0, 2.0):g}",
        "max_tokens": int(number("max_tokens", 800, 64, 4096)),
        "history_len": int(number("history_len", 12, 0, 40)),
        "min_interval": int(number("min_interval", 3, 0, 60)),
        "proxy": str(body.get("proxy", "")).strip(),
        "enabled": bool(enabled),
    }


@app.post("/api/ai/models")
def ai_available_models(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """按接口地址 + Key 拉模型列表。编辑已有智能体时 Key 留空，就用存下来的那把。"""
    agent: dict[str, Any] | None = None
    raw_id = body.get("agent_id")
    if raw_id not in (None, "", 0, "0"):
        try:
            agent = db.get_agent(int(raw_id))
        except (TypeError, ValueError):
            agent = None
    base_url = str(body.get("base_url", "")).strip() or str((agent or {}).get("base_url") or "")
    api_key = str(body.get("api_key", "")).strip() or str((agent or {}).get("api_key") or "")
    proxy = str(body.get("proxy", "")).strip() or str((agent or {}).get("proxy") or "")
    try:
        return bot.list_models(base_url, api_key, proxy)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/bot/test-ai")
def bot_test_ai(body: dict[str, Any] | None = None, _: None = Depends(require_auth)) -> dict[str, Any]:
    """测 AI 连通性。传 agent_id 就测那个智能体，不传按当前默认的测。"""
    agent = None
    raw = str((body or {}).get("agent_id", "") or "").strip()
    if raw:
        if not raw.isdigit() or not db.get_agent(int(raw)):
            raise HTTPException(status_code=400, detail="指定的 AI 智能体不存在")
        agent = db.get_agent(int(raw))
    else:
        agent = db.agent_for_bot(None)
    try:
        result = bot.test_ai(agent=agent)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result["agent"] = (agent or {}).get("name", "")
    return result


@app.get("/api/ai/agents")
def list_ai_agents(_: None = Depends(require_auth)) -> dict[str, Any]:
    default_id = db.default_agent_id()
    agents = []
    for item in db.list_agents():
        data = dict(item)
        data.pop("api_key", None)
        data["is_default"] = item["id"] == default_id
        agents.append(data)
    return {"agents": agents, "default_id": default_id}


@app.post("/api/ai/agents")
def create_ai_agent(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    agent_id = db.add_agent(_agent_payload(body))
    # 第一个智能体自动成为默认，省得再点一次
    if not db.get_setting("ai_agent_id", "").strip():
        db.set_setting("ai_agent_id", str(agent_id))
    return {"ok": True, "id": agent_id}


@app.put("/api/ai/agents/{agent_id}")
def edit_ai_agent(agent_id: int, body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    current = db.get_agent(agent_id)
    if not current:
        raise HTTPException(status_code=404, detail="智能体不存在")
    db.update_agent(agent_id, _agent_payload(body, current))
    return {"ok": True}


@app.delete("/api/ai/agents/{agent_id}")
def remove_ai_agent(agent_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="智能体不存在")
    if db.get_setting("ai_agent_id", "") == str(agent_id):
        # 删的是默认智能体，就把默认让给剩下的第一个
        remain = db.default_agent_id()
        db.set_setting("ai_agent_id", str(remain or ""))
    return {"ok": True, "default_id": db.default_agent_id()}


@app.post("/api/ai/agents/{agent_id}/default")
def set_default_ai_agent(agent_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.get_agent(agent_id):
        raise HTTPException(status_code=404, detail="智能体不存在")
    db.set_setting("ai_agent_id", str(agent_id))
    return {"ok": True, "default_id": agent_id}


@app.post("/api/ai/agents/{agent_id}/test")
def test_ai_agent(agent_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    agent = db.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    try:
        return bot.test_ai(agent=agent)
    except bot.BotError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------- 智能体记忆 ----------------

@app.get("/api/memory")
def list_memory(
    agent_id: int = Query(0),
    scope: str = Query(""),
    limit: int = Query(200),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    if scope and scope not in ("global", "chat", "user"):
        raise HTTPException(status_code=400, detail="scope 只能是 global / chat / user")
    return {
        "notes": db.list_notes(agent_id, scope or None, None, limit),
        "stats": db.note_stats(agent_id),
    }


@app.post("/api/memory")
def add_memory(body: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
    """手动塞一条记忆：面板里补一句「他叫小王，在杭州做后端」比等模型自己悟更可靠。"""
    try:
        agent_id = int(body.get("agent_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="agent_id 必须是数字")
    content = str(body.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    if len(content) > 300:
        raise HTTPException(status_code=400, detail="一条记忆别超过 300 字，拆成几条更好用")
    scope = str(body.get("scope", "global")).strip() or "global"
    if scope not in ("global", "chat", "user"):
        raise HTTPException(status_code=400, detail="scope 只能是 global / chat / user")
    scope_key = str(body.get("scope_key", "")).strip()
    if scope != "global" and not scope_key:
        raise HTTPException(status_code=400, detail="按会话或按人记的时候，要填会话 ID 或对方名字")
    note_id = db.add_note(agent_id, scope, scope_key, content, source="manual")
    return {"ok": True, "id": note_id, "stats": db.note_stats(agent_id)}


@app.delete("/api/memory/{note_id}")
def remove_memory(note_id: int, _: None = Depends(require_auth)) -> dict[str, Any]:
    if not db.delete_note(note_id):
        raise HTTPException(status_code=404, detail="这条记忆不存在")
    return {"ok": True}


@app.delete("/api/memory")
def clear_memory(
    agent_id: int = Query(0),
    scope: str = Query(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    removed = db.clear_notes(agent_id, scope or "")
    return {"ok": True, "removed": removed, "stats": db.note_stats(agent_id)}