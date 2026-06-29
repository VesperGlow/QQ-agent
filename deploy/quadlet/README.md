# 最小 Podman Quadlet 部署

这里的两份 `.container` 分别运行 GHCR 中的 AI API 和 QQ BotGo 桥接镜像，并共享 `qq-agent.network`。

`qq-agent-app` 仍需要可访问的 Neo4j 和 Embedding 服务。示例环境文件默认通过 `host.containers.internal` 连接宿主机的 `7687` 和 `8080` 端口。

## Rootless 安装

需要 Podman 5.x 与用户级 systemd：

```sh
mkdir -p ~/.config/containers/systemd
cp qq-agent.network qq-agent-app.container qq-agent-qqbot.container \
  ~/.config/containers/systemd/
cp app.env.example ~/.config/containers/systemd/app.env
cp qqbot.env.example ~/.config/containers/systemd/qqbot.env
chmod 600 ~/.config/containers/systemd/app.env \
  ~/.config/containers/systemd/qqbot.env
```

编辑两个 `.env` 文件。当前 GHCR 包若保持私有，还需先登录：

```sh
podman login ghcr.io
```

加载并启动：

```sh
systemctl --user daemon-reload
systemctl --user enable --now qq-agent-app.service qq-agent-qqbot.service
loginctl enable-linger "$USER"
```

检查：

```sh
systemctl --user status qq-agent-app.service qq-agent-qqbot.service
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:9000/healthz
```

示例 `qqbot.env` 默认使用 `QQ_EVENT_MODE=websocket`，不需要公网域名或反向代理；只有改成 `webhook` 时，才需要把 QQ 开放平台的 HTTPS 回调反向代理到 `127.0.0.1:9000/qqbot`。

启用 Podman 自带的镜像自动更新定时器：

```sh
systemctl --user enable --now podman-auto-update.timer
```
