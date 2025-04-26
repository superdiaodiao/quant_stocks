#!/bin/bash

PYTHON_EXEC=/data/miniconda3/envs/quant_stocks/bin/python
PROJECT_PATH=/data/quant_stocks
SCRIPT_PATH="$PROJECT_PATH/main.py"
LOG_PATH="$PROJECT_PATH/logs/running_info.log"
MAX_SIZE=1048576 # 1MB

# 判断是否为周末
DAY_OF_WEEK=$(date +%u) # 获取当前是星期几（1-7，1=周一，7=周日）
if [ "$DAY_OF_WEEK" -gt 5 ]; then
    echo "今天是周末，不执行脚本。" >> "$LOG_PATH"
    exit 0
fi

if [ -f "$LOG_PATH" ]; then
    FILE_SIZE=$(stat -c%s "$LOG_PATH")

    if [ "$FILE_SIZE" -ge "$MAX_SIZE" ]; then
        # shellcheck disable=SC2188
        > "$LOG_PATH"
        echo "日志文件太大，已清空重新写。" >> "$LOG_PATH"
    fi
fi

# shellcheck disable=SC2129
echo "Script started at: $(date)" >> "$LOG_PATH"

pip install akshare --upgrade -i https://pypi.org/simple

PYTHONPATH="$PROJECT_PATH" "$PYTHON_EXEC" "$SCRIPT_PATH" >> "$LOG_PATH" 2>&1

echo "Script ended at: $(date)" >> "$LOG_PATH"
