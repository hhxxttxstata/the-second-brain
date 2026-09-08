# Personal Knowledge Agent — Docker 镜像
# 多阶段构建：Node 构建前端 → Python 运行时由 FastAPI 同时托管 API 与 /workspace UI
FROM node:22-slim AS frontend-build

WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim

WORKDIR /app

# 系统依赖最小化（jieba 无编译依赖，纯 Python）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 依赖清单（先复制以利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 依赖安装之后追加
RUN pip install --no-cache-dir aliyun-bootstrap && aliyun-bootstrap -a install

# 复制代码 + eval 评测集 + 前端产物
COPY app/ ./app/
COPY agent_data/eval/ ./agent_data/eval/
COPY --from=frontend-build /web/dist/ ./frontend/dist/

# 健康检查（FastAPI 托管 Workspace UI + API）
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Workspace UI: http://localhost:8000/workspace/   API docs: http://localhost:8000/docs
# CMD ["python", "-X utf8", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
# 原 CMD 换成由 aliyun-instrument 拉起
CMD ["aliyun-instrument", "python", "-X", "utf8", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
