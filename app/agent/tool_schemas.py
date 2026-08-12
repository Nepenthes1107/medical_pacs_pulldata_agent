"""Agent 工具的 Pydantic 输入/输出 Schema（plan-autonomous §2 / spec §4.2）。

设计要点：
- 所有工具输出统一继承 BaseToolOutput（success/error/retryable）——护栏 3 的落点：
  异常一律收敛为 success=false + error，绝不抛裸异常进循环。
- 输出字段必须稳定命名、可被引用校验器逐条核对（护栏 2，见 §5.2）。
- 计数口径：唯一 SOP 权威来源是 local_dicom_file（local_unique_sop_count），
  storescp_received_count 仅作接收端活动信号（spec §4.2 / §9）。
- 9 只读工具 + 1 写工具。写工具 execute_repull_plan 不绑定给 LLM、不经 MCP 暴露。
"""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class BaseToolOutput(BaseModel):
    """所有 Tool 的通用返回字段。"""

    success: bool  # 必填，禁止因默认值遗漏真实失败
    error: str = ""  # 空字符串 = 无错误
    retryable: bool = False  # 网络超时、连接失败等瞬时错误可重试


# --- query_pacs_target：只答连通性/存在性/期望数（不再顺带查层级）---

class PacsTargetInput(BaseModel):
    source_id: str = Field(..., description="PACS 数据源 ID")
    study_instance_uid: Optional[str] = Field(None, description="Study Instance UID")
    series_instance_uid: Optional[str] = Field(None, description="Series Instance UID")


class PacsTargetOutput(BaseToolOutput):
    pacs_reachable: bool = False
    study_exists: bool = False
    series_exists: Optional[bool] = None
    parent_study_uid: Optional[str] = None
    expected_instance_count: Optional[int] = None
    modality: Optional[str] = None
    dicom_status_summary: Optional[str] = None


# --- query_pacs_hierarchy：Study 下各 Series 期望数，支撑 LLM 下钻 ---

class PacsHierarchyInput(BaseModel):
    source_id: str = Field(..., description="PACS 数据源 ID")
    study_instance_uid: str = Field(..., description="Study Instance UID")


class SeriesNode(BaseModel):
    series_instance_uid: str
    modality: Optional[str] = None
    instance_count: Optional[int] = None  # NumberOfSeriesRelatedInstances


class PacsHierarchyOutput(BaseToolOutput):
    study_instance_uid: str = ""
    series_count: int = 0
    series: List[SeriesNode] = Field(default_factory=list)


# --- query_task_context：只返回当前任务状态与阶段时间线 ---

class TaskContextInput(BaseModel):
    task_id: Optional[str] = Field(None, description="任务 ID")
    study_instance_uid: Optional[str] = Field(None, description="Study Instance UID")
    series_instance_uid: Optional[str] = Field(None, description="Series Instance UID")


class TaskSummary(BaseModel):
    task_id: str
    level: str
    status: str
    expected_count: Optional[int] = None
    created_at: Optional[str] = None
    queued_at: Optional[str] = None
    download_started_at: Optional[str] = None
    move_finished_at: Optional[str] = None
    checked_at: Optional[str] = None
    last_error: Optional[str] = None
    task_retry_times: int = 0
    can_retry: bool = False


class TaskContextOutput(BaseToolOutput):
    tasks: List[TaskSummary] = Field(default_factory=list)


# --- query_task_history：历史重试次数、错误演变，支撑「反复失败该上报」判断 ---

class TaskHistoryInput(BaseModel):
    task_id: Optional[str] = Field(None, description="任务 ID")
    study_instance_uid: Optional[str] = Field(None, description="Study Instance UID")
    series_instance_uid: Optional[str] = Field(None, description="Series Instance UID")


class TaskHistoryOutput(BaseToolOutput):
    task_id: str = ""
    total_retry_times: int = 0  # download_task.task_retry_times（累计重试次数）
    current_status: str = ""
    distinct_errors: List[str] = Field(default_factory=list)  # 去重后的历史错误信息
    last_error: Optional[str] = None
    repeatedly_failing: bool = False  # 重试次数达阈值仍未成功


# --- query_worker_health（合并旧 query_queue_status）：Worker 存活 + 消费者 + 积压 ---

class WorkerHealthInput(BaseModel):
    # 按需工具：无业务参数，队列名默认取配置的 download_queue。
    queue_name: Optional[str] = Field(None, description="队列名，缺省用下载队列")


class WorkerHealthOutput(BaseToolOutput):
    queue_accessible: bool = False
    queue_name: str = ""
    message_count: Optional[int] = None
    consumer_count: Optional[int] = None
    worker_alive: bool = False  # consumer_count > 0
    backlog_likely: bool = False  # message_count > 0 且 consumer_count == 0


# --- query_receive_status：storescp 活动信号 + 本地扫描（唯一 SOP 以本地为权威）---

class ReceiveStatusInput(BaseModel):
    study_instance_uid: str = Field(..., description="Study Instance UID")
    series_instance_uid: Optional[str] = Field(None, description="指定时仅统计该 Series")


class ReceiveStatusOutput(BaseToolOutput):
    storescp_received_count: int = 0  # storescp_image 登记记录数（非唯一 SOP，仅活动信号）
    local_parsed_count: int = 0  # 成功解析的本地 DICOM 文件数
    local_unique_sop_count: int = 0  # 本地唯一 SOP 数，唯一权威计数
    first_received_at: Optional[str] = None
    last_received_at: Optional[str] = None
    series_distribution: Dict[str, int] = Field(default_factory=dict)


# --- compute_integrity（护栏 1）：只做算术，不做任何根因判断 ---

class ComputeIntegrityInput(BaseModel):
    expected: Optional[int] = Field(None, description="期望数量（来自 PACS，可为 None）")
    local_unique_sop: int = Field(0, description="本地唯一 SOP 数（权威计数）")
    level: str = Field("study", description="'study' | 'series' | 'sop'")


class ComputeIntegrityOutput(BaseToolOutput):
    level: str = "study"
    expected: Optional[int] = None
    local_unique_sop: int = 0
    missing: Optional[int] = None  # max(expected - local_unique_sop, 0)；expected 为 None → None
    result: str = "unverified"  # "complete" | "incomplete" | "unverified"


# --- query_missing_instances：IMAGE 级差集，给出 sop 粒度补拉目标 ---

class MissingInstancesInput(BaseModel):
    source_id: str = Field(..., description="PACS 数据源 ID")
    study_instance_uid: str = Field(..., description="Study Instance UID")
    series_instance_uid: str = Field(..., description="Series Instance UID")


class MissingInstancesOutput(BaseToolOutput):
    """PACS 侧 SOP 集合 − 本地 SOP 集合。差集算术归代码，LLM 只消费结果（同护栏 1）。"""

    study_instance_uid: str = ""
    series_instance_uid: str = ""
    pacs_sop_count: int = 0
    local_sop_count: int = 0
    missing_count: int = 0
    missing_sop_instance_uids: List[str] = Field(default_factory=list)


# --- search_knowledge（Agentic RAG）：LLM 按需查知识 ---

class SearchKnowledgeInput(BaseModel):
    query: str = Field(..., description="检索查询文本（症状/故障描述/知识问题）")
    category: Optional[str] = Field(None, description="可选类目过滤，如 fault_sop / experience")
    top_n: Optional[int] = Field(None, description="返回条数，缺省用配置 top_k")


class KnowledgeItem(BaseModel):
    content: str
    category: Optional[str] = None
    source: Optional[str] = None


class SearchKnowledgeOutput(BaseToolOutput):
    query: str = ""
    hits: List[KnowledgeItem] = Field(default_factory=list)
    available: bool = True  # embedding/知识库是否可用


# --- execute_repull_plan（写工具，不绑定给模型，仅审批后由固定节点执行）---

class RepullPlanInput(BaseModel):
    run_id: str = Field(..., description="诊断 Run ID")
    task_id: Optional[str] = Field(None, description="任务 ID；手动下载场景可为空（新建任务）")
    strategy: str = Field(..., description="'retry_task' | 'targeted_cmove' | 'escalate'")
    idempotency_key: str = Field(..., description="由目标 + strategy + 范围指纹派生的短期锁键")
    missing_targets: List[dict] = Field(default_factory=list,
                                        description="定向补拉范围 [{series_instance_uid, sop_instance_uid_list}]")
    study_instance_uid: Optional[str] = Field(None, description="新建补拉任务所需的 Study UID")
    source_id: Optional[str] = Field(None, description="新建补拉任务所用 PACS 源")
    operator: Optional[str] = Field(None, description="审批人")
    force: bool = Field(False, description="是否强制重试非失败任务")


class RepullExecutionOutput(BaseToolOutput):
    task_id: str = ""  # 定向补拉新建任务时为新 task_id（Checker 收尾/失败事件据此关联 Run）
    strategy: str = ""
    new_status: str = ""
    audit_log_id: str = ""
    submitted: bool = False  # 是否已投递补拉队列（escalate 为 False）
    degraded: bool = False  # targeted_cmove 无法定向时降级为整任务重试
    created_task: bool = False  # 是否新建了独立定向任务（原任务保持不动）
    level: str = ""  # 实际执行粒度 study_level|series_level|sop_level
    note: str = ""


# ==========================================================================
# 路由意图识别（route_request 的 LLM 兜底）——路由节点保留，本 Schema 保留。
# ==========================================================================

class RouteDecision(BaseModel):
    """模糊自然语言输入时，LLM 的意图分类 + 实体抽取结果。

    实体只允许从用户原文摘取（route_request 会做子串校验防幻觉），
    LLM 不得自行构造 task_id / UID / 数量。
    """

    route: str  # "diagnosis" | "knowledge_qa" | "clarification"
    task_id: Optional[str] = None
    study_instance_uid: Optional[str] = None
    series_instance_uid: Optional[str] = None
    clarification: Optional[str] = None  # route=clarification 时给用户的追问语


# ==========================================================================
# 自主循环结构化输出 Schema（Stage C/D/E）：reason 决策 / 诊断（带引用）/ 反思 / 补拉计划。
# ==========================================================================

CONFIDENCE_LEVELS = {"confirmed", "high", "uncertain"}
REPULL_STRATEGIES = {"retry_task", "targeted_cmove", "escalate"}


class ToolCallRequest(BaseModel):
    """reason 决定的一次工具调用（name + args）。"""

    name: str
    args: Dict = Field(default_factory=dict)


class ReasonDecision(BaseModel):
    """reason 节点的结构化输出（Stage C）：更新假设 + 决定下一步。

    converged=True → 证据足够，进入 diagnose；否则 tool_calls 非空，继续采证。
    工具名/参数在 act 前过白名单与 Pydantic，模型越界调用会被拒绝（护栏 3/4）。
    """

    hypothesis: str = ""  # 当前根因假设（随轮次更新）
    converged: bool = False
    tool_calls: List[ToolCallRequest] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    """一条事实断言的来源引用（护栏 2）：哪个工具的哪个字段、值是多少。"""

    tool: str
    field: str
    value: Optional[object] = None


class Claim(BaseModel):
    """一条带引用的事实断言。"""

    claim: str
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)


class GroundedDiagnosis(BaseModel):
    """结构化根因（Stage C）：每条断言带 evidence_refs，供引用校验器逐条核对。"""

    diagnostic_level: str = "study"
    summary: str = ""  # ≤150 字
    root_cause: str = ""
    confidence: str = "uncertain"  # confirmed | high | uncertain
    claims: List[Claim] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)  # 低置信时列缺哪些证据

    @model_validator(mode="after")
    def _normalize(self):
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "uncertain"
        return self


class Reflection(BaseModel):
    """reflect 自检输出（护栏 7）：结论能否由所引证据推出、有无过度归因。"""

    supported: bool = True  # 结论是否被证据支持
    over_attribution: bool = False  # 是否过度归因
    notes: str = ""


class SeriesRepullTarget(BaseModel):
    """一个定向补拉目标：某 Series，或该 Series 下的若干 SOP。

    sop_instance_uid_list 为空 → 整个 Series 都要补；非空 → 只补列出的这几张。
    """

    series_instance_uid: str
    sop_instance_uid_list: List[str] = Field(default_factory=list)
    expected_image_number: Optional[int] = None


def derive_repull_level(missing_targets) -> str:
    """由补拉范围推导粒度：无目标→study，任一目标点名了 SOP→sop，否则→series。

    粒度不作为字段存储——存了就可能与 targets 分叉，而分叉会误导审批人（按粒度决定批不批）
    与经验记忆（粒度是 RAG 检索维度）。唯一真相是 missing_targets，粒度一律现算。
    接受 SeriesRepullTarget 或等价 dict，供图内节点与 worker 共用。
    """
    def _sops(t):
        return (t.get("sop_instance_uid_list") if isinstance(t, dict) else t.sop_instance_uid_list) or []

    if not missing_targets:
        return "study"
    return "sop" if any(_sops(t) for t in missing_targets) else "series"


class RepullScope(BaseModel):
    """补拉范围。missing_targets 是唯一真相——粒度用 derive_repull_level() 现算，不存字段。"""

    missing_targets: List[SeriesRepullTarget] = Field(default_factory=list)
    missing_instance_count: Optional[int] = None


class RepullPlan(BaseModel):
    """参数化补拉计划（Stage E / spec §6.1）：范围 + 策略 + 依据 + 引用 + 置信度。"""

    root_cause: str = ""
    scope: RepullScope = Field(default_factory=RepullScope)
    strategy: str = "escalate"  # retry_task | targeted_cmove | escalate
    rationale: str = ""
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)
    confidence: str = "uncertain"

    @model_validator(mode="after")
    def _normalize(self):
        if self.strategy not in REPULL_STRATEGIES:
            self.strategy = "escalate"
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "uncertain"
        return self


class FaultSignature(BaseModel):
    """经验记忆去重签名（需求 5）：LLM 把自由文本根因归一化为一个规范 slug。

    约束为单个短标签（如 worker_crash_in_queue），防自由发挥；
    route_request/write_experience 侧会做清洗（小写、下划线、截断）。
    """

    signature: str = ""  # 规范化故障签名 slug
