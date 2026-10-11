#!/bin/bash
# Scheduled data guard, run by launchd from a dedicated clone (see README 5.4.2):
#   data_guard.sh verify   weekly: quant data verify on all frozen versions
#   data_guard.sh backup   monthly: back up the growing caches (borrow fees, forward observation)
# Pulls master first, logs to research_cache/data_guard/, and raises a macOS notification on failure.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
CLONE="$(cd "$(dirname "$0")/.." && pwd)"
PY=/Users/bytedance/code/quant_stocks/.venv/bin/python
LOGDIR=/Users/bytedance/code/quant_stocks/research_cache/data_guard
mkdir -p "$LOGDIR"
log="$LOGDIR/$1_$(date +%Y%m%d).log"
cd "$CLONE" && git pull --ff-only -q origin master >>"$log" 2>&1
case "$1" in
  verify) PYTHONPATH=. "$PY" -m quant data verify --version all >>"$log" 2>&1 ;;
  backup) bash scripts/backup_data.sh borrow-fees >>"$log" 2>&1 && bash scripts/backup_data.sh forward-observation >>"$log" 2>&1 ;;
  *) echo "usage: $0 verify|backup" >&2; exit 2 ;;
esac
code=$?
echo "exit $code $(date -u +%FT%TZ)" >>"$log"
if [ "$code" -ne 0 ]; then
  osascript -e "display notification \"$1 failed (exit $code); see $log\" with title \"quant_stocks data guard\""
fi
exit "$code"
