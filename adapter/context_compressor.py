"""
上下文压缩器 - 智能压缩对话历史，保留关键信息

设计思路：
1. 分析对话历史，提取关键信息
2. 使用LLM自我总结
3. 保留最近N轮的完整上下文
4. 将旧对话压缩为摘要
"""

import logging
import re
from typing import List, Dict, Optional

log = logging.getLogger(__name__)


class ContextCompressor:
    """上下文压缩器"""
    
    def __init__(self, keep_recent=3, max_tokens=2000):
        """
        Args:
            keep_recent: 保留最近N轮的完整对话
            max_tokens: 压缩后的最大token数（估算）
        """
        self.keep_recent = keep_recent
        self.max_tokens = max_tokens
    
    def compress(self, conversation_history: List[Dict]) -> List[Dict]:
        """
        压缩对话历史
        
        Args:
            conversation_history: 对话历史列表 [{"role": "user/assistant", "content": "..."}]
            
        Returns:
            压缩后的对话历史
        """
        if len(conversation_history) <= self.keep_recent:
            return conversation_history
        
        log.info("压缩上下文：%d轮 → 保留最近%d轮", 
                 len(conversation_history), self.keep_recent)
        
        # 1. 分离旧对话和最近对话
        old_messages = conversation_history[:-self.keep_recent]
        recent_messages = conversation_history[-self.keep_recent:]
        
        # 2. 压缩旧对话
        compressed_summary = self._compress_old_messages(old_messages)
        
        # 3. 组合：压缩摘要 + 最近完整对话
        compressed_history = [
            {"role": "system", "content": compressed_summary}
        ] + recent_messages
        
        log.info("压缩完成：%d条消息 → %d条消息", 
                 len(conversation_history), len(compressed_history))
        
        return compressed_history
    
    def _compress_old_messages(self, messages: List[Dict]) -> str:
        """压缩旧消息为摘要"""
        
        # 提取关键信息
        tools_used = set()
        findings = []
        flags_found = []
        errors = []
        
        for msg in messages:
            content = msg.get("content", "")
            
            # 提取工具使用
            tools = re.findall(r'\b(nmap|curl|sqlmap|gobuster|nikto|hydra)\b', content, re.IGNORECASE)
            tools_used.update(tools)
            
            # 提取flag
            flags = re.findall(r'flag\{[^}]+\}', content)
            flags_found.extend(flags)
            
            # 提取发现
            if any(keyword in content.lower() for keyword in ['发现', 'found', 'discovered']):
                # 提取该句
                sentences = content.split('。')
                for s in sentences:
                    if any(k in s.lower() for k in ['发现', 'found', 'discovered']):
                        findings.append(s.strip())
            
            # 提取错误
            if any(keyword in content.lower() for keyword in ['error', '错误', 'failed', '失败']):
                errors.append("某次尝试失败")
        
        # 生成摘要
        summary = f"""
**前期尝试总结**（共{len(messages)}轮对话已压缩）：

**使用的工具**：
{', '.join(tools_used) if tools_used else '无'}

**关键发现**：
{chr(10).join(f"- {f[:100]}" for f in findings[:5]) if findings else '- 暂无关键发现'}

**已发现的flag**：
{chr(10).join(f"- {f}" for f in set(flags_found)) if flags_found else '- 尚未发现flag'}

**失败尝试**：
- 共{len(errors)}次尝试失败或遇到错误

**重要提示**：
以上是前期探索的摘要。请基于这些信息继续推进，避免重复失败的尝试。
"""
        
        return summary.strip()
    
    def should_compress(self, history_length: int) -> bool:
        """判断是否应该压缩"""
        # 当历史长度超过保留数量的2倍时，建议压缩
        return history_length > self.keep_recent * 2
    
    def estimate_tokens(self, text: str) -> int:
        """估算token数（简单估算：1 token ≈ 4个字符）"""
        return len(text) // 4


def compress_context_with_llm(messages: List[Dict], llm) -> str:
    """
    使用LLM自我总结上下文（更智能的压缩）
    
    Args:
        messages: 对话历史
        llm: LLM实例
        
    Returns:
        LLM生成的摘要
    """
    try:
        # 构建总结提示
        history_text = "\n\n".join([
            f"[{m['role']}]: {m['content'][:500]}"  # 每条消息最多取500字符
            for m in messages
        ])
        
        prompt = f"""
请总结以下渗透测试对话的关键信息：

{history_text}

**总结要求**：
1. 使用了哪些工具
2. 发现了什么信息（端口、路径、漏洞等）
3. 哪些尝试失败了
4. 当前进展到哪个阶段
5. 下一步应该做什么

请用200字以内简洁总结。
"""
        
        response = llm.chat([{"role": "user", "content": prompt}], max_tokens=300)
        summary = getattr(response, "text", "") or getattr(response, "content", "") or ""
        
        log.info("LLM上下文压缩完成：%d → %d字符", len(history_text), len(summary))
        
        return summary.strip()
    
    except Exception as e:
        log.warning("LLM压缩失败，使用简单压缩: %s", e)
        # 降级到简单压缩
        compressor = ContextCompressor()
        return compressor._compress_old_messages(messages)
