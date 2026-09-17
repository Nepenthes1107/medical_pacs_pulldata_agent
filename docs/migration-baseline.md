# Phase 0 迁移基线

本文件记录基座化改造前的兼容边界。所有后续阶段都必须保持这些业务不变量，除非在规格文档和迁移说明中显式更新。

## 现有入口

- `GET /health`
- `POST /agent/chat`
- `GET /agent/runs/{run_id}`
- `GET /agent/runs/{run_id}/stream`
- `POST /agent/runs/{run_id}/action`
- `POST /agent/runs/{run_id}/abort`
- `POST /agent/runs/{run_id}/cancel`

Phase 1 新增基座式入口：

- `GET /agents/info`
- `POST /agents/{agent_id}/invoke`
- `POST /agents/{agent_id}/stream`
- `POST /agents/{agent_id}/history`
- `GET /agents/{agent_id}/threads`

Phase 4 业务命令入口：

- `POST /pacs/runs`
- `GET /pacs/runs/{run_id}`
- `POST /pacs/runs/{run_id}/approval`
- `POST /pacs/runs/{run_id}/abort`
- `POST /pacs/runs/{run_id}/cancel`

旧 `/agent/*` 入口作为薄兼容 facade 保留在代码中，通过 `LEGACY_AGENT_API_ENABLED=true` 临时启用；默认服务只注册 `/agents/*` 与 `/pacs/runs/*`，任何写操作仍必须经过审批。

## 不变量

1. `thread_id` 表示会话，`run_id` 表示一次业务执行，不能互换。
2. 同一 `thread_id` 的活跃 Run 不能并发创建。
3. `execute_repull_plan` 不得经 MCP、普通工具或公开 API 直接调用。
4. PACS 查询失败不能被转换成“目标不存在”或“下载成功”。
5. Checker 是下载完整性的最终裁判；Agent 只负责诊断和计划。
6. Chroma/RAG 只提供知识证据，不承担 Checkpointer 或会话记忆。
7. 工具证据必须按 `thread_id` 隔离，并支持从 MySQL 审计记录回溯。
8. 通用 Agent Service 的 history/threads 只读取 LangGraph Checkpointer；Checkpoint 不可用时返回 503，禁止静默切换到进程内内存。
9. Service 层的 Run/Task 查询和 RabbitMQ 投递经过 `src/infrastructure` repository/adapter；事务仍由请求作用域控制。
10. Agent 只读 Task/Receive/History 查询通过 `TaskRepository`；审计幂等查询通过 `ActionAuditRepository`，工具层不再直接编写这些 ORM 查询。
11. 执行轨迹、工具证据和过程事件的实际持久化入口位于 `src/infrastructure/db/audit.py` 与 `src/infrastructure/events.py`；`app.agent.audit/events` 仅保留兼容导出。
12. Agent、Routing 和 MCP 通过 `src/infrastructure/rag.py` / `rag_pipeline.py` 获取统一 `search_knowledge` callable；RAG 索引构建脚本仍在兼容迁移阶段，不复制第二套在线检索入口。
13. 补拉幂等锁和 RabbitMQ 队列状态查询通过 `src/infrastructure/idempotency.py` 与 `src/infrastructure/messaging.py`；Agent 工具不直接导入 Redis/Pika。
14. Agent、Routing、首拉节点和 Service 的数据库 session 通过 `src/infrastructure/db/session.py` 组合，ORM 查询由 repository 承担。
15. 批量 Study 并发、瞬时重试、聚合和审批恢复由 `src/application/batch.py` 负责；`app.agent.batch_orchestrator` 仅为兼容导出。
16. 首拉/补拉建任务和止损服务由 `src/application/pull_service.py`、`src/application/abort.py` 持有；旧 `app.services` 路径仅为兼容导出。
17. PACS 业务路由通过 `src/service/pacs_routes.py` 参与应用组合；旧 `/agent/*` URL 仅在兼容开关启用时可用。
18. 在线 RAG Pipeline、文档解析、来源下载、初始化编排和知识索引 adapter 使用 `src/infrastructure/rag_pipeline.py`、`rag_documents.py`、`rag_sources.py`、`rag_index.py`、`rag_store.py`、`rag_lexical.py`；旧 RAG 模块仅保留兼容接口和 CLI 入口。
19. Downloader、Agent Worker 和 Checker 的 Run/Task/Series 查询及失败事件发布通过 infrastructure repository/messaging；Worker 事务和队列 ACK 边界保持不变。

## 新旧入口迁移

基座入口优先用于新客户端：

```text
GET  /agents/info
POST /agents/pacs-diagnostician/invoke
POST /agents/pacs-diagnostician/stream
POST /agents/pacs-diagnostician/history
GET  /agents/pacs-diagnostician/threads
```

`/agent/chat` 和 `/agent/runs/{run_id}/stream` 仅在兼容开关启用时注册；旧流式接口会返回 `Deprecation: true` 和替代入口提示。默认入口使用 `/agents/*`，审批、止损、取消等 PACS 业务命令使用 `/pacs/runs/*`。

`/agents/pacs-diagnostician/stream` 通过 `src/agents/pacs/graph.py` 的 `stream_updates` 使用同一套 PACS 图和 Checkpointer；Service 不再维护第二套图拓扑。

PACS 图和状态实现已迁移到 `src/agents/pacs/graph.py`、`src/agents/pacs/state.py`；`app.agent.graph/state` 仅保留兼容导出。LLM 工厂、Settings、Checkpointer 和 RabbitMQ 传输分别由 `src/core` 与 `src/infrastructure` 持有。

服务启动不再默认执行 `create_all`；开发环境如确需自动创建表，显式设置 `AUTO_CREATE_SCHEMA=true`。生产环境必须先执行 `config/schema.sql` 和 migrations。

平台代码统一从 `src.core.settings` 读取配置；`app.core.config` 仅作为兼容实现保留，避免新入口继续扩散旧配置导入。

容器服务使用 `python -m src.run_service` 启动，镜像同时复制 `app/`、`src/` 和 `config/`；`app.api.main` 仍是 FastAPI 应用对象的兼容导入路径。

依赖声明已迁移到根目录 `pyproject.toml`，按 core/pacs/rag/mcp/dev 分组；Docker 从该声明安装运行 extras。当前环境无法访问 PyPI 时不生成伪造的 `uv.lock`。待网络或内部镜像可用后，使用 `uv lock` 生成并将 lock 文件纳入发布流程。

## 验证命令

在安装 `requirements.txt` 后执行：

```powershell
python -m pytest -q
```

当前工作机的全局 Python 缺少项目依赖，测试收集会因 `langchain_core`、`pydantic`、`pika` 等模块不存在而失败；该环境问题不能作为业务测试基线。
