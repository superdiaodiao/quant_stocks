#!/bin/bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"
log_dir="$project_dir/logs"
log_path="$log_dir/running_info.log"
python_exec="$project_dir/.venv/bin/python"

mkdir -p "$log_dir"
if [[ ! -x "$python_exec" ]]; then
    echo "Missing virtualenv Python: $python_exec" >&2
    exit 1
fi

cd "$project_dir"
echo "Daily pipeline started: $(date -Iseconds)" >> "$log_path"
PYTHONPATH="$project_dir" "$python_exec" -m src.research.daily_pipeline >> "$log_path" 2>&1
if PYTHONPATH="$project_dir" "$python_exec" "$project_dir/scripts/shadow_forward_observation.py" \
    --observation-date latest >> "$log_path" 2>&1; then
    echo "Shadow forward observation completed: $(date -Iseconds)" >> "$log_path"
else
    echo "Shadow forward observation unavailable; keeping release gate unchanged: $(date -Iseconds)" >> "$log_path"
fi
PYTHONPATH="$project_dir" "$python_exec" "$project_dir/scripts/shadow_forward_status.py" >> "$log_path" 2>&1
echo "Daily pipeline completed: $(date -Iseconds)" >> "$log_path"
