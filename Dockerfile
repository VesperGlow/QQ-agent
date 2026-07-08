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
    QQ_AI_URL=http://127.0.0.1:8000/v1/chat

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/src ./src
COPY app/static ./static
COPY --from=gobuild /out/qqbot /usr/local/bin/qqbot
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh && \
    useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000 9000
CMD ["entrypoint.sh"]
