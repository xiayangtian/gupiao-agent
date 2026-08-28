#!/usr/bin/env bash
# stop.sh — 根据 logs/app.pid 中记录的 PID 停止 Web 服务
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="logs/app.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "未找到 PID 文件 ($PID_FILE)，服务可能未在运行" >&2
  exit 0
fi

PID="$(cat "$PID_FILE")"

if ! kill -0 "$PID" 2>/dev/null; then
  echo "进程 $PID 已不存在，清理过期 PID 文件" >&2
  rm -f "$PID_FILE"
  exit 0
fi

# 安全校验：确认该 PID 确实属于本服务，避免误杀被系统复用的进程
CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
if [[ "$CMD" != *"uvicorn"* && "$CMD" != *"webapp.server"* ]]; then
  echo "警告：PID $PID 对应进程为「$CMD」，不是本服务，已取消停止操作" >&2
  echo "如确认无需保留，请手动删除 $PID_FILE 后再试" >&2
  exit 1
fi

echo "正在停止服务 (PID $PID)..."
PIDS="$PID $(pgrep -P "$PID" 2>/dev/null || true)"   # 含 uvicorn 子进程

# 优雅终止（TERM）
for p in $PIDS; do
  kill "$p" 2>/dev/null || true
done

# 等待退出（最多 10 秒）
for _ in $(seq 1 20); do
  ALIVE=""
  for p in $PIDS; do
    if kill -0 "$p" 2>/dev/null; then
      ALIVE="$ALIVE $p"
    fi
  done
  [[ -z "$ALIVE" ]] && break
  sleep 0.5
done

# 未退出则强制终止（KILL）
if [[ -n "${ALIVE:-}" ]]; then
  echo "以下进程未在 10 秒内退出，强制终止：$ALIVE" >&2
  for p in $ALIVE; do
    kill -9 "$p" 2>/dev/null || true
  done
fi

rm -f "$PID_FILE"
echo "服务已停止"
