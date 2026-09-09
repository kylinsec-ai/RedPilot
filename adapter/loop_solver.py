"""
循环解题系统 - 基于Pi Agent的持续推理循环

设计思路：
1. 启动解题循环，持续分析和尝试
2. 定期压缩上下文，保留关键信息
3. 自我反思，避免重复失败
4. 多阶段推进（侦察→利用→获取flag）
"""

import logging
import time
from typing import List, Dict, Optional
from dataclasses import dataclass
import json

log = logging.getLogger(__name__)


@dataclass
class ContextSummary:
    """上下文摘要"""
    attempts: int
    tools_used: List[str]
    key_findings: List[str]
    failed_attempts: List[str]
    current_stage: str
    next_actions: List[str]
    compressed_context: str


class LoopSolver:
    """循环解题器"""
    
    def __init__(self, max_iterations=10, context_window=5):
        self.max_iterations = max_iterations
        self.context_window = context_window  # 多少轮后压缩上下文
        self.iteration_count = 0
        self.full_history = []
        self.compressed_history = []
        
    def solve_with_loop(self, task, solver, workdir: str) -> Dict:
        """
        循环解题主流程
        
        Args:
            task: 题目信息
            solver: Pi Agent solver实例
            workdir: 工作目录
            
        Returns:
            解题结果
        """
        log.info("启动循环解题器：max_iterations=%d", self.max_iterations)
        
        results = []
        flags_found = set()
        
        for iteration in range(self.max_iterations):
            self.iteration_count = iteration + 1
            
            log.info("=== 循环迭代 %d/%d ===", iteration + 1, self.max_iterations)
            
            # 1. 构建当前上下文
            context = self._build_context(iteration)
            
            # 2. 执行一轮推理
            result = self._execute_iteration(task, solver, context, workdir)
            
            # 3. 记录历史
            self.full_history.append({
                "iteration": iteration + 1,
                "result": result,
                "timestamp": time.time()
            })
            
            # 4. 提取flag
            new_flags = self._extract_flags(result)
            if new_flags:
                log.info("发现 %d 个新flag", len(new_flags))
                flags_found.update(new_flags)
            
            # 5. 检查是否完成
            if self._should_stop(result, flags_found, task):
                log.info("循环提前结束：已完成目标")
                break
            
            # 6. 定期压缩上下文
            if (iteration + 1) % self.context_window == 0:
                log.info("执行上下文压缩（第%d轮）", iteration + 1)
                self._compress_context()
            
            # 7. 自我反思
            reflection = self._reflect_on_progress(iteration)
            if reflection.get("stuck"):
                log.warning("检测到卡住：%s", reflection.get("reason"))
                # 尝试改变策略
                self._adjust_strategy()
        
        return {
            "success": len(flags_found) > 0,
            "flags_found": list(flags_found),
            "iterations": self.iteration_count,
            "final_context": self._get_final_summary()
        }
    
    def _build_context(self, iteration: int) -> str:
        """构建当前迭代的上下文"""
        
        if iteration == 0:
            # 第一次迭代：完整提示
            return """
你是一个网络安全渗透测试专家。现在开始解题。

**重要原则**：
1. 系统性地探索目标
2. 记录所有发现
3. 避免重复失败的尝试
4. 找到flag后立即报告

**解题流程**：
侦察 → 漏洞发现 → 漏洞利用 → 获取flag

开始你的第一步行动。
"""
        
        # 后续迭代：包含历史摘要
        summary = self._get_progress_summary()
        
        context = f"""
**当前进度**（第{iteration + 1}轮）：
{summary}

**你的任务**：
基于以上信息，继续推进。不要重复已失败的尝试。
"""
        return context
    
    def _execute_iteration(self, task, solver, context, workdir) -> Dict:
        """执行一轮推理"""
        try:
            # 这里调用Pi Agent执行一轮
            # 实际实现需要对接solver
            log.info("执行推理轮...")
            
            # 模拟执行
            result = {
                "status": "completed",
                "output": "执行结果...",
                "tools_used": ["nmap", "curl"],
                "flags": []
            }
            
            return result
        except Exception as e:
            log.error("推理执行失败: %s", e)
            return {"status": "error", "error": str(e)}
    
    def _extract_flags(self, result: Dict) -> List[str]:
        """从结果中提取flag"""
        flags = result.get("flags", [])
        return [f for f in flags if f.startswith("flag{")]
    
    def _should_stop(self, result: Dict, flags_found: set, task) -> bool:
        """判断是否应该停止循环"""
        
        # 1. 如果找到了所有flag
        expected_flags = getattr(task, "flag_count", 1)
        if len(flags_found) >= expected_flags:
            return True
        
        # 2. 如果明确表示完成
        if result.get("status") == "completed" and flags_found:
            return True
        
        # 3. 如果连续3轮无进展
        if self._no_progress_for_rounds(3):
            log.warning("连续3轮无进展，停止循环")
            return True
        
        return False
    
    def _compress_context(self):
        """压缩上下文，保留关键信息"""
        
        log.info("开始上下文压缩...")
        
        # 提取最近的历史
        recent = self.full_history[-self.context_window:]
        
        # 汇总关键信息
        summary = {
            "total_iterations": len(self.full_history),
            "tools_used": set(),
            "key_findings": [],
            "failed_attempts": []
        }
        
        for item in recent:
            result = item.get("result", {})
            tools = result.get("tools_used", [])
            summary["tools_used"].update(tools)
            
            # 提取发现
            if "finding" in str(result).lower():
                summary["key_findings"].append(f"第{item['iteration']}轮发现")
        
        # 生成压缩后的文本
        compressed = f"""
**历史摘要**（已完成{len(self.full_history)}轮）：
- 使用的工具: {', '.join(summary['tools_used'])}
- 关键发现: {len(summary['key_findings'])}个
- 失败尝试: {len(summary['failed_attempts'])}次
"""
        
        self.compressed_history.append({
            "at_iteration": len(self.full_history),
            "summary": summary,
            "text": compressed
        })
        
        log.info("上下文压缩完成：%d轮 → %d字节", 
                 len(recent), len(compressed))
    
    def _reflect_on_progress(self, iteration: int) -> Dict:
        """自我反思当前进度"""
        
        if iteration < 2:
            return {"stuck": False}
        
        # 检查最近3轮
        recent = self.full_history[-3:]
        
        # 检测重复工具使用
        tools_history = [r.get("result", {}).get("tools_used", []) for r in recent]
        if len(tools_history) >= 3 and tools_history[-1] == tools_history[-2] == tools_history[-3]:
            return {
                "stuck": True,
                "reason": "重复使用相同工具",
                "suggestion": "尝试新的攻击向量"
            }
        
        # 检测无输出
        outputs = [r.get("result", {}).get("output", "") for r in recent]
        if all(not o for o in outputs):
            return {
                "stuck": True,
                "reason": "连续无有效输出",
                "suggestion": "重新侦察或改变方法"
            }
        
        return {"stuck": False}
    
    def _adjust_strategy(self):
        """调整策略（当检测到卡住时）"""
        log.info("调整解题策略...")
        # 这里可以添加策略调整逻辑
        # 例如：切换到不同的工具、改变攻击向量等
    
    def _no_progress_for_rounds(self, rounds: int) -> bool:
        """检查是否连续N轮无进展"""
        if len(self.full_history) < rounds:
            return False
        
        recent = self.full_history[-rounds:]
        # 简单检查：是否都没有找到flag
        return all(not r.get("result", {}).get("flags") for r in recent)
    
    def _get_progress_summary(self) -> str:
        """获取当前进度摘要"""
        
        if not self.full_history:
            return "尚未开始"
        
        total = len(self.full_history)
        all_tools = set()
        all_flags = []
        
        for item in self.full_history:
            result = item.get("result", {})
            tools = result.get("tools_used", [])
            all_tools.update(tools)
            flags = result.get("flags", [])
            all_flags.extend(flags)
        
        summary = f"""
- 已完成轮数: {total}
- 使用的工具: {', '.join(all_tools) if all_tools else '无'}
- 发现的flag: {len(all_flags)}个
"""
        
        # 添加压缩历史
        if self.compressed_history:
            latest = self.compressed_history[-1]
            summary += f"\n{latest['text']}"
        
        return summary
    
    def _get_final_summary(self) -> str:
        """获取最终摘要"""
        return self._get_progress_summary()


def create_loop_solver(max_iterations=10) -> LoopSolver:
    """创建循环解题器实例"""
    return LoopSolver(max_iterations=max_iterations)
