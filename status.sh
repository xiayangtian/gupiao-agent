#!/usr/bin/env bash
# status.sh — 查询服务运行状态与代码版本对比
#
# 用法：./status.sh
# 输出：运行状态 / PID / 启动时间 / AI·索引状态 / 代码最后修改 / 是否最新
#   （启动时间来自 /api/health 的 started_at，用于判断服务是否加载了最新代码）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
PID_FILE="logs/app.pid"
HEALTH_URL="http://${HOST}:${PORT}/api/health"

# ── 1) 运行状态：优先以健康检查判定（kill -0 在受限环境可能被拒）──
HEALTH_JSON="$(curl -fsS -m 5 "$HEALTH_URL" 2>/dev/null || true)"
RUNNING=0
if [[ -n "$HEALTH_JSON" ]]; then
  RUNNING=1
fi

PID=""
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if [[ "$RUNNING" != "1" ]] && kill -0 "$PID" 2>/dev/null; then
    RUNNING=1   # 进程在但 API 未就绪
  fi
fi

if [[ "$RUNNING" != "1" ]]; then
  echo "🔴 服务未运行"
  echo "   启动：./start.sh | 前台：./start.sh -f | 重启：./restart.sh"
  exit 1
fi

if [[ -n "$PID" ]]; then
  echo "🟢 服务运行中 (PID $PID)  http://${HOST}:${PORT}"
else
  echo "🟢 服务运行中  http://${HOST}:${PORT}"
fi

# ── 2) 健康检查 + 启动时间 / 代码版本对比 ─────────────────────
if [[ -z "$HEALTH_JSON" ]]; then
  echo "⚠️ 健康检查失败（进程在但 API 未响应，可能正在启动或已卡死）"
  exit 1
fi

echo "$HEALTH_JSON" | python3 -c "
import json, os, sys, time

d = json.load(sys.stdin)
started_ts = d.get('started_ts') or 0
started_at = d.get('started_at') or '未知（旧版本服务）'
print('启动时间:', started_at)
print('AI 状态 :', '已配置' if d.get('ai_key_configured') else '未配置')
print('索引状态:', '就绪' if d.get('index_ready') else '未就绪')

# 代码最新修改时间（排除缓存/依赖目录）
roots = ['webapp', 'financial_report_fetcher', 'config.example.yaml', 'README.md']
latest = 0.0
for root in roots:
    if os.path.isfile(root):
        latest = max(latest, os.path.getmtime(root))
    elif os.path.isdir(root):
        for dp, dirs, fns in os.walk(root):
            dirs[:] = [x for x in dirs if x not in ('__pycache__', '.git', 'node_modules', 'vendor')]
            for fn in fns:
                if fn.endswith('.pyc'):
                    continue
                latest = max(latest, os.path.getmtime(os.path.join(dp, fn)))
print('代码最后修改:', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(latest)))

if not started_ts:
    print('⚠️ 服务为旧版本（未上报启动时间），请 ./restart.sh 重启')
elif latest > started_ts + 1:
    print('⚠️ 代码比服务新，建议 ./restart.sh 重启')
else:
    print('✅ 服务为最新代码')
"
