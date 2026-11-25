#!/bin/sh
# 容器以 root 启动只为三件事：初始化数据目录、同步代码副本、降权。
#
# 为什么要同步代码副本：
# 容器里的进程改不了自己的镜像，所以「在线升级」只能在可写目录做。镜像里
# 带一份基线代码（/opt/baseline/app），首次启动复制到 $DATA_DIR/code，
# 面板升级时就覆盖那一份、再重启进程。
#
# 注意：这里不能写 `set -e`。降权是「尽力而为」的增强项，任何一步失败都不该让
# 整个服务起不来 —— 之前用 set -e 时，gosu 在受限权限模型下返回非 0，脚本立刻
# 退出，容器表现为 exit 255 无限重启，而日志还抓不到，排查成本极高。
set -u

DATA="${DATA_DIR:-/app/data}"
CODE="$DATA/code"
BASE="/opt/baseline/app"

mkdir -p "$DATA" 2>/dev/null || true
# 宿主目录挂进来时属主通常是 root，chown 一次让 app 用户能读写
chown -R app:app "$DATA" 2>/dev/null || true

# 首次启动：把镜像里的基线代码铺到可写目录。
# 已有的副本不覆盖 —— 那可能是用户在面板上升级过的版本，比镜像新。
if [ -d "$BASE" ]; then
    if [ ! -f "$CODE/app/main.py" ]; then
        echo "初始化代码副本 -> $CODE"
        mkdir -p "$CODE" 2>/dev/null || true
        cp -R "$BASE/." "$CODE/" 2>/dev/null || true
    fi
    chown -R app:app "$CODE" 2>/dev/null || true
fi

# 有副本就在副本目录里启动（Python 的 sys.path[0] 就是工作目录），
# 没有就退回镜像里的基线，保证一定能起来。
if [ -f "$CODE/app/main.py" ]; then
    cd "$CODE" 2>/dev/null || cd /opt/baseline
else
    cd /opt/baseline 2>/dev/null || true
fi

if [ "$(id -u)" = "0" ]; then
    # rootfs 里的 setuid 位是镜像自带、跑起来后才丢的，所以直接 exec gosu 会
    # 报 "failed switching to app: operation not permitted"。先探一次，探不通
    # 就说明是本机/平台的权限模型不让切（缺 CAP_SETUID），那就老实以当前用户
    # 跑，别为了降权把服务起不来。
    if command -v gosu >/dev/null 2>&1 && gosu app true 2>/dev/null; then
        exec gosu app "$@"
    fi
    echo "warn: 无法降权到 app 用户，以 uid $(id -u) 运行" >&2
fi

exec "$@"
