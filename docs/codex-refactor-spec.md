# Medical PACS Pull Data Agent 面向 Codex 的基座化改造规格

- 文档状态：Draft，可作为后续 Codex 实施任务的唯一入口
- 目标项目：`medical_pacs_pulldata_agent`
- 参考项目：`D:\VSCode\MedFlow_agent`
- 基座：[`agent-service-toolkit`](https://github.com/JoshuaC215/agent-service-toolkit)

## 1. 目标与非目标

### 1.1 目标

将当前项目改造为“领域代码依赖通用 Agent Service 基座”的结构：

1. 复用基座的 FastAPI Agent Service、Agent Registry、同步/异步调用、消息流、线程历史、Pydantic 协议、LLM 工厂、生命周期和测试约定。
2. 保留 PACS 拉取领域能力：DICOM/PACS 访问、任务创建、C-MOVE、C-STORE 接收、完整性检查、人工审批、补拉、批处理、审计和 RAG。
3. 让新增 Agent、工具、节点或 API 变成“新增一个领域模块并注册”，而不是复制一套服务协议、SSE、线程、模型和错误处理代码。
4. 把基础设施故障与业务判定分开：Agent 负责诊断/计划，Downloader 与 Checker 负责执行/验收。
5. 用可执行的迁移阶段、验收标准和禁止事项约束 Codex，避免一次性大重写。

### 1.2 非目标

- 不更换 DICOM/PACS 协议库，不把 PACS 直接交给 LLM。
- 不把人工审批绕过为 Agent 工具调用。
- 不把 Chroma/RAG 当作对话记忆或 LangGraph Checkpointer。
- 不在第一阶段同时更换 MySQL、Redis、RabbitMQ 和部署拓扑。
- 不为每个节点、工具或队列再造一套 Web 协议。

## 2. 现状审查结论

### 2.1 当前项目的可复用业务资产

- `app/dicom/`：PACS 客户端、DIMSE、扫描和 DICOM 解析。
- `app/workers/`：Downloader、Checker、Agent Worker、Scheduler。
- `app/services/`：拉取和止损业务服务。
- `app/agent/nodes/` 与 `app/agent/graph.py`：诊断、观察、验证、反思、审批、执行图。
- `app/agent/tools.py` 与 `tool_schemas.py`：只读查询工具和结构化输出。
- `app/agent/rag/`：文档、混合召回、RRF、重排和知识初始化。
- `app/core/models.py`、迁移和 `docs/architecture.md`：审计与存储边界。

### 2.2 应优先删除或下沉到基座的重复实现

| 当前实现 | 问题 | 改造方向 |
| --- | --- | --- |
| `app/api/routes_agent.py` 的自建 `/chat`、Run 轮询、SSE、Last-Event-ID、终态判断 | 重复实现 Agent Service 的调用和流式协议，接口与图生命周期紧耦合 | 迁移到基座 Service；PACS API 只保留业务命令和 Run 查询适配 |
| `_acquire_thread_lock`、`_active_run_for_thread` 与自定义 thread 冲突协议 | 线程生命周期、并发和 Checkpointer 之外再维护一套状态 | 使用基座 thread/user 约定；PACS Run 通过 `run_id` 关联，不复制对话协议 |
| `app/agent/llm.py` 的单一 DashScope 工厂 | 只支持一类 Provider，配置、重试、流式和测试模型分散 | 接入基座 `core/llm.py` 的 Provider 工厂；保留 DashScope 为 OpenAI-compatible 配置 |
| `app/agent/mcp_server.py` 手工包装每个工具 | MCP 函数与 StructuredTool 重复，维护两份参数和文档 | 工具以单一函数/Schema 定义；MCP 仅作薄适配，且只注册只读工具 |
| `app/core/config.py` YAML + 环境变量手工覆盖 | 环境变量映射、默认值和路径处理重复，难以测试 | 迁移到基座 `core/settings.py` 风格的 `pydantic-settings`，YAML 仅保留 PACS 清单兼容层 |
| `app/api/main.py` 的 `on_event` 与 `init_db` | 生命周期不可组合，启动时隐式建表 | 使用 FastAPI lifespan；数据库迁移由部署命令负责，开发环境可显式 init |
| `app/agent/observability.py` 自建指标 | 与 LangSmith/服务日志/请求上下文重复 | 复用基座 tracing、请求级 metadata 和统一日志；PACS 指标只补领域指标 |
| `app/agent/events.py` + 手工 SSE 事件格式 | 业务事件与传输格式耦合 | 图事件使用 LangGraph stream；PACS 领域事件定义为 typed event，再由 Service 转换 |
| `batch_orchestrator.py` 自建批处理状态机 | 批处理与单 Run 状态、审批和流式协议混杂 | 抽成领域 Application Service；复用基座调用/线程协议，批任务只维护业务聚合状态 |

### 2.3 参考项目应保留的设计原则

参考项目将 `src/agents`、`src/schema`、`src/core`、`src/service`、`src/client` 和 `tests` 分层；通过 Agent Registry 暴露多个 Agent，通过通用 Service 提供 invoke/stream/history/threads，并使用 Pydantic 协议、异步生命周期和统一 LLM 工厂。基座 README 明确提供 FastAPI 服务、Streamlit 客户端、AG-UI、线程历史、多 Agent、RAG、反馈、Docker 与测试能力；这些属于平台能力，PACS 代码不应重复实现。[agent-service-toolkit README](https://github.com/JoshuaC215/agent-service-toolkit)

## 3. 目标架构

```text
src/
  agents/
    registry.py                 # 基座式 Agent 注册
    pacs/
      graph.py                  # 仅定义 PACS LangGraph 图
      state.py                   # Agent 状态
      nodes/                    # 领域节点
      tools.py                  # 领域工具，单一 Schema 来源
      prompts.py
      policies.py               # 审批、引用、重试规则
    knowledge.py                # 可选知识问答 Agent（若仍需要独立 Agent）
  schema/
    api.py                      # 基座请求/响应协议扩展
    pacs.py                     # PACS 领域 DTO
  core/
    settings.py                 # Settings 与 Provider 配置
    llm.py                      # 复用/扩展基座模型工厂
    logging.py
  service/
    service.py                  # 基座 FastAPI Service，不复制 SSE/threads
    pacs_routes.py              # 仅保留 PACS 命令、审批、止损、Run 领域查询
  infrastructure/
    db/                         # SQLAlchemy models/repositories/migrations
    messaging/                  # RabbitMQ publisher/consumer adapter
    checkpoint/                 # Redis Checkpointer factory
    pacs/                       # pynetdicom/pydicom adapter
    rag/                        # Chroma/BM25/DashScope adapter
  workers/
    agent.py
    downloader.py
    checker.py
  run_service.py
```

原则：`agents/pacs` 不导入 FastAPI、RabbitMQ、Redis 客户端或 SQLAlchemy Session；通过 context/dependency 注入端口。`service` 不实现诊断规则；`workers` 不调用 LLM。每个外部系统只有一个 adapter。

## 4. 目标协议与边界

### 4.1 Agent Registry

实现基座式注册表：

```python
@dataclass(frozen=True)
class AgentDefinition:
    description: str
    graph: AgentGraph
    capabilities: frozenset[str]

AGENTS = {"pacs-diagnostician": AgentDefinition(...)}
```

`/info`、`/{agent_id}/invoke`、`/{agent_id}/stream`、`/history`、`/threads` 由基座提供。PACS Agent 只需编译并注册图。若保留现有 `/agent/chat`，必须成为兼容 facade，并在文档中标记弃用。

### 4.2 PACS 领域命令

保留以下业务端点，统一返回 Pydantic DTO，并复用基座认证、错误和请求上下文：

- `POST /pacs/runs`：创建诊断/拉取 Run，返回 `run_id`、`thread_id`。
- `GET /pacs/runs/{run_id}`：返回业务状态、诊断、提议动作和批摘要。
- `POST /pacs/runs/{run_id}/approval`：`approve|reject`，唯一允许进入写操作。
- `POST /pacs/runs/{run_id}/abort`：设置止损标志；不直接操作 Agent 图。
- `POST /pacs/runs/{run_id}/cancel`：取消等待中的业务 Run，不取消已投递 PACS 任务。

通用 Agent 消息流通过基座 stream；PACS 领域状态通过 typed event 或 Run 查询取得。不得再新增一套手写 SSE 协议。

### 4.3 状态和持久化

- LangGraph Checkpointer：只保存会话/图状态，Redis 实现由 adapter 创建。
- MySQL：保存 `AgentRun`、任务、审计、工具证据和业务结果；使用 repository，禁止节点直接创建 Session。
- RabbitMQ：只承载 Agent Run、下载任务和补拉失败事件；publisher/consumer 封装在 messaging adapter。
- Chroma/BM25：只保存知识库；RAG 返回 `KnowledgeItem` 和来源，不写入会话记忆。
- `run_id` 是一次业务执行键；`thread_id` 是对话键。二者不可混用。

## 5. 依赖与配置改造

### 5.1 依赖分层

迁移到 `pyproject.toml` + `uv.lock`，按四组管理：

- `core`：FastAPI、Pydantic Settings、LangGraph、LangChain、uvicorn。
- `pacs`：pydicom、pynetdicom、SQLAlchemy、PyMySQL、redis、pika。
- `rag`：Chroma、bm25s、jieba、DashScope、PyMuPDF、python-docx、beautifulsoup4。
- `dev`：pytest、pytest-asyncio、ruff、mypy、pre-commit。

删除未被代码引用的依赖；每个可选 Provider 用 extras，避免基础服务安装全部模型 SDK。

### 5.2 Settings

- `core/settings.py` 使用 `BaseSettings`、嵌套配置和显式 env prefix。
- API key 只来自环境变量/secret，不进入 YAML、数据库或日志。
- YAML 仅作为 `PACS_SOURCES_FILE` 读取的领域配置；加载后立即转成 Pydantic 模型。
- 所有路径在 settings 层解析为绝对路径。
- 配置缺失在启动时失败并指出字段；运行期间禁止隐式回退到 SQLite、内存队列或默认模型。

## 6. 图与工具改造规则

1. `build_graph()` 只负责图拓扑和依赖注入；模型、仓储、PACS client、RAG store 从 context 获取。
2. 节点保持纯函数倾向：输入 State/Context，输出 State patch；禁止节点自行发布 RabbitMQ 或写 SSE。
3. `human_approval` 使用 LangGraph `interrupt()`；恢复使用 `Command(resume=...)`，审批动作由 service 做鉴权和幂等校验。
4. `execute_repull_plan` 不注册为 MCP、公开 API 或普通 Agent tool；只能从审批后的固定节点调用。
5. 只读工具全部使用 Pydantic 输入/输出；Agent、MCP、测试共用同一函数对象和 Schema。
6. 工具证据保留 `tool_id`、参数、输出、成功标志和 `thread_id`；State 只保留有界窗口。
7. `observe`、`verify_diagnosis`、`reflect` 继续作为确定性护栏，但规则放入 `policies.py`，避免散落在节点中。
8. RAG 保留当前混合召回/RRF/重排策略；在线入口只暴露 `search_knowledge`，索引构建和评估放到 scripts/fixtures。
9. 批处理是 Application Service：单 Study 图逻辑不复制；批处理只负责并发、重试、聚合和审批状态映射。

## 7. 分阶段实施计划

### Phase 0：冻结行为与建立基线

- 固定现有 API、状态枚举、数据库表、队列名和安全不变量。
- 运行现有测试，补充一份端到端 smoke 清单：诊断、审批、拒绝、止损、Checker 收尾、RAG、MCP 只读边界。
- 产出 `docs/migration-baseline.md` 和兼容矩阵。

验收：现有 `pytest` 通过；每个 Run 终态和审批路径都有可重复样例。

### Phase 1：引入基座 Service，不改业务图

- 将参考项目的 `schema/core/service/client` 结构移入 `src/`（或等价包名）。
- 接入基座 `/info`、invoke、stream、history、threads 和 lifespan。
- 把现有 `app.agent.graph` 包装为一个 Registry Agent。
- `/agent/chat` 仅做兼容转发，新增调用优先走基座协议。

验收：同一个 PACS 图可通过基座 invoke/stream 调用；线程历史可读；原有业务端点仍通过兼容测试。

### Phase 2：迁移配置、LLM、可观测性

- 用基座 Settings 替换手工 YAML/env 合并。
- 用基座模型工厂支持 DashScope OpenAI-compatible、Fake model 和未来 Provider。
- 接入统一 tracing/metadata；保留 PACS 专属指标。

验收：无 API key 的单测可使用 Fake model；生产配置缺失快速失败；日志不包含 secret。

### Phase 3：抽取 Infrastructure Adapters

- 抽取 `PacsClientPort`、`TaskRepository`、`RunRepository`、`EventPublisher`、`KnowledgeStore`。
- 将节点中的 SQLAlchemy、Redis、RabbitMQ、Chroma 访问移出 `agents/pacs`。
- 保持表结构和队列协议不变，先做 adapter contract tests。

验收：Agent 图可用 fake adapters 单测；Downloader/Checker 可独立启动；领域包不依赖基础设施客户端。

### Phase 4：收敛流式、审批和批处理

- 删除自建 SSE 生成器和重复事件终态逻辑，统一使用基座 stream。
- 将审批改为 interrupt/resume + 幂等 command。
- 将 batch orchestrator 改为 application service，并复用单 Study 领域服务。

验收：断线重连、重复审批、并发审批、批内部分失败均有测试；写工具无法从 MCP/API 直达。

### Phase 5：清理与兼容层下线

- 删除重复 `app/api/routes_agent.py` 协议实现、旧事件格式和无调用依赖。
- 更新 Docker、README、LangGraph Studio 配置和部署迁移说明。
- 以 feature flag 控制旧接口下线，发布一个版本后删除兼容 facade。

验收：`rg` 不再发现第二套 Agent Service/SSE/LLM 工厂；文档中的启动、调用和迁移命令可在干净环境执行。

## 8. Codex 实施约束

- 每个阶段单独提交，提交信息包含 `refactor:` 或 `feat:` 和阶段号。
- 修改前先阅读 `docs/architecture.md`、数据库 schema、相关测试和本规格；不得只根据文件名推断行为。
- 禁止大范围机械重命名；每次迁移保持一个可运行状态。
- 新增依赖前说明它替代了哪段自建代码；没有替代关系不得引入。
- 不改变 DICOM UID、任务状态枚举、审批安全边界和证据引用语义，除非先更新兼容矩阵。
- 外部副作用必须位于 adapter/application service；图节点不能隐式创建线程、发布消息或提交数据库事务。
- 任何删除前先用 `rg` 查找引用，并保留迁移期兼容测试。
- 所有异步接口优先 async；阻塞的 pynetdicom、SQLAlchemy、RabbitMQ 操作必须明确放入 worker/线程边界。

## 9. 验收指标

### 结构

- 一个 Agent Registry；一个通用 Service；一个 LLM 工厂；一个线程/流式协议。
- `agents/pacs` 不直接导入 FastAPI、pika、redis、sqlalchemy、chromadb。
- Agent、MCP、测试共享工具 Schema 和函数实现。

### 行为

- 诊断、知识问答、首次拉取、补拉、审批、拒绝、止损、Checker 验收和批处理行为与基线一致。
- 同一 `thread_id` 的并发运行仍返回冲突；审批写操作仍不可绕过。
- 工具证据可在 State 窗口外通过 MySQL 回溯，并严格按 `thread_id` 隔离。

### 质量

- 单元测试覆盖 settings、registry、graph routing、工具 contract、repositories、审批幂等。
- 集成测试覆盖 Redis checkpointer、MySQL、RabbitMQ、PACS fake server、RAG store。
- `ruff`、`mypy`（或项目约定的类型检查）和 `pytest` 在 CI 通过。
- README 能让新开发者在不阅读业务细节的情况下启动服务、调用 Agent、查看线程和运行测试。

## 10. 推荐的首批 Codex 任务拆分

1. `refactor(phase-0)`: 建立基线、兼容矩阵和 smoke 测试。
2. `refactor(phase-1)`: 引入基座 schema/core/service/client 与 Agent Registry。
3. `refactor(phase-2)`: 迁移 Settings 和 LLM factory，保留 DashScope。
4. `refactor(phase-3)`: 定义 PACS/存储/消息/RAG ports 并实现 adapters。
5. `refactor(phase-4)`: 收敛 stream、approval、batch application service。
6. `chore(phase-5)`: 删除重复实现、更新部署文档和下线兼容接口。

每个任务必须在 PR 描述中写明：改造前重复实现、基座替代点、保留的领域代码、测试命令、兼容性风险和回滚方式。
