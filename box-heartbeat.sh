#!/bin/bash
# box 容器内核睡觉套：本地心跳 + 记录休眠间隔
# 装法（在 box 里）：
#   sudo install -m 644 box-heartbeat.sh          /usr/local/bin/box-heartbeat.sh
#   sudo install -m 644 box-heartbeat.service     /etc/systemd/system/
#   sudo install -m 644 box-heartbeat.timer       /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now box-heartbeat.timer
set -euo pipefail

LOG=/workspace/keepalive/box-heartbeat.log
mkdir -p "$(dirname "$LOG")"

now=$(date -u +%FT%TZ)
up=$(cut -d. -f1 /proc/uptime)
printf '%s up=%ss\n' "$now" "$up" >> "$LOG"

# 日志封顶 256KB，保住最近 2000 行
if [ "$(wc -c < "$LOG")" -gt 262144 ]; then
  tail -n 2000 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"
fi
