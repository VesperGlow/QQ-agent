# 单一镜像内包含两个进程：AI API（Python，内嵌 SQLite 存储与 ONNX embedding 推理）
# 与 QQ 桥接（Go）。写完 .env 即可 docker run / compose up，无任何外部服务依赖。
FROM golang:1.24-alpine AS gobuild

ARG GOPROXY=https://goproxy.cn,direct
ENV GOPROXY=${GOPROXY} CGO_ENABLED=0
WORKDIR /src

COPY qqbot/go.mod qqbot/go.sum ./
RUN go mod download
COPY qqbot/*.go ./
RUN go test ./... && go build -trimpath -ldflags="-s -w" -o /out/qqbot .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    QQ_AI_URL=http://127.0.0.1:8000/v1/chat \
    HF_HOME=/models

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/src ./src
COPY app/static ./static
COPY --from=gobuild /out/qqbot /usr/local/bin/qqbot
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh && \
    useradd --create-home --uid 10001 appuser && \
    mkdir -p /data /models && \
    chown -R appuser:appuser /app /data /models

USER appuser

# /data 存 SQLite 数据库，/models 缓存 embedding 模型（首次启动下载约 640MB）
VOLUME /data /models

EXPOSE 8000 9000
CMD ["entrypoint.sh"]
