#!/usr/bin/env python3
"""
TsecBench Worker 监督系统
监控3个Worker的运行状态，确保：
1. 发现flag后能够成功提交
2. 不产生幻觉flag
3. Worker异常时自动重启
"""

import subprocess
import time
import json
import re
from datetime import datetime
from pathlib import Path

WORK_DIR = Path("/home/xiaohei/桌面/TsecBench-main")
WORKERS = ["tsecbench-worker-1", "tsecbench-worker-2", "tsecbench-worker-3"]
CHECK_INTERVAL = 60  # 每分钟检查一次

class WorkerSupervisor:
    def __init__(self):
        self.last_flags = {w: 0 for w in WORKERS}
        self.last_sessions = {w: 0 for w in WORKERS}
        self.alert_log = []
        
    def get_container_logs(self, worker, minutes=5):
        """获取容器日志"""
        try:
            result = subprocess.run(
                ["docker", "logs", "--since", f"{minutes}m", worker],
                capture_output=True, text=True, timeout=30
            )
            return result.stdout + result.stderr
        except Exception as e:
            return f"Error: {e}"
    
    def container_running(self, worker):
        """检查容器是否处于运行状态（docker inspect State.Running）"""
        try:
            result = subprocess.run(
                ["docker", "inspect", worker, "--format", "{{.State.Running}}"],
                capture_output=True, text=True, timeout=30
            )
            return result.stdout.strip() == "true"
        except Exception:
            return True  # 无法判定时视为存活，避免误杀

    def check_worker_status(self, worker):
        """检查Worker状态"""
        logs = self.get_container_logs(worker, 5)
        
        # 统计session
        sessions = len(re.findall(r"pi session done", logs))
        
        # 统计flag发现
        flags_found = 0
        for match in re.finditer(r"pi session done.*?(\d+) flags", logs):
            flags_found += int(match.group(1))
        
        # 统计flag验证
        verify_pass = logs.count("verify PASS")
        verify_reject = logs.count("verify REJECT")
        
        # 统计提交
        submit_correct = logs.count("FLAG CORRECT")
        submit_incorrect = logs.count("flag INCORRECT")
        
        # 验证流程是否真正跑过（verify.py 宽松策略输出 verify PASS/REJECT）
        verification_ran = verify_pass + verify_reject
        
        return {
            "sessions": sessions,
            "flags_found": flags_found,
            "verify_pass": verify_pass,
            "verify_reject": verify_reject,
            "verification_ran": verification_ran,
            "submit_correct": submit_correct,
            "submit_incorrect": submit_incorrect
        }
    
    def analyze_and_alert(self, worker, status):
        """分析状态并发出警告"""
        alerts = []
        
        # 警告1: 发现flag但全被验证拒绝（verify REJECT 连续出现）
        if status["flags_found"] > 0 and status["submit_correct"] == 0 and status["verify_reject"] > 3:
            alerts.append(f"⚠️  {worker}: 发现{status['flags_found']}个flag但全被验证拒绝！")
        
        # 警告2: 没有session活动
        if status["sessions"] == 0 and self.last_sessions.get(worker, 0) == 0:
            alerts.append(f"⚠️  {worker}: 过去10分钟没有解题活动")
        
        # 警告3: 验证流程未生效（找到flag但没有任何 verify PASS/REJECT 输出）
        if status["flags_found"] > 0 and status["verification_ran"] == 0:
            alerts.append(f"⚠️  {worker}: 验证流程未生效（找到 {status['flags_found']} 个flag但无 verify PASS/REJECT 日志）")
        
        # 成功信息
        if status["submit_correct"] > 0:
            alerts.append(f"✅ {worker}: 成功提交{status['submit_correct']}个flag")
        
        return alerts
    
    def restart_worker(self, worker):
        """重启Worker"""
        print(f"🔄 重启 {worker}...")
        try:
            if worker == "tsecbench-worker-1":
                # worker-1 是共享 netns 提供者（network_mode: service:worker-1）。
                # 重启它会打掉 worker-2/3 的共享网络，必须随后重启 worker-2/3 让它们重挂；
                # 即使 docker restart 未能重挂，worker-2/3 内的 netns 自愈看门狗也会
                # 检测到网络丢失并退出进程，触发 restart: on-failure 重建重挂。
                subprocess.run(["docker", "restart", worker], timeout=60)
                subprocess.run(["docker", "restart", "tsecbench-worker-2"], timeout=60)
                subprocess.run(["docker", "restart", "tsecbench-worker-3"], timeout=60)
            else:
                subprocess.run(["docker", "restart", worker], timeout=60)
            return True
        except Exception as e:
            print(f"❌ 重启失败: {e}")
            return False
    
    def monitor_loop(self):
        """监控循环"""
        print("=== TsecBench Worker 监督系统启动 ===")
        print(f"监控间隔: {CHECK_INTERVAL}秒")
        print(f"监控Worker: {', '.join(WORKERS)}")
        print("")
        
        while True:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 检查Worker状态...")
            
            for worker in WORKERS:
                try:
                    if not self.container_running(worker):
                        print(f"⚠️  {worker}: 容器未运行，正在重启...")
                        if self.restart_worker(worker):
                            print(f"✓ {worker}: 已通过 docker restart 拉起")
                        else:
                            print(f"❌ {worker}: 重启失败，尝试 docker start")
                            subprocess.run(["docker", "start", worker], timeout=60)
                    status = self.check_worker_status(worker)
                    alerts = self.analyze_and_alert(worker, status)
                    
                    if alerts:
                        for alert in alerts:
                            print(alert)
                            self.alert_log.append((datetime.now(), alert))
                    else:
                        print(f"✓ {worker}: 正常 (sessions={status['sessions']}, flags={status['flags_found']}, submitted={status['submit_correct']})")
                    
                    # 更新状态
                    self.last_sessions[worker] = status["sessions"]
                    self.last_flags[worker] = status["flags_found"]
                    
                except Exception as e:
                    print(f"❌ {worker}: 检查失败 - {e}")
            
            # 保存监督日志
            self.save_report()
            
            time.sleep(CHECK_INTERVAL)
    
    def save_report(self):
        """保存监督报告"""
        report_file = WORK_DIR / "supervisor_report.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(f"TsecBench Worker 监督报告\n")
            f.write(f"生成时间: {datetime.now()}\n")
            f.write(f"\n最近警告:\n")
            for ts, alert in self.alert_log[-20:]:
                f.write(f"[{ts.strftime('%H:%M:%S')}] {alert}\n")

if __name__ == "__main__":
    supervisor = WorkerSupervisor()
    try:
        supervisor.monitor_loop()
    except KeyboardInterrupt:
        print("\n监督系统已停止")
