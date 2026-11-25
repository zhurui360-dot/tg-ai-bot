#!/bin/sh
# fnOS / 群晖等「不方便 docker build」的 NAS 上直接跑源码用的启动脚本。
# 用法：把整个目录放进容器，command 指向本脚本即可（见 README「NAS 免构建部署」）。
set -e

cd /app

# 依赖只装一次；已装过时 pip 会很快跳过
if [ ! -f /app/.deps-ok ]; then
    pip install --no-cache-dir -r /app/requirements.txt
    touch /app/.deps-ok
fi

# 数据目录：容器以 root 启动，装完依赖后降权到 1000 运行，
# 这样面板写出来的 sqlite 文件在 NAS 上归 fnOS 用户所有，方便备份。
# 如果你的 NAS 用户不是 1000，改下面的数字。
mkdir -p /app/data
chown -R 1000:1000 /app/data 2>/dev/null || true

exec setpriv --reuid=1000 --regid=1000 --clear-groups \
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000