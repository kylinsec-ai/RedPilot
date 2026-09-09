"""
Docker容器清理机制
防止容器槽位耗尽
"""

import subprocess
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


def cleanup_stale_containers(max_age_hours=3, dry_run=False):
    """
    清理超过指定时间未更新的容器
    
    Args:
        max_age_hours: 容器最大存活时间（小时）
        dry_run: 只列出不删除
    """
    try:
        # 获取所有容器
        result = subprocess.run(
            ['docker', 'ps', '-a', '--format', '{{.ID}}|{{.Names}}|{{.CreatedAt}}|{{.Status}}'],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            return []
        
        cleaned = []
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            
            parts = line.split('|')
            if len(parts) < 4:
                continue
            
            container_id, name, created, status = parts
            
            # 只清理已停止的容器
            if 'Exited' not in status:
                continue
            
            # 跳过worker容器
            if 'tsecbench-worker' in name:
                continue
            
            # 解析创建时间
            try:
                # 简化：超过3小时的Exited容器
                if 'hours ago' in created or 'days ago' in created:
                    if dry_run:
                        log.info("Will cleanup: %s (%s)", name, status)
                    else:
                        subprocess.run(['docker', 'rm', '-f', container_id], 
                                     capture_output=True, timeout=10)
                        log.info("Cleaned up container: %s", name)
                        cleaned.append(name)
            except Exception as e:
                log.warning("Failed to cleanup %s: %s", name, e)
        
        return cleaned
    except Exception as e:
        log.error("Container cleanup error: %s", e)
        return []


def enforce_container_limit(max_containers=50):
    """
    强制限制总容器数，按创建时间清理最旧的
    """
    try:
        result = subprocess.run(
            ['docker', 'ps', '-a', '--format', '{{.ID}}|{{.Names}}|{{.Status}}'],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            return 0
        
        containers = [line for line in result.stdout.strip().split('\n') if line]
        
        if len(containers) <= max_containers:
            return 0
        
        # 清理超出部分
        to_cleanup = len(containers) - max_containers
        cleaned = 0
        
        for line in containers[:to_cleanup]:
            parts = line.split('|')
            if len(parts) < 3:
                continue
            
            container_id, name, status = parts
            
            # 跳过运行中的容器和worker
            if 'Up' in status or 'tsecbench-worker' in name:
                continue
            
            try:
                subprocess.run(['docker', 'rm', '-f', container_id], 
                             capture_output=True, timeout=10)
                log.info("Removed container (limit enforcement): %s", name)
                cleaned += 1
            except Exception:
                pass
        
        return cleaned
    except Exception as e:
        log.error("Container limit enforcement error: %s", e)
        return 0
