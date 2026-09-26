# 冷库温度越限复盘 API
# 依赖在镜像构建时预装；容器启动时由 entrypoint.sh 执行初始化并启动服务。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    IN_DOCKER=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

WORKDIR /app

# 先拷贝依赖清单，利用 Docker 层缓存。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码与启动脚本。
COPY app ./app
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8000

# 单条启动命令：初始化检查 + 启动 uvicorn（依赖已在构建期安装，启动时会自动跳过）。
ENTRYPOINT ["./entrypoint.sh"]
