"""在线升级：检查 GitHub 上有没有新版本，有就下下来覆盖代码，然后重启进程。

为什么是「覆盖代码 + 重启进程」而不是「换镜像」：
容器里的进程改不了自己的镜像（镜像文件系统是只读的底座），也没权限调 Docker
API 重建自己。所以在线升级只能在容器内部完成 —— 拿一份新代码，写进可写目录，
再让进程自己退出、由容器重启策略把它拉起来。

前提：代码必须放在可写目录里。镜像里的 /app/app 是只读底座，运行时复制一份到
$DATA_DIR/code，之后每次升级都覆盖那一份，启动时优先用它。
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("tg-ai-bot.updater")

# 升级源。默认官方仓库，可用环境变量改成自己的 fork。
REPO = os.getenv("UPDATE_REPO", "zhurui360-dot/tg-ai-bot").strip() or "zhurui360-dot/tg-ai-bot"
BRANCH = os.getenv("UPDATE_BRANCH", "main").strip() or "main"

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
# 运行时用的代码副本（可写）。镜像里的那份是只读基线，只在没有副本时复制过去。
CODE_DIR = DATA_DIR / "code"
STATE_FILE = DATA_DIR / "update-state.json"

# 代理：复用 AI_PROXY / TG_PROXY 的设置，容器里出网有时只能走代理
_PROXY = (os.getenv("UPDATE_PROXY", "").strip()
          or os.getenv("AI_PROXY", "").strip()
          or os.getenv("TG_PROXY", "").strip())

STATE_LOCK = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _opener() -> urllib.request.OpenerDirector:
    if _PROXY:
        handler = urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def read_state() -> dict[str, Any]:
    """本地记录的升级历史：当前提交、上次检查时间、上次升级时间。"""
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
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
            log.warning("写升级状态失败：%s", exc)


def current_commit() -> str:
    """当前「已安装」的是哪个提交。安装时写进 state，没有就是镜像自带版本。

    注意它不代表「正在跑」的版本：install_code 会立刻把 state 改成新 commit，
    但新代码要等进程重启才真正加载。要判断实际在跑什么，用 RUNNING_COMMIT。
    """
    return str(read_state().get("commit", "") or "")


def baseline_commit() -> str:
    """镜像自带那份代码对应的提交。

    CI 构建时把 `GIT_COMMIT` 写进镜像（Dockerfile 里的 ENV），本地跑源码时没有
    这个变量，返回空串。它解决的是一个具体的坑：容器首次启动、state 里还没有
    任何记录时，`current_commit()` 是空串，旧逻辑会把 has_update 判成 False，
    于是一个落后好几个版本的实例永远显示「已是最新」—— 用户只能靠手动重启
    容器重建代码副本，但那样又被 entrypoint 的「已有副本不覆盖」挡住。
    """
    return os.getenv("GIT_COMMIT", "").strip()[:40]


# 进程启动那一刻加载的代码版本，模块导入时算一次就不动了。
# 和 state 里的 commit 会短暂不一致 —— 这段窗口正是「下载完了还没重启」的状态，
# 前端靠它判断升级到底生效没生效，而不是被 state 骗过去。
#
# 镜像内置版本没有 state 记录，退回到构建时注入的 GIT_COMMIT。两者都空
# （本地跑源码、或老镜像没注入）才真的一无所知。
RUNNING_COMMIT: str = current_commit() or baseline_commit()


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "tg-ai-bot-updater",
        "Accept": "application/vnd.github+json",
    })
    with _opener().open(req, timeout=timeout) as resp:
        return resp.read()


def remote_commit(timeout: int = 20) -> dict[str, Any]:
    """查 GitHub 上目标分支的最新提交，同时当连通性探针用。"""
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    raw = _get(url, timeout=timeout)
    data = json.loads(raw.decode("utf-8", "replace"))
    return {
        "sha": str(data.get("sha", ""))[:40],
        "short": str(data.get("sha", ""))[:8],
        "date": (data.get("commit", {}).get("committer", {}) or {}).get("date", ""),
        "message": (data.get("commit", {}).get("message", "") or "").split("\n")[0][:120],
    }


def check() -> dict[str, Any]:
    """检查有没有新版本。返回结构化结果，界面直接渲染。"""
    local = RUNNING_COMMIT
    result: dict[str, Any] = {
        "ok": False,
        "repo": REPO,
        "branch": BRANCH,
        "current_commit": local,
        "current_short": local[:8] if local else "",
        "has_update": False,
        "checked_at": _now(),
    }
    try:
        info = remote_commit()
    except urllib.error.HTTPError as exc:
        result["error"] = f"访问 GitHub 失败（HTTP {exc.code}）"
        result["hint"] = "仓库不存在、分支改名，或容器出网被拦。"
        return result
    except Exception as exc:
        result["error"] = f"连不上 GitHub：{type(exc).__name__}"
        result["hint"] = "容器可能无法直连外网。如果 NAS 需要代理，把代理地址填进 UPDATE_PROXY 环境变量。"
        log.warning("检查更新失败：%s", exc)
        return result

    result["ok"] = True
    result["remote_commit"] = info["short"]
    result["remote_date"] = info["date"]
    result["remote_message"] = info["message"]
    installed = current_commit()
    result["installed_short"] = installed[:8] if installed else ""
    result["source"] = "install" if installed else ("baseline" if baseline_commit() else "")
    # 代码已经下好、只是还没重启：这种情况不该再说「发现有新版本」，
    # 否则用户会反复点升级、每次都在下载同一份。
    result["pending_restart"] = bool(installed) and installed[:40] == info["sha"] and local[:40] != info["sha"]
    if local:
        result["has_update"] = local[:40] != info["sha"]
    else:
        # 连基线提交都没有：老镜像没注入 GIT_COMMIT，无从判断是否落后。
        # 说清楚，别拍胸脯说「已是最新」。
        result["has_update"] = False
        result["note"] = "当前版本没有记录提交，无法判断是否落后。点升级会直接拉取最新代码。"
    if result["pending_restart"]:
        result["has_update"] = True
        result["note"] = f"最新代码 {info['short']} 已下载，重启容器即可生效。"

    state = read_state()
    state["last_check"] = result["checked_at"]
    write_state(state)
    return result


def _safe_extract(blob: bytes, dest: Path) -> int:
    """解压 GitHub 的 tar.gz 到 dest。

    GitHub 打包的顶层目录是 `仓库名-分支名/`，剥掉这一层。
    逐个成员校验路径，防止 `..` 之类的穿越（虽然源是我们自己的仓库，
    但这个函数将来可能被别人复用，不留下隐患）。
    """
    count = 0
    dest = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if not name or name.startswith("/") or ".." in Path(name).parts:
                continue
            parts = Path(name).parts
            if len(parts) < 2:
                continue  # 顶层目录本身，跳过
            rel = Path(*parts[1:])
            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest)):
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue  # 不处理符号链接、设备文件
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            with extracted, target.open("wb") as fh:
                shutil.copyfileobj(extracted, fh)
            count += 1
    return count


def install_code() -> dict[str, Any]:
    """拉最新代码覆盖到 CODE_DIR，返回结果。不动 data 里的数据库。"""
    result: dict[str, Any] = {"ok": False, "at": _now()}
    try:
        info = remote_commit()
    except Exception as exc:
        result["error"] = f"连不上 GitHub：{type(exc).__name__}"
        result["hint"] = "检查容器出网，或在 UPDATE_PROXY 里配代理。"
        return result

    local = RUNNING_COMMIT
    if local and local[:40] == info["sha"]:
        result["ok"] = True
        result["already_latest"] = True
        result["commit"] = info["short"]
        result["message"] = "已经是最新版本，无需升级。"
        return result

    # 代码上次已经下好、只是没重启成功：别再下一遍，直接让调用方重启即可。
    installed = current_commit()
    if installed and installed[:40] == info["sha"]:
        result.update({
            "ok": True,
            "needs_restart": True,
            "commit": info["short"],
            "message": f"{info['short']} 的代码已在本地，重启后生效。",
        })
        log.info("代码已是 %s，等待重启", info["short"])
        return result

    url = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
    try:
        blob = _get(url, timeout=90)
    except Exception as exc:
        result["error"] = f"下载代码失败：{type(exc).__name__}"
        result["hint"] = "codeload.github.com 可能被拦，试试配代理。"
        log.warning("下载升级包失败：%s", exc)
        return result

    # 先解压到临时目录，成功了再覆盖。半途失败不会把正在用的代码弄坏。
    staging = DATA_DIR / ".code-staging"
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        files = _safe_extract(blob, staging)
        if not files:
            result["error"] = "升级包里没有文件，可能下载不完整。"
            return result

        # 就地覆盖每一个文件，不要整目录 rename。
        # 进程的 cwd 认的是 inode 不是路径：把 CODE_DIR 改名后，老进程还待在旧目录里，
        # 紧接着 execv 出来的新进程也继承那个旧 cwd，于是导入的还是旧代码 ——
        # 面板显示「已升级」，跑的一直是老版本。就地覆盖才能保证 inode 不变。
        CODE_DIR.mkdir(parents=True, exist_ok=True)
        keep: set[Path] = set()
        for root, _dirs, names in os.walk(staging):
            rel_dir = Path(root).relative_to(staging)
            (CODE_DIR / rel_dir).mkdir(parents=True, exist_ok=True)
            for name in names:
                src = Path(root) / name
                shutil.copy2(src, CODE_DIR / rel_dir / name)
                keep.add(rel_dir / name)
        # 上游删掉的模块要跟着删，否则残留的旧文件会被继续 import
        for root, _dirs, names in os.walk(CODE_DIR):
            for name in names:
                rel = Path(root).joinpath(name).relative_to(CODE_DIR)
                if rel not in keep:
                    try:
                        (CODE_DIR / rel).unlink()
                    except OSError:
                        pass
        # 数据目录是挂载卷，代码副本里不该有它的内容；有就删掉
        stray = CODE_DIR / "data"
        if stray.exists():
            shutil.rmtree(stray, ignore_errors=True)
        # 清掉老版本 rename 策略留下的备份目录，别一直占着磁盘
        for leftover in (DATA_DIR / ".code-backup",):
            if leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)
    except Exception as exc:
        log.exception("替换代码失败")
        result["error"] = f"替换代码失败：{type(exc).__name__}"
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    state = read_state()
    state.update({
        "commit": info["sha"],
        "short": info["short"],
        "installed_at": result["at"],
        "files": files,
        "repo": REPO,
        "branch": BRANCH,
    })
    write_state(state)

    result.update({
        "ok": True,
        "files": files,
        "commit": info["short"],
        "message": f"已下载 {files} 个文件，重启后生效。",
    })
    log.info("代码已更新到 %s（%d 个文件）", info["short"], files)
    return result


def restart_process(delay: float = 1.0) -> None:
    """原地重启：用 execve 把当前进程映像整体换成新代码。

    为什么不用「退出让容器拉起」：那会依赖容器的 restart 策略，万一部署时没配
    restart，升级完服务就起不来了。execve 是同一进程号直接换映像，端口不断、
    不产生重启计数、也不依赖任何外部策略，最稳。

    命令按 CMD 的形态重建。不能用 sys.argv 原样拼 —— `python -m uvicorn` 的
    argv[0] 是 `__main__.py` 的路径，直接执它和 -m 的语义不完全一致。
    """
    port = os.getenv("PANEL_PORT", "").strip() or "8000"
    host = os.getenv("PANEL_HOST", "").strip() or "0.0.0.0"

    def _do() -> None:
        time.sleep(delay)
        log.info("升级完成，正在重启进程")
        argv = [sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", host, "--port", port]
        # 先把工作目录切到代码副本：即使旧进程的 cwd 因为历史原因停在别处
        # （比如被 rename 走的旧目录），重启后也要从新代码目录导入。
        try:
            os.chdir(CODE_DIR)
        except OSError as exc:
            log.warning("切到代码目录失败（%s），沿用当前目录", exc)
        try:
            os.execv(sys.executable, argv)
        except Exception as exc:
            log.error("execv 失败，改为退出让容器拉起：%s", exc)
            os._exit(1)

    threading.Thread(target=_do, name="upgrade-restart", daemon=True).start()
