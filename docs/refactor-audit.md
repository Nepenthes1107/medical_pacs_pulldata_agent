# 基座化改造验收审计

更新时间：2026-09-09

本审计以 `docs/codex-refactor-spec.md` 为准，记录当前工作树的可验证结果。

## 已满足

- Agent Registry 唯一入口：`src/agents/registry.py`，注册 `pacs-diagnostician`。
- 通用 Agent Service：`src/service/service.py` 与 `src/service/agent_routes.py`，提供 info、invoke、stream、history、threads。
  invoke 通过 `asyncio.to_thread` 隔离阻塞的 PACS/图执行；stream 将 LangGraph 原生更新增量适配为异步 SSE，避免在 HTTP 层复制业务事件状态机。
- PACS 业务命令：`src/service/pacs_routes.py` 提供 `/pacs/runs/*`，实际命令逻辑位于
  `src/application/run_commands.py`；旧 `/agent/*` 默认关闭，由 `LEGACY_AGENT_API_ENABLED=true` 启用。
- 图和状态所有权：`src/agents/pacs/graph.py`、`src/agents/pacs/state.py`；旧 `app.agent.graph/state` 为兼容导出。
- 节点和工具 Schema 所有权：`src/agents/pacs/context.py`、`src/agents/pacs/nodes/`、`src/agents/pacs/schemas.py`；旧 `app.agent` 节点/Schema 路径为兼容 facade。
- Prompt 与 MCP 入口：`src/agents/pacs/prompts.py`、`src/agents/pacs/mcp.py`；旧模块仅作兼容启动入口。
- 单一 LLM 工厂：实现位于 `src/core/llm.py`；旧 `app.agent.llm` 仅保留兼容导出。
- `app.agent.llm`、`app.agent.audit`、`app.agent.harness` 现在是指向 canonical `src`
  模块的兼容模块别名；领域代码已不再通过旧路径反向依赖实现。
- 单一 Settings：实现位于 `src/core/settings.py`；旧 `app.core.config` 仅保留兼容导出。
- Checkpointer adapter：`src/infrastructure/checkpoint.py`，保留 Redis TTL、setup 和 interrupt/resume 语义。
- RabbitMQ adapter：`src/infrastructure/rabbitmq.py`；消息发布由 `src/infrastructure/messaging.py` 组合。
- Repository、审计、事件、幂等、在线 RAG pipeline、文档解析、来源下载、embedding
  与 RAG adapters 已集中在 `src/infrastructure`；`app.agent.rag` 仅保留兼容导出和命令入口。
- 诊断目标解析和首拉事务已下沉到 `src/application/target.py`、`src/application/pull.py`；
  `src/agents/pacs/nodes` 只做路由与 State patch 映射，不再创建数据库 Session。
- PACS Run/审批/止损 DTO 的 canonical 定义位于 `src/schema/pacs.py`；`app.core.schemas`
  仅重新导出兼容名称（包括首拉、DICOM 查询和 C-MOVE 结果 DTO），避免 API 层维护第二套模型。
- Agent、MCP、测试共享同一个 `search_knowledge` callable；写工具不进入只读注册表或 MCP。
- 审批仍由 `human_approval` interrupt 和 `Command(resume=...)` 驱动，写操作只从固定 `execute` 节点触发。
- Downloader、Agent Worker 的 ACK/NACK 语义未改变；Checker 仍是完整性唯一裁判。
- `src/workers/{agent,downloader,checker,scheduler}.py` 持有 canonical worker 实现；迁移期旧
  `app.workers` 路径只是模块别名，消费者不会因导入模块而自动启动。Worker 仍通过
  `app.core.models` 和 `app.dicom` 读取现有持久化/PACS 实现，后续可按 adapter contract
  独立迁移这些 legacy 资产。
- SQLAlchemy 模型、Session、PACS client、DICOM scanner 和 parser 已迁入
  `src/infrastructure/{db,pacs}`；`app.core.database/models` 与 `app.dicom.*` 仅保留模块别名。
- 容器入口统一为 `python -m src.run_service`，数据库 schema 创建改为显式 opt-in。 

## 验证证据

```text
python -m pytest -q              -> 107 passed, 8 warnings
python -m compileall -q app src  -> passed
git diff --check                 -> passed
```

本轮新增验证：

```text
src/agents/pacs forbidden-client import scan -> NO_FORBIDDEN_IMPORTS
canonical DTO declaration scan             -> all PACS/DICOM DTOs only in src/schema/pacs.py
```

静态边界检查确认 `src/agents/pacs` 不直接导入 FastAPI、pika、redis、SQLAlchemy 或 Chroma 客户端。

补充静态检查：PACS 领域节点的枚举、拉取任务 DTO、目标解析、审计适配器、工具 Schema 和证据
harness 已经从 `app.*` 路径收敛到 `src.core`、`src.schema`、`src.application`、
`src.infrastructure` 与 `src.agents.pacs`。`src/agents/pacs` 中保留的 `app.agent.llm`
引用是迁移期测试兼容桥：旧测试通过 monkeypatch 该稳定路径注入 fake model；真正的
LLM 实现仍唯一位于 `src/core/llm.py`，`app/agent/llm.py` 只做导出。PACS 的 RAG
pipeline 同样保留旧 LLM 入口以维持外部调用方兼容，但 Schema 已切换到领域所有权。

## 本机补齐证据（2026-09-09）

原「迁移期保留项」指出的 ruff / mypy / `uv.lock` 与真实外部服务集成，已在本机补齐：
自包含 `.venv311c`（CPython 3.11.16）执行静态 gate；Docker Desktop 起
`mysql / redis / rabbitmq / orthanc`；mock 语料由 `scripts/generate_mock_dicom.py`
生成后经 `scripts/import_dicom_to_orthanc.py` 导入 Orthanc 作真实 PACS。

```text
ruff check src app tests                            -> All checks passed!
mypy src                                            -> Success: no issues found in 70 source files
uv lock --check --offline --python .venv311c\python.exe
                                                    -> Resolved 169 packages（与 pyproject 一致）
python -m pytest -q                                 -> 107 passed, 17 deselected, 2 warnings
python -m pytest tests/integration -m integration -q
                                                    -> 17 passed, 1 warning
```

集成测试（`tests/integration/`，默认被 `addopts = "-m 'not integration'"` 排除，不破坏单测基线）
覆盖 spec §9 要求的全部外部服务：Redis 真实 checkpointer 的 interrupt/resume 落盘、MySQL 一次性库
事务 / 幂等审计 / 按 thread 隔离、RabbitMQ 发布与队列计数、Orthanc DICOM C-ECHO / C-FIND、
本地 Chroma / BM25 落盘检索。C-MOVE 端到端经 `pull-data-storescp` 在本机验证
（storescp 日志增量 == C-MOVE completed）；无 storescp 的环境会如实 skip（见
`test_c_move_received_by_storescp`）。

## 迁移期保留项

- 在线检索、文档解析、来源下载、embedding、Chroma 和 BM25 实现已迁移到
  `src/infrastructure/rag_*.py`；`app.agent.rag.*` 仅保留兼容 facade 和初始化命令入口。
- 旧 `/agent/*` 路由在 `app/api/routes_agent.py` 保留为薄兼容 Router，仅转换旧 URL/SSE
  协议并委托 `src/application/run_commands.py`；默认不注册，删除前需完成外部客户端迁移和兼容窗口。
- 依赖声明以 `pyproject.toml` 为 canonical；Docker 已从该声明安装 `pacs/rag/mcp` extras。
  `uv.lock` 已生成（阿里云镜像 `https://mirrors.aliyun.com/pypi/simple/`，覆盖全部依赖组），
  经 `uv lock --check` 与 pyproject 核验一致。
- RAG 的旧模块仅保留兼容别名；在线入口已通过 `src/infrastructure/rag.py` 收敛。
- 首拉/补拉建任务实现已迁移到 `src/application/pull_service.py`，`src/application/pull.py`
  提供稳定的事务边界；`app.services.pull` 仅保留兼容导出。
- Run 创建、查询、审批、止损和取消已由 `src/application/run_commands.py` 持有；旧
  `app/api/routes_agent.py` 的同名函数仅作为兼容 URL 的实现保留，默认 `/pacs` 路由不再
  导入旧 Router。
- `ruff` / `mypy` / `uv` 本机已执行通过（见「本机补齐证据」）；`.github/workflows/ci.yml` 的
  test job 持续执行 lint / 类型 / 单测 gate，并新增 integration job 按本机 compose 等价步骤跑真实服务集成。
- LLM 配置支持显式 `LLM_PROVIDER`（`openai-compatible`/`dashscope`/`openai`），密钥仅从
  `LLM_API_KEY` 或 `DASHSCOPE_API_KEY` 读取，YAML 中的 `api_key` 会被忽略。

上述 `uv lock`、ruff、mypy 与真实外部服务集成均已于本机完成（见「本机补齐证据」），
不再依赖 CI/集成环境补齐。

## 遗留项（如实记录，未伪造）

- 本轮把 `src/infrastructure/db/models.py` 的 12 处 JSON 列注解从 `Mapped[Any]` 修正为
  `Mapped[Any | None]`：迁移到 SQLAlchemy 2.0 `Mapped[...]` 后，非 `Optional` 注解会被推导为
  `nullable=False`，与 `config/schema.sql` 的 `JSON DEFAULT NULL` 冲突（插入省略该列时抛
  `1364 Field ... doesn't have a default value`）。涉及 `study/series.find_source`、
  `study.move_source`、`download_task.task_body/move_result`、
  `agent_run.study_instance_uid_list/batch_summary/study_results/diagnosis/proposed_action`、
  `agent_action_audit.detail`、`agent_execution_log.payload`；随后用脚本核验 models metadata
  与 `config/schema.sql` 的 nullable 全量一致。
- Docker 的 MySQL 数据卷早于 `config/schema.sql` 补入 `agent_run` 4 个 batch 列（`004_study_batch`）
  初始化，运行库 `pulldata` 缺失这些列。需 `docker compose down -v` 重放 schema.sql，
  或对现存卷手动重放 `config/migrations/004_study_batch.sql`；集成测试用一次性库不受影响。
- `.github/workflows/ci.yml` 新增 integration job（本机 compose 等价命令已 17/17 全绿），
  尚未在托管 GitHub runner 实跑——首次推送后需观察：拉取 mysql:8.0 / redis:8 /
  rabbitmq:3-management / orthancteam/orthanc 镜像耗时，C-MOVE 用例因无 storescp 在 CI 会 skip。
- DashScope 联网链路已用真实 key 验证（2026-09-09）：`.env` 提供 `DASHSCOPE_API_KEY`，
  `src/core/settings.py` 新增最小 `.env` 加载器（本地 python 进程与 docker compose 行为一致，
  仅在不覆盖既有进程环境变量的前提下补齐），key 仍只来自进程输入、不会被提交。三链路实测：
  `qwen-plus` chat 正常返回、`text-embedding-v4` 输出 1024 维、`qwen3-rerank` 相关文档
  `relevance_score=0.9172` 排第一。随后带真实 key 的全量在线检索也已实测（隔离 tmp Chroma/BM25，
  不触碰 `data/chroma`）：真实 embedding 写入 4 原子 → BM25 建索引 →
  `search_knowledge` 全链路（`qwen-plus` 改写 `rewrite=True`、dense+BM25 RRF 融合、`qwen3-rerank`
  重排），category 过滤正确、相关文档 top1、无关干扰垫底。集成 RAG 用例仍用本地确定性
  embedding 离线断言召回、不依赖网络。
- 以上结果均基于当前工作树（含大量未提交重构），是否提交 / 如何分组由用户决定。
