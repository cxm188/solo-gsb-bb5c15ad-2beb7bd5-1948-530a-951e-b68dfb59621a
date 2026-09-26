#!/usr/bin/env bash
# 单条启动命令：依赖安装 -> 初始化检查 -> 启动 API。
#
# 用法：
#   容器内（镜像 ENTRYPOINT）：自动执行本脚本
#   本地：./entrypoint.sh            （首次运行会自动创建 .venv 并安装依赖）
#
# 可配置环境变量：
#   APP_HOST  监听地址，默认 0.0.0.0
#   APP_PORT  监听端口，默认 8000
set -euo pipefail
cd "$(dirname "$0")"

# 本地首次运行时创建虚拟环境；容器内（IN_DOCKER=1）直接使用系统解释器。
if [ -z "${IN_DOCKER:-}" ] && [ ! -d ".venv" ]; then
  echo ">> [0/3] 创建本地虚拟环境 .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
[ -d ".venv" ] && source .venv/bin/activate

# ---- [1/3] 依赖安装（已满足时自动跳过，离线/重启不重复下载） ----
if ! python -c "import fastapi, uvicorn, pydantic" >/dev/null 2>&1; then
  echo ">> [1/3] 安装依赖（pip install -r requirements.txt）"
  python -m pip install --disable-pip-version-check -r requirements.txt
else
  echo ">> [1/3] 依赖已就绪，跳过安装"
fi

# ---- [2/3] 初始化检查：加载应用，提前暴露配置/导入错误 ----
echo ">> [2/3] 应用初始化检查"
python -c "from app.main import app; print('   应用模块加载成功，路由数:', len(app.routes))"

# ---- [3/3] 启动服务 ----
HOST="${APP_HOST:-0.0.0.0}"
PORT="${APP_PORT:-8000}"
echo ">> [3/3] 启动 uvicorn：http://${HOST}:${PORT}（接口文档 /docs）"
exec uvicorn app.main:app --host "$HOST" --port "$PORT"
