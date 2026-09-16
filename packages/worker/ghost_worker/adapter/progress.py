"""
题目进度保存与恢复模块（合规设计）

设计原则：
1. 只保存本题的尝试记录，不跨题共享
2. 动态生成，不硬编码解法
3. 保存失败的攻击向量，避免重复尝试
"""

import os
import json
from datetime import datetime
from typing import Dict, List, Optional


class ChallengeProgress:
    """单个题目的进度管理"""
    
    def __init__(self, workdir: str):
        self.workdir = workdir
        self.memory_file = os.path.join(workdir, "MEMORY.md")
        self.progress_file = os.path.join(workdir, "_progress.json")
    
    def save_attempt(self, 
                     tools_used: List[str],
                     findings: List[str],
                     dead_ends: List[str],
                     next_steps: List[str]):
        """
        保存本次尝试的记录
        
        Args:
            tools_used: 使用的工具列表 ["nmap", "gobuster"]
            findings: 发现的信息 ["发现/admin路径", "发现API密钥泄露"]
            dead_ends: 失败的攻击向量 ["SQLi on /login (WAF拦截)"]
            next_steps: 建议的下一步 ["尝试利用泄露的API密钥"]
        """
        # 保存结构化数据
        progress = {
            "last_updated": datetime.now().isoformat(),
            "tools_used": tools_used,
            "findings": findings,
            "dead_ends": dead_ends,
            "next_steps": next_steps,
            "attempt_count": self._get_attempt_count() + 1
        }
        
        try:
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        
        # 同时更新MEMORY.md（人类可读）
        self._update_memory_md(progress)
    
    def load_resume_context(self) -> Optional[Dict]:
        """加载续接上下文"""
        try:
            if os.path.exists(self.progress_file):
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return None
    
    def build_resume_prompt(self) -> str:
        """构建续接提示词"""
        context = self.load_resume_context()
        if not context:
            return ""
        
        prompt = f"""
## 上次尝试总结（第{context.get('attempt_count', 1)}次尝试）

### 已使用工具
{chr(10).join(f"- {tool}" for tool in context.get('tools_used', []))}

### 已发现信息
{chr(10).join(f"- {finding}" for finding in context.get('findings', []))}

### 已证明无效的方法（避免重复）
{chr(10).join(f"- ❌ {dead_end}" for dead_end in context.get('dead_ends', []))}

### 建议的下一步
{chr(10).join(f"- {step}" for step in context.get('next_steps', []))}

**请基于以上信息继续攻击，不要重复已失败的方法。**
"""
        return prompt
    
    def _get_attempt_count(self) -> int:
        """获取当前尝试次数"""
        context = self.load_resume_context()
        return context.get('attempt_count', 0) if context else 0
    
    def _update_memory_md(self, progress: Dict):
        """更新MEMORY.md文件"""
        content = f"""# 解题进度记录

**最后更新**: {progress['last_updated']}
**尝试次数**: {progress['attempt_count']}

## 已使用的工具
{chr(10).join(f"- {tool}" for tool in progress.get('tools_used', []))}

## 已发现的信息
{chr(10).join(f"- ✓ {finding}" for finding in progress.get('findings', []))}

## 已证明无效的攻击向量
{chr(10).join(f"- ❌ {dead_end}" for dead_end in progress.get('dead_ends', []))}

## 建议的下一步
{chr(10).join(f"{i+1}. {step}" for i, step in enumerate(progress.get('next_steps', [])))}

---
*此文件由系统自动生成，记录本题的尝试历史*
"""
        try:
            with open(self.memory_file, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception:
            pass


def extract_progress_from_result(result) -> Dict:
    """
    从Pi Agent的执行结果中提取进度信息
    
    这是一个启发式提取，从工具输出中识别：
    - 使用了哪些工具
    - 发现了什么
    - 什么方法失败了
    """
    progress = {
        "tools_used": [],
        "findings": [],
        "dead_ends": [],
        "next_steps": []
    }
    
    # 从tool_outputs提取
    if hasattr(result, 'tool_outputs') and result.tool_outputs:
        for tool, args, output in result.tool_outputs:
            if tool not in progress["tools_used"]:
                progress["tools_used"].append(tool)
            
            # 识别发现
            output_str = str(output or "")
            if any(keyword in output_str.lower() for keyword in ['found', '发现', 'discovered', 'flag']):
                progress["findings"].append(f"在{tool}中发现关键信息")
    
    # 从final_text提取失败信息
    if hasattr(result, 'final_text') and result.final_text:
        text = result.final_text.lower()
        if any(keyword in text for keyword in ['failed', '失败', 'blocked', '拦截', 'forbidden']):
            progress["dead_ends"].append("上次尝试被拦截或失败")
    
    return progress
