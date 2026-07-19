# 最小 Podman Quadlet 部署

这份 `mneme.container` 运行 GHCR 中的镜像——单个 Rust 二进制包含 AI API、进程内 ONNX embedding、SQLite 分级记忆与 QQ 桥接，不需要任何外部服务。数据保存在 `qq-agent-data`（SQLite）、`qq-agent-models`（模型缓存）两个卷里（卷名沿用历史，未随项目改名，以免挂到新空卷丢数据）。

## Rootless 安装

需要 Podman 5.x 与用户级 systemd：

```sh
mkdir -p ~/.config/containers/systemd
cp mneme.container ~/.config/containers/systemd/
cp mneme.env.example ~/.config/containers/systemd/mneme.env
chmod 600 ~/.config/containers/systemd/mneme.env
```

编辑 `mneme.env`。当前 GHCR 包若保持私有，还需先登录：

```sh
podman login ghcr.io
```

加载并启动：

```sh
systemctl --user daemon-reload
systemctl --user enable --now mneme.service
loginctl enable-linger "$USER"
```

检查：

```sh
systemctl --user status mneme.service
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:9000/healthz
```

示例 `mneme.env` 默认使用 `QQ_EVENT_MODE=websocket`，不需要公网域名或反向代理；只有改成 `webhook` 时，才需要把 QQ 开放平台的 HTTPS 回调反向代理到 `127.0.0.1:9000/qqbot`。

再精简一点的话，env 只需 8 行就能跑：`APP_API_KEY`、`AI_BASE_URL`、`AI_API_KEY`、`MEMORY_MODEL`、`CHAT_MODEL`、`QQ_APP_ID`、`QQ_APP_SECRET`、`QQ_EVENT_MODE=websocket`——存储路径、embedding 模型、记忆等级梯度等全部有代码默认值。

## 从旧的 qq-agent 部署切到 mneme

项目改名后，单元与容器名从 `qq-agent` 变成 `mneme`，但**卷名保持 `qq-agent-data`/`qq-agent-models` 不变**，所以既有 `memory.db` 会被新容器直接挂上、无需迁移数据：

```sh
# 停掉并移除旧单元
systemctl --user disable --now qq-agent.service
rm ~/.config/containers/systemd/qq-agent.container ~/.config/containers/systemd/qq-agent.env

# 装新单元（env 内容照搬旧的）
cp mneme.container ~/.config/containers/systemd/
cp mneme.env.example ~/.config/containers/systemd/mneme.env   # 或直接沿用旧 env 内容
chmod 600 ~/.config/containers/systemd/mneme.env
systemctl --user daemon-reload
systemctl --user enable --now mneme.service
```

镜像换成了 `ghcr.io/vesperglow/mneme:latest`（新的 GHCR 包，首次可能是私有，需 `podman login` 或在包设置里改公开）。若日后想把卷也改名成 `mneme-*`，得先停服务、用临时容器把 `qq-agent-data` 的内容拷进新建的 `mneme-data` 卷，再改 `.container` 里的 `Volume=`——不迁移直接改名会丢记忆。

从更旧版（Neo4j 时代）升级：`qq-agent-data` 卷的属主是当年的 neo4j 用户，新版应用（uid 10001）写不进去，启动会报 `unable to open database file`。旧数据新版用不上，停服务后 `podman volume rm qq-agent-data` 重建即可；`qq-agent-models` 卷不受影响。

启用 Podman 自带的镜像自动更新定时器：

```sh
systemctl --user enable --now podman-auto-update.timer
```
