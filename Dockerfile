# 单一二进制：HTTP API + 进程内 ONNX embedding + SQLite 分级记忆 + QQ 桥接，
# 全部在一个 Rust 进程里。写完 .env 即可 docker run / compose up。
# rust:1 = 最新 stable；锁文件里的依赖会随更新抬高 rust-version 下限，别钉旧小版本
FROM rust:1-bookworm AS builder

WORKDIR /src

# 先只拷贝依赖清单构建一次空壳，让依赖编译结果进缓存层。
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && \
    cargo build --release --locked && \
    rm -rf src

COPY src ./src
COPY static ./static
RUN touch src/main.rs && cargo build --release --locked

# ort 若为动态链接会产出 libonnxruntime*.so，收集起来；静态链接时无产物。
RUN mkdir -p /out/bin /out/lib && \
    cp target/release/qq-agent /out/bin/ && \
    (find target/release -maxdepth 4 -name 'libonnxruntime*.so*' -exec cp -a {} /out/lib/ \; || true)

FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 10001 appuser && \
    mkdir -p /data /models && \
    chown appuser:appuser /data /models

COPY --from=builder /out/bin/qq-agent /usr/local/bin/qq-agent
COPY --from=builder /out/lib/ /usr/local/lib/

ENV HF_HOME=/models \
    LD_LIBRARY_PATH=/usr/local/lib

USER appuser

# /data 存 SQLite 数据库，/models 缓存 embedding 模型（首次启动下载约 640MB）
VOLUME /data /models

EXPOSE 8000 9000
CMD ["qq-agent"]
