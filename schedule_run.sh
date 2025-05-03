#!/bin/bash

PYTHON_EXEC=/data/miniconda3/envs/quant_stocks/bin/python
PROJECT_PATH=/data/quant_stocks
LOG_PATH="$PROJECT_PATH/logs/running_info.log"
MAX_SIZE=1048576 # 1MB

# 判断是否为周末
DAY_OF_WEEK=$(date +%u) # 获取当前是星期几（1-7，1=周一，7=周日）
if [ "$DAY_OF_WEEK" -eq 7 || "$DAY_OF_WEEK" -eq 1 ]; then
    echo "今天是周日/周一，不执行脚本。" >> "$LOG_PATH"
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


cd "$PROJECT_PATH"
# run the main.py script
PYTHONPATH="$PROJECT_PATH" "$PYTHON_EXEC" "./main.py" >> "$LOG_PATH" 2>&1
# run the get_best_rsi_adx.py script
PYTHONPATH="$PROJECT_PATH" "$PYTHON_EXEC" "./src/opt_params/get_best_rsi_adx.py" >> "$LOG_PATH" 2>&1

# update git repository
check_success() {
    if [ $? -ne 0 ]; then
        echo "$1 failed." >> "$LOG_PATH"
        exit 1
    fi
}

echo "Script ended at: $(date) and will update the git if no error occurs." >> "$LOG_PATH"

git add .
check_success "git add ."

git commit -m "scheduled run $(date +%Y-%m-%d)"
check_success "git commit"

git push --set-upstream origin master
check_success "git push"
