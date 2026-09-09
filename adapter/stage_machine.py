"""
多阶段渗透测试状态机（借鉴ARTEX）

阶段流程：
侦察 (Recon) → 漏洞发现 (Discovery) → 漏洞利用 (Exploit) → 后渗透 (Post) → Flag获取 (Capture)
"""

import logging
from enum import Enum
from typing import Optional, Dict, List
from dataclasses import dataclass
import time

log = logging.getLogger(__name__)


class PenetrationStage(Enum):
    """渗透测试阶段"""
    RECON = "recon"           # 侦察：端口扫描、指纹识别
    DISCOVERY = "discovery"   # 发现：目录枚举、漏洞扫描
    EXPLOIT = "exploit"       # 利用：执行exploit获取权限
    POST_EXPLOIT = "post"     # 后渗透：提权、横向移动
    CAPTURE_FLAG = "capture"  # 获取flag
    COMPLETED = "completed"   # 完成


@dataclass
class StageResult:
    """阶段执行结果"""
    stage: PenetrationStage
    success: bool
    findings: List[str]       # 发现的信息
    next_actions: List[str]   # 建议的下一步
    tools_used: List[str]     # 使用的工具
    duration: float           # 耗时（秒）
    

class PenetrationStateMachine:
    """渗透测试状态机"""
    
    def __init__(self, target: str, challenge_info: Dict):
        self.target = target
        self.challenge_info = challenge_info
        self.current_stage = PenetrationStage.RECON
        self.stage_history: List[StageResult] = []
        self.findings: Dict[str, List[str]] = {stage.value: [] for stage in PenetrationStage}
        self.start_time = time.time()
        
    def transition_to(self, next_stage: PenetrationStage, result: StageResult):
        """状态转换"""
        log.info("Stage transition: %s -> %s (success=%s)", 
                 self.current_stage.name, next_stage.name, result.success)
        
        self.stage_history.append(result)
        self.findings[self.current_stage.value].extend(result.findings)
        self.current_stage = next_stage
    
    def can_transition_to(self, next_stage: PenetrationStage) -> bool:
        """检查是否可以转换到下一阶段"""
        # 定义合法的状态转换
        valid_transitions = {
            PenetrationStage.RECON: [PenetrationStage.DISCOVERY, PenetrationStage.EXPLOIT],
            PenetrationStage.DISCOVERY: [PenetrationStage.EXPLOIT, PenetrationStage.CAPTURE_FLAG],
            PenetrationStage.EXPLOIT: [PenetrationStage.POST_EXPLOIT, PenetrationStage.CAPTURE_FLAG],
            PenetrationStage.POST_EXPLOIT: [PenetrationStage.CAPTURE_FLAG],
            PenetrationStage.CAPTURE_FLAG: [PenetrationStage.COMPLETED],
        }
        
        return next_stage in valid_transitions.get(self.current_stage, [])
    
    def get_stage_prompt(self) -> str:
        """获取当前阶段的指导提示"""
        prompts = {
            PenetrationStage.RECON: """
## 当前阶段：侦察 (Reconnaissance)

**目标**: 收集目标信息，识别攻击面

**建议操作**:
1. 端口扫描: `nmap -sV -sC {target} -p 1-10000 --min-rate 3000`
2. Web指纹: `whatweb http://{target}` (如果有HTTP)
3. 服务识别: 记录所有开放端口和服务版本

**输出要求**:
- 开放的端口列表
- 运行的服务及版本
- 潜在的攻击向量
""",
            PenetrationStage.DISCOVERY: """
## 当前阶段：漏洞发现 (Discovery)

**目标**: 发现可利用的漏洞和弱点

**建议操作**:
1. 目录枚举: `gobuster dir -u http://{target} -w /usr/share/wordlists/common.txt -x php,html,txt`
2. 漏洞扫描: `nikto -h http://{target}`
3. 参数测试: 测试SQL注入、XSS等

**输出要求**:
- 敏感路径（/admin、/api、/upload等）
- 已知漏洞（CVE）
- 可疑的输入点
""",
            PenetrationStage.EXPLOIT: """
## 当前阶段：漏洞利用 (Exploitation)

**目标**: 利用发现的漏洞获取权限

**建议操作**:
1. 构造并执行exploit
2. 验证漏洞可利用性
3. 获取初始访问权限（webshell/reverse shell）

**输出要求**:
- 成功的exploit代码
- 获得的权限级别
- 可执行的命令
""",
            PenetrationStage.POST_EXPLOIT: """
## 当前阶段：后渗透 (Post-Exploitation)

**目标**: 提权和横向移动

**建议操作**:
1. 提权: 查找SUID、sudo权限、内核漏洞
2. 信息收集: 用户、密码、配置文件
3. 横向移动: 其他服务、内网探测

**输出要求**:
- 提权方法
- 获得的新权限
- 可访问的资源
""",
            PenetrationStage.CAPTURE_FLAG: """
## 当前阶段：获取Flag (Capture the Flag)

**目标**: 找到并提取flag

**建议操作**:
1. 搜索flag文件: `find / -name "flag*" 2>/dev/null`
2. 检查环境变量: `env | grep -i flag`
3. 数据库查询: `SELECT * FROM flags;` (如有数据库访问)
4. 读取flag: `cat /path/to/flag.txt`

**输出要求**:
- Flag内容: flag{...}
- Flag位置: 文件路径或数据库表
"""
        }
        
        return prompts.get(self.current_stage, "").format(target=self.target)
    
    def suggest_next_stage(self, current_findings: List[str]) -> PenetrationStage:
        """根据当前发现建议下一阶段"""
        findings_text = " ".join(current_findings).lower()
        
        if self.current_stage == PenetrationStage.RECON:
            # 如果发现了Web服务，进入发现阶段
            if any(word in findings_text for word in ['http', 'web', '80', '443', '8000', '8080']):
                return PenetrationStage.DISCOVERY
            # 如果直接发现了exploit，跳到利用阶段
            elif any(word in findings_text for word in ['exploit', 'vulnerability', 'cve']):
                return PenetrationStage.EXPLOIT
            else:
                return PenetrationStage.DISCOVERY
        
        elif self.current_stage == PenetrationStage.DISCOVERY:
            # 如果发现了漏洞，进入利用阶段
            if any(word in findings_text for word in ['sqli', 'xss', 'rce', 'lfi', 'upload', 'xxe']):
                return PenetrationStage.EXPLOIT
            # 如果直接找到flag，进入获取阶段
            elif 'flag{' in findings_text:
                return PenetrationStage.CAPTURE_FLAG
            else:
                return PenetrationStage.EXPLOIT
        
        elif self.current_stage == PenetrationStage.EXPLOIT:
            # 如果获得了shell，可能需要后渗透
            if any(word in findings_text for word in ['shell', 'access', 'root']):
                return PenetrationStage.POST_EXPLOIT
            # 或者直接找flag
            else:
                return PenetrationStage.CAPTURE_FLAG
        
        elif self.current_stage == PenetrationStage.POST_EXPLOIT:
            return PenetrationStage.CAPTURE_FLAG
        
        elif self.current_stage == PenetrationStage.CAPTURE_FLAG:
            return PenetrationStage.COMPLETED
        
        return self.current_stage
    
    def get_summary(self) -> str:
        """获取状态机执行摘要"""
        elapsed = time.time() - self.start_time
        
        summary = f"""
## 渗透测试进度

**当前阶段**: {self.current_stage.name}
**已用时间**: {elapsed:.1f}秒
**已完成阶段**: {len(self.stage_history)}

### 各阶段发现
"""
        for stage, findings in self.findings.items():
            if findings:
                summary += f"\n**{stage.upper()}**:\n"
                for finding in findings[:5]:  # 最多显示5个
                    summary += f"- {finding}\n"
        
        return summary
