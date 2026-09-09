#!/bin/bash
# 系统健康持续监控

LOG_FILE="health_monitor.log"

echo "=== 系统健康监控启动 ==="
echo "日志文件: $LOG_FILE"
echo ""

while true; do
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    # 检查Worker
    worker_count=$(docker ps --filter "name=tsecbench-worker" --filter "status=running" | grep -c worker || echo 0)
    healthy_count=$(docker ps --filter "name=tsecbench-worker" --filter "health=healthy" | grep -c healthy || echo 0)
    
    # 检查API
    api_status=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8003/api/v1/agent/status 2>/dev/null || echo "000")
    
    # 检查flag提交
    flags_1h=$(docker logs --since 60m tsecbench-worker-1 2>&1 | grep -c "FLAG CORRECT" || echo 0)
    flags_2h=$(docker logs --since 60m tsecbench-worker-2 2>&1 | grep -c "FLAG CORRECT" || echo 0)
    flags_3h=$(docker logs --since 60m tsecbench-worker-3 2>&1 | grep -c "FLAG CORRECT" || echo 0)
    total_flags=$((flags_1h + flags_2h + flags_3h))
    
    # 记录日志
    echo "[$timestamp] Workers:$worker_count/$healthy_count API:$api_status Flags/h:$total_flags" | tee -a $LOG_FILE
    
    # 告警检查
    if [ $healthy_count -lt 3 ]; then
        echo "  ⚠️  警告: 只有 $healthy_count 个Worker健康" | tee -a $LOG_FILE
    fi
    
    if [ "$api_status" != "200" ]; then
        echo "  ⚠️  警告: API状态异常 ($api_status)" | tee -a $LOG_FILE
    fi
    
    sleep 60  # 每分钟检查一次
done
