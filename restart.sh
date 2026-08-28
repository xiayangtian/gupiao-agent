#!/usr/bin/env bash
# restart.sh — 先停止再启动（等价于依次执行 stop.sh + start.sh）
# 用法：./restart.sh [--foreground|-f]（透传给 start.sh）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE=""
case "${1:-}" in
  "" ) ;;
  --foreground|-f ) MODE="$1" ;;
  * )
    echo "未知参数：$1（用法：./restart.sh [--foreground|-f]）" >&2
    exit 2
    ;;
esac

"$SCRIPT_DIR/stop.sh"
"$SCRIPT_DIR/start.sh" $MODE
