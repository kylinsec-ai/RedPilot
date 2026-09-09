#!/bin/bash
# 自动检测并重启卡住的Worker
# 策略：以容器心跳（/tmp/driver_heartbeat 新鲜度）为主判定，
#       healthy(心跳新鲜)的 worker 即使日志少也绝不动 ——
#       pentest/逆向类题 agent 做侦查时可能 10 分钟无一行日志，
#       只按日志行数判定会误杀正常解题的 worker（历史误杀根因）。
#       只有 容器未运行 或 心跳陈旧 + 日志少 才重启。

# 切换到脚本所在目录，保证相对路径日志/数据库可写
cd "$(dirname "$0")" || exit 1
LOG_FILE="auto_restart.log"

MAX_IDLE_SECONDS="${WATCHDOG_MAX_IDLE_SECONDS:-300}"

is_heartbeat_fresh() {
    # 返回 0 = 心跳新鲜（driver 活着）
    local worker="$1"
    local ts
    ts=$(docker exec "$worker" sh -c 'stat -c %Y /tmp/driver_heartbeat 2>/dev/null' 2>/dev/null)
    [ -z "$ts" ] && return 1
    local now; now=$(date +%s)
    [ $((now - ts)) -lt "$MAX_IDLE_SECONDS" ]
}

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检查Worker状态..." >> "$LOG_FILE"

    # 注意：worker-1 是网络命名空间提供者（network_mode: service:worker-1），
    # 绝不能自动重启 —— 每次重启都会把 worker-2/3 的共享 netns 打掉，
    # 造成全队 DNS/网络崩溃（历史事故根因）。只巡检 worker-2/3。
    for worker in tsecbench-worker-2 tsecbench-worker-3; do
        running=$(docker inspect "$worker" --format '{{.State.Running}}' 2>/dev/null)
        if [ "$running" != "true" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️  $worker 容器未运行，docker start..." >> "$LOG_FILE"
            docker start "$worker"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $worker 已 start" >> "$LOG_FILE"
            continue
        fi

        # 心跳新鲜 → 健康，绝不动（哪怕日志少）
        if is_heartbeat_fresh "$worker"; then
            continue
        fi

        # 心跳陈旧：再看日志量，双重确认才重启（避免误杀）
        recent_logs=$(docker logs --since 10m "$worker" 2>&1 | wc -l)
        if [ "$recent_logs" -lt 5 ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️  $worker 心跳陈旧且日志少($recent_logs行)，判定卡住，重启..." >> "$LOG_FILE"
            docker restart "$worker"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ $worker 已重启" >> "$LOG_FILE"
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ℹ️  $worker 心跳陈旧但日志活跃($recent_logs行)，暂不重启" >> "$LOG_FILE"
        fi
    done

    # 每10分钟检查一次
    sleep 600
done
