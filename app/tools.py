"""受限工具调用：让机器人能改自己的名字、设提醒、上网查资料。

**为什么不用原生 function calling**

项目里在用的 deepseek-v4.1-flash、sensenova-6.8-flash-lite 都不支持 tools 参数
（中转的 /v1/models 里 supports_tool_call 明确是 false）。如果按标准 function
calling 实现，换一个不支持多模态/工具的模型，整个能力就静默认失效——用户只会
看到「机器人突然不会设提醒了」。

所以这里用**文本动作协议**：模型在回复里输出一段

    [ACT]{"tool":"set_reminder","args":{"text":"喝水","minutes":20}}[/ACT]

我们负责解析、白名单校验、权限门禁、参数强校验，再执行。纯文本模型也能用，
换模型不影响。代价是模型可能写错格式或编参数，所以校验必须严，且**只认白名单**。

**安全底线**

1. 动作标记来自模型，模型输入来自任意群成员 —— 等同于不可信输入。白名单之外
   一律忽略，不报错不外泄。
2. 改名字、看别人的提醒这类操作绑定 owner_id，没配 owner 就直接不可用。
3. 时间不让模型算。模型只说「20 分钟后」或「明天 9 点」，由代码换算成 UTC，
   算错的可能性从模型手里拿走。
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import db

log = logging.getLogger("tg-ai-bot.tools")

# 动作标记。允许模型写成 ```json 包裹或直接贴，前后空白随意。
# 不用单个大正则去捕 JSON：args 是嵌套对象，非贪婪匹配会在第一个 } 截断。
_ACT_OPEN = re.compile(r"\[ACT\]")
_ACT_CLOSE = "[/ACT]"

MAX_ACTIONS_PER_TURN = 3
MAX_SEARCH_QUERY = 120
SEARCH_TIMEOUT = 12
SEARCH_PER_MINUTE = 8

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


# ---------------- 动作协议说明（注入系统提示） ----------------

PROTOCOL = """你可以调用工具来完成一些「光靠说话做不到」的事。需要时，在回复里单独输出一行：

[ACT]{"tool":"工具名","args":{参数}}[/ACT]

可用工具：
- set_reminder —— 设提醒。args: {"text":"提醒内容（20 字内）","minutes":多少分钟后（整数）}
  或者用 {"text":"...","at":"2026-11-27 09:00"} 指定具体时间（按用户当地时区）。
  用户说「明天九点」「20 分钟后」「下午三点半」都算，由你换算成参数。
- list_reminders —— 看还没触发的提醒。args: {}
- cancel_reminder —— 取消提醒。args: {"keyword":"内容里的关键词，可空表示全部"}
- web_search —— 联网搜索最新信息。args: {"query":"搜索词"}
  什么时候用：用户问新闻、价格、政策、天气、赛事、版本号，或任何你不确定、
  可能过时的事实。不要凭记忆回答这类问题。
- remember —— 记一条长期备忘。args: {"content":"要记住的事","scope":"user"}
- rename_self —— 改变你自己的名字。args: {"name":"新名字"}

规则：
1. 一次最多用一个工具；不需要工具时正常说话，不要输出 [ACT]。
2. 工具执行结果会再次发给你，那时再自然地告诉用户结果，**不要复述 JSON**。
3. 参数不确定就先用 web_search 或直接问用户，不要编。
4. [ACT] 标记用户是看不到的，别在正文里解释它。"""


# ---------------- 解析 ----------------

def _find_objects(text: str, start: int) -> list[tuple[int, int]]:
    """从 start 开始扫出所有顶层 {...} 的区间。

    不能用正则 `\\{.*?\\}`：非贪婪会在第一个 `}` 就收手，而 args 是嵌套对象
    （`{"tool":"x","args":{"name":"y"}}`），会被切成半个 JSON 直接解析失败。
    这里数字符串内的花括号，避免把 "说个 }" 这种内容当成结束。
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    begin = -1
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                begin = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and begin >= 0:
                    spans.append((begin, index + 1))
                    begin = -1
    return spans


def parse_actions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """从模型回复里切出动作，返回 (去掉动作后的正文, 动作列表)。

    截断的半个标记（有 [ACT] 没 [/ACT]）不算动作，但要把它从正文里清掉，
    免得用户看到一串没头没尾的 JSON。
    """
    raw = text or ""
    actions: list[dict[str, Any]] = []
    pieces: list[str] = []
    cursor = 0

    for marker in _ACT_OPEN.finditer(raw):
        begin = marker.end()
        close = raw.find("[/ACT]", begin)
        if close == -1:
            # 没闭合：多半是被 max_tokens 截断，别执行，直接丢弃后面全部内容
            pieces.append(raw[cursor:marker.start()])
            cursor = len(raw)
            break
        pieces.append(raw[cursor:marker.start()])
        blob = raw[begin:close]
        # 可能一段里塞了多个对象，逐个试
        for start, end in _find_objects(blob, 0) or []:
            try:
                data = json.loads(blob[start:end])
            except (ValueError, TypeError):
                log.info("动作 JSON 解析失败：%s", blob[start:end][:160])
                continue
            if isinstance(data, dict) and data.get("tool"):
                args = data.get("args")
                actions.append({
                    "tool": str(data["tool"]).strip(),
                    "args": args if isinstance(args, dict) else {},
                })
        cursor = close + len("[/ACT]")

    pieces.append(raw[cursor:])
    clean = "".join(pieces)
    if "[ACT]" in clean:
        # 兜底：残留半截标记，砍掉之后的内容，通常是坏 JSON
        clean = clean.split("[ACT]")[0]
    return clean.strip(), actions[:MAX_ACTIONS_PER_TURN]


# ---------------- 工具名与权限 ----------------

TOOL_FLAGS = {
    "set_reminder": "tools_reminders",
    "list_reminders": "tools_reminders",
    "cancel_reminder": "tools_reminders",
    "web_search": "tools_web_search",
    "rename_self": "tools_rename",
    "remember": "",          # 跟随总开关，不单独设
}


def enabled(tool: str) -> bool:
    """总开关 + 单项开关都开才可用。"""
    if db.get_setting("tools_enabled", "1") != "1":
        return False
    flag = TOOL_FLAGS.get(tool)
    if not flag:
        return True
    return db.get_setting(flag, "1") == "1"


def is_owner(author_id: str) -> bool:
    """没配 owner_id 时任何人都不算管理员——功能不可用，好过谁都能改名。"""
    owner = (db.get_setting("owner_id", "") or "").strip()
    return bool(owner) and str(author_id or "").strip() == owner


def owner_required(tool: str) -> bool:
    return tool in ("rename_self",)


# ---------------- 时区与时间 ----------------

def _tz():
    name = (db.get_setting("timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8))


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(_tz())


def to_utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def from_utc_iso(raw: str) -> datetime:
    return datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def human_local(utc_iso: str) -> str:
    """把 UTC 时间翻成用户看得懂的本地时间。"""
    try:
        local = from_utc_iso(utc_iso).astimezone(_tz())
    except (ValueError, TypeError):
        return str(utc_iso)
    today = now_local().date()
    delta = (local.date() - today).days
    stamp = local.strftime("%H:%M")
    if delta == 0:
        return f"今天 {stamp}"
    if delta == 1:
        return f"明天 {stamp}"
    if delta == 2:
        return f"后天 {stamp}"
    return local.strftime("%m月%d日 %H:%M")


_CN_DAY = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM = r"[0-9]{1,2}|[零一二两三四五六七八九十]{1,3}"
_CN_TIME_RE = re.compile(
    r"(?:(今天|今日|明天|明日|后天|大后天)\s*)?"
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*"
    rf"({_NUM})\s*(?:点|时|[:：])\s*(半|{_NUM})?\s*分?"
)
# ISO 风格必须先识别：否则 "2026-11-30 08:00" 会被中文规则当成「8 点」，
# 日期整个丢掉，变成今天/明天 8 点——这类静默错误比报错难查得多。
_ISO_RE = re.compile(
    r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2})[:：](\d{2})(?::(\d{2}))?)?\s*$"
)


def _cn_number(raw: str) -> int | None:
    """把「三」「十一」「二十三」翻成数字。只处理 1~59 这个量级（时/分够用）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_NUM.get(head, 1) if head else 1
        ones = _CN_NUM.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_NUM.get(text)


def _parse_cn_time(raw: str, local_now: datetime) -> datetime | None:
    """认「明天9点」「下午三点半」这类中文说法。

    模型和用户都爱这么写，而 strptime 一个都不认。宁可这里多几十行，
    也好过让用户每次都得打成 ISO 格式。
    """
    match = _CN_TIME_RE.search(raw or "")
    if not match:
        return None
    day_word, period, hour_raw, minute_raw = match.groups()
    hour = _cn_number(hour_raw)
    if hour is None:
        return None

    if period in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 12:
        hour = 12
    elif period == "凌晨" and hour == 12:
        hour = 0

    if minute_raw == "半":
        minute = 30
    elif minute_raw:
        minute = _cn_number(minute_raw)
        if minute is None:
            return None
    else:
        minute = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    offset = _CN_DAY.get(day_word or "", 0)
    cand = (local_now + timedelta(days=offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    # 没说哪天而这个点今天已经过了 → 顺延到明天。
    # 「晚上8点」在 21:08 说，指的必然是明天晚上 8 点，报「已过去」是跟用户较劲。
    if not day_word and cand <= local_now:
        cand += timedelta(days=1)
    return cand


def _resolve_due(args: dict[str, Any]) -> tuple[datetime | None, str]:
    """把模型给的时间参数换算成 UTC 时刻。算不出来就明确报错，不猜。

    两个来源：minutes（相对分钟数）优先，其次 at（本地时间字符串）。
    相对时间由我们加，不让模型做加法——它算「三刻钟后」经常差几分钟。
    """
    minutes = args.get("minutes")
    if minutes not in (None, ""):
        try:
            value = float(minutes)
        except (TypeError, ValueError):
            return None, "minutes 不是数字"
        if value <= 0:
            return None, "时间必须是将来的"
        if value > 60 * 24 * 365:
            return None, "时间太远了（最多一年）"
        return datetime.now(timezone.utc) + timedelta(minutes=value), ""

    at = str(args.get("at") or "").strip()
    if not at:
        return None, "没给时间"
    local_now = now_local()

    # 1) ISO 风格优先，完整带日期，不能被中文规则拆散
    iso = _ISO_RE.match(at)
    if iso:
        year, month, day, hour, minute, second = iso.groups()
        try:
            cand = datetime(
                int(year), int(month), int(day),
                int(hour or 0), int(minute or 0), int(second or 0),
                tzinfo=local_now.tzinfo,
            )
        except ValueError:
            return None, f"这个日期不存在：{at}"
        if cand <= local_now:
            return None, "这个时间已经过去了"
        return cand.astimezone(timezone.utc), ""

    # 2) 中文说法：明天9点、下午三点半
    chinese = _parse_cn_time(at, local_now)
    if chinese is not None:
        if chinese <= local_now:
            return None, "这个时间已经过去了"
        return chinese.astimezone(timezone.utc), ""

    # 3) 剩下几种常见的纯数字格式
    for pattern in ("%m-%d %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(at, pattern)
        except ValueError:
            continue
        if pattern == "%H:%M":
            cand = local_now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if cand <= local_now:
                cand += timedelta(days=1)   # 说「9 点」而现在已过 9 点 → 明天 9 点
        else:
            cand = parsed.replace(year=local_now.year, tzinfo=local_now.tzinfo)
            if cand < local_now - timedelta(days=1):
                cand = cand.replace(year=local_now.year + 1)
        if cand <= local_now:
            return None, "这个时间已经过去了"
        return cand.astimezone(timezone.utc), ""
    return None, f"看不懂这个时间：{at}"


def split_time_prefix(text: str) -> tuple[str, str]:
    """把「20m 喝水」「2026-11-30 08:00 体检」切成 (时间串, 剩下的正文)。

    命令行的 `/remind 时间 内容` 用空格分隔，但时间本身可能带空格
    （`2026-11-30 08:00`），简单 split 会把日期和时间切成两半。
    这里按「时间能认多长就吃多长」的前缀匹配来切。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    # 顺序很重要：相对时间（20m/2h）必须排在中文「N点」之前。
    # 否则 "20m 喝水" 里的 20 会被当成「20 点」，而 m 掉进正文里。
    # 数字与「点」之间不能有空格：否则 "明天9点 吃药" 会被切成 ("明天9","点 吃药")，
    # 因为 \s* 把点前面的空格吃掉了，正则在此处提前满足。
    patterns = (
        (r"^\d+(?:\.\d+)?\s*(?:m|min|分钟|分|h|hour|小时|个小时|d|天)(?=\s|$)", False),
        # 「90 分钟后」「2 小时以后」：数字与单位之间可以有空格
        (r"^\d+(?:\.\d+)?\s*(?:分钟|分|小时|个小时|天|m|h|d)\s*(?:后|之后|以后)", False),
        (r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}[:：]\d{2}(?::\d{2})?)?", False),
        # _NUM 自带 `|`，必须用 (?:...) 包住，否则整个正则被拆成两个分支：
        # "明天9点 吃药" 会只匹配到 "明天9"，"点" 掉进正文。
        (rf"^(?:今天|今日|明天|明日|后天|大后天)?\s*(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*(?:{_NUM})(?:点|时)(?:半|(?:{_NUM}))?分?", False),
        (r"^\d{1,2}[:：]\d{2}", False),
        (r"^(?:今天|今日|明天|明日|后天|大后天)(?=\s|$)", False),
    )
    for pattern, ci in patterns:
        match = re.match(pattern, raw, re.I if ci else 0)
        if match and match.group(0).strip():
            return match.group(0).strip(), raw[match.end():].strip()
    # 兜底：取第一个词，剩下的当正文（比如「乱写 测试」→ 时间解析会报看不懂）
    head, _, tail = raw.partition(" ")
    return head, tail.strip()


# ---------------- 联网搜索 ----------------

_search_hits: dict[str, list[float]] = {}
_search_lock = threading.Lock()


def _search_limited(key: str) -> bool:
    """每个会话每分钟最多几次，防止群里被人刷。"""
    now = time.time()
    with _search_lock:
        hits = [t for t in _search_hits.get(key, []) if now - t < 60]
        if len(hits) >= SEARCH_PER_MINUTE:
            _search_hits[key] = hits
            return True
        hits.append(now)
        _search_hits[key] = hits
    return False


def _strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw or "")
    return html.unescape(text).strip()


def _unwrap_ddg(link: str) -> str:
    """DuckDuckGo 的结果链接是自己的跳转地址，把真实 URL 拆出来。"""
    if "duckduckgo.com/l/" not in link:
        return link
    query = urllib.parse.urlparse(link).query
    target = urllib.parse.parse_qs(query).get("uddg") or []
    return target[0] if target else link


def _search_ddg(query: str, timeout: int) -> list[dict[str, str]]:
    """DuckDuckGo 的 HTML 版：免密钥，结果比 Bing RSS 具体得多。

    Bing 的 RSS 出口对中文长尾词会退化成一堆首页（搜「python 3.14 新特性」
    返回 python.org 首页），拿这个喂模型等于没搜。DDG 给的是具体文章。
    """
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")

    results: list[dict[str, str]] = []
    pattern = re.compile(
        r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?result__snippet"[^>]*>(.*?)</a>',
        re.S,
    )
    for link, title, snippet in pattern.findall(body)[:6]:
        title_text = _strip_tags(title)
        if not title_text:
            continue
        results.append({
            "title": title_text[:120],
            "url": _unwrap_ddg(html.unescape(link))[:300],
            "snippet": _strip_tags(snippet)[:220],
        })
    return results


def _search_bing(query: str, timeout: int) -> list[dict[str, str]]:
    """Bing 的 RSS 出口，作为 DDG 不可用时的兜底。"""
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"})
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")

    results: list[dict[str, str]] = []
    for item in re.findall(r"<item>(.*?)</item>", body, re.S)[:6]:
        title = re.search(r"<title>(.*?)</title>", item, re.S)
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        desc = re.search(r"<description>(.*?)</description>", item, re.S)
        title_text = _strip_tags(title.group(1)) if title else ""
        link_text = _strip_tags(link.group(1)) if link else ""
        if title_text and link_text:
            results.append({
                "title": title_text[:120],
                "url": link_text[:300],
                "snippet": _strip_tags(desc.group(1))[:220] if desc else "",
            })
    return results


def web_search(query: str, timeout: int = SEARCH_TIMEOUT) -> list[dict[str, str]]:
    """两个源依次试，任一拿到结果就返回。

    抓 HTML 的风险是搜索引擎改样式就失效，所以保留 RSS 这个结构稳定的兜底；
    反过来只用 RSS 又搜不到具体内容。两个都失败才算失败，并如实说明。
    """
    query = (query or "").strip()[:MAX_SEARCH_QUERY]
    if not query:
        return []
    errors: list[str] = []
    for name, engine in (("duckduckgo", _search_ddg), ("bing", _search_bing)):
        try:
            results = engine(query, timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if results:
            return results
        errors.append(f"{name}: 没有结果")
    raise RuntimeError("搜索没成功（" + "；".join(errors) + "）")


# ---------------- 执行 ----------------

class Ctx:
    """执行动作时需要知道的「谁在哪」——这些由代码注入，不让模型填。"""

    def __init__(
        self,
        agent: dict[str, Any] | None,
        bot_id: int | None,
        chat_id: str,
        chat_title: str,
        author: str,
        author_id: str,
    ) -> None:
        self.agent = agent or {}
        self.agent_id = int(self.agent["id"]) if self.agent.get("id") is not None else 0
        self.bot_id = int(bot_id or 0)
        self.chat_id = str(chat_id)
        self.chat_title = chat_title
        self.author = author
        self.author_id = str(author_id or "")


def _do_rename(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    name = re.sub(r"\s+", " ", name)[:24]
    if not name:
        return {"ok": False, "error": "新名字是空的"}
    if not ctx.agent_id:
        return {"ok": False, "error": "当前没有绑定智能体，改名无处可写"}
    if not db.update_agent(ctx.agent_id, {"name": name}):
        return {"ok": False, "error": "改名失败：写入数据库出错"}
    # 同时记一条记忆：名字写进 ai_agents.name 后，系统提示会带上它，
    # 但模型在长对话里仍可能忘了自己刚改名，记一条更稳。
    try:
        db.add_note(ctx.agent_id, "global", "", f"我的名字是「{name}」，这是主人给我起的。", source="tool")
    except Exception as exc:
        log.info("改名后写记忆失败（不影响改名）：%s", exc)
    return {"ok": True, "name": name, "result": f"已改名为「{name}」"}


def _do_remember(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    content = str(args.get("content") or "").strip()[:200]
    if not content:
        return {"ok": False, "error": "要记的内容是空的"}
    scope = str(args.get("scope") or "user").strip()
    if scope not in ("user", "chat", "global"):
        scope = "user"
    key = "" if scope == "global" else (ctx.chat_id if scope == "chat" else ctx.author[:120])
    db.add_note(ctx.agent_id, scope, key, content, source="tool")
    return {"ok": True, "result": f"记下了：{content}"}


def _do_set_reminder(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text") or "").strip()[:200]
    if not text:
        return {"ok": False, "error": "没说要提醒什么"}
    due, why = _resolve_due(args)
    if due is None:
        return {"ok": False, "error": f"时间不对：{why}"}
    reminder_id = db.add_reminder(
        ctx.agent_id, ctx.bot_id, ctx.chat_id, ctx.chat_title,
        ctx.author, ctx.author_id, text, to_utc_iso(due),
    )
    local = due.astimezone(_tz())
    return {
        "ok": True,
        "id": reminder_id,
        "due": to_utc_iso(due),
        "due_local": local.strftime("%Y-%m-%d %H:%M"),
        "result": f"提醒已设：{local.strftime('%m月%d日 %H:%M')} 提醒你「{text}」",
    }


def _do_list_reminders(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    # 只列自己的。群里的张三不该看到李四的提醒。
    rows = db.list_reminders(ctx.agent_id, ctx.chat_id, ctx.author_id, status="pending", limit=20)
    if not rows:
        return {"ok": True, "count": 0, "result": "现在没有待触发的提醒"}
    lines = [f"{human_local(str(r['due_at']))} — {r['text']}" for r in rows]
    return {"ok": True, "count": len(rows), "items": lines, "result": "待触发的提醒：\n" + "\n".join(lines)}


def _do_cancel_reminder(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    keyword = str(args.get("keyword") or "").strip()
    removed = db.cancel_reminders(ctx.agent_id, ctx.chat_id, ctx.author_id, keyword)
    if not removed:
        return {"ok": True, "count": 0, "result": "没找到符合条件的提醒" if keyword else "现在没有可取消的提醒"}
    names = "、".join(f"「{r['text']}」" for r in removed[:5])
    return {"ok": True, "count": len(removed), "result": f"已取消 {len(removed)} 条提醒：{names}"}


def _do_web_search(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()[:MAX_SEARCH_QUERY]
    if not query:
        return {"ok": False, "error": "搜索词是空的"}
    if _search_limited(ctx.chat_id):
        return {"ok": False, "error": "这个会话一分钟内搜太多次了，等一分钟再试"}
    try:
        results = web_search(query)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    if not results:
        return {"ok": True, "count": 0, "result": f"「{query}」没搜到结果"}
    lines = [f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}" for i, r in enumerate(results, 1)]
    return {"ok": True, "count": len(results), "items": results, "result": f"「{query}」的搜索结果：\n" + "\n".join(lines)}


_HANDLERS: dict[str, Callable[[Ctx, dict[str, Any]], dict[str, Any]]] = {
    "rename_self": _do_rename,
    "remember": _do_remember,
    "set_reminder": _do_set_reminder,
    "list_reminders": _do_list_reminders,
    "cancel_reminder": _do_cancel_reminder,
    "web_search": _do_web_search,
}


def available() -> list[str]:
    """当前配置下真正可用的工具名，用于拼系统提示。"""
    return [name for name in _HANDLERS if enabled(name)]


def run(ctx: Ctx, action: dict[str, Any]) -> dict[str, Any]:
    """执行一个动作。任何异常都必须被吃掉并转成 ok=False——工具出错不能炸掉回复。"""
    tool = str(action.get("tool") or "").strip()
    args = action.get("args")
    if not isinstance(args, dict):
        args = {}

    if tool not in _HANDLERS:
        log.info("忽略未知动作：%s", tool)
        return {"ok": False, "error": f"没有这个工具：{tool}"}
    if not enabled(tool):
        return {"ok": False, "error": f"「{tool}」这个能力被关掉了"}
    if owner_required(tool) and not is_owner(ctx.author_id):
        return {"ok": False, "error": "只有机器人主人能改名字"}

    try:
        return _HANDLERS[tool](ctx, args)
    except Exception as exc:  # 工具内部异常一律降级，不影响已经生成的回答
        log.warning("动作 %s 执行失败：%s", tool, exc)
        return {"ok": False, "error": f"执行出错：{exc}"}


def result_text(outcome: dict[str, Any]) -> str:
    """把执行结果压成一行给模型看的话。"""
    if outcome.get("ok"):
        return str(outcome.get("result") or "已完成")
    return f"没做成：{outcome.get('error') or '未知原因'}"
