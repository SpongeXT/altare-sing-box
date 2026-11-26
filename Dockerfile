# Altare sing-box 节点镜像 (Stockholm, 免费档)
# 官方 sing-box 镜像没有配置文件、Altare 向导又没有上传 config.json 的入口,
# 所以用 python:3.12-slim + app.py: 容器首次启动时下载 sing-box 1.14.4,
# 生成 UUID + Reality 密钥对并写到 /data/config.json (Altare data folder, 重部署保留)。
# 凭据只写在容器内的 /data 里, 不打印到控制台, 本镜像不含任何 UUID、密钥、链接。
FROM python:3.12-slim

# ca-certificates: app.py 要从 github.com 下载 sing-box, 需要可信 CA;
# curl: 排障用 (容器内 curl 检查端口/下载)。不装别的, 保持镜像小。
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py
RUN mkdir -p /data \
 && python3 -m py_compile /app/app.py

# 默认监听端口只是文档/占位: Altare 向导的 Port 字段会把 $PORT 传进来,
# app.py 按 PORT > SERVER_PORT > 8080 解析, 实际以环境变量为准。
EXPOSE 8080

CMD ["python", "app.py"]
