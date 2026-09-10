# 自主红队架构设计

## 概述

设计一个简单的自主红队系统，使用 TsecBench 作为第三方执行平台（我们无法控制的外部基础设施）。

## 核心原则

1. **平台无关性** - TsecBench 是可替换的执行后端之一
2. **自主决策** - AI 驱动的攻击路径规划和技术选择
3. **知识积累** - 从每次行动中学习并改进
4. **安全边界** - 明确授权范围和安全护栏

## 架构分层

```
┌─────────────────────────────────────────────────────────────┐
│ 指挥层 (Command Layer)                                        │
│ - 战役管理 (Campaign Management)                             │
│ - 目标定义 (Target Definition)                               │
│ - 授权控制 (Authorization Control)                           │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│ 规划层 (Planning Layer)                                       │
│ - 侦察分析 (Reconnaissance Analysis)                         │
│ - 攻击路径生成 (Attack Path Generation)                      │
│ - 技术选择 (Technique Selection - MITRE ATT&CK)             │
│ - 优先级排序 (Priority Ranking)                              │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│ 执行层 (Execution Layer)                                      │
│ ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│ │ TsecBench    │  │ 自定义工具    │  │ 手动操作      │      │
│ │ Workers      │  │ Custom Tools │  │ Manual Ops   │      │
│ │ (第三方平台) │  │ (自有脚本)   │  │ (人工介入)    │      │
│ └──────────────┘  └──────────────┘  └──────────────┘      │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│ 观测层 (Observation Layer)                                    │
│ - 执行结果收集 (Execution Results)                           │
│ - 目标状态监控 (Target State Monitoring)                     │
│ - 检测响应感知 (Detection Awareness)                         │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│ 知识层 (Knowledge Layer)                                      │
│ - 技术库 (Technique Library)                                 │
│ - 载荷库 (Payload Repository)                                │
│ - 经验回放 (Experience Replay)                               │
│ - 对抗模型 (Adversary Simulation Models)                     │
└─────────────────────────────────────────────────────────────┘
```

## 组件详细设计

### 1. 指挥层 (Command Layer)

**职责**: 定义战役目标、管理授权范围、全局协调

**核心实体**:
```python
@dataclass
class Campaign:
    """红队战役"""
    campaign_id: str
    name: str
    objectives: list[str]          # 战役目标
    scope: AuthorizationScope      # 授权范围
    constraints: list[str]          # 约束条件
    start_time: datetime
    end_time: datetime | None
    status: CampaignStatus

@dataclass
class AuthorizationScope:
    """授权范围"""
    targets: list[str]              # IP/域名/网段
    allowed_techniques: list[str]   # 允许的 ATT&CK 技术
    forbidden_actions: list[str]    # 禁止操作
    time_windows: list[TimeWindow]  # 允许时间窗口
    escalation_required: bool       # 是否需要升级批准
```

**关键功能**:
- 战役生命周期管理
- 授权验证 (每个操作执行前检查)
- 紧急停止机制
- 审计日志记录

### 2. 规划层 (Planning Layer)

**职责**: 基于侦察结果生成攻击路径，选择合适技术

**核心流程**:
```
侦察结果 → 威胁建模 → 路径生成 → 技术映射 → 优先级排序
```

**核心实体**:
```python
@dataclass
class AttackPath:
    """攻击路径"""
    path_id: str
    campaign_id: str
    initial_access: TechniqueStep
    stages: list[AttackStage]       # 多阶段攻击
    success_probability: float      # 成功概率估计
    stealth_score: float            # 隐蔽性评分
    required_resources: list[str]   # 需要的资源/工具

@dataclass
class AttackStage:
    """攻击阶段"""
    stage_name: str                 # 如 "Privilege Escalation"
    techniques: list[Technique]     # 可选技术列表
    dependencies: list[str]         # 依赖的前置条件
    fallback_options: list[str]     # 备选方案
```

**AI 驱动决策**:
- LLM 分析侦察数据，生成威胁模型
- 基于 MITRE ATT&CK 知识图谱规划路径
- 考虑隐蔽性、成功率、资源消耗三维评估
- 动态调整：根据执行反馈重新规划

### 3. 执行层 (Execution Layer)

**职责**: 协调多个执行后端，统一接口

**执行后端抽象**:
```python
class ExecutionBackend(ABC):
    """执行后端抽象接口"""
    
    @abstractmethod
    async def execute(self, task: ExecutionTask) -> ExecutionResult:
        """执行单个任务"""
        pass
    
    @abstractmethod
    async def check_status(self, task_id: str) -> TaskStatus:
        """检查任务状态"""
        pass
    
    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        """取消任务"""
        pass

class TsecBenchBackend(ExecutionBackend):
    """TsecBench 平台适配器 (第三方)"""
    
    def __init__(self, base_url: str, token: str):
        self.client = TSecBenchmarkAsync(base_url, token)
    
    async def execute(self, task: ExecutionTask) -> ExecutionResult:
        # 将任务转换为 TsecBench challenge 格式
        # 提交到 TsecBench worker 执行
        # 轮询结果并转换回统一格式
        pass

class CustomToolBackend(ExecutionBackend):
    """自定义工具后端"""
    # 执行本地脚本、工具链
    pass
```

**任务分发逻辑**:
```python
class ExecutionCoordinator:
    """执行协调器"""
    
    def __init__(self):
        self.backends: dict[str, ExecutionBackend] = {
            "tsecbench": TsecBenchBackend(...),
            "custom": CustomToolBackend(...),
            "manual": ManualOperationBackend(...),
        }
    
    async def execute_step(self, step: TechniqueStep) -> ExecutionResult:
        # 根据技术类型选择合适的后端
        backend_name = self._select_backend(step)
        backend = self.backends[backend_name]
        
        # 授权检查
        if not self._check_authorization(step):
            return ExecutionResult.unauthorized()
        
        # 执行
        result = await backend.execute(step.to_task())
        
        # 记录观测数据
        await self._record_observation(result)
        
        return result
```

**TsecBench 集成要点**:
- TsecBench 是**黑盒服务** - 我们只通过 API 交互
- 不依赖 TsecBench 内部实现细节
- 处理 TsecBench 不可用的情况 (降级到其他后端)
- 从 TsecBench observability API 获取 agent 日志用于学习

### 4. 观测层 (Observation Layer)

**职责**: 收集执行结果、监控目标状态、感知防御响应

**核心实体**:
```python
@dataclass
class Observation:
    """观测记录"""
    obs_id: str
    campaign_id: str
    task_id: str
    timestamp: datetime
    observation_type: ObservationType
    data: dict[str, Any]
    
class ObservationType(Enum):
    EXECUTION_SUCCESS = "execution_success"
    EXECUTION_FAILURE = "execution_failure"
    TARGET_RESPONSE = "target_response"      # 目标系统响应
    DETECTION_ALERT = "detection_alert"      # 检测到防御告警
    NETWORK_CHANGE = "network_change"        # 网络拓扑变化
    CREDENTIAL_FOUND = "credential_found"    # 发现凭证
```

**数据源**:
- TsecBench observability API (agent 日志、执行结果)
- 自定义工具输出
- 目标系统监控 (如果有权限)
- 外部威胁情报

### 5. 知识层 (Knowledge Layer)

**职责**: 积累技术知识、存储成功案例、支持经验学习

**知识库结构**:
```
knowledge/
├── techniques/          # 技术库
│   ├── T1190_exploit_public_facing_app.yaml
│   ├── T1078_valid_accounts.yaml
│   └── ...
├── payloads/           # 载荷库
│   ├── web_shells/
│   ├── privilege_escalation/
│   └── persistence/
├── campaigns/          # 历史战役
│   └── 2026-09-campaign-alpha/
│       ├── attack_graph.json
│       ├── execution_logs/
│       └── lessons_learned.md
└── models/             # AI 模型
    ├── path_planning_prompts/
    └── technique_selection_embeddings/
```

**经验学习**:
```python
class KnowledgeBase:
    """知识库"""
    
    async def learn_from_campaign(self, campaign: Campaign):
        """从战役中学习"""
        # 分析成功/失败的技术组合
        # 更新技术成功率统计
        # 提取可复用的攻击模式
        # 生成 lessons learned 文档
        pass
    
    async def recommend_techniques(
        self, 
        context: TargetContext
    ) -> list[Technique]:
        """基于上下文推荐技术"""
        # 向量检索相似历史场景
        # LLM 生成技术建议
        # 结合成功率排序
        pass
```

## 数据流

### 完整执行流程

```
1. [指挥层] 创建战役 → 定义目标和授权范围
   ↓
2. [规划层] 侦察阶段
   - 执行被动/主动侦察
   - 收集目标信息 (服务、版本、漏洞)
   ↓
3. [规划层] 威胁建模
   - LLM 分析侦察结果
   - 生成多条候选攻击路径
   - 评估可行性和隐蔽性
   ↓
4. [指挥层] 人工审批 (可选)
   - 审查攻击路径
   - 批准或调整计划
   ↓
5. [执行层] 执行攻击路径
   For each stage in path:
     For each technique in stage:
       - 授权检查
       - 选择执行后端 (TsecBench | Custom | Manual)
       - 执行并等待结果
       - 如果失败，尝试 fallback
       - 如果检测到防御响应，暂停并重新评估
   ↓
6. [观测层] 实时监控
   - 收集执行日志
   - 监控目标反应
   - 检测防御措施
   ↓
7. [规划层] 动态调整
   - 基于观测结果调整路径
   - 如果被发现，切换到隐蔽技术
   ↓
8. [知识层] 经验归档
   - 保存完整攻击图
   - 记录成功/失败的技术组合
   - 更新知识库
```

## TsecBench 集成细节

### TsecBench 作为第三方平台的定位

**我们能控制的**:
- 何时调用 TsecBench API
- 传递什么样的 challenge 定义
- 如何解读执行结果

**我们不能控制的**:
- TsecBench 内部调度逻辑
- Worker 的具体执行环境
- 平台的可用性和性能

### 适配器实现

```python
class TsecBenchAdapter:
    """TsecBench 平台适配器"""
    
    def __init__(self, base_url: str, token: str, admin_token: str | None = None):
        self.client = TSecBenchmarkAsync(base_url, token)
        self.admin_token = admin_token
        self.base_url = base_url
    
    async def execute_technique(
        self, 
        technique: Technique,
        target: Target
    ) -> ExecutionResult:
        """将红队技术转换为 TsecBench challenge 并执行"""
        
        # 1. 转换为 challenge 格式
        challenge = self._technique_to_challenge(technique, target)
        
        # 2. 如果需要 VPN，预先配置 (通过 admin API)
        if technique.requires_vpn:
            await self._setup_vpn(target.vpn_config)
        
        # 3. 提交 challenge
        result = await self.client.submit_challenge(challenge)
        
        # 4. 轮询直到完成
        while True:
            status = await self.client.get_status(result.challenge_id)
            if status.is_terminal:
                break
            await asyncio.sleep(5)
        
        # 5. 获取 agent 日志 (从 observability API)
        logs = await self._fetch_agent_logs(result.challenge_id)
        
        # 6. 转换回统一的 ExecutionResult 格式
        return ExecutionResult(
            success=status.flags_found > 0,
            technique_id=technique.id,
            output=logs,
            artifacts=self._extract_artifacts(logs),
            observations=self._parse_observations(logs),
        )
    
    def _technique_to_challenge(
        self, 
        technique: Technique, 
        target: Target
    ) -> dict:
        """将 MITRE ATT&CK 技术转换为 TsecBench challenge 定义"""
        return {
            "unique_code": f"redteam_{technique.id}_{uuid4().hex[:8]}",
            "description": self._generate_challenge_description(technique, target),
            "difficulty": technique.difficulty,
            "level": self._map_to_tsecbench_level(technique),
            "total_score": 100,
            "flags": [self._generate_flag()],
            "container_addr": target.address,
            "container_port": target.port,
        }
    
    def _generate_challenge_description(
        self, 
        technique: Technique, 
        target: Target
    ) -> str:
        """生成给 AI agent 的任务描述"""
        return f"""
Red Team Operation - {technique.name}

Target: {target.address}:{target.port}
Objective: {technique.objective}

Reconnaissance Data:
{target.recon_summary}

Instructions:
{technique.execution_guidance}

Success Criteria:
- {technique.success_criteria}

Flag Format: flag{{...}}

IMPORTANT: This is an authorized red team operation.
All actions are within the approved scope.
"""
    
    async def _fetch_agent_logs(self, challenge_id: str) -> str:
        """从 TsecBench observability API 获取 agent 日志"""
        # 假设 TsecBench 提供了观测 API
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/observability/challenges/{challenge_id}/logs"
            async with session.get(url) as resp:
                return await resp.text()
```

### 降级策略

当 TsecBench 不可用时:
```python
class ResilientExecutor:
    """带降级的执行器"""
    
    async def execute(self, task: ExecutionTask) -> ExecutionResult:
        try:
            # 优先使用 TsecBench (适合复杂的多步骤技术)
            return await self.tsecbench.execute(task)
        except TsecBenchUnavailable:
            logger.warning("TsecBench unavailable, falling back to custom tools")
            # 降级到自定义工具
            return await self.custom_tools.execute(task)
        except Exception as e:
            # 最后降级到手动操作队列
            await self.manual_queue.enqueue(task)
            raise ExecutionDeferred(f"Queued for manual execution: {e}")
```

## 技术栈建议

### 核心服务
- **语言**: Python 3.11+
- **Web 框架**: FastAPI (指挥中心 API)
- **数据库**: PostgreSQL (战役数据) + Redis (任务队列)
- **消息队列**: Redis Streams 或 RabbitMQ
- **AI 模型**: Claude/GPT-4 (路径规划) + 向量数据库 (知识检索)

### 执行层
- **TsecBench 客户端**: `tsec-benchmark` SDK (异步)
- **自定义工具**: Metasploit RPC, Nuclei, custom scripts
- **容器隔离**: Docker (隔离执行环境)

### 观测层
- **日志聚合**: Loki 或 Elasticsearch
- **时序数据**: Prometheus + Grafana
- **事件总线**: Redis Pub/Sub

## 最小 MVP 实现

### 第一阶段: 单一技术执行器
- [x] TsecBench 适配器
- [x] 授权范围检查
- [x] 执行结果记录
- [ ] 简单的 CLI 界面

### 第二阶段: 基础规划能力
- [ ] 侦察数据收集
- [ ] LLM 驱动的技术选择
- [ ] 多技术序列执行

### 第三阶段: 知识积累
- [ ] 技术库管理
- [ ] 历史战役归档
- [ ] 成功率统计

### 第四阶段: 自主决策
- [ ] 攻击路径自动生成
- [ ] 动态调整能力
- [ ] 对抗感知

## 安全考量

1. **授权验证**: 每个操作前强制检查授权范围
2. **审计日志**: 所有操作不可篡改的审计轨迹
3. **紧急停止**: 一键停止所有进行中的操作
4. **凭证管理**: 敏感数据加密存储
5. **网络隔离**: 执行环境与管理网络隔离
6. **时间窗口**: 仅在授权时间段内执行
7. **升级机制**: 高风险操作需要人工审批

## 与 TsecBench 的边界

```
┌─────────────────────────────────────────────┐
│ 我们的红队系统 (Autonomous Red Team)         │
│                                             │
│ ┌─────────────────────────────────────┐   │
│ │ 指挥层: 战役管理、授权、协调         │   │
│ └──────────────┬──────────────────────┘   │
│                │                            │
│ ┌──────────────▼──────────────────────┐   │
│ │ 规划层: 侦察分析、路径生成          │   │
│ └──────────────┬──────────────────────┘   │
│                │                            │
│ ┌──────────────▼──────────────────────┐   │
│ │ 执行层: 多后端协调                   │   │
│ │  ┌──────────────┐                   │   │
│ │  │ TsecBench    │  ← API 调用       │   │
│ │  │ 适配器       │  → 结果收集        │   │
│ │  └──────────────┘                   │   │
│ └─────────────────────────────────────┘   │
│                                             │
│ ┌─────────────────────────────────────┐   │
│ │ 观测层: 数据聚合、学习               │   │
│ └─────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
                    │
                    │ REST API 调用
                    │
┌───────────────────▼─────────────────────────┐
│ TsecBench (第三方平台 - 不可控)              │
│                                             │
│ - 控制面 (core)                             │
│ - Worker 调度                               │
│ - Agent 执行                                │
│ - Observability                            │
└─────────────────────────────────────────────┘
```

**清晰的职责分离**:
- **我们的系统**: 战略决策、技术选择、知识管理
- **TsecBench**: 战术执行、Agent 编排、环境隔离

## 下一步行动

1. **需求确认**:
   - 目标场景 (CTF / 渗透测试 / 持续演练)
   - 授权模型 (完全自主 / 半自动 / 人工审批)
   - 规模 (单目标 / 多目标并行)

2. **技术选型确认**:
   - LLM provider (Claude / GPT-4 / 开源模型)
   - 部署方式 (单机 / 分布式)
   - 是否需要 Web UI

3. **MVP 范围**:
   - 从哪个阶段开始实现
   - 是否需要与现有 TsecBench 代码集成

## 总结

这个架构将红队能力分为清晰的层次:
- **指挥层**控制全局和授权
- **规划层**提供智能决策
- **执行层**协调多个后端 (TsecBench 是其中之一)
- **观测层**提供反馈和态势感知
- **知识层**支持持续学习

TsecBench 的定位是**执行后端之一**，而非整个红队系统。这样的设计使得系统:
- 不依赖 TsecBench 的内部实现
- 可以灵活切换或组合多个执行后端
- 在 TsecBench 不可用时有降级方案
- 能够复用 TsecBench 的 AI agent 能力,同时补充战略规划层
