#!/bin/bash
# 在同一容器内先启动 AI API，等它就绪后再启动 QQ 桥接；
# 任一进程退出即整体退出，交给容器的 restart 策略拉起。
set -eu

app_pid=""
bot_pid=""
stopping=0

shutdown() {
  stopping=1
  # shellcheck disable=SC2086
  kill -TERM $app_pid $bot_pid 2>/dev/null || true
}
trap shutdown TERM INT

uvicorn src.main:app --host 0.0.0.0 --port 8000 --proxy-headers &
app_pid=$!

ready=0
for _ in $(seq 1 60); do
  if [ "$stopping" -eq 1 ]; then
    # 尚未就绪就被要求停止：直接退出，残留子进程随容器一起被清理。
    exit 143
  fi
  if ! kill -0 "$app_pid" 2>/dev/null; then
    echo "AI API 进程已退出，放弃启动" >&2
    exit 1
  fi
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "等待 AI API 就绪超时" >&2
  shutdown
  exit 1
fi

qqbot &
bot_pid=$!

wait -n "$app_pid" "$bot_pid" && status=0 || status=$?
shutdown
wait || true
exit "$status"
