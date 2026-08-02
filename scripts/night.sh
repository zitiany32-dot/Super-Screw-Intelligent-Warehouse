#!/usr/bin/env bash
# 夜间跑批脚本。放进 crontab 每天凌晨跑一次：
#
#   0 3 * * * /path/to/Super-Screw-Intelligent-Warehouse/scripts/night.sh
#
# 跑完会在 data/briefs/ 生成当天的早报，data/logs/ 留一份日志。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# 有虚拟环境就用虚拟环境
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif [ -x "venv/bin/python" ]; then
    PYTHON="venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

LOG_DIR="${RADAR_DATA_DIR:-$REPO_DIR/data}/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/night-$(date +%Y%m%d).log"

{
    echo "===== $(date '+%F %T') 开始跑批 ====="
    "$PYTHON" -m customsradar.cli run-night "$@"
    echo "----- 拉取回复 -----"
    # 没配 IMAP 时这一步会失败，不影响主流程
    "$PYTHON" -m customsradar.cli poll-replies || echo "（跳过收信：未配置 IMAP 或收信失败）"
    echo "===== $(date '+%F %T') 结束 ====="
} 2>&1 | tee -a "$LOG_FILE"

# 只保留最近 30 天的日志
find "$LOG_DIR" -name 'night-*.log' -mtime +30 -delete 2>/dev/null || true
