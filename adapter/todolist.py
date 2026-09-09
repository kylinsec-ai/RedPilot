"""
TodoList驱动系统（借鉴ARTEX架构）

设计原则：
1. 事件驱动：新发现触发新任务
2. 优先级队列：重要任务优先执行
3. 动态生成：不硬编码任务内容
4. 状态追踪：记录任务执行状态
"""

import logging
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
import json
import os

log = logging.getLogger(__name__)


class TaskPriority(Enum):
    """任务优先级"""
    CRITICAL = 0    # 关键任务：获取flag
    HIGH = 1        # 高优先级：漏洞利用
    MEDIUM = 2      # 中优先级：漏洞扫描
    LOW = 3         # 低优先级：信息收集
    

class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"       # 待执行
    IN_PROGRESS = "in_progress"  # 执行中
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 失败
    SKIPPED = "skipped"       # 跳过


@dataclass
class PenetrationTask:
    """渗透测试任务"""
    id: str
    description: str
    priority: TaskPriority
    status: TaskStatus = TaskStatus.PENDING
    dependencies: List[str] = field(default_factory=list)  # 依赖的任务ID
    triggers: List[str] = field(default_factory=list)      # 触发条件
    tool: Optional[str] = None                             # 使用的工具
    target: Optional[str] = None                           # 目标
    expected_output: Optional[str] = None                  # 期望输出
    actual_output: Optional[str] = None                    # 实际输出
    metadata: Dict = field(default_factory=dict)           # 额外信息


class TodoListManager:
    """TodoList管理器"""
    
    def __init__(self, workdir: str):
        self.workdir = workdir
        self.tasks: Dict[str, PenetrationTask] = {}
        self.completed_tasks: Set[str] = set()
        self.failed_tasks: Set[str] = set()
        self.task_counter = 0
        
    def add_task(self, 
                 description: str,
                 priority: TaskPriority,
                 tool: Optional[str] = None,
                 target: Optional[str] = None,
                 dependencies: List[str] = None,
                 triggers: List[str] = None) -> str:
        """添加新任务"""
        self.task_counter += 1
        task_id = f"task_{self.task_counter}"
        
        task = PenetrationTask(
            id=task_id,
            description=description,
            priority=priority,
            tool=tool,
            target=target,
            dependencies=dependencies or [],
            triggers=triggers or []
        )
        
        self.tasks[task_id] = task
        log.info("Added task %s: %s (priority=%s)", task_id, description, priority.name)
        return task_id
    
    def get_next_task(self) -> Optional[PenetrationTask]:
        """获取下一个应该执行的任务（优先级队列）"""
        # 找出所有可执行的任务（依赖已满足）
        executable = []
        for task in self.tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            
            # 检查依赖是否满足
            deps_satisfied = all(
                dep_id in self.completed_tasks 
                for dep_id in task.dependencies
            )
            
            if deps_satisfied:
                executable.append(task)
        
        if not executable:
            return None
        
        # 按优先级排序
        executable.sort(key=lambda t: t.priority.value)
        return executable[0]
    
    def mark_completed(self, task_id: str, output: str = None):
        """标记任务完成"""
        if task_id in self.tasks:
            self.tasks[task_id].status = TaskStatus.COMPLETED
            self.tasks[task_id].actual_output = output
            self.completed_tasks.add(task_id)
            log.info("Task %s completed", task_id)
            
            # 触发事件驱动的新任务生成
            self._trigger_event(task_id, output)
    
    def mark_failed(self, task_id: str, reason: str = None):
        """标记任务失败"""
        if task_id in self.tasks:
            self.tasks[task_id].status = TaskStatus.FAILED
            self.failed_tasks.add(task_id)
            log.warning("Task %s failed: %s", task_id, reason)
    
    def _trigger_event(self, completed_task_id: str, output: str):
        """事件驱动：根据完成的任务输出触发新任务"""
        completed_task = self.tasks[completed_task_id]
        output_lower = (output or "").lower()
        
        # 事件1: 发现开放端口 → 触发端口扫描
        if completed_task.tool == "nmap" and output:
            if "open" in output_lower:
                ports = self._extract_ports(output)
                for port in ports:
                    if port == 80 or port == 443:
                        self.add_task(
                            f"Web目录扫描 port {port}",
                            TaskPriority.HIGH,
                            tool="gobuster",
                            target=f":{port}",
                            dependencies=[completed_task_id]
                        )
                    elif port == 22:
                        self.add_task(
                            f"SSH弱密码测试 port {port}",
                            TaskPriority.MEDIUM,
                            tool="hydra",
                            dependencies=[completed_task_id]
                        )
        
        # 事件2: 发现敏感路径 → 触发深入测试
        if completed_task.tool == "gobuster" and output:
            if "/admin" in output_lower or "/api" in output_lower:
                self.add_task(
                    "测试敏感路径权限",
                    TaskPriority.HIGH,
                    tool="curl",
                    dependencies=[completed_task_id]
                )
        
        # 事件3: 发现文件上传 → 触发上传测试
        if "upload" in output_lower:
            self.add_task(
                "测试文件上传漏洞",
                TaskPriority.CRITICAL,
                tool="curl",
                dependencies=[completed_task_id]
            )
        
        # 事件4: 获得shell → 触发flag搜索
        if "shell" in output_lower or "rce" in output_lower:
            self.add_task(
                "搜索flag文件",
                TaskPriority.CRITICAL,
                tool="bash",
                target="find / -name flag* 2>/dev/null",
                dependencies=[completed_task_id]
            )
    
    def _extract_ports(self, nmap_output: str) -> List[int]:
        """从nmap输出提取端口号"""
        import re
        ports = []
        for match in re.finditer(r'(\d+)/tcp\s+open', nmap_output):
            ports.append(int(match.group(1)))
        return ports
    
    def generate_initial_tasks(self, target: str, challenge_info: Dict):
        """生成初始任务列表（侦察阶段）"""
        # 阶段1: 端口扫描
        self.add_task(
            f"端口扫描 {target}",
            TaskPriority.HIGH,
            tool="nmap",
            target=target
        )
        
        # 阶段2: 根据题目描述生成特定任务
        description = challenge_info.get("description", "").lower()
        
        if "web" in description or "http" in description:
            task1 = self.add_task(
                f"Web指纹识别 {target}",
                TaskPriority.HIGH,
                tool="whatweb",
                target=target
            )
            
            self.add_task(
                f"Web目录枚举 {target}",
                TaskPriority.HIGH,
                tool="gobuster",
                target=target,
                dependencies=[task1]
            )
        
        if "sql" in description or "database" in description:
            self.add_task(
                "SQL注入测试",
                TaskPriority.HIGH,
                tool="sqlmap"
            )
        
        if "upload" in description or "文件" in description:
            self.add_task(
                "文件上传点测试",
                TaskPriority.HIGH,
                tool="burp"
            )
    
    def save_to_file(self):
        """保存TodoList到文件"""
        todo_file = os.path.join(self.workdir, "_todolist.json")
        try:
            data = {
                "tasks": {
                    tid: {
                        "id": t.id,
                        "description": t.description,
                        "priority": t.priority.name,
                        "status": t.status.value,
                        "tool": t.tool,
                        "target": t.target,
                        "dependencies": t.dependencies
                    }
                    for tid, t in self.tasks.items()
                },
                "completed": list(self.completed_tasks),
                "failed": list(self.failed_tasks)
            }
            
            with open(todo_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("Failed to save todolist: %s", e)
    
    def get_summary(self) -> str:
        """获取TodoList摘要"""
        total = len(self.tasks)
        completed = len(self.completed_tasks)
        failed = len(self.failed_tasks)
        pending = sum(1 for t in self.tasks.values() if t.status == TaskStatus.PENDING)
        
        summary = f"""
## TodoList 状态

- 总任务数: {total}
- 已完成: {completed}
- 失败: {failed}
- 待执行: {pending}

### 待执行任务
"""
        for task in sorted(self.tasks.values(), key=lambda t: t.priority.value):
            if task.status == TaskStatus.PENDING:
                summary += f"- [{task.priority.name}] {task.description}\n"
        
        return summary
