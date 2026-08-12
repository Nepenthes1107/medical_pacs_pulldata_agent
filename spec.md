# MedFlow Agent 技术规格说明

## 1. 文档目的

本文档用于指导 `medical-pacs-pulldata-agent` 在现有 PACS 数据补拉链路基础上完成 Agent 化改造。项目聚焦单一医疗影像后端业务：对 PACS 连通、PACS 查询、补拉任务、C-MOVE 传输、storescp 收图、本地扫描与完整性校验进行智能诊断，并在人工确认后执行安全补拉。

项目不扩展为通用 AIOps 平台，不引入复杂多 Agent 架构，也不承担医学影像诊断任务。

## 2. 项目现状

当前系统已经具备以下业务链路：

```text
Orthanc / Mock PACS
  -> C-FIND 查询 Study / Series
  -> FastAPI 创建 DownloadTask
  -> RabbitMQ 投递下载任务
  -> Downloader Worker 执行 C-MOVE
  -> storescp 接收 DICOM
  -> Checker 扫描本地文件
  -> MySQL 更新任务与完整性状态
  -> 规则 Agent 输出诊断结果
```

Agent 改造应复用现有 PACS、任务、队列、收图、扫描和校验能力，不重写 PullData 核心业务。

## 3. 改造目标

改造后的 Agent 应支持以下能力：

1. 根据 `task_id`、`StudyInstanceUID` 或 `SeriesInstanceUID` 发起诊断。
2. 自动识别诊断层级，补齐 Study 与 Series 的上下文关系。
3. 通过受约束的 Function Calling 采集 PACS、任务、队列、收图和完整性证据。
4. 结合确定性规则与 RAG 知识，定位故障阶段并解释原因。
5. 使用代码 Guardrail 与一次可选语义复核检查诊断输出，避免无依据归因。
6. 经交人工确认后提任务重试，不在同一 Agent Run 中等待异步补拉完成。
7. 保存 Agent Run 状态，支持人工审批中断与恢复。
8. 通过单一 `/agent/chat` 入口接收自然语言和可选结构化标识，由 LangGraph 决定诊断、知识问答或澄清路由。
9. 控制每次诊断的 Token 消耗，通过证据摘要、按需 Few-shot 和可选重排控制上下文长度。

## 4. 技术选型

### 4.1 LangGraph

LangGraph 作为 Agent 主编排框架，负责：

- Agent State 管理；
- 节点与条件边；
- 请求路由、串行和并行工具调用；
- 人工审批中断；
- Checkpoint 持久化；
- 写操作后的恢复执行；
- 诊断、复核和重试提交的流程控制。

### 4.2 LangChain

LangChain 作为基础能力层，负责：

- 大模型适配；
- Tool 定义与注册；
- PromptTemplate；
- Pydantic 参数校验；
- 结构化输出；
- Retriever 与 RAG 链接入。

### 4.3 Function Calling 策略

本项目采用**受约束的混合模式**，不采用完全自主的 ReAct 工具循环：

- 模型通过 `bind_tools()` 生成真实 Tool Calls，负责理解自然语言意图和选择补充性只读工具；
- 代码根据 Study/Series 层级定义最小证据集，校验并补齐模型不可跳过的核心工具；
- Pydantic 校验工具参数，代码控制依赖顺序、并发执行、超时与重试；
- `retry_pull_task` 不绑定到普通 Tool Calling 节点，只能在人工审批通过后由固定节点调用；
- 数量、UID 归属、完整性和重试资格由确定性代码判断，不由模型推算。

该模式既能展示 Function Calling 的真实调用过程，又避免完全自主模式漏调核心工具、重复调用、参数漂移和误触发写操作。

## 5. 总体架构

```text
用户 / 前端
   |
   v
Agent API（FastAPI）
   |-- POST /agent/chat → 统一入口，返回 run_id
   |-- GET  /agent/runs/{run_id} → 轮询诊断状态与结果
   |-- POST /agent/runs/{run_id}/action → 审批（approve / reject）
   |
   v 写入 agent_run + 投递 RabbitMQ agent_runs 队列
Agent Worker（执行 LangGraph，不使用 FastAPI 后台线程）
   |-- route_request（规则优先；必要时 LLM 结构化解析）
   |      |-- knowledge_qa → retrieve_and_answer → END
   |      |-- clarification → present_clarification → END
   |      |-- diagnosis → resolve_target
   |-- plan_tools（模型产生只读 Tool Calls）
   |-- validate_tool_plan（白名单 + 参数 + 层级 + 最小证据集校验）
   |-- collect_evidence（按依赖串行/并行执行只读工具）
   |-- deterministic_diagnosis（确定性数量比对 + 故障阶段判断）
   |-- retrieve_and_explain（RAG + LLM；失败时返回规则结果）
   |-- validate_output（代码 Guardrail + 最多一次语义修正）
   |      |-- passed       → determine_action
   |      |-- insufficient → present_uncertain_result → END
   |-- determine_action
   |      |-- 无写操作 → END
   |      |-- 建议重试 → human_approval
   |                       |-- 拒绝 → END
   |                       |-- 同意 → enqueue_retry → END
   |
   +--> LangChain Tools (5)
   |      |-- query_pacs_target       (PACS 连通 + 查询，只读，可绑定)
   |      |-- query_task_context      (MySQL 任务状态，只读，可绑定)
   |      |-- query_queue_status      (RabbitMQ 队列状态，按需，只读，可绑定)
   |      |-- query_receive_status    (storescp + 本地扫描，只读，可绑定)
   |      |-- retry_pull_task         (安全补拉，写操作，需审批，不绑定)
   |      （数量比对由 deterministic_diagnosis 节点内代码完成，不是可绑定工具）
   |
   +--> RAG Knowledge Base (ChromaDB)
   |      |-- Embedding: DashScope text-embedding-v4
   |      |-- Optional Reranker: DashScope qwen3-rerank
   |
   +--> Checkpoint: LangGraph SqliteSaver（单实例简历项目）
```

Agent 通过工具访问现有业务能力，不允许模型直接执行 SQL、Shell、Docker 命令或修改 PACS 配置。

`/agent/chat` 是唯一的初始入口，但查询 Run 和提交审批仍使用独立资源端点。API 层只负责请求校验、创建 Run 和投递消息，不判断业务路由。LangGraph 优先使用显式标识和规则解析；只有自然语言存在歧义时才调用 LLM 解析，从而避免冗余模型调用。

## 6. 诊断对象与业务规则

### 6.1 Study 级诊断

Study 级诊断以 `StudyInstanceUID` 为主键，关注：

- PACS 中是否存在该 Study；
- Study 下 Series 数量与 Instance 总数；
- 是否存在 Study 级下载任务；
- C-MOVE 是否触发；
- storescp 是否接收到影像；
- 本地实际 Instance 数量；
- Study 总体完整性；
- 是否存在缺失 Series。

### 6.2 Series 级诊断

Series 级诊断以 `SeriesInstanceUID` 为主键，同时必须解析其所属 `StudyInstanceUID`，关注：

- PACS 中是否存在该 Series；
- 所属 Study 是否存在；
- Series 期望 Instance 数量；
- 是否存在 Series 级任务；
- storescp 与本地文件是否仅针对该 Series 统计；
- Series 实际数量与期望数量是否一致；
- 该 Series 是否缺失、部分下载或重复接收。

### 6.3 层级优先规则

- 用户提供 `task_id`：以任务中的 level、Study UID、Series UID 为准。
- 用户同时提供 `task_id` 和 `StudyInstanceUID` / `SeriesInstanceUID`：以 `task_id` 为准，在 evidence 中标注"用户提供的 Study/Series UID 与 task 记录不一致，以 task 为准"。
- 用户仅提供 `SeriesInstanceUID`：先解析所属 Study，再执行 Series 级诊断。
- 用户仅提供 `StudyInstanceUID`：执行 Study 级诊断。
- 同时提供 Study 和 Series：验证二者归属关系，不一致时停止诊断并返回参数冲突。
- Series 级完整性判断不得使用整个 Study 的本地文件数量。

### 6.4 事实层前置修复

Agent 改造前必须先保证工具读取到的业务事实可靠：

- Downloader 在执行 C-MOVE 前提交 `downloading` 与 `download_started_at`，避免长事务导致状态不可观测；
- C-MOVE 仅将最终 Success/可接受 Warning 判为完成，`0xFF00`、`0xFF01` Pending 不得作为最终成功；
- DownloadTask 增加 `queued_at`、`download_started_at`、`move_finished_at`、`checked_at`、`failed_at`；
- 传输状态与完整性结果分离，expected 缺失时记录 `unverified`，避免 Checker 永久重复扫描；
- Study 级完整性比较 PACS 与本地 Series 集合；Series 级完整性只比较目标 Series 的唯一 SOP 数量。

### 6.5 数据库结构改动

本次改造涉及三处数据库变更：一是给现有 `download_task` 增加阶段时间字段（服务于 6.4 的可观测性），二是新增 `agent_run` 表（承载 Agent Run 状态与人工审批中断/恢复），三是新增 `agent_action_audit` 表（记录写操作审计）。全部沿用现有 [config/schema.sql](config/schema.sql) 的 utf8mb4、`BIGINT` 自增主键与 `idx_`/`uk_` 索引命名风格。

现有表 `storescp_image` **不改动**——完整性以 `local_dicom_file.sop_instance_uid` 为权威口径，见 [9.4 计数口径](#94-query_receive_status)。

**（1）`download_task` 增加阶段时间字段**

```sql
ALTER TABLE download_task
  ADD COLUMN queued_at           DATETIME DEFAULT NULL COMMENT '进入 in_queue 的时间' AFTER status,
  ADD COLUMN download_started_at DATETIME DEFAULT NULL COMMENT 'Downloader 提交 downloading 的时间',
  ADD COLUMN move_finished_at    DATETIME DEFAULT NULL COMMENT 'C-MOVE 最终状态确认时间',
  ADD COLUMN checked_at          DATETIME DEFAULT NULL COMMENT 'Checker 完成完整性校验的时间',
  ADD COLUMN failed_at           DATETIME DEFAULT NULL COMMENT '任务判定 fail 的时间';
```

这些字段是 6.4「事实层前置修复」的落地：没有阶段时间，Agent 无法区分“任务卡在队列”“C-MOVE 阻塞”“Checker 未运行”，也无法给出 Case 4/6 那样的时间线证据。

**（2）新增 `agent_run` 表**

```sql
CREATE TABLE IF NOT EXISTS agent_run (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL COMMENT 'UUID，Agent Run 唯一标识，也是隔离边界',
  route VARCHAR(32) DEFAULT NULL COMMENT 'diagnosis / knowledge_qa / clarification',
  status VARCHAR(32) NOT NULL DEFAULT 'running' COMMENT 'running / awaiting_approval / completed / failed / rejected',
  message TEXT DEFAULT NULL COMMENT '用户输入的自然语言 message',
  task_id VARCHAR(64) DEFAULT NULL COMMENT '解析出的任务 ID',
  study_instance_uid VARCHAR(128) DEFAULT NULL,
  series_instance_uid VARCHAR(128) DEFAULT NULL,
  diagnostic_level VARCHAR(16) DEFAULT NULL COMMENT 'study / series / unknown',
  diagnosis JSON DEFAULT NULL COMMENT '结构化 DiagnosisOutput',
  proposed_action JSON DEFAULT NULL COMMENT '待审批的写操作描述',
  approval_status VARCHAR(16) DEFAULT NULL COMMENT 'pending / approved / rejected',
  operator VARCHAR(64) DEFAULT NULL COMMENT '审批人标识（多用户隔离）',
  error TEXT DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_run_id (run_id),
  KEY idx_status (status),
  KEY idx_task_id (task_id),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

`agent_run` 是轮询 API（`GET /agent/runs/{run_id}`）的读取源，也是人工审批中断后恢复的依据。LangGraph Checkpoint（SqliteSaver）保存图执行的内部状态用于恢复，`agent_run` 保存对外可查询的业务结果，两者职责分离（见 [13.3](#133-为什么审批状态必须返回诊断)）。

**（3）新增 `agent_action_audit` 表**

```sql
CREATE TABLE IF NOT EXISTS agent_action_audit (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL COMMENT '发起该动作的 Agent Run',
  task_id VARCHAR(64) NOT NULL COMMENT '被操作的补拉任务',
  action VARCHAR(32) NOT NULL COMMENT '写操作类型，如 retry_pull_task',
  operator VARCHAR(64) DEFAULT NULL COMMENT '审批人标识',
  idempotency_key VARCHAR(128) DEFAULT NULL COMMENT 'run_id+task_id+action 派生的短期锁键',
  result VARCHAR(32) NOT NULL COMMENT 'submitted / rejected / failed',
  detail JSON DEFAULT NULL COMMENT '执行结果摘要',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_run_id (run_id),
  KEY idx_task_id (task_id),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

每次经审批执行的写操作都必须落一条审计记录，对应 [9.6](#96-retry_pull_task) 的调用限制。审计表只追加不更新，是安全边界（[第 14 节](#14-安全边界)）的落地证据。

## 7. LangGraph 状态设计

Agent State 使用 LangGraph `TypedDict`。结构化诊断以单个 Run 为边界，不把通用聊天历史混入诊断状态：

```python
from typing import Literal, TypedDict


class ToolResult(TypedDict, total=False):
    """单个工具调用的结果。"""
    success: bool
    error: str
    retryable: bool
    # 各工具的专属字段由具体 Schema 定义，见 Section 9


class AgentState(TypedDict):
    # ── 运行标识 ──
    run_id: str
    status: str

    # ── 输入与路由 ──
    message: str
    route: Literal["diagnosis", "knowledge_qa", "clarification"]
    parse_source: Literal["explicit", "rule", "llm"]
    task_id: str | None
    source_id: str
    diagnostic_level: Literal["study", "series", "unknown"]
    study_instance_uid: str | None
    series_instance_uid: str | None

    # ── 工具计划与采集结果 ──
    planned_tool_calls: list[dict]
    validated_tool_calls: list[dict]
    tool_results: dict
    evidence: list[str]

    # ── RAG 检索结果 ──
    retrieved_knowledge: list[str]

    # ── 诊断输出 ──
    deterministic_diagnosis: dict | None
    diagnosis: dict | None

    # ── 复核与审批 ──
    review_result: str | None
    review_attempts: int
    pending_action: str | None
    approval_status: str | None
    action_result: dict | None

    # ── 错误收集 ──
    errors: list[str]
```

**上下文截断策略**：

- 单个 Agent Run 只处理当前 `message`，不默认携带 20 轮历史消息；后续若增加会话追问，再单独设计消息摘要。
- `evidence` 每条不超过 120 字符，总数不超过 15 条。
- `tool_results` 只保留本次诊断的工具调用结果，不含历史运行的累积数据。
- `retrieved_knowledge` 最多保留 3 条，每条不超过 300 字。
- Checkpoint 仅保存 UID、状态、计数、时间、错误摘要和结构化诊断，不保存完整 DICOM Dataset 或原始日志。

### 7.1 记忆管理

Agent 的记忆分两层，各有明确边界，不混用：

| 记忆类型 | 载体 | 生命周期 | 用途 |
|---|---|---|---|
| 短期记忆（工作记忆） | LangGraph `AgentState` + SqliteSaver Checkpoint | 单个 Run 内 | 保存本次诊断的证据、工具结果、中间状态；支撑人工审批中断后从断点恢复 |
| 长期记忆（经验记忆） | ChromaDB 知识库中的“经验记忆”条目 | 跨 Run 持续累积 | 把人工确认过的真实诊断案例沉淀为可检索知识，回灌 RAG，逐步提升同类故障的解释质量 |

- **短期记忆**以 `run_id` 为边界，Run 之间无共享可变状态。上文的截断策略正是短期记忆的上下文管理手段：控制单 Run 内 token 增长，规避“历史越滚越长”导致的成本与幻觉。
- **长期记忆（经验记忆）**：每当一次诊断被人工审批确认（尤其是补拉重试的结果被验证），就将“现象—证据—根因—处理动作”结构化为一条经验记忆写入知识库（见 [11.4](#114-初始化数据量估算)）。它不是对话历史的堆积，而是被确认过的结论，因此可安全回灌到后续检索。这实现了 Agent 的“反思迭代”：错误或不确定的诊断经人工修正后，成为下一次检索的证据。
- 刻意**不做**跨 Run 的自动对话记忆：诊断是一次性的证据驱动任务，携带上一次 Run 的临时状态只会污染当前判断。需要延续时，由用户携带 `task_id`/UID 重新发起，而非依赖隐式记忆。

## 8. LangGraph 工作流

```text
START
  -> route_request
      |-- 显式标识或规则可无歧义解析 -> resolve_target（不调用 LLM）
      |-- 自然语言存在歧义 -> parse_with_llm -> resolve_target
      |-- 知识问答 -> retrieve_and_answer -> END
      |-- 信息不足 -> present_clarification -> END
  -> plan_tools               (LLM 通过 bind_tools 生成只读 Tool Calls)
  -> validate_tool_plan       (白名单、参数、层级和最小证据集校验)
  -> collect_evidence         (按依赖串行/并行执行)
  -> deterministic_diagnosis  (代码完成数量和故障阶段判断)
  -> retrieve_and_explain     (RAG + LLM，失败时返回规则诊断)
  -> validate_output          (代码 Guardrail；必要时最多修正一次)
      |-- passed       -> determine_action
      |-- insufficient -> present_uncertain_result -> END
      |-- correctable 且 review_attempts=0 -> retrieve_and_explain
  -> determine_action
      |-- 无写操作 -> END
      |-- 建议重试 -> human_approval
                       |-- 拒绝 -> END
                       |-- 同意 -> enqueue_retry -> END
```

**路由与工具执行规则**：

- `POST /agent/chat` 可同时接收自然语言 `message` 和可选结构化 UID。显式字段优先；没有显式字段时先使用 UUID/UID 规则解析，只有歧义场景调用 LLM。
- `plan_tools` 体现真实 Function Calling；`validate_tool_plan` 保证模型不能跳过核心证据或调用写工具。
- `collect_evidence` 的执行策略：
  - `query_pacs_target`、`query_task_context`、`query_receive_status` 三个只读工具**独立执行，并行调用**。
  - `query_queue_status` 依赖 `query_task_context` 的结果（仅在任务状态为 `in_queue` 时触发），串行于该结果之后。
  - 完整性数量比对不在本节执行；它是下游 `deterministic_diagnosis` 节点内的代码步骤，消费 `query_pacs_target` 与 `query_receive_status` 的结果，不占用一次 Tool Call。
- `review_attempts` 显式限制语义修正最多一次，避免条件边形成无限循环。
- `enqueue_retry` 只提交 RabbitMQ 下载任务并返回 `in_queue`，补拉完成后由用户再次发起诊断，不在当前 Run 中同步等待。

## 9. Function Calling 工具体系

当前定义五个业务工具，数量由业务边界决定，不以“凑齐工具数”为目标。四个只读工具可绑定给模型；写工具 `retry_pull_task` 与普通 Tool Calling 隔离。

数量比对（期望值 vs 实际值）不设计为可绑定工具，而是作为 `deterministic_diagnosis` 节点内的确定性代码步骤，见 [9.5](#95-完整性比对确定性代码步骤非工具)。这样做的理由：数量计算必须完全确定、不受模型影响，且它只是对 `query_pacs_target` 与 `query_receive_status` 已采集结果的重组，把它包装成模型可调用的工具只会增加一次无意义的 Tool Call 往返。

### 9.0 统一工具返回格式

所有工具继承同一个 Pydantic 基类：

```python
from pydantic import BaseModel, Field


class BaseToolOutput(BaseModel):
    """所有 Tool 的通用返回字段。"""
  success: bool              # 必填，禁止因默认值遗漏真实失败
  error: str = ""           # 空字符串 = 无错误
  retryable: bool = False   # 网络超时、连接失败等瞬时错误可重试
```

Agent 通过三个字段做出决策：
- `success = True` 表示工具已完成并得到可信业务结果；例如 C-ECHO 明确返回不可达时，可以是 `success=True, pacs_reachable=False`
- `success = False` 表示工具自身异常、参数非法或无法获得可信结果；诊断不得基于该工具做出确定结论
- `error` 非空 → 记录到 `evidence` 作为故障线索
- `retryable = True` → `collect_evidence` 可按工具策略自动重试

### 9.0.1 Tool Plan 校验

模型生成的 Tool Calls 必须经过以下确定性校验：

1. 只允许调用只读工具白名单；
2. 参数必须通过 Pydantic Schema；
3. Study/Series UID 必须与 `resolve_target` 的结果一致；
4. Study/Series 诊断的最小证据集缺失时由代码补齐；
5. 重复的同参数调用合并为一次；
6. 依赖工具未完成时，不执行后置工具。

### 9.1 `query_pacs_target`

用途：检查 PACS 连通性并查询目标 Study/Series 元数据。一次调用完成 C-ECHO + C-FIND。

参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source_id` | `str` | 是 | PACS 数据源 ID |
| `study_instance_uid` | `str | None` | 否 | Study UID |
| `series_instance_uid` | `str | None` | 否 | Series UID |

返回：

```python
class PacsTargetOutput(BaseToolOutput):
    pacs_reachable: bool = False
    study_exists: bool = False
    series_exists: bool | None = None
    parent_study_uid: str | None = None        # 查询 Series 时返回所属 Study
    expected_instance_count: int | None = None  # Study 级总数量或 Series 级数量
    modality: str | None = None
    dicom_status_summary: str | None = None     # DICOM 状态码与错误摘要
```

### 9.2 `query_task_context`

用途：查询任务及其关键状态时间线。

参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_id` | `str | None` | 否 | 任务 ID |
| `study_instance_uid` | `str | None` | 否 | Study UID |
| `series_instance_uid` | `str | None` | 否 | Series UID |

返回：

```python
class TaskSummary(BaseModel):
    task_id: str
    level: str
    status: str
    expected_count: int | None
    created_at: str | None
    queued_at: str | None
    download_started_at: str | None
    move_finished_at: str | None
    checked_at: str | None
    last_error: str | None
    can_retry: bool


class TaskContextOutput(BaseToolOutput):
  tasks: list[TaskSummary] = Field(default_factory=list)
```

### 9.3 `query_queue_status`

用途：查询 RabbitMQ 队列状态。**按需调用** —— 仅在任务处于 `in_queue` 或 Worker 状态不明时触发，不作为每次诊断的固定步骤。

返回：

```python
class QueueStatusOutput(BaseToolOutput):
    queue_accessible: bool = False
    queue_name: str = ""
    message_count: int | None = None
    consumer_count: int | None = None
    backlog_likely: bool = False   # message_count > 0 且 consumer_count == 0
```

### 9.4 `query_receive_status`

用途：统一查询 storescp 接收记录与本地扫描结果，一次调用获得接收端全貌。

参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `study_instance_uid` | `str` | 是 | Study UID |
| `series_instance_uid` | `str | None` | 否 | 指定时仅统计该 Series |

返回：

```python
class ReceiveStatusOutput(BaseToolOutput):
    storescp_received_count: int = 0    # storescp_image 表登记的接收记录数（非唯一 SOP）
    local_parsed_count: int = 0         # 成功解析的本地 DICOM 文件数
    local_unique_sop_count: int = 0     # 本地唯一 SOP Instance UID 数，唯一权威计数
    first_received_at: str | None = None
    last_received_at: str | None = None
    series_distribution: dict[str, int] = Field(default_factory=dict)
```

**计数口径（重要）**：现有 `storescp_image` 表以 `image_name` 为唯一键、不含 `sop_instance_uid` 列，因此它是接收登记的快照表，行数只能表示“storescp 登记了多少条接收记录”，既不等于真实接收事件次数，也不能去重为唯一 SOP 数。本项目据此确立单一口径：

- `storescp_received_count` 仅作为“接收端是否有活动”的信号，例如 `=0` 表示 storescp 从未登记该目标；
- **唯一 SOP 的权威来源只有 `local_dicom_file`（`sop_instance_uid` 唯一键）**，即 `local_unique_sop_count`，一切完整性判定以它为准；
- 不改动 `storescp_image` 表结构，避免为一次计数改动收图侧写入链路。

`query_receive_status` 只返回接收端事实，不直接输出 `missed_by_checker`。Checker 是否遗漏必须由规则节点结合任务状态、`checked_at` 和本地完整性综合判断。

### 9.5 完整性比对（确定性代码步骤，非工具）

完整性比对**不是可绑定工具**，而是 `deterministic_diagnosis` 节点内的一段纯代码逻辑。它的输入是前序已采集的 `query_pacs_target`（期望值）与 `query_receive_status`（实际值）结果，输出结构化的 `IntegrityResult` 写入 `AgentState`。**所有数量计算由代码完成，不交给大模型推算，也不额外产生一次 Tool Call。**

节点内产出结构：

```python
class IntegrityResult(BaseModel):
    diagnostic_level: str = "study"         # "study" | "series"
    expected: int | None = None             # 期望数量（PACS 或任务声明）
    storescp_received_count: int = 0        # storescp_image 登记记录数（仅活动信号，非权威计数）
    local_parsed: int = 0                   # 成功解析文件数
    local_unique_sop: int = 0               # 本地唯一 SOP 数，唯一权威完整性依据
    missing: int | None = None              # max(expected - local_unique_sop, 0)
    result: str = "unverified"              # "complete" | "incomplete" | "unverified"
    missing_series: list[str] = Field(default_factory=list)  # Study 级缺失 Series UID
    missing_instance_count: int | None = None  # Series 级：缺失的 Instance 数量
```

完整性计算规则：

$$
missing = \max(expected - local\_unique\_sop, 0)
$$

- `expected is None` 时，`missing=None` 且 `result="unverified"`；
- `local_unique_sop` 是唯一权威完整性计数；`storescp_received_count` 只作接收端活动信号，不参与 complete/incomplete 判定；
- Study 级额外比较 PACS Series UID 集合与本地 Series UID 集合；
- Series 级所有计数必须先按目标 Series UID 过滤。

> 面试要点：这里刻意区分“工具”与“节点内代码步骤”。可绑定工具的价值在于让模型决定“要不要采集某类证据”；而数量比对没有决策空间、必须确定性执行，因此放进节点而非暴露为工具，是“工具体系”边界设计的一个具体取舍，而不是把所有能力都做成工具。

### 9.6 `retry_pull_task`

用途：经人工审批后重试现有补拉任务。**这是唯一的写操作工具。**

参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_id` | `str` | 是 | 任务 ID |
| `force` | `bool` | 否 | 是否强制重试非失败任务 |
| `idempotency_key` | `str` | 是 | 由 `run_id + task_id + action` 派生的短期锁键 |

返回：

```python
class RetryOutput(BaseToolOutput):
    task_id: str = ""
    new_status: str = ""
    audit_log_id: str = ""
```

调用限制：

- 只能在 `human_approval` 节点通过后由 `enqueue_retry` 节点调用；
- 必须校验任务当前状态（非 `fail` 且非 `force` 时拒绝）；
- 使用 Redis `SET idempotency:{key} {run_id} NX EX 300` 作为 300 秒短期并发锁，防止审批接口并发重复提交；
- Redis 锁不声明为永久幂等保证。锁过期后仍通过 Agent Run 的审批状态和任务当前状态拒绝重复执行；
- 必须记录审计日志到 MySQL `agent_action_audit` 表（字段：`run_id`, `task_id`, `action`, `operator`, `timestamp`, `result`）；
- 执行后只返回 RabbitMQ 提交结果和 `new_status=in_queue`。补拉完成后通过新的 Chat Run 重新诊断，不在当前 Run 中同步复查。

### 9.7 MCP 暴露薄层（可选，不改业务逻辑）

> 定位：这是一个**可选的适配层**，把已有的四个只读工具按 [MCP（Model Context Protocol）](https://modelcontextprotocol.io) 标准对外暴露，用于覆盖“MCP 协议基础”。它**不改动任何业务逻辑、不新增诊断能力**，只是给现有工具换一种标准化的接入方式。默认关闭，作为演示/加分项按需启用。

**为什么可以低成本做**：本项目的工具已经具备 MCP 要求的两个前提——参数有 Pydantic Schema（可直接映射为 MCP tool 的 `inputSchema`），返回有统一的 `BaseToolOutput`（可直接序列化为 MCP tool result）。因此只需一个 `mcp_server` 薄封装即可，无需重写工具。

**暴露范围**：仅四个只读工具（`query_pacs_target`、`query_task_context`、`query_queue_status`、`query_receive_status`）。写工具 `retry_pull_task` **不经 MCP 暴露**——它必须走 `human_approval` 审批节点，脱离审批的任何调用通道都违反安全边界（[第 14 节](#14-安全边界)）。

**两个方向的价值点，二选一或都做**：

| 方向 | 说明 | 命中的 JD 能力 |
|---|---|---|
| 作为 MCP Server | 把四个只读工具暴露为标准 MCP Server，任意 MCP 客户端（如 Claude Desktop、其他 Agent）可复用本项目的 PACS 诊断能力 | 工具注册、MCP 协议基础 |
| 作为 MCP Client（可选延伸） | LangGraph 通过 `langchain-mcp-adapters` 反向接入外部 MCP Server 提供的工具，验证 MCP 双向互通 | MCP 协议基础、工具调用体系 |

**边界声明**：MCP 层与 LangGraph 内部的 `bind_tools()` Function Calling 是**两条并行的暴露通道，共用同一份工具实现**。启用 MCP 不改变 [第 8 节工作流](#8-langgraph-工作流)，也不改变受约束 Function Calling 策略（[第 4.3 节](#43-function-calling-策略)）。这是“用标准协议复用已有能力”，不是“为了用 MCP 而重构”。

## 10. Prompt 设计

### 10.1 System Prompt

```
你是 PACS-DICOM 补拉链路诊断 Agent。

## 角色边界
- 只诊断 PACS 查询 → 任务创建 → C-MOVE 传输 → storescp 接收 → 本地校验 这条链路。
- 不进行任何临床医学判断。
- 不给出 PACS、RabbitMQ、数据库或容器配置的修改命令。

## 证据规则
- 所有结论必须引用至少一条工具输出或知识库片段。
- 区分三个等级：
  "已确认" = 多条独立工具证据交叉验证一致
  "高可能" = 一条工具证据支持，但缺少交叉验证
  "证据不足" = 工具结果为空或全部失败
- 工具结果为 None 或 success=false 时，不得基于该工具做确定性归因。
- 工具结果与模型推断冲突时，以工具结果为准。
- 不得自行构造 UID、数量或 DICOM 状态码。

## 层级规则
- Series 级诊断的 received/local/missing 统计必须只针对目标 Series。
- 禁止用 Study 级总数判定 Series 级完整性。
- expected count 缺失时，result 必须为 "unverified"，不得判定 complete。

## 输出要求
- 输出 JSON，字段见下方 Schema。
- summary 不超过 150 字。
- evidence 中每条标注工具来源，如 "[query_pacs_target] C-ECHO ok, latency=45ms"。
```

### 10.2 诊断输出 Schema

```python
class DiagnosisOutput(BaseModel):
    diagnostic_level: str               # "study" | "series"
    summary: str                        # 不超过 150 字
    fault_stage: str                    # "pacs_connectivity" | "pacs_query" | "task_queue"
                                        # | "cmove_transfer" | "storescp_receive" | "local_check"
                                        # | "complete" | "unknown"
    confidence: str                     # "confirmed" | "high" | "uncertain"
    evidence: list[str]                 # 每条标注工具来源
    likely_causes: list[str]            # 按可能性排序
    missing_evidence: list[str]         # 缺失的关键证据
    next_actions: list[str]             # 建议的下一步动作
    retry_recommended: bool
    approval_required: bool             # retry_recommended 为 True 时同步为 True
```

### 10.3 输出复核

`validate_output` 以代码 Guardrail 为主：

1. 代码校验是否混淆 Study 和 Series 的统计数字；
2. Pydantic Validator 校验 `result == "unverified"` 时 `confidence == "uncertain"`；
3. 代码校验 diagnosis 是否引用 `success=false` 的工具；
4. Pydantic Validator 校验 `retry_recommended` 与 `approval_required` 同步；
5. 只有“可能原因是否受到证据语义支持”可选使用 LLM 复核。

确定性约束失败时优先由代码修正；语义问题可触发一次 `retrieve_and_explain`，并将 `review_attempts` 从 0 更新为 1。仍不通过则返回 `insufficient`，不得继续循环。

### 10.4 少样本示例

以下 9 个案例覆盖典型故障场景，全部作为自动化测试与候选 Few-shot。生产 Prompt 每次最多按故障阶段动态选择 3 个案例，不固定注入全部案例。

---

#### Case 1: PACS 不可达

**用户输入**:
```
question: "诊断 study 1.2.840.113619.2.55.1", source_id: "orthanc-local"
```

**工具返回**:
```
query_pacs_target: { success: true, pacs_reachable: false, study_exists: false,
  dicom_status_summary: "C-ECHO failed: Association rejected, status=0x0001" }
query_task_context: { success: true, tasks: [] }
query_receive_status: { success: true, storescp_received_count: 0, local_parsed_count: 0,
  local_unique_sop_count: 0 }
integrity_result: { success: true, diagnostic_level: "study", expected: null,
  storescp_received_count: 0, local_parsed: 0, local_unique_sop: 0, missing: null, result: "unverified" }
```

**诊断输出**:
```json
{
  "diagnostic_level": "study",
  "summary": "PACS orthanc-local C-ECHO 失败，补拉链路无法开始。PACS 服务不可达或 AE Title 配置有误。",
  "fault_stage": "pacs_connectivity",
  "confidence": "confirmed",
  "evidence": [
    "[query_pacs_target] C-ECHO failed, status=0x0001, pacs_reachable=false",
    "[query_receive_status] storescp_received_count=0, local_unique_sop_count=0",
    "[integrity] result=unverified"
  ],
  "likely_causes": [
    "PACS 服务未启动或网络不通",
    "AE Title 配置错误（PULLDATA_SCU ↔ ORTHANC 不匹配）",
    "PACS 端口 4242 不可达"
  ],
  "missing_evidence": ["PACS 服务端日志", "网络连通性结果"],
  "next_actions": [
    "检查 PACS 服务运行状态",
    "核对 PACS 与本地 AE Title 配置"
  ],
  "retry_recommended": false,
  "approval_required": false
}
```

---

#### Case 2: Study 不存在

**用户输入**:
```
question: "诊断 study 9.9.9.9.9.9.9.9", source_id: "orthanc-local"
```

**工具返回**:
```
query_pacs_target: { success: true, pacs_reachable: true, study_exists: false,
  dicom_status_summary: "C-FIND returned 0 matching records" }
query_task_context: { success: true, tasks: [] }
query_receive_status: { success: true, storescp_received_count: 0,
  local_parsed_count: 0, local_unique_sop_count: 0 }
integrity_result: { success: true, result: "unverified" }
```

**诊断输出**:
```json
{
  "diagnostic_level": "study",
  "summary": "PACS 可达，但 C-FIND 未查询到 Study 9.9.9.9.9.9.9.9。该 Study 在 PACS 中不存在或 UID 有误。",
  "fault_stage": "pacs_query",
  "confidence": "confirmed",
  "evidence": [
    "[query_pacs_target] C-ECHO ok, C-FIND returned 0 records, study_exists=false",
    "[query_task_context] 无匹配任务",
    "[query_receive_status] 本地无文件"
  ],
  "likely_causes": [
    "StudyInstanceUID 输入错误",
    "该 Study 已从 PACS 中删除",
    "PACS 查询权限不足"
  ],
  "missing_evidence": ["PACS 管理界面直接查询结果"],
  "next_actions": [
    "核对 StudyInstanceUID 是否正确",
    "在 Orthanc Web UI 中搜索该 Study"
  ],
  "retry_recommended": false,
  "approval_required": false
}
```

---

#### Case 3: Series 不存在或归属冲突

**用户输入**:
```
question: "诊断 task f47ac10b-58cc-4372-a567-0e02b2c3d479", source_id: "orthanc-local"
```

**工具返回**:
```
query_task_context: { success: true, tasks: [{
  task_id: "f47ac10b-58cc-4372-a567-0e02b2c3d479", level: "series_level",
  status: "fail", study_instance_uid: "1.2.840.113619.2.55.1",
  series_instance_uid: "4.5.6.7.8.9", expected_count: 120, last_error: "Series not found in PACS",
  can_retry: false
}]}
query_pacs_target: { success: true, pacs_reachable: true, study_exists: true,
  series_exists: false, parent_study_uid: "1.2.840.113619.2.55.1",
  dicom_status_summary: "C-FIND Series returned 0 records" }
```

**诊断输出**:
```json
{
  "diagnostic_level": "series",
  "summary": "Study 存在于 PACS，但目标 Series 4.5.6.7.8.9 在 C-FIND 中未找到。任务 level=series_level 的 Series 与 PACS 不匹配。",
  "fault_stage": "pacs_query",
  "confidence": "confirmed",
  "evidence": [
    "[query_task_context] task=f47ac10b..., level=series_level, series_uid=4.5.6.7.8.9, status=fail, last_error='Series not found in PACS'",
    "[query_pacs_target] study_exists=true, series_exists=false, C-FIND Series returned 0 records"
  ],
  "likely_causes": [
    "SeriesInstanceUID 不存在于 PACS 中",
    "该 Series 已从 PACS 删除或从未上传"
  ],
  "missing_evidence": [],
  "next_actions": [
    "在 PACS 中确认 Study 下的 Series 列表",
    "如 Series 确实不存在，取消任务并通知上游"
  ],
  "retry_recommended": false,
  "approval_required": false
}
```

---

#### Case 4: 任务停留 in_queue

**用户输入**:
```
question: "诊断 task a1b2c3d4-e5f6-7890-abcd-ef1234567890", source_id: "orthanc-local"
```

**工具返回**:
```
query_task_context: { success: true, tasks: [{
  task_id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890", level: "study_level",
  status: "in_queue", expected_count: 240, created_at: "2026-07-15T08:00:00",
  queued_at: "2026-07-15T08:00:01", download_started_at: null,
  move_finished_at: null, last_error: null, can_retry: false
}]}
query_pacs_target: { success: true, pacs_reachable: true, study_exists: true,
  expected_instance_count: 240 }
query_queue_status: { success: true, queue_accessible: true, queue_name: "download_tasks",
  message_count: 5, consumer_count: 0, backlog_likely: true }
```

**诊断输出**:
```json
{
  "diagnostic_level": "study",
  "summary": "任务 a1b2c3d4... 创建后停留在 in_queue 超过 8 小时。RabbitMQ 队列有 5 条消息但 consumer_count=0，Worker 未消费。",
  "fault_stage": "task_queue",
  "confidence": "confirmed",
  "evidence": [
    "[query_task_context] status=in_queue, created_at=2026-07-15T08:00:00",
    "[query_queue_status] message_count=5, consumer_count=0, backlog_likely=true",
    "[query_pacs_target] pacs_reachable=true, study_exists=true"
  ],
  "likely_causes": [
    "pull-data-worker 未运行或已崩溃",
    "RabbitMQ consumer 注册失败"
  ],
  "missing_evidence": ["Worker 进程日志", "Worker 容器状态"],
  "next_actions": [
    "检查下载 Worker 运行状态",
    "检查 Worker 消费者注册与运行日志"
  ],
  "retry_recommended": false,
  "approval_required": false
}
```

---

#### Case 5: C-MOVE 后 storescp 数量为 0

**用户输入**:
```
question: "诊断 task b2c3d4e5-f6a7-8901-bcde-f12345678901", source_id: "orthanc-local"
```

**工具返回**:
```
query_task_context: { success: true, tasks: [{
  task_id: "b2c3d4e5-f6a7-8901-bcde-f12345678901", level: "study_level",
  status: "downloaded", expected_count: 240, move_finished_at: "2026-07-15T08:05:00",
  last_error: null, can_retry: true
}]}
query_pacs_target: { success: true, pacs_reachable: true, study_exists: true,
  expected_instance_count: 240 }
query_receive_status: { success: true, storescp_received_count: 0, local_parsed_count: 0,
  local_unique_sop_count: 0 }
integrity_result: { success: true, diagnostic_level: "study", expected: 240,
  storescp_received_count: 0, local_parsed: 0, local_unique_sop: 0,
  missing: 240, result: "incomplete", missing_series: [] }
```

**诊断输出**:
```json
{
  "diagnostic_level": "study",
  "summary": "C-MOVE 已执行（任务状态=downloaded），但 storescp 接收数量为 0。数据在传输阶段完全丢失。",
  "fault_stage": "storescp_receive",
  "confidence": "confirmed",
  "evidence": [
    "[query_task_context] status=downloaded, expected_count=240",
    "[query_receive_status] storescp_received_count=0, local_unique_sop_count=0",
    "[integrity] expected=240, local_unique_sop=0, missing=240, result=incomplete"
  ],
  "likely_causes": [
    "PACS C-MOVE 目标 AE Title 配置错误（Move Destination 不是 PULLDATA_SCP）",
    "storescp 端口 11112 不可达或被防火墙拦截",
    "PACS C-MOVE 返回 success 但实际未发送数据"
  ],
  "missing_evidence": ["storescp 服务日志", "PACS C-MOVE 详细响应"],
  "next_actions": [
    "核对 Move Destination 的 AE Title 和端口配置",
    "检查 storescp 服务运行状态与接收日志"
  ],
  "retry_recommended": true,
  "approval_required": true
}
```

---

#### Case 6: storescp 已收图但 Checker 未更新状态

**用户输入**:
```
question: "诊断 task c3d4e5f6-a7b8-9012-cdef-123456789012", source_id: "orthanc-local"
```

**工具返回**:
```
query_task_context: { success: true, tasks: [{
  task_id: "c3d4e5f6-a7b8-9012-cdef-123456789012", level: "study_level",
  status: "downloaded", expected_count: 240, move_finished_at: "2026-07-15T08:10:00",
  checked_at: null,
  last_error: null, can_retry: true
}]}
query_receive_status: { success: true, storescp_received_count: 240,
  local_parsed_count: 240, local_unique_sop_count: 240,
  first_received_at: "2026-07-15T08:12:00", last_received_at: "2026-07-15T08:13:00" }
integrity_result: { success: true, diagnostic_level: "study", expected: 240,
  storescp_received_count: 240, local_parsed: 240, local_unique_sop: 240,
  missing: 0, result: "complete" }
```

**诊断输出**:
```json
{
  "diagnostic_level": "study",
  "summary": "storescp 已成功接收 240 个 Instance，本地文件完整，但 Checker 未更新任务状态为 success。Checker 可能未运行或统计逻辑异常。",
  "fault_stage": "local_check",
  "confidence": "confirmed",
  "evidence": [
    "[query_receive_status] storescp_received_count=240, local_unique_sop_count=240",
    "[integrity] expected=240, local_unique_sop=240, result=complete",
    "[query_task_context] status=downloaded, checked_at=null"
  ],
  "likely_causes": [
    "pull-data-checker 容器未运行或已崩溃",
    "Checker 查询条件与 storescp 写入记录不一致",
    "Checker fail_after_seconds 超时过早触发"
  ],
  "missing_evidence": ["Checker 容器日志"],
  "next_actions": [
    "检查 Checker 服务运行状态",
    "查看 Checker 日志确认扫描周期是否正常"
  ],
  "retry_recommended": false,
  "approval_required": false
}
```

---

#### Case 7: Study 部分 Series 缺失

**用户输入**:
```
question: "诊断 study 1.2.840.113619.2.55.1", source_id: "orthanc-local"
```

**工具返回**:
```
query_pacs_target: { success: true, pacs_reachable: true, study_exists: true,
  expected_instance_count: 240 }
query_task_context: { success: true, tasks: [{ task_id: "d4e5f6a7...",
  level: "study_level", status: "downloaded", expected_count: 240 }]}
query_receive_status: { success: true, storescp_received_count: 180,
  local_parsed_count: 180, local_unique_sop_count: 180,
  series_distribution: {"1.2.3.4.5": 120, "1.2.3.4.6": 60} }
integrity_result: { success: true, diagnostic_level: "study", expected: 240,
  storescp_received_count: 180, local_parsed: 180, local_unique_sop: 180,
  missing: 60, result: "incomplete",
  missing_series: ["1.2.3.4.7"] }
```

**诊断输出**:
```json
{
  "diagnostic_level": "study",
  "summary": "Study 缺失 1 个 Series（UID=1.2.3.4.7），共缺失 60 个 Instance。已有 2 个 Series（180 个 Instance）接收完整。",
  "fault_stage": "cmove_transfer",
  "confidence": "confirmed",
  "evidence": [
    "[query_pacs_target] study_exists=true, expected=240",
    "[query_receive_status] storescp_received_count=180, local_unique_sop_count=180",
    "[integrity] expected=240, local_unique_sop=180, missing=60, result=incomplete, missing_series=['1.2.3.4.7']"
  ],
  "likely_causes": [
    "C-MOVE Study 级拉取时 PACS 漏传了 Series 1.2.3.4.7",
    "该 Series 在 PACS 中存在但 C-MOVE 传输失败"
  ],
  "missing_evidence": ["PACS C-MOVE per-series 响应日志"],
  "next_actions": [
    "对缺失的 Series 1.2.3.4.7 创建 Series 级补拉任务"
  ],
  "retry_recommended": true,
  "approval_required": true
}
```

---

#### Case 8: Series Instance 数量不完整

**用户输入**:
```
question: "诊断 series 1.2.3.4.5，study 1.2.840.113619.2.55.1", source_id: "orthanc-local"
```

**工具返回**:
```
query_pacs_target: { success: true, pacs_reachable: true, study_exists: true,
  series_exists: true, parent_study_uid: "1.2.840.113619.2.55.1",
  expected_instance_count: 120 }
query_receive_status: { success: true, storescp_received_count: 95,
  local_parsed_count: 95, local_unique_sop_count: 95,
  series_distribution: {"1.2.3.4.5": 95} }
integrity_result: { success: true, diagnostic_level: "series", expected: 120,
  storescp_received_count: 95, local_parsed: 95, local_unique_sop: 95,
  missing: 25, result: "incomplete",
  missing_instance_count: 25 }
```

**诊断输出**:
```json
{
  "diagnostic_level": "series",
  "summary": "Series 1.2.3.4.5 期望 120 个 Instance，实际仅接收 95 个，缺失 25 个。归属关系验证通过。",
  "fault_stage": "cmove_transfer",
  "confidence": "confirmed",
  "evidence": [
    "[query_pacs_target] series_exists=true, parent_study_uid=1.2.840.113619.2.55.1, expected=120",
    "[query_receive_status] storescp_received_count=95, local_unique_sop_count=95（仅统计目标 Series）",
    "[integrity] level=series, expected=120, local_unique_sop=95, missing=25, result=incomplete"
  ],
  "likely_causes": [
    "C-MOVE 传输过程中 25 个 Instance 丢失（网络中断或 PACS 发送失败）",
    "PACS 侧该 Series 的实际 Instance 数不足 120"
  ],
  "missing_evidence": ["PACS 侧 per-instance 发送记录"],
  "next_actions": [
    "对目标 Series 发起受控重试"
  ],
  "retry_recommended": true,
  "approval_required": true
}
```

---

#### Case 9: expected count 缺失，结果为 unverified

**用户输入**:
```
question: "诊断 study 1.2.840.113619.2.55.3", source_id: "orthanc-local"
```

**工具返回**:
```
query_pacs_target: { success: true, pacs_reachable: true, study_exists: true,
  expected_instance_count: null,
  dicom_status_summary: "C-FIND study returned 1 record, but NumberOfStudyRelatedInstances is empty" }
query_receive_status: { success: true, storescp_received_count: 200,
  local_parsed_count: 200, local_unique_sop_count: 200 }
integrity_result: { success: true, diagnostic_level: "study", expected: null,
  storescp_received_count: 200, local_parsed: 200, local_unique_sop: 200,
  missing: null, result: "unverified" }
```

**诊断输出**:
```json
{
  "diagnostic_level": "study",
  "summary": "PACS 返回了 Study 但未提供期望 Instance 数量，无法判定完整性。本地已接收 200 个 Instance，但缺乏基准值比对。",
  "fault_stage": "unknown",
  "confidence": "uncertain",
  "evidence": [
    "[query_pacs_target] study_exists=true, expected_instance_count=null（PACS 未返回 NumberOfStudyRelatedInstances）",
    "[query_receive_status] storescp_received_count=200, local_unique_sop_count=200",
    "[integrity] expected=null, result=unverified"
  ],
  "likely_causes": [
    "PACS 侧 DICOM 数据缺少 NumberOfStudyRelatedInstances 标签",
    "数据源本身不提供期望数量"
  ],
  "missing_evidence": ["PACS 中该 Study 的 Series 列表与各 Series Instance 数量"],
  "next_actions": [
    "手动查询 PACS 中 Study 的 Series 组成",
    "逐 Series 创建诊断以获取 per-series 期望数量"
  ],
  "retry_recommended": false,
  "approval_required": false
}
```

## 11. RAG 知识库

### 11.1 技术栈

| 组件 | 选型 | 说明 |
|---|---|---|
| Embedding 模型 | DashScope `text-embedding-v4` | 阿里云兼容 OpenAI API 格式，1024 维向量 |
| 向量数据库 | **ChromaDB** | 嵌入式向量库（基于 SQLite），零运维，与单体部署一致 |
| 重排模型 | DashScope `qwen3-rerank` | 可配置启用，只有离线评估证明有收益时进入默认链路 |
| 分块器 | LangChain `RecursiveCharacterTextSplitter` | 仅用于长文档；短 SOP 条目作为独立知识原子 |

Embedding 配置：

```python
from langchain_openai import OpenAIEmbeddings

embeddings = OpenAIEmbeddings(
    model="text-embedding-v4",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key=os.environ["DASHSCOPE_API_KEY"],
    check_embedding_ctx_length=False,
)
```

### 11.2 检索管道

采用“元数据过滤 → 向量召回 → 可选重排”的检索流程。

```
知识库全量（~55 条）
   │
   ▼ 元数据过滤 (Metadata Filtering)
   │  ChromaDB 的 where 条件按结构化字段筛选
   │  例：{"category": "cmove_transfer"} 只取 C-MOVE 相关条目
   │  ~55 → ~15 条
   │
   ▼ 向量召回 (Vector Recall)
   │  Embedding 语义相似度 Top-K
   │  例：k=10
   │  ~15 → 10 条
   │
  ▼ 可选重排 (Reranking)
  │  候选数量和相似度达到配置阈值时调用 qwen3-rerank
  │  10 → 3 条（最终注入 LLM Prompt）
   │
   ▼
LLM Prompt（retrieved_knowledge）
```

**元数据过滤**：按结构化标签（`category`、`stage`、`dicom_operation` 等）预先缩小检索范围，排除明显不相关的类别。实现方式：在 ChromaDB 写入时为每条知识附带 metadata，检索时通过 `collection.query(where={"category": "cmove_transfer"})` 过滤。

**向量召回**：将用户 question + 工具返回摘要拼接为查询文本，通过 Embedding 编码为向量，在过滤后的文档子集中计算余弦相似度，取 Top-K（K=10）。

**可选重排**：候选较少或 Top-3 相似度已达到阈值时直接返回，避免额外模型调用；只有候选较多且排序不稳定时调用 `qwen3-rerank`，取 Top-3 注入 LLM 上下文。是否默认启用由离线评估决定。

### 11.3 知识库内容与分块策略

知识库只保存稳定知识，不保存实时任务状态。按“故障现象—所需证据—可能原因—处理动作”组织。200～500 字的故障 SOP 直接作为知识原子；仅对较长的协议文档使用 LangChain `RecursiveCharacterTextSplitter`：

```python
from langchain.text_splitter import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    separators=["\n## ", "\n### ", "\n#### ", "\n", "。", ".", " "],
)
```

每个 chunk 的 metadata 包含：`category`（故障类别）、`stage`（链路阶段）、`dicom_operation`（相关 DICOM 操作）、`symptom`（故障现象关键词）。

### 11.4 初始化数据量估算

| 分类 | 条目数 | 说明 |
|---|---|---|
| DICOM 协议基础（C-ECHO/C-FIND/C-MOVE/C-STORE） | 8 | 每种操作：目的、请求/响应模型、常见问题 |
| DIMSE 常见状态码 | 12 | 0x0000, 0xA700, 0xA900, 0xC000 等 |
| AE Title / Move Destination / 端口配置 | 5 | 配置项说明与常见错误 |
| PullData 状态机 | 8 | 7 个状态 + 转换规则 |
| Study/Series/Instance 层级关系 | 4 | 数据模型层级与统计规则 |
| storescp / local scan / checker 职责边界 | 6 | 各组件输入/输出/失败模式 |
| 常见故障 SOP（对应 9 个 few-shot 场景） | 9 | 每场景 1 条："现象—证据—原因—动作" |
| 经验记忆（人工确认案例，初始化） | 3 | Agent 长期记忆，运行后逐步累积，见 [7.1](#71-记忆管理) |
| **总计** | **~55 条** | |

每条约 200-500 字，总知识库约 **15K-25K 字**，ChromaDB 单文件存储即可。

其中「经验记忆」是唯一会随运行增长的类别：初始化仅 3 条，之后每次经人工审批确认的诊断都追加一条，metadata 额外标注 `source="experience"` 与来源 `run_id`，便于与静态知识区分和评估其检索贡献。这是 Agent 长期记忆的物理载体，与 [7.1 记忆管理](#71-记忆管理)呼应。

### 11.5 检索评估

RAG 必须通过固定问题集评估，而不是只验证“能够返回文档”：

- 指标：Recall@3、MRR@5、平均检索延迟和单次检索成本；
- 对照：无 RAG、仅向量召回、向量召回 + 重排；
- 仅当重排提升命中率且额外延迟可接受时，才默认启用 Reranker；
- 记录最终注入 Prompt 的文档数量与 Token 数，避免知识上下文无限增长。

## 12. 异常处理与降级

### 12.1 工具级异常

- 所有工具返回统一的 `BaseToolOutput`（`success`, `error`, `retryable`）。
- MySQL、Redis 临时连接失败：`retryable=True`，`collect_evidence` 节点自动重试最多 2 次（间隔 1s / 3s）。
- PACS C-ECHO 超时：最多重试 2 次（间隔 1s / 3s），失败后将 `pacs_reachable` 标记为 `false`。避免持续增加 PACS 压力。
- RabbitMQ 查询失败不影响诊断主流程（`query_queue_status` 为按需工具）。

### 12.2 Agent 级降级

**LLM Fallback 机制**（企业级工程优化关键设计）：

当 LLM（DashScope / OpenAI 兼容 API）不可用时（网络超时、配额耗尽、5xx 错误），`retrieve_and_explain` 节点执行以下降级：

1. 停止 LLM 解释和语义复核；
2. 将 `deterministic_diagnosis` 的输出直接作为最终诊断结果；
3. 在 `summary` 前追加 `[LLM Fallback]` 标记。
4. 在 `evidence` 中追加 `"[system] LLM 不可用，本次诊断为规则诊断结果"`。

该机制确保 Agent 在 LLM 故障时仍然可用，不丢失核心诊断能力。

### 12.3 诊断级异常

- 不按失败工具数量机械降级，而是按目标结论校验最小证据集；缺少该结论必需证据时返回 `confidence="uncertain"`。
- PACS 不可达至少需要 C-ECHO 结果；完整性结论至少需要 expected 与目标范围内的本地唯一 SOP 数；Checker 异常至少需要任务状态、`checked_at` 与本地完整性。
- 证据不足时 `validate_output` 返回 `insufficient`，直接呈现不确定结果。
- 语义修正最多一次，由 `review_attempts` 显式限制。

### 12.4 可观测性与成本度量

Token 成本与推理延迟不能只靠“声称优化了”，必须可量化。本项目用 LangSmith 作为度量基线（依赖已随 LangChain 引入）：

- **链路追踪**：每次 Run 的完整节点执行链（route_request → … → determine_action）、每次 LLM 调用的 prompt/completion token、每个工具调用的耗时都被记录为一条 trace，可回溯定位“慢在哪个节点、贵在哪次调用”。
- **成本度量指标**：单次诊断的总 token 数、LLM 调用次数、端到端延迟、各节点耗时占比。这些是 [第 15 节验收标准](#15-验收标准)中 Token/延迟项的数据来源。
- **优化闭环**：先测基线，再验证各项优化的真实收益——按需 Few-shot（最多 3 个而非全量注入）、证据摘要截断、规则优先避免冗余 LLM 解析、可选重排。每项优化前后的 token/延迟差值都能从 trace 读出，避免“凭感觉优化”。
- LangSmith 仅作可观测层，**不在关键路径上**：其不可用不影响诊断主流程，与 [12.2 LLM Fallback](#122-agent-级降级) 同样遵循“可观测组件故障不拖垮业务”的原则。

### 12.5 并发与多用户隔离

- **Run 级隔离**：每次请求以 `run_id` 为唯一边界，状态全部落在该 Run 的 `AgentState` 与 Checkpoint 内，Run 之间无共享可变状态，因此天然支持多用户并发而不串扰。
- **水平扩展**：Agent Worker 是无状态消费者，多个 Worker 实例可同时消费同一 `agent_runs` 队列，靠 RabbitMQ 的消息分发实现负载分担；扩容只需增加 Worker 副本，无需改代码。
- **写操作并发防护**：审批触发的补拉写操作用 Redis `SET idempotency:{key} NX EX 300` 短期锁防止并发重复提交（见 [9.6](#96-retry_pull_task)），锁键由 `run_id+task_id+action` 派生，天然按用户/任务隔离。
- **异步任务处理**：`/agent/chat` 立即返回 `run_id`（202），实际诊断在 Worker 异步执行，API 进程不被长任务占用，这是支撑高并发的基础形态。

## 13. API 设计

### 13.1 单一 Chat 入口

取消 `/agent/diagnose` 和 `/agent/retry`，只保留 `/agent/chat` 作为初始入口。查询 Run 和审批属于异步资源操作，继续使用独立端点：

```
POST /agent/chat
  → 202 Accepted
  → {"run_id": "uuid", "status": "running"}

GET /agent/runs/{run_id}
  → 200 OK
  → {
      "run_id": "uuid",
      "status": "running" | "completed" | "failed" | "awaiting_approval",
      "route": "diagnosis" | "knowledge_qa" | "clarification",
      "diagnosis": { ... DiagnosisOutput ... },
      "proposed_action": { ... } | null,
      "approval_status": "pending" | "approved" | "rejected" | null
    }

POST /agent/runs/{run_id}/action
  → {"action": "approve" | "reject"}
  → 200 OK
  → {"run_id": "uuid", "status": "completed" | "rejected"}
```

流程：

1. 客户端调用 `POST /agent/chat`，API 创建 `agent_run` 并向 RabbitMQ `agent_runs` 队列投递 `run_id`，立即返回 202；
2. Agent Worker 执行 LangGraph，API 进程不使用后台线程承载长任务；
3. LangGraph 根据显式字段、规则解析和必要时的 LLM 解析决定请求路由；
4. 客户端通过 `GET /agent/runs/{run_id}` 轮询；
5. 当状态为 `awaiting_approval` 时，同时返回 `diagnosis` 与 `proposed_action`，用户基于诊断证据决定是否审批；
6. 审批通过后提交重试任务，Run 记录 `action_submitted`，不等待补拉完成。

### 13.2 Chat 请求参数

`POST /agent/chat` 接受：

```json
{
  "message": "请诊断这个补拉任务",
  "task_id": "uuid | null",
  "study_instance_uid": "str | null",
  "series_instance_uid": "str | null",
  "source_id": "str (default: orthanc-local)"
}
```

`message` 必填，结构化标识可选：

- 存在结构化标识时直接进入 `resolve_target`，不调用 LLM 做实体抽取；
- 没有结构化标识时，`route_request` 先使用规则识别 UUID 和 DICOM UID；
- 规则结果唯一且无冲突时不调用 LLM；
- 信息有歧义时才调用 LLM 结构化解析；
- 属于 DICOM/PullData 稳定知识问题时进入 RAG 问答；
- 无法获得诊断目标时返回 `clarification`，不执行业务工具。

### 13.3 为什么审批状态必须返回诊断

`awaiting_approval` 表示系统正在请求用户授权写操作。用户必须同时看到诊断结论、证据、重试原因和拟执行动作，否则审批会退化为“盲批”，不满足 Human-in-the-loop 的安全目的。

LangGraph Checkpoint 内虽然已有 diagnosis，但轮询客户端无法直接读取内部状态，因此 API 必须显式返回 `diagnosis` 与 `proposed_action`。只有 `running` 状态可以暂不返回诊断；`awaiting_approval` 必须返回已生成结果。

## 14. 安全边界

- 本次简历项目不建设敏感字段识别、脱敏或合规模块，也不将其作为验收项；默认工具仅返回规格定义的补拉链路字段。
- 不允许模型直接访问数据库连接或文件系统。
- 所有工具参数必须经过 Pydantic 校验。
- 只读工具可自动执行。
- 重试属于写操作，必须人工审批（`human_approval` 节点）。
- Redis 300 秒锁只用于防止并发重复提交，不对外宣称永久幂等。
- 不支持自动修改 PACS、RabbitMQ、数据库或容器配置。
- 不允许生成任意 SQL、Shell 或 Docker 命令。

## 15. 验收标准

### 15.1 Study 级

- 能按 StudyInstanceUID 查询 PACS、任务、收图和完整性状态。
- 能识别缺失 Series 或 Study 总数量不完整。
- expected count 缺失时返回 `unverified`，不得判定成功。

### 15.2 Series 级

- 能按 SeriesInstanceUID 自动解析所属 Study。
- 能验证 Study 与 Series 的归属关系。
- 所有接收、扫描和完整性统计只针对目标 Series。
- 能识别 Series 不存在、未下载、部分下载和完整下载。

### 15.3 Agent

- 工具参数合法率达到 100%。
- LangChain 模型能够产生真实只读 Tool Calls，且工具计划必须通过代码策略校验。
- 显式标识或规则可无歧义解析时，不调用 LLM 做意图和实体解析。
- 诊断结论包含明确证据引用（每项 evidence 标注工具来源）。
- 核心工具失败时不产生确定性根因（`confidence` 不为 `confirmed`）。
- 未经审批不得触发补拉。
- `awaiting_approval` 响应同时包含 diagnosis 与 proposed_action。
- LLM 不可用时能够返回规则诊断（LLM Fallback）。
- Redis 锁有效期内，同一短期锁键不得并发创建重复重试任务。
- RAG 输出具有 Recall@3、MRR@5、平均延迟和 Token 用量评估结果。
- 每次 Run 的 token 用量与各节点延迟可从 LangSmith trace 读出（可观测性基线）。

## 16. 开发顺序

1. 修复 C-MOVE 最终状态判断、Downloader 事务边界和 Checker `unverified` 重复扫描问题。
2. 按 [6.5](#65-数据库结构改动) 增加 `download_task` 阶段时间字段，补齐 SeriesInstanceUID 归属解析和 Series 级隔离统计。
3. 建立 Study/Series 规则测试集，先验证完整性口径和状态转换。
4. 将现有查询能力封装为 4 个只读 LangChain Tools，实现必填 `BaseToolOutput.success` 和 Pydantic 参数校验。
5. 使用 `bind_tools()` 实现只读 Function Calling，并实现 Tool Plan 白名单、最小证据集和依赖校验。
6. 建立最小 LangGraph：`route_request → resolve_target → plan_tools → validate_tool_plan → collect_evidence → deterministic_diagnosis`（数量比对为该节点内代码步骤）。
7. 补齐 `retrieve_and_explain → validate_output → determine_action`，实现结构化 Prompt、DiagnosisOutput 和代码 Guardrail；将 9 个案例作为测试，运行时最多选择 3 个 Few-shot。
8. 建设 ChromaDB 知识库（含经验记忆类别）并完成向量检索评估，根据收益决定是否默认启用 qwen3-rerank。
9. 增加 RabbitMQ Agent Worker、SqliteSaver、`agent_run`、`agent_action_audit`、Human-in-the-loop、审计与 Redis 300 秒短期锁。
10. 接入 LangSmith 采集 token/延迟基线；完成诊断准确率、工具成功率、RAG 命中率、Token、延迟和降级测试。
11.（可选加分）按 [9.7](#97-mcp-暴露薄层可选不改业务逻辑) 增加 MCP 薄层，把 4 个只读工具暴露为 MCP Server，不改动业务逻辑。

## 17. 最终项目边界

最终项目应保持以下定位：

```text
一个围绕 PACS-DICOM 数据补拉链路的单业务 Agent，
能够对 Study 和 Series 两个层级进行证据驱动的诊断，
并通过受控 Function Calling 完成人工审批式补拉。
```

不扩展 GPU 调度、AI 结果存储、报告生成、通用平台运维、多模态医学诊断或复杂多 Agent 协作。

### 17.1 刻意不做的能力：理由与扩展接口

以下能力是**清醒的取舍**而非能力缺口——它们要么违背单业务边界，要么在当前场景收益低于复杂度成本。这里说明“为什么不做”和“若要做从哪接”，以便评审看到边界是想清楚的。

**多模态（图文 / 语音）——不做。**
- 理由：本项目诊断的是“补拉链路为什么没拉到数据”，证据来自 PACS 响应码、任务状态、计数比对，都是结构化文本；引入 DICOM 像素或预览图分析会滑向**临床影像判断**，直接违反“不做任何临床医学判断”的角色边界。语音更是无对应场景。
- 扩展接口：数据集本身带 `.png` 预览图（见 [README.md](README.md)），天然存在图文接入点。若未来要做“影像可读性检查”这类**非临床**的多模态任务，可新增一个只读工具 `inspect_preview_image`，产出仍走同一套 `BaseToolOutput` 与证据规则，不需改图结构。当前刻意不做，是因为它无法在不触碰临床判断的前提下产生诊断价值。

**多 Agent 协作 / A2A 协议——不做。**
- 理由：补拉链路是**单条、强顺序**的业务流（PACS→任务→C-MOVE→收图→校验），故障阶段互斥，用一个 Agent 顺序采证即可覆盖；拆成多个 Agent 只会引入通信、任务调度、冲突解决的额外复杂度，却不提升诊断质量——这正是“不过度设计”的体现。
- 扩展接口：当前的 LangGraph 节点（route/plan/collect/diagnose/explain/act）已经是清晰的职责切分，等价于“单进程内的角色分工”。若未来诊断对象扩展到多条独立链路（如同时诊断补拉链路 + 归档链路 + 分发链路），可将每条链路的诊断封装为一个子图或子 Agent，由一个 supervisor 按 A2A 风格路由。届时现有的 `route_request` 就是 supervisor 的雏形，`agent_run` 表可平滑扩展为多 Agent 的运行记录。当前单链路场景下，引入这层协作是负收益。

**核心判断**：这两项不是“做不到”，而是“在 PACS 补拉这个具体业务里，做了会破坏优雅”。保留清晰的扩展接口，比过早搭建通用框架更符合工程理性。
