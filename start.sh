#!/usr/bin/env bash
# start.sh — 启动 Web 服务，并将当前进程 PID 记录到 logs/app.pid
#
# 用法：
#   ./start.sh               后台启动（默认，nohup，适合普通终端）
#   ./start.sh --foreground  前台启动（-f；适合 Codex 桌面会话等
#                            命令结束后会回收后台子进程的环境）
# 停止：./stop.sh 或 Ctrl+C；重启：./restart.sh [--foreground|-f]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 参数解析 ──────────────────────────────────────────────────
FOREGROUND=0
case "${1:-}" in
  "" ) ;;
  --foreground|-f ) FOREGROUND=1 ;;
  * )
    echo "未知参数：$1（用法：./start.sh [--foreground|-f]）" >&2
    exit 2
    ;;
esac

# ── 可配置项（可用环境变量覆盖）────────────────────────────────
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
PYTHON="${PYTHON:-python3}"
PID_FILE="logs/app.pid"
LOG_FILE="logs/uvicorn.log"
HEALTH_URL="http://${HOST}:${PORT}/api/health"
START_TIMEOUT=15          # 健康检查等待秒数

mkdir -p logs

# 已在运行则拒绝重复启动
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "服务已在运行 (PID $OLD_PID)：http://${HOST}:${PORT}" >&2
    echo "如需重启请执行 ./restart.sh，或先执行 ./stop.sh" >&2
    exit 1
  fi
  echo "检测到过期 PID 文件，已清理" >&2
  rm -f "$PID_FILE"
fi

# ── 前台模式：适配「命令结束后回收后台子进程」的环境 ─────────────
# 脚本本身不退出，直到服务停止（Ctrl+C 或 ./stop.sh），因此进程可常驻；
# 日志直接输出到当前终端/会话；PID 落盘供 stop.sh 使用。
if [[ "$FOREGROUND" == "1" ]]; then
  # 前台模式日志直接输出到当前终端/会话（不依赖 /dev/fd，兼容受限环境）
  "$PYTHON" -m uvicorn webapp.server:app --host "$HOST" --port "$PORT" &
  PID=$!
  echo "$PID" > "$PID_FILE"
  echo "已启动服务（前台模式）：http://${HOST}:${PORT} (PID $PID)"
  echo "日志输出到当前终端/会话；按 Ctrl+C 停止，或另开终端执行 ./stop.sh"

  READY=0
  for _ in $(seq 1 "$START_TIMEOUT"); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      READY=1
      break
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "服务进程已退出（请查看上方终端日志）" >&2
      rm -f "$PID_FILE"
      exit 1
    fi
    sleep 1
  done

  if [[ "$READY" != "1" ]]; then
    echo "服务在 ${START_TIMEOUT} 秒内未就绪（请查看上方终端日志）" >&2
    kill "$PID" 2>/dev/null || true     # 启动失败则回滚
    rm -f "$PID_FILE"
    exit 1
  fi

  echo "服务就绪：$HEALTH_URL"
  # 保持前台运行；等待服务进程退出（Ctrl+C / ./stop.sh / 异常退出）
  if wait "$PID"; then RC=0; else RC=$?; fi
  rm -f "$PID_FILE"
  exit "$RC"
fi

# ── 后台模式（默认）：nohup 启动，日志写入 logs/uvicorn.log ──────
nohup "$PYTHON" -m uvicorn webapp.server:app --host "$HOST" --port "$PORT" \
  >>"$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

echo "已启动服务：http://${HOST}:${PORT} (PID $PID)"

# 健康检查：等待服务就绪
for _ in $(seq 1 "$START_TIMEOUT"); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "服务就绪：$HEALTH_URL"
    exit 0
  fi
  sleep 1
done

echo "服务在 ${START_TIMEOUT} 秒内未就绪，请查看日志：$LOG_FILE" >&2
tail -n 20 "$LOG_FILE" 2>/dev/null || true
kill "$PID" 2>/dev/null || true     # 启动失败则回滚
rm -f "$PID_FILE"
exit 1
