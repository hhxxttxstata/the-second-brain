FROM python:3.11-slim

WORKDIR /app

# 系统依赖（jieba 分词 + 编译需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY app/ ./app/
COPY agent_data/eval/ ./agent_data/eval/

# 创建数据目录（云上运行时挂载或初始化）
RUN mkdir -p agent_data/traces agent_data/benchmark agent_data/feedback \
    agent_data/tasks agent_data/handoffs agent_data/memory \
    agent_data/outputs agent_data/code_runs

# 默认启动 Streamlit Web 界面
EXPOSE 8501
CMD ["streamlit", "run", "app/chat_web.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
