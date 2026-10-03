#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' '请先按 README 创建 Python 环境并安装依赖。'
  exit 1
fi
if [ ! -f frontend/dist/index.html ]; then
  printf '%s\n' '请先在 frontend 执行 npm ci && npm run build。'
  exit 1
fi
printf '%s\n' '映作工作台：http://127.0.0.1:8787' '停止服务：按 Ctrl+C。关闭终端将停止本地制作。'
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --no-access-log
