# 🚀 TsecBench系统使用指南

**更新时间**: 2026-09-01 20:20

---

## 📋 目录

1. [快速开始](#快速开始)
2. [Web界面使用](#web界面使用)
3. [监控系统使用](#监控系统使用)
4. [常见操作](#常见操作)
5. [故障排查](#故障排查)

---

## 🎯 快速开始

### 1. 启动系统

```bash
cd /home/xiaohei/桌面/TsecBench-main

# 启动所有服务
docker-compose up -d

# 等待1-2分钟系统初始化
sleep 60

# 检查状态
docker ps | grep tsecbench
```

**预期看到**:
- tsecbench-worker-1: Up X minutes (healthy)
- tsecbench-worker-2: Up X minutes (healthy)
- tsecbench-worker-3: Up X minutes (healthy)

---

### 2. 访问Web界面

**地址**: http://192.168.31.143:8003/

**功能**:
- 📊 查看解题进度
- 🎯 查看当前题目
- 📈 查看统计数据
- ⚙️ 配置AI参数

---

### 3. 开始解题

#### 方法1: Web界面（推荐）

1. 打开 http://192.168.31.143:8003/
2. 点击"开始任务"或"继续任务"
3. Worker会自动开始解题
4. 实时查看进度

#### 方法2: 命令行

```bash
# Worker会自动开始
# 查看日志
docker logs -f tsecbench-worker-1
```

---

## 🌐 Web界面使用

### 主界面功能

#### 1. 首页 (Home)
- **总览信息**
  - 当前运行Worker数
  - 已完成题目数
  - 总得分
  - 已提交flags数

#### 2. 题目列表 (Challenges)
- 查看所有题目
- 题目状态：
  - 🟢 已完成
  - 🟡 进行中
  - ⚪ 未开始
  - 🔴 失败

#### 3. 解题配置 (Settings)

**AI参数配置**:
```
自动获取提示: 关闭（会扣分）
AI最大轮数: 6-50（推荐10-20）
自动关闭容器: 开启（节省资源）
```

**时间配置**:
```
easy题: 3600秒（1小时）
medium题: 3600秒（1小时）
hard题: 3600秒（1小时）
```

#### 4. 实时日志 (Logs)
- 查看Worker实时日志
- 查看解题过程
- 查看错误信息

---

## 📊 监控系统使用

### 1. 实时监控面板

```bash
cd /home/xiaohei/桌面/TsecBench-main
bash monitor_realtime.sh
```

**显示内容**:
- Worker状态
- 当前题目
- 最近活动
- Flag提交情况

**操作**:
- 按 Ctrl+C 退出（Worker继续运行）

---

### 2. 监控总结

```bash
bash summary_monitor.sh
```

**显示内容**:
- Worker总体统计
- Session完成数
- 成功提交数
- 平台数据汇总
- CSV事件统计

---

### 3. 健康监控

**后台自动运行**，检查：
- Worker健康状态
- 容器运行状态
- VPN连接状态

**查看日志**:
```bash
tail -f health_monitor.log
```

---

### 4. 源代码检测

```bash
bash check_all_code.sh
```

**检查内容**:
- Python语法
- 关键模块
- 逻辑验证
- 容器同步

---

## 🔧 常见操作

### 查看Worker日志

```bash
# Worker-1
docker logs -f tsecbench-worker-1

# Worker-2
docker logs -f tsecbench-worker-2

# Worker-3
docker logs -f tsecbench-worker-3

# 查看最近100行
docker logs --tail 100 tsecbench-worker-1

# 查看最近10分钟
docker logs --since 10m tsecbench-worker-1
```

---

### 重启Worker

```bash
# 重启单个Worker
docker restart tsecbench-worker-1

# 重启所有Worker
docker restart tsecbench-worker-1 tsecbench-worker-2 tsecbench-worker-3

# 或使用docker-compose
docker-compose restart
```

---

### 停止系统

```bash
cd /home/xiaohei/桌面/TsecBench-main

# 停止所有服务
docker-compose down

# 或手动停止
docker stop tsecbench-worker-1 tsecbench-worker-2 tsecbench-worker-3
```

---

### 查看系统状态

```bash
# Worker状态
docker ps | grep tsecbench-worker

# API状态
curl http://localhost:8003/api/v1/agent/status | jq

# 完整状态
bash summary_monitor.sh
```

---

### 清理和重置

```bash
# 停止并删除容器
docker-compose down

# 清理工作目录（保留数据）
rm -rf work/*.lock

# 清理Python缓存
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# 重新启动
docker-compose up -d
```

---

## 🔍 故障排查

### 问题1: Worker不工作（0 turns）

**症状**:
```
pi session done: 0 turns, 20s, 0 flags
```

**原因**:
- VPN连接失败
- API Key无效
- 网络问题

**解决**:
```bash
# 1. 检查VPN
docker exec tsecbench-worker-1 ip addr show tun0

# 2. 检查API Key
docker exec tsecbench-worker-1 env | grep SOLVER_API_KEY

# 3. 测试网络
docker exec tsecbench-worker-1 curl https://api.deepseek.com

# 4. 重启Worker
docker restart tsecbench-worker-1
```

---

### 问题2: 找到flag但不提交

**症状**:
```
pi session done: 18 turns, 1 flags
verify REJECT
flag REJECTED
```

**原因**: 验证器配置过严

**解决**: 已经修复，验证阈值降低到0.3

---

### 问题3: 频繁切换题目

**症状**: 题目每隔几分钟就切换

**原因**:
- Stop-loss触发（连续无进展）
- 题目太难
- VPN问题

**解决**:
```bash
# 查看详细日志
docker logs tsecbench-worker-1 | grep "stop-loss"

# 检查是否有flag发现
docker logs tsecbench-worker-1 | grep "flags"
```

---

### 问题4: Web界面无法访问

**症状**: http://192.168.31.143:8003/ 打不开

**解决**:
```bash
# 检查Web进程
ps aux | grep fastapi_console

# 如果没运行，启动
cd fastapi-console
nohup python3 -m uvicorn fastapi_console.main:app --host 0.0.0.0 --port 8003 > /tmp/fastapi.log 2>&1 &

# 检查日志
tail -f /tmp/fastapi.log
```

---

### 问题5: Worker一直卡住

**症状**: Worker长时间无日志

**解决**:
```bash
# 1. 检查健康状态
docker ps | grep tsecbench-worker

# 2. 查看最近日志
docker logs --since 5m tsecbench-worker-1 | tail -50

# 3. 重启Worker
docker restart tsecbench-worker-1

# 4. 如果还卡住，完全重启
docker-compose down
docker-compose up -d
```

---

## 📈 性能优化建议

### 1. 调整并发数

当前配置: 3个Worker

**如果性能足够**，可增加到5个：
```bash
# 修改 docker-compose.yaml
# 添加 worker-4, worker-5
```

### 2. 调整时间配置

当前: 3600秒（1小时）

**如果题目简单**，可减少到30分钟：
```python
# adapter/config.py
timebox_easy: 1800
timebox_medium: 1800
timebox_hard: 3600
```

### 3. 调整Stop-loss

当前: 3次干Session放弃

**如果想多尝试**，增加到5次：
```python
# adapter/stoploss.py
dry_cutoff: 5
```

---

## 📝 日常维护

### 每天检查

```bash
# 1. 查看总结
bash summary_monitor.sh

# 2. 检查健康日志
tail -50 health_monitor.log

# 3. 查看成功率
docker logs tsecbench-worker-1 | grep "FLAG CORRECT" | wc -l
```

### 每周维护

```bash
# 1. 清理旧日志
docker-compose logs --tail=1000 > backup.log
docker-compose down
docker-compose up -d

# 2. 检查磁盘空间
df -h

# 3. 更新代码（如果有新版本）
git pull
docker-compose down
docker-compose up -d --build
```

---

## 🎯 最佳实践

### 1. 监控频率
- **实时监控**: 解题时使用
- **定期检查**: 每小时查看一次总结
- **健康监控**: 自动运行，出问题会告警

### 2. 日志管理
- 保留最近24小时完整日志
- 定期备份重要日志
- 使用 `docker logs --tail` 限制输出

### 3. 资源管理
- 开启"自动关闭容器"
- 定期清理未使用的容器
- 监控CPU和内存使用

### 4. 安全建议
- 不要在日志中输出完整API Key
- 定期更换API Key
- VPN配置文件权限设置为600

---

## 🆘 获取帮助

### 查看配置
```bash
# 查看所有配置
docker exec tsecbench-worker-1 env | grep -E "ADAPTER|SOLVER"

# 查看时间配置
grep "timebox" adapter/config.py

# 查看验证配置
grep "def verify" adapter/verify.py -A 20
```

### 查看文档
- 主文档: ALL_DONE.txt
- 配置文档: TIME_CONFIG_FINAL.md
- Worker配置: FINAL_WORKER_CONFIG.md
- 问题诊断: FINAL_DIAGNOSIS.md

---

## ✅ 快速命令参考

```bash
# === 启动/停止 ===
docker-compose up -d          # 启动
docker-compose down           # 停止
docker-compose restart        # 重启

# === 监控 ===
bash monitor_realtime.sh      # 实时监控
bash summary_monitor.sh       # 查看总结
docker logs -f tsecbench-worker-1  # 查看日志

# === 状态检查 ===
docker ps | grep tsecbench    # Worker状态
curl localhost:8003/api/v1/agent/status | jq  # API状态

# === 故障排查 ===
docker logs --since 10m tsecbench-worker-1 | grep error  # 错误日志
bash check_all_code.sh        # 代码检查
docker restart tsecbench-worker-1  # 重启Worker

# === Web界面 ===
http://192.168.31.143:8003/   # 访问地址
```

---

**系统已配置完善，按照本指南使用即可！** 🚀
