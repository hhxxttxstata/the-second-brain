# Personal Knowledge Agent — Docker 镜像
# 仿 Dify 模式：docker-compose up 一条命令启动
FROM python:3.11-slim

WORKDIR /app

# 系统依赖最小化（jieba 无编译依赖，纯 Python）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 依赖清单（先复制以利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码 + eval 评测集
COPY app/ ./app/
COPY agent_data/eval/ ./agent_data/eval/

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

EXPOSE 8501
EXPOSE 8000

# 默认启动 Web 界面（引导 → Streamlit）
CMD ["sh", "-c", "python -c 'from app.cloud_bootstrap import main; main()' && exec streamlit run app/chat_web.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true"]
