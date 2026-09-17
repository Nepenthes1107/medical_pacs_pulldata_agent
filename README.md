# Medical PACS PullData Agent

这是一个单机可运行的 PACS 数据补拉与链路诊断 MVP。项目保留 PACS C-FIND/C-MOVE、RabbitMQ 异步任务、storescp 接收、本地 DICOM 扫描、MySQL 状态机和规则 Agent 诊断能力，不依赖旧 uAF 平台组件。

## 链路说明

```text
Orthanc / Mock PACS
  -> C-FIND 查询 Study / Series
  -> FastAPI 创建 DownloadTask
  -> RabbitMQ download_tasks
  -> Downloader Worker 执行 C-MOVE
  -> storescp 接收 DICOM
  -> Checker 扫描 data/fileserver，裁定完整性（唯一裁判）
  -> MySQL 更新任务状态
  -> 仅失败时发 repull_events -> Agent 重新诊断并提补拉方案（须人工审批）
```

组件职责边界：

| 组件 | 职责 | 不做什么 |
| --- | --- | --- |
| Downloader Worker | 执行 C-MOVE；失败时发 `failure_stage=move` 失败事件 | 不判定完整性 |
| Checker | **完整性唯一裁判**：`local >= expected` → success；`expected` 缺失 → unverified；超时仍不完整 → fail。success/unverified 同事务直接结束关联 `AgentRun`，不发消息；仅超时不完整发 `failure_stage=integrity` | 不做根因诊断 |
| Agent Worker | 对**已确认失败**的任务做根因诊断、产出补拉方案 | 不重复证明完整性、不自动执行补拉（必经人工审批） |

## Agent 架构与记忆分层

- LangGraph 编排路由、诊断 ReAct 循环、状态流转、工具调用和人工审批；执行期数据统一保存在 `AgentState`。
- Redis Checkpointer 按 `thread_id` 持久化 State，负责多轮上下文、任务中断恢复和审批 `interrupt/resume`；`run_id` 只标识单次执行。
- MySQL 的 `agent_run` 保存任务与最终诊断；`agent_execution_log` 只追加节点、事件、状态摘要和 `tool_id` 引用；`agent_tool_evidence` 按唯一 `tool_id` 独占保存完整工具参数、输出与成功状态；`agent_action_audit` 保存经审批的写操作。
- Chroma RAG 是独立业务知识库，不承担 Agent checkpoint，也不接收运行时会话经验；执行经验保存在 MySQL 诊断与轨迹记录中。上下文采用 Token Budget + Summary + Recent Raw Messages，滑动窗口只在仍超预算时兜底。
- 固定 System Prompt 和 Tool Definition 保持稳定前缀，以利用 `qwen-plus` 自动启用的隐式 Prompt Caching；缓存不属于 Memory。
- Prompt 文本统一托管在 `config/prompts.yml`：包含版本号以及 Reason、诊断、路由、反思、补拉计划、Query Rewrite、RAG 回答和会话摘要的静态规则。Python 装配器将用户、工具、证据和检索内容作为独立 XML 块转义注入；不引入在线 Prompt 平台或 A/B 系统。

诊断引用不再按工具名读取“最近一次结果”。每次工具调用都会生成唯一 `tool_id`，模型使用 `{tool_id, field, value}` 引用具体调用；近期证据直接在 State 中校验，超过 State 窗口的证据按 `tool_id` 从 MySQL 回查。

诊断采用明确的两段式护栏：`verify_diagnosis` 只确定性校验引用的 `tool_id`、会话归属、调用成功状态、字段和值；`reflect` 再统一判断证据充分性、推理有效性和过度归因，并输出 `supported / overstated / unsupported`。后两种状态会按规则降低置信度，`uncertain` 诊断不会生成补拉计划。

`observe` 同时承担轻量 Rule Validator：瞬时工具错误按相同参数自动重试一次，明确错误交回 ReAct 决策；只有 Action、Input 和 Observation 连续不变才触发无进展熔断。首次正常返回的 `unsupported` Reflection 可回到 Reason 补证一次，第二次仍不支持则降为 `uncertain`。模型调用异常不会触发该回环。

MySQL 保存 Agent Run 的 `source_id` 并以唯一 `idempotency_key` 作为审批写操作的最终防重边界；Redis 仅承担快速锁。Downloader 只领取 `in_queue` 任务，Abort 同时持久化取消任务，避免重复消息或 Redis 标记过期后错误执行。

详细边界和恢复语义见 [docs/architecture.md](docs/architecture.md)。

## Hybrid RAG 知识库

项目只有一个在线 RAG 入口：`app.agent.rag.pipeline.search_knowledge(query, context="", category=None, top_n=None)`。知识问答节点、Agent `StructuredTool` 和 MCP 都直接绑定这个函数，不各自封装检索逻辑。处理链固定为：查询规范化/可选上下文改写 → Chroma Dense Top-20 与本地 BM25 Top-20 → RRF(k=60) Top-20 → `qwen3-rerank` → 默认 Top-5。Chroma 显式使用 HNSW：`space=cosine`、`ef_construction=100`、`ef_search=100`、`max_neighbors=16`。启用的任一阶段失败都会直接报错，不使用原查询、单路召回、RRF 顺序或检索摘要兜底。

官方语料清单保存在 `app/agent/rag/sources.yml`，文档本体和下载时的 URL、时间、SHA-256 写入被 Git 忽略的 `data/rag/sources/`。额外 PDF、DOCX、HTML、JSON 可放进 `data/rag/sources/local/`。首版不支持旧 `.doc` 和 OCR；清单文件缺失、文档解析失败或扫描 PDF 无正文都会终止索引构建。PDF 按编号章节并延续跨页章节，DOCX/HTML 保留多级标题路径，JSON 保持知识原子边界；各章节内按段落和句子优先切分，再限制为 800 字符、重叠 120 字符（15%），不同章节不产生重叠。每个 chunk 保存 `doc_id`、`document_hash`、标题、章节、页码范围和内容哈希。

索引首次建立使用全量初始化；文档替换或新增后运行 `python -m app.agent.rag.init_knowledge --incremental`。增量流程先比较 `doc_id + document_hash`，只解析和 Embedding 变化文档，对其 chunk 执行 upsert，并删除已删除文档的旧 chunk；未变化文档不会重复 Embedding。BM25 使用更新后的统一 Chroma corpus 重建一次，保证两路索引不出现 chunk 不一致。

在线请求先由 `route_request` 做意图分类：只有 `knowledge_qa` 设置 `use_rag=true` 并进入知识问答节点，`diagnosis`、`first_pull` 和 `clarification` 不会自动经过 RAG。Agent Tool/MCP 仍注册同一个 `search_knowledge` 函数，但诊断循环调用它会被意图闸门拒绝。

下载官方资料并初始化双索引：

```bash
docker compose --profile init run --rm rag-init
```

直接在 Python 环境运行：

```bash
python -m app.agent.rag.download_sources
python -m app.agent.rag.init_knowledge
python -m app.agent.rag.evaluate
```

初始化需要 `DASHSCOPE_API_KEY` 生成 Dense embedding；在线查询改写只在 `context` 非空时调用 `qwen-plus`。完整来源文档不会提交仓库。

## Windows + Ubuntu 虚拟机开发说明

本项目推荐在 Ubuntu 虚拟机内运行 Docker Compose。Windows 只作为宿主机和 VS Code 客户端使用。

请将项目放在 Ubuntu 文件系统中，例如：

```bash
mkdir -p ~/projects
cd ~/projects
git clone <repo> medical-pacs-pulldata-agent
cd medical-pacs-pulldata-agent
```

Windows 上的 DICOM 数据需要复制到 Ubuntu 项目目录下：

```text
medical-pacs-pulldata-agent/data/dicom_samples/
```

可用 VS Code Remote SSH 上传，也可以在 Windows PowerShell 中执行：

```powershell
scp -r "D:\UII_PACS_Agent_Data\1.20251203000331" <ubuntu_user>@<ubuntu_ip>:/home/<ubuntu_user>/projects/medical-pacs-pulldata-agent/data/dicom_samples/
```

## 测试数据集

原始 DICOM 数据位于 `data/dicom_samples/1.20251203000331/`，共 30 个 series、4459 个 `.dcm` 实例（6.0 GB）。日常开发测试使用按字母序的**前 10 个 series**，约 2100 个实例，已足够覆盖多 series 导入、C-FIND、C-MOVE 补拉、Checker 扫描、Agent 诊断等核心链路。

使用 `--series-limit 10` 导入的 series（字母序前 10）：

```text
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000007   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000070   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000133   ( 116 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000360   (  62 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000484   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000547   (  34 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000610   (  29 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000663   (  29 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300000716   ( 962 实例)
1.3.12.2.1107.5.2.50.176780.30000025120408263809300002644   ( 802 实例)
```

> 注：每个 series 目录下还可能包含 `.mhd`（MetaImage 头）、`.vol`（体数据）、`.png`（预览图）、`series.mark` 等辅助文件，导入脚本只读取 `.dcm` 文件。

测试 StudyInstanceUID：**`1.20251203000331`**

## 本地启动

先启动基础服务：

```bash
docker compose up -d mysql redis rabbitmq orthanc
```

再启动业务服务：

```bash
docker compose up --build -d pull-data-api pull-data-storescp pull-data-worker pull-data-checker agent-worker
```

健康检查：

```bash
curl http://localhost:8000/health
```

Agent 平台入口：

```bash
curl http://localhost:8000/agents/info
curl -X POST http://localhost:8000/agents/pacs-diagnostician/invoke \
  -H 'Content-Type: application/json' \
  -d '{"message":"检查任务状态","thread_id":"demo-thread"}'
```

PACS Run 业务命令统一使用 `/pacs/runs/*`：创建 Run 使用 `POST /pacs/runs`，审批使用
`POST /pacs/runs/{run_id}/approval`，止损和取消分别使用 `/abort`、`/cancel`。旧
`/agent/*` 路由默认关闭；迁移期间如需临时启用，设置 `LEGACY_AGENT_API_ENABLED=true`。

## 导入测试 DICOM 到 Orthanc

导入前 10 个测试 series（约 2100 个实例）：

```bash
python3 scripts/import_dicom_to_orthanc.py \
  --input-dir data/dicom_samples/1.20251203000331 \
  --orthanc-url http://localhost:8042 \
  --series-limit 10
```

导入后验证：

```bash
curl http://localhost:8042/studies
# 预期返回 1 个 Study、10 个 Series、~2100 个 Instances
```

如需导入全部 series（4459 个实例，约 6 GB），去掉 `--series-limit` 参数即可。

Orthanc 默认 HTTP 端口是 `8042`，DICOM 端口是 `4242`，AE Title 是 `ORTHANC`。

## PACS 测试

C-ECHO：

```bash
curl -X POST http://localhost:8000/pacs/orthanc-local/echo
```

C-FIND Study：

```bash
curl "http://localhost:8000/pacs/orthanc-local/studies?study_instance_uid=1.20251203000331"
```

C-FIND Series：

```bash
curl http://localhost:8000/pacs/orthanc-local/studies/1.20251203000331/series
```

## 创建补拉任务

Study 级补拉默认会先 C-FIND Orthanc，并将真实 Study/Series 元数据写入 MySQL：

```bash
curl -X POST http://localhost:8000/tasks/pull \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "orthanc-local",
    "level": "study_level",
    "study_instance_uid": "1.20251203000331",
    "priority": 5
  }'
```

Series 级补拉：

```bash
curl -X POST http://localhost:8000/tasks/pull \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "orthanc-local",
    "level": "series_level",
    "study_instance_uid": "1.20251203000331",
    "series_instance_uid": "12.2.1107...00716_001"
  }'
```

如确实需要手动创建任务，可传 `"skip_pacs_find": true`，同时建议显式传入 `expected_image_number`。

查询任务：

```bash
curl http://localhost:8000/tasks/{task_id}
```

重试任务：

```bash
curl -X POST http://localhost:8000/tasks/{task_id}/retry -H "Content-Type: application/json" -d '{"force": false}'
```

取消任务：

```bash
curl -X POST http://localhost:8000/tasks/{task_id}/cancel
```

扫描本地文件：

```bash
curl http://localhost:8000/studies/1.20251203000331/local-scan
```

## 端到端验收

```bash
# 1. 启动基础服务
docker compose up -d mysql redis rabbitmq orthanc

# 2. 导入前 10 个测试 series 到 Orthanc
python3 scripts/import_dicom_to_orthanc.py \
  --input-dir data/dicom_samples/1.20251203000331 \
  --orthanc-url http://localhost:8042 \
  --series-limit 10

# 3. 查看 Orthanc 是否有 Study (预期 10 Series, ~2100 Instances)
curl http://localhost:8042/studies

# 4. 启动业务服务
docker compose up --build -d pull-data-api pull-data-storescp pull-data-worker pull-data-checker agent-worker

# 5. 测试 C-ECHO
curl -X POST http://localhost:8000/pacs/orthanc-local/echo

# 6. 查询 Study
curl "http://localhost:8000/pacs/orthanc-local/studies"

# 7. 创建补拉任务
curl -X POST http://localhost:8000/tasks/pull \
  -H "Content-Type: application/json" \
  -d '{"source_id":"orthanc-local","level":"study_level","study_instance_uid":"<StudyInstanceUID>"}'

# 8. 查看接收文件数
find data/fileserver -type f | wc -l

# 9. 查看 MySQL 任务状态
docker exec -it pulldata-mysql mysql -u pulldata -ppulldata pulldata -e "
SELECT task_id, study_instance_uid, status, expected_image_number, last_error
FROM download_task
ORDER BY id DESC
LIMIT 5;
"

# 10. 查看 storescp 接收记录
docker exec -it pulldata-mysql mysql -u pulldata -ppulldata pulldata -e "
SELECT image_name, study_instance_uid, series_instance_uid, file_path, received_at
FROM storescp_image
ORDER BY id DESC
LIMIT 10;
"
```

## Agent 诊断

平台 Agent Service 的新入口是 `/agents/*`；旧 `/agent/*` 仅在
`LEGACY_AGENT_API_ENABLED=true` 时启用，迁移期用于兼容旧客户端。

```bash
curl http://localhost:8000/agents/info
curl -X POST http://localhost:8000/agents/pacs-diagnostician/invoke \
  -H 'Content-Type: application/json' \
  -d '{"message":"检查任务状态","thread_id":"demo-thread"}'
```

PACS 业务命令使用 `/pacs/runs/*`，通用消息流使用 `/agents/{agent_id}/stream`。
审批只能通过 `/pacs/runs/{run_id}/approval` 进入固定的人工闸门。

创建异步诊断 Run；响应同时返回 `run_id` 和 `thread_id`：

```bash
curl -X POST http://localhost:8000/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"诊断这个补拉任务","task_id":"替换为任务 UUID"}'
```

批量诊断多个 Study（单次最多 20 个，默认并发 4）：

```bash
curl -X POST http://localhost:8000/agent/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message":"诊断这些 Study 是否缺失并生成补拉计划",
    "study_instance_uid_list":["1.20251203000331","1.20251203000332"],
    "source_id":"orthanc-local"
  }'
```

批量 Run 的 `run_id` 是整个批次的执行标识；每个 Study 的下载任务仍有独立 `task_id`。查询同一个 Run 时，响应中的 `batch_summary` 和 `study_results` 分别提供聚合统计与 Study 明细。

后续对话复用响应中的 `thread_id`，Redis Checkpointer 会恢复对应 State：

```bash
curl -X POST http://localhost:8000/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"继续分析刚才的问题","thread_id":"替换为会话 ID"}'
```

如果 `/agent/chat` 返回 `409` 和 `detail="thread_id 已被占用，请使用新的 thread_id 重试"`，客户端必须生成新的 `thread_id` 后重试；不要等待原请求结束后继续复用冲突 ID。

查询 Run 与订阅过程事件：

```bash
curl http://localhost:8000/agent/runs/{run_id}
curl -N http://localhost:8000/agent/runs/{run_id}/stream
```

## 常见失败场景

| 场景 | 诊断方向 |
| --- | --- |
| PACS 不可达 | 网络、端口、AE Title、Orthanc 服务状态 |
| Study 查不到 | StudyInstanceUID 或 StudyDate 条件错误，上游未入库 |
| 任务停留 in_queue | RabbitMQ 队列堆积或 pull-data-worker 未运行 |
| downloaded 后本地文件为 0 | storescp 未收到文件，Move Destination AE 或 11112 端口配置错误 |
| 文件数量不完整 | C-MOVE 部分失败、PACS 数据不完整或 storescp 保存失败 |
| expected count 缺失 | checker 只能标记 unverified，不会把任务误判为 success |

## 数据库

数据库初始化脚本位于 `config/schema.sql`。Compose 会在 MySQL 首次启动时自动执行该脚本，创建 `study`、`series`、`download_task`、`local_dicom_file`、`archive`、`storescp_image` 等表。
