# TG AI Bot

给 Telegram 接上大模型，做成一个会聊天、记得住事的机器人。自带中文网页面板。

只做一件事：**群里 @ 它就回**。没有签到、没有账号托管、不碰你的个人号。纯 Bot API，一个 Token 就能跑。

<!-- 想加截图的话，把图片放在 docs/ 下，取消下面这行注释 -->
<!-- ![面板预览](docs/preview.png) -->

---

## 它能做什么

- **群里 @ 才理人** —— 也支持「每条都回」和「只回私聊」两种模式，按群单独绑定
- **多个机器人同时跑** —— 每个 Token 一个独立轮询线程，一个挂了不影响别人
- **多个智能体** —— 不同群用不同人设、不同模型、不同 Key，可以随时切换
- **长期记忆** —— 聊完自动提炼值得记的事，下次聊天自己带上；也能手写几条让它记住
- **流式思考、上下文控制** —— 历史轮数、温度、最大 token、冷却时间都能在面板上调
- **出网代理** —— 直连不了 `api.telegram.org` 时填个 socks5/http 代理就行
- **网络自检** —— 面板里点一下，告诉你直连通不通、代理通不通、最后走的哪条路

面板块：

| 页面 | 干什么 |
| --- | --- |
| **概览** | 今日回复数、活跃会话、机器人在线状态、最近对话、一键网络自检 |
| **机器人** | 添加 / 启停 / 验证 Token / 重启轮询 / 设置触发方式 |
| **智能体** | 新建人设、选模型、拉取可用模型列表、设为默认、在线试一句话 |
| **记忆** | 按智能体 / 会话 / 用户查看和增删长期记忆 |
| **设置** | 时区、面板密码、记忆开关、注入条数 |

---

## 快速开始

### 1. 拿到 Bot Token

在 Telegram 里找 [@BotFather](https://t.me/BotFather)，发 `/newbot`，按提示起名字，最后会给你一串 `123456789:AAxxxxxxxx`。这就是 Bot Token。

### 2. 起服务

```bash
git clone https://github.com/zhurui360-dot/tg-ai-bot.git
cd tg-ai-bot
cp .env.example .env
```

编辑 `.env`，至少改掉这两项：

```ini
PANEL_PASSWORD=换成你自己的强密码
BOT_TOKEN=123456789:AAxxxxxxxx
```

然后：

```bash
docker compose up -d
```

打开 `http://你的IP:8000`，用刚设的密码登录。

> 没装 Docker？见下面的「不用 Docker」。

### 3. 让它能被 @ 到

- **私聊**：直接给机器人发消息就能聊（触发方式选「只回私聊」或「被 @ 时回复」都行）
- **群聊**：把机器人拉进群，然后**给它发一条消息并 @ 它一次**，否则 Telegram 默认不把群消息推给机器人
  - 如果群里开了隐私模式（BotFather 里 `/setprivacy` → `Enable`），机器人只能看到 @ 它的消息 —— 这正好够用
  - 关掉隐私模式才能看到全部群消息，`/setprivacy` → `Disable`
- **频道**：把机器人加成频道管理员，它才收得到消息

配好之后在面板「概览」看到「在线」就通了。

---

## 出网这一侧（重要）

这个程序是**主动出去拉消息**的 —— 靠 `getUpdates` 长轮询，由你的机器发起连接。所以：

- ✅ 不需要公网 IP
- ✅ 不需要路由器端口映射
- ✅ 不需要 Lucky / frp / ngrok 之类的内网穿透
- ❌ 内网穿透在这一层完全没用，因为压根没有入站连接

真正要通的是**出站**。如果 `api.telegram.org` 直连不通，配代理：

```ini
TG_PROXY=http://192.168.1.2:7890
# 或
TG_PROXY=socks5://192.168.1.2:7891
# 带账号密码
TG_PROXY=http://user:pass@192.168.1.2:7890
```

配完重启，在面板「概览 → 网络自检」里点一下，会分别告诉你「直连」「代理」两条路的结果，以及最终实际用的是哪条。

> AI 接口的代理是另一个变量 `AI_PROXY`，两者互相独立。比如 Telegram 能直连但 AI 服务需要走代理，就只填 `AI_PROXY`。

---

## 不用 Docker

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export DATA_DIR=./data
export PANEL_PASSWORD=你的密码
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Windows PowerShell 用 `$env:DATA_DIR="./data"` 这种写法。

---

## NAS 部署

### 路线 A：能 build 的 NAS（推荐）

群晖、威联通、fnOS 只要 Docker 能正常用，直接照「快速开始」走就行，把项目目录放在 Docker 的共享目录里。

### 路线 B：不方便 build 的 NAS

有些 NAS 的 Docker 面板只让你填镜像名和命令，不给构建。两个办法：

**B1. 用现成镜像**

```bash
docker build -t tg-ai-bot:latest .    # 在任意一台能 build 的机器上
docker save tg-ai-bot:latest | gzip > tg-ai-bot.tar.gz
```

把 tar 传到 NAS，在面板里「导入镜像」，然后按下面填：

| 项 | 值 |
| --- | --- |
| 镜像 | `tg-ai-bot:latest` |
| 端口 | `8000` → `8000` |
| 挂载 | `/vol1/docker/tg-ai-bot/data` → `/app/data` |
| 环境变量 | `PANEL_PASSWORD`、`TZ=Asia/Shanghai`、需要的 `TG_PROXY` |

**B2. 免构建，直接跑源码**

把仓库整个目录传到 NAS（比如 `/vol1/docker/tg-ai-bot`），用官方 `python:3.11-slim` 起容器，命令改成：

```bash
sh /app/nas-start.sh
```

这个脚本会做三件事：第一次启动时装依赖（之后用标记文件跳过）、建 `data` 目录、降权到 uid 1000 后启动。

挂载同样要配：`/vol1/docker/tg-ai-bot` → `/app`。**注意这时数据落在 `/app/data`**，也就是 NAS 上的 `tg-ai-bot/data`，别和别的容器混用。

> `nas-start.sh` 里的 `1000` 是 NAS 用户的 uid。如果你的 fnOS / 群晖用户不是 1000，`id` 一下改掉，否则面板写文件会 Permission denied。

---

## 配置项

全部可以通过环境变量给个初始值，之后在面板里改（面板里的值优先，存在数据库里）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PANEL_PORT` | `8000` | 映射到宿主机的面板端口 |
| `PANEL_PASSWORD` | — | 面板密码。**留空则只允许本机 127.0.0.1 访问**，用来做初始化 |
| `TZ` | `Asia/Shanghai` | 时区，影响「今日回复」这类统计口径 |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `DATA_DIR` | `/app/data` | sqlite 和配置文件放哪 |
| `BOT_TOKEN` | — | 首次启动时写入的 bot token |
| `AI_BASE_URL` | `https://api.deepseek.com/v1` | 兼容 OpenAI 协议的接口地址 |
| `AI_API_KEY` | — | AI 服务的 Key |
| `AI_MODEL` | `deepseek-chat` | 模型名 |
| `TG_PROXY` | — | Telegram 出网代理 |
| `TG_API_ROOT` | `https://api.telegram.org` | 自建反代时改这里，结尾不加 `/` |
| `AI_PROXY` | — | AI 接口出网代理，和 `TG_PROXY` 独立 |
| `STANDBY` | — | 设 `1` 则只开面板不跑机器人（多实例防抢消息） |

### 关于 `STANDBY`

同一个 bot token 同时被两个进程 `getUpdates`，Telegram 会返回 `Conflict`，消息会被随机抢走。所以：

- 只跑一个实例 → 不用管
- 想做「面板随时能看，机器人只在主机跑」→ 备用实例设 `STANDBY=1`

---

## 安全说明

这东西会存 bot token 和 AI Key，请注意：

- **面板别裸奔**。`PANEL_PASSWORD` 留空时只允许 `127.0.0.1` 直连；一旦设了密码就对外可用，**务必用强密码**
- 接口一律不回传明文凭据：Bot Token 和 AI Key 只会告诉你「已保存」，要换就整个重填
- 数据目录权限会自动收紧到 `700`，sqlite 文件 `600`
- 登录有按 IP 的失败限速，防止暴力猜密码
- 有 CSP、CSRF 同源校验、请求体大小限制
- 容器内以非特权用户运行
- 想挂到公网？**前面套一层 HTTPS 反代**（Nginx / Caddy / Lucky），并且让反代传 `X-Forwarded-Proto: https`，登录 cookie 才会带上 `Secure`

---

## 数据在哪

全部在 `DATA_DIR` 下，一个 sqlite 文件加 WAL：

```
data/
├── bot.db
├── bot.db-wal
└── bot.db-shm
```

备份就是把这几个文件复制走（先停容器更稳妥）。恢复就是放回去。

---

## 常见问题

**面板打开了，机器人一直「离线」**

先去「概览 → 网络自检」。如果直连和代理都不通，就是出网被挡了，配 `TG_PROXY`。如果自检通但状态还是离线，检查 token 有没有填错、是不是已经被别的程序（比如另一个容器、或者你本地调试的进程）在用 —— 那会报 `Conflict`。

**群里 @ 了没反应**

1. 机器人进群后，你得**先 @ 它说一句话**，Telegram 才会开始推群消息
2. 检查这个群有没有绑到机器人上（「机器人」页面里能看到待过的群）
3. 触发方式如果是「只回私聊」，群里当然不回
4. 群里开了隐私模式的话，它只能看到 @ 它的消息 —— 这个反而够用

**换了模型没生效**

智能体页面里点「设为默认」，或者在机器人配置里指定这个智能体。改完不用重启。

**「今日回复」数字不对**

时区问题。设置页里把时区调对，或者确认容器 `TZ` 环境变量是你想要的时区。

**面板更新后还是旧界面**

不会的。静态资源带 mtime 版本戳，改了文件 URL 就变，天然绕开缓存。真遇到就用强刷（Ctrl+Shift+R）。

---

## 技术栈

FastAPI + SQLite + 原生 JS，没有前端构建步骤。

```
app/
├── main.py       # FastAPI 路由、鉴权、请求守卫
├── bot.py        # Telegram Bot API 长轮询、消息处理、AI 调用
├── db.py         # sqlite 持久化：设置 / 机器人 / 智能体 / 消息 / 记忆
├── memory.py     # 长期记忆的提炼与注入
├── security.py   # 会话签名、CSP、限速、文件权限
└── static/       # 面板前端（index.html / app.js / style.css）
```

---

## License

MIT