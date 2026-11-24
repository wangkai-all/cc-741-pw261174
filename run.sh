#!/usr/bin/env bash
# 本地电池退化数据应用启动脚本
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

# 依赖检查（缺失时尝试安装到用户目录）
if ! "$PY" -c "import numpy, scipy, flask" >/dev/null 2>&1; then
  echo "[run.sh] 正在安装依赖 numpy / scipy / flask ..."
  "$PY" -m pip install --user -r requirements.txt
fi

export PORT="${PORT:-8000}"
export HOST="${HOST:-127.0.0.1}"
echo "[run.sh] 启动本地应用： http://${HOST}:${PORT}"
exec "$PY" -m backend.server
