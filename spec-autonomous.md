# PACS 补拉自主诊断 Agent — 技术规格（自主 Agent 版）

> 本文档面向"学习本项目、准备 Agent 岗面试"。它讲的不是代码怎么写，而是**每个设计为什么这么做**。
> 核心命题一句话：**自主推理，接地执行（Autonomous reasoning, grounded execution）**。
> 设计信条：**不过度设计，保证优雅**——每引入一个组件，必须回答"删掉它会缺什么"。

## 1. 项目定位与要解决的问题

### 1.1 业务背景

PACS（医学影像归档与通信系统）通过 DICOM 协议在医院内传输影像。一次"补拉"是指：本地系统向远端 PACS 发起请求，把某个检查（Study）或序列（Series）的影像拉取到本地。补拉链路涉及多个环节，任一环节出问题都会导致"数据不全"：

```text
PACS 连通(C-ECHO) → PACS 查询(C-FIND) → 补拉任务入队 → C-MOVE 传输
   → storescp 接收落盘 → 本地扫描解析 → 完整性校验
```

### 1.2 要解决的真实痛点

运维人员面对"这个检查怎么没拉全"时，需要**跨 7 个环节逐一排查**，判断故障卡在哪、缺了哪些数据、该怎么补。这件事有三个特征，正好决定了它适合自主 Agent：

- **调查路径不固定**：PACS 不可达就没必要查本地文件；部分缺失才需要下钻定位缺哪些。下一步查什么，取决于上一步查到什么。
- **根因来自多信号组合**："任务卡在 in_queue + 队列有积压 + 消费者数为 0" 三个信号叠加才能推出"下载 Worker 挂了"。组合是爆炸性的。
- **存在未知故障**：规则没预设过的证据组合，需要基于领域知识做假设并验证。

### 1.3 Agent 的能力边界（先划清楚，避免过度设计）

| 能做 | 不做 |
|---|---|
| 只读诊断：自主调查、定位根因、评估完整性 | 不直接改 PACS/DB/容器配置 |
| 产出**补拉计划**（范围+策略+依据） | 不自主执行写操作（必须人工批） |
| 智能补拉：重试/定向 C-MOVE/上报 | 不做脱敏/合规模块（非本项目范围） |
| 统一入口下编排首次拉取（确定性直达，不进推理循环） | 不把首拉硬塞进多轮 ReAct（无推断空间，见 §10.1） |
| 知识问答（DICOM/补拉链路稳定知识） | 不做临床医学判断 |

> **入口统一的边界（二期）**：所有请求进同一入口，但 Agent 只对"需要探索式调查"的诊断/补拉走 reason↔act 循环；首次拉取这种"建任务→投队列、无多信号推断"的动作走**确定性直达节点**，不进循环、不需审批。统一的是入口与 Run 概念，不是"让首拉也逐轮推理"——这恰恰是 §10.1 反向界定的落地：无探索空间就不套 Agent。

## 2. 核心设计哲学：为什么是自主 Agent

### 2.1 一个反例先讲清楚（面试高频追问）

一种常见的"安全"做法是**受约束混合模式**：代码写死"必须调哪些工具、按什么顺序"，LLM 只负责提名工具和写解释。这种做法在本场景有个致命问题——

> **如果证据集合和调查顺序是固定的，那 LLM 就是可有可无的：纯代码就能跑完整条链路，LLM 只是个可降级的展示位。**

这不是 LLM 的价值，这是"为了用 LLM 而用 LLM"。面试官一眼看穿。

### 2.2 职责划分

LLM 的价值只在"决策"，不在"计算"

| 职责 | 谁来做 | 为什么 |
|---|---|---|
| **决定下一步查什么** | LLM（自主） | 路径不固定，依赖上一步结果，代码写死会变规则地狱 |
| **关联多信号推断根因** | LLM（自主） | 组合爆炸，代码枚举不完 |
| **规划补拉范围与策略** | LLM（自主） | 需要权衡代价，属于判断而非计算 |
| **算数、计数、完整性** | 代码工具（确定性） | 必须精确可复现，LLM 会算错 |
| **执行写操作** | 人工审批后由代码执行 | 不可逆，医疗高风险 |

一句话：**LLM 负责"想"，工具负责"做"，人负责"批"。** LLM 的自主性体现在"想"，而"想"恰恰是纯代码做不好的部分。这就回答了"为什么需要 LLM"。

### 2.3 与"纯自主"也要划清界限

纯自主 ReAct（模型爱调什么调什么、爱算什么算什么、爱写什么写什么）在 demo 里好看，在医疗场景是灾难：会漏调、算错、幻觉、无限循环、误触发写操作。

所以本设计的定位是**中间态**，也是它真正的技术含量所在：

> **给 LLM 推理自主权，用 Harness（护栏层）约束它的"手"和"嘴"——能自由地想，但不能陈述未经核实的事实、不能算错数、不能不批就写。**

这既不是"纯约束"（LLM 是摆设），也不是"纯自主"（危险），而是**自主性与安全性的平衡工程**。

## 3. Agent 核心循环（本项目的心脏）

### 3.1 从"直线"到"循环"

自主性的物理载体是一个**推理循环**，而非固定流水线。用 LangGraph 的带条件边的环实现：

```text
         ┌───────────────────────────────────────────┐
         ▼                                            │
    ┌─────────┐                                       │
    │ reason  │  LLM：基于当前证据，更新根因假设，       │
    │ (LLM)   │       决定"还需要查什么"或"够了"          │
    └────┬────┘                                       │
         │                                            │
    证据够了? ──否──→ ┌──────┐   ┌─────────┐           │
         │           │ act  │──→│ observe │───────────┘
         是          │(工具) │   │(证据回灌)│
         │           └──────┘   └─────────┘
         ▼
    ┌──────────┐   ┌──────────┐   ┌──────────────┐
    │ diagnose │──→│ reflect  │──→│ plan_repull  │
    │(结构化根因)│   │(自我批判) │   │(补拉计划,可选)│
    └──────────┘   └──────────┘   └──────┬───────┘
                                          │需要写操作
                                          ▼
                                   ┌──────────────┐
                                   │human_approval│（唯一写闸门）
                                   └──────┬───────┘
                                    approve│
                                          ▼
                                   execute + audit
```

### 3.2 每个阶段在做什么

- **reason（LLM 自主）**：读取已收集的证据，更新对"根因是什么"的假设，并决定下一个动作——继续调工具，还是证据已足够可以下结论。这是自主性的核心：**下一步不是代码规定的，是模型基于当下判断的。**
- **act（执行工具）**：执行 LLM 选定的工具调用。参数经 Pydantic 校验（护栏，见 §5）。
- **observe（证据回灌）**：把工具返回的结构化结果追加进证据集，回到 reason。这就是 ReAct 的"多轮"——看到新证据再想下一步。
- **diagnose（结构化输出）**：产出根因结论，**每条断言必须带证据引用**（护栏 2）。
- **reflect（自我批判）**：终局前一次自检——"结论真能由所引证据推出吗？有没有过度归因？"（护栏 7）。
- **plan_repull（LLM 自主）**：若判断需要补拉，产出参数化补拉计划（见 §6）。
- **human_approval → execute**：写操作唯一通道，人工批准后才执行（护栏 4）。

### 3.3 循环如何终止（这是优雅的关键）

自主循环最怕"停不下来"。三个确定性的终止条件，全部由代码控制、不交给 LLM：

1. **LLM 主动收敛**：reason 判断证据已足够 → 进入 diagnose。
2. **预算耗尽**：达到 max_iterations 或 token 上限 → 强制用现有证据下结论（可能是 uncertain）。
3. **无进展熔断**：连续两轮提出完全相同的工具+参数 → 判定卡死，强制跳出。

> 面试要点：**自主 ≠ 无边界**。给模型选择权（查什么、怎么推），但把"什么时候必须停"这种安全属性留给代码。这是"优雅"的体现——控制权放在最合适的层。

## 4. 工具体系：给自主决策留出真实空间

### 4.1 设计原则：工具粒度决定自主性的上限

如果只有 4 个粗粒度工具、且每次都得全调，那 LLM 没有"选择"可做，自主就是空话。**工具要拆得够细，让"调不调、先调哪个、要不要下钻"成为有意义的决策。**

但也不能无限拆——每个工具都要能回答"删掉它，Agent 会缺哪种判断能力"。凑数的工具就是过度设计。

### 4.2 工具清单

**只读工具（LLM 自主调用）**

| 工具 | 回答什么问题 | 让 LLM 能做的决策 |
|---|---|---|
| `query_pacs_target` | PACS 通不通？目标存不存在？ | 连通性/存在性判断，决定是否早停 |
| `query_pacs_hierarchy` | Study 下有哪些 Series/Instance？各多少张？ | **下钻**定位到底缺哪个 Series/Instance |
| `query_task_context` | 当前任务状态与阶段时间线？ | 判断卡在哪个阶段 |
| `query_task_history` | 这个目标历史上重试过几次、错误怎么演变？ | 判断是否反复失败（该上报而非再重试） |
| `query_worker_health` | 下载 Worker 存活？队列消费者数？积压？ | 定位"Worker 挂了/队列堵了" |
| `query_receive_status` | storescp 收了没？本地落盘多少？ | 判断接收端与本地情况 |
| `compute_integrity` | 期望 vs 实际，缺多少？ | **算数确定性工具**（见护栏 1），LLM 只解读不计算 |
| `search_knowledge` | 这类故障的 SOP/处理经验？ | **Agentic RAG**：LLM 自己决定何时需要查知识 |

**写工具（不绑定给 LLM 自由调用，仅审批后由固定节点执行）**

| 工具 | 说明 |
|---|---|
| `execute_repull_plan` | 唯一写操作。执行 LLM 产出、人工批准的补拉计划（见 §6） |

### 4.3 两个关键取舍

**取舍一：`compute_integrity` 为什么是工具，不是让 LLM 算？**
数量比对（缺 = 期望 − 本地唯一 SOP 数）必须 100% 精确可复现。做成 LLM 必调的工具后：**推理归 LLM（这些数字意味着什么故障），算术归代码（数字是多少）**。这一条化解了"要自主"和"算数必须准"的矛盾——它们本就不在一个层面。

**取舍二：`search_knowledge` 为什么是工具，不是固定的 RAG 节点？**
传统做法把 RAG 设成必经节点，每次都检索。但很多故障（如 PACS 直接不可达）根本不需要查知识库。做成工具后，**LLM 自己判断"这个故障我需不需要外部知识"**——这才是 Agentic RAG，检索成为自主决策的一部分，而非流程负担。

## 5. Harness 护栏层（本项目最有技术含量的部分）

这一节回答你要的"优化 harness、减少幻觉"。给了 LLM 自主权，就必须有一层东西保证它在医疗场景不闯祸。**七道护栏，每一道针对自主性带来的一种具体风险。** 记住一个总原则：

> **护栏约束的是 LLM 的"手"（能调用什么）和"嘴"（能断言什么），不约束它的"脑"（怎么推理）。**

### 5.1 护栏 1：数字永不出自 LLM

- **风险**：LLM 算术不可靠，"缺 25 张"可能是编的。
- **做法**：完整性计算封装为 `compute_integrity` 工具，LLM 想知道缺多少必须调它，不许自己算。
- **本质**：把"确定性计算"从"概率性推理"里剥离。推理可以自主，算术必须确定。

### 5.2 护栏 2：断言附带来源引用（抗幻觉主力）

这是整个 Harness 的核心。

- **风险**：LLM 陈述工具从没返回过的"事实"（幻觉归因）。
- **做法**：诊断输出的每一条事实断言，**必须附带来源引用**（哪个工具的哪个字段、值是多少）。一个确定性校验器逐条核对：被引用的字段是否真实存在于该工具的输出、值是否匹配。

```json
{
  "claim": "该 Series 期望 120 个 Instance，本地仅 95 个",
  "evidence_refs": [
    {"tool": "query_pacs_hierarchy", "field": "series[C].instance_count", "value": 120},
    {"tool": "compute_integrity", "field": "local_unique_sop", "value": 95}
  ]
}
```

- **裁决**：**引用缺失、或引用的值与工具实际输出对不上 → 判定为幻觉，打回重述（reflect 阶段修正，最多一次），仍不过则降级为 uncertain。**
- **本质**：LLM 只能"知道"工具真实返回过的东西。它可以自由推理这些事实**意味着**什么，但不能凭空捏造事实**本身**。

### 5.3 护栏 3：唯一真相来源 = 结构化工具输出

- **风险**：LLM 用训练知识"脑补"本系统状态（比如臆断某个默认端口、某张表结构）。
- **做法**：LLM 不接触 DB/FS/Shell，一切认知只能来自 Pydantic 强类型的工具返回。
- **本质**：切断"脑补"的信息来源。系统的真实状态只有一个入口——工具。

### 5.4 护栏 4：写操作仍是人工闸门（不可退让）

- **风险**：自主 Agent 误触发不可逆的补拉/重试。
- **做法**：补拉计划再"智能"，也必须过 `human_approval` + Redis 幂等锁 + 审计表，人批准后才由固定节点执行。
- **实现（canonical HITL interrupt/replay）**：`plan_repull` 产出计划后进 `human_approval` 节点，用 LangGraph `interrupt()` 暂停、把执行栈存进 checkpoint；审批接口用 `Command(resume={action})` 从暂停处恢复，approve 时图内 `execute` 固定节点调 `execute_repull_plan` 执行写操作，reject 直接收尾。写工具**仍不进 LLM 只读白名单、不经 MCP 暴露**（`execute_repull_plan not in READ_ONLY_TOOLS` 断言保持），"由固定节点执行"落到图内 `execute` 节点——比图外 API handler 更贴"人批准后才执行"。Redis 幂等锁同时是 replay 重复执行的天然防线。
- **本质**：**自主性到"提出计划"为止，绝不延伸到"擅自执行"。** 这是所有护栏失效后的最终兜底——即便前面全被幻觉突破，也还有一个人在按钮前面。

### 5.5 护栏 5：循环收敛守卫

- **风险**：ReAct 死循环、成本失控。
- **做法**：`max_iterations` 上限 + token 预算上限 + 无进展熔断（连续两轮相同工具+参数即跳出）。全部由代码控制。
- **本质**：把"何时必须停"这个安全属性，从 LLM 手里收回到代码。

### 5.6 护栏 6：置信度与弃权

- **风险**：证据不足时，LLM 倾向于"编一个确定结论"讨好用户。
- **做法**：LLM 必须输出 `confidence`；关键证据缺失或低置信 → 结论为 `uncertain`，**不产出补拉建议**，转而说明"还缺哪些证据"。
- **本质**：**弃权比幻觉安全。** 允许 Agent 说"我不确定"，是医疗场景的必备美德。

### 5.7 护栏 7：反思自检 + 全程可回溯

- **风险**：推理链中途跑偏，或过度归因。
- **做法**：终局前一次 `reflect` 自我批判（"结论真能由所引证据推出吗？"）；同时用 LangSmith 记录每一步 reason/act/observe 为可回放的 trace。
- **本质**：一层软性语义兜底（reflect）+ 事后可审计（trace）。任何错误诊断都能回放定位到是哪一步、哪个证据出的问题。

### 5.8 护栏与自主性的关系（一张图记住）

```text
        LLM 的脑（推理）  ← 完全自主，不约束
              │
   ┌──────────┼──────────┐
   ▼          ▼          ▼
 护栏2/3     护栏1/5     护栏4/6
 管"嘴"      管"手"      管"不可逆动作+诚实"
(能断言什么) (能调用/循环) (写操作+弃权)
              │
              ▼
         护栏7：事后可回溯
```

## 6. 智能补拉（"智能"真正的落点）

### 6.1 从"重试 task_id"到"补拉计划"

旧思路的补拉是 `retry(task_id)`——盲目地把整个任务重跑一遍。自主 Agent 的补拉应该是**有范围、有策略、有依据**的计划：

```json
{
  "root_cause": "C-MOVE 传输中断，Study 下 Series C 完全缺失",
  "scope": {
    "level": "series",
    "missing_series": ["1.2.3.C"],
    "missing_instance_count": 25
  },
  "strategy": "targeted_cmove",
  "rationale": "整 Study 重拉浪费带宽且 Series A/B 已完整；仅对缺失的 Series C 发起定向 C-MOVE",
  "evidence_refs": [
    {"tool": "query_pacs_hierarchy", "field": "series[C].instance_count", "value": 120},
    {"tool": "compute_integrity", "field": "series[C].local_unique_sop", "value": 0}
  ],
  "confidence": "high"
}
```

### 6.2 三种策略，LLM 自主选择

| 策略 | 适用 | 体现的判断 |
|---|---|---|
| `retry_task` | 任务级瞬时失败（网络抖动） | 最省事，整任务重来 |
| `targeted_cmove` | 部分缺失，能定位到具体 Series/Instance | **最优雅**：只补缺的，省带宽省时间 |
| `escalate` | 反复失败 / PACS 侧问题 / 低置信 | 知道"这个不该自动补，该上报人" |

> 面试要点：`escalate` 这个选项很重要——它体现 Agent **知道自己能力的边界**。反复重试一个 PACS 侧根本没有的数据是有害的，自主 Agent 要有"该放手时放手"的判断。

### 6.3 补拉计划的执行闭环（Checker 是完整性唯一裁判）

补拉的 C-MOVE 由既有 Downloader Worker 异步执行、耗时不可控，所以补拉执行本身仍与诊断循环解耦——**不在循环内同步等 C-MOVE 跑完**。"确认补拉是否成功"这一步由 **Checker 独家裁定**，Agent 不再做第二遍完整性证明：

1. 人工审批通过 → `execute_repull_plan` 向补拉队列投递任务，Run 进入 `awaiting_repull` 状态（**不收尾，等 Checker**）。
2. Checker 按既有口径判定完整性（`local_count >= expected_count` → success；`expected_count` 缺失 → unverified；超时仍不完整 → fail）。
   - **success / unverified**：Checker **在同一个数据库事务内**把关联 `AgentRun` 直接置 `completed` 并写入验收结论（`integrity_result` = `complete` / `unverified`）——**不发任何消息**。事务提交后再做外部副作用（`mark_done` 结束 SSE、best-effort 写经验），失败只记日志，不回滚已提交的终态。
   - **fail**：只有两种失败会发 `repull_events` 消息——Downloader C-MOVE 失败（`failure_stage=move`）与 Checker 超时仍不完整（`failure_stage=integrity`）。消息只带 `task_id` + `failure_stage`，不带结论；期望数/本地数/错误详情以数据库中的 `DownloadTask`/`ArchiveModel` 为唯一事实来源。
3. Agent Worker 收到失败事件 → 找该 task 的 `awaiting_repull` Run → **重新运行正常诊断图**（`run_agent_streaming`，`intent=diagnosis`），产出**新的补拉提议**。新计划仍须走 `human_approval` 闸门，**禁止自动执行**。

> **为什么删掉复核子图**：Checker 已经用本地扫描 + PACS 期望数判定过完整性，Agent 再跑一遍 `compute_integrity`/`query_missing_instances` 只是用同样的数据重算同样的结论——两个裁判、一套事实，分叉时无法仲裁。改造后完整性口径只有一处（Checker），Agent 的职责回到它真正擅长的事：**对已经确认失败的任务做根因诊断并提方案**。
>
> **同一 Run、同一 thread 重跑**：重新诊断复用原 `run_id`（也就是原 LangGraph `thread_id`），不新建 Run——用户看到的是同一条会话继续推进。为此 `_initial_state()` 显式重置上一轮的周期字段（`diagnosis`/`reflection`/`repull_plan`/`approval_status`/`operator`/`action_result`/`stop_reason`/`hypothesis`/`citation_violations`/`evidence`），防止 checkpoint 里的旧结论泄漏进新一轮。
>
> **收敛守卫延伸到跨轮**：新一轮诊断若给不出可靠计划（`confidence=uncertain`），`plan_repull` 不产出计划、Run 直接 `completed`——**不自动再补拉**，避免"补拉↔诊断"无限成环。再补拉永远是新的人工决策。
>
> **不复活已终态 Run**：`process_repull_failure` 只认 `awaiting_repull` 状态，且进入重诊断前先把 Run 移出该状态（行锁 + 清空 `proposed_action`/`approval_status`/`operator`/`error`），重复失败消息、并发取消都幂等安全。非 `fail` 的事件记告警后安全忽略，不阻断队列。
>
> **事件丢失兜底**：Checker 主循环周期扫描 `awaiting_repull` 超时 Run（`repull_terminal_timeout_seconds` 默认 30 分钟）→ `completed` + 注明「等待补拉任务终态处理超时，请人工核查」，只负责释放永久卡住的 thread，**不把未知状态伪装成成功，也不写经验**。
>
> **可取消**：`POST /agent/runs/{run_id}/cancel` 可取消活跃 Run 并释放 thread；已投递的补拉/下载任务由 Downloader/Checker 独立驱动、照常后台跑，Run 离开 `awaiting_repull` 后只是不再被失败事件唤醒重诊断。人工止损/取消落 `CANCEL` 态，**不进失败事件管道**——那不是 PACS 故障，不该触发 Agent 诊断。
>
> **仍守的解耦**：失败事件唤醒走 **DB 状态 + 事件消息**，**刻意不用** LangGraph interrupt replay。这与审批闸门（§5.4 用 interrupt/replay）是有意的分工：审批是"人在环内、同一会话、同步等待"的 HITL，interrupt 是教科书场景；而补拉终态是"跨进程、异步、长窗口、完成信号来自另一个 Worker"的场景——interrupt 消不掉那个事件、只会在其上叠一层 checkpoint replay，反而更复杂。补拉作业本身仍是长后台任务，不阻塞会话线程。

## 7. 系统架构

### 7.1 全景

```text
用户/前端
   │  POST /agent/chat（自然语言 + 可选结构化 UID）
   ▼
Agent API（FastAPI）──创建 agent_run + 投递 RabbitMQ──▶ agent_runs 队列
   │  202 + run_id                                          │
   │  GET /agent/runs/{run_id}（轮询状态/结论）              ▼
   │  GET /agent/runs/{run_id}/stream（SSE 过程事件，只读）  Agent Worker
   │  POST /agent/runs/{run_id}/action（审批）        （执行 reason↔act 循环 + Harness）
   │  POST /agent/runs/{run_id}/cancel（取消，释放 thread）
   ▼                                                        │
 三层存储                                        审批后投递 ▼ 补拉队列
   ├─ agent_run 表（对外结果/状态）                          │
   ├─ Checkpoint（循环内部状态，可中断恢复）                  ▼
   └─ agent_action_audit（写操作审计，只追加）         Downloader Worker
                                                    （既有，执行 C-MOVE）
```

### 7.2 为什么异步 + 队列（不过度设计的体现）

- **API 立即返回 202**：Agent Run 不在 HTTP request 生命周期内执行，而由独立 Worker 消费队列执行。FastAPI 只负责创建 Run、状态查询和事件订阅，因此避免长时间占用 HTTP worker。诊断循环耗时不定（多轮 ReAct），API 不能被长任务占住，否则并发一上来就崩。
- **队列解耦**：Agent Worker 可水平扩展（无状态消费者，靠 run_id 隔离）。补拉触发也走队列，与既有 Downloader Worker 天然解耦。
- **不引入不需要的东西**：单实例场景 Checkpoint 用 SQLite 即可，不上分布式存储；可观测用 LangSmith 挂在旁路，不进关键路径。规模没到，就不预支复杂度。

### 7.3 三层存储职责分离（面试常问）

| 存储 | 存什么 | 谁读 | 特性 |
|---|---|---|---|
| Checkpoint | 循环内部完整状态（走到第几轮、证据集）+ 审批 interrupt 暂停态 | LangGraph 引擎（中断恢复 / HITL replay） | 每步覆盖 |
| `agent_run` 表 | 对外的结论、状态、补拉计划 | 用户轮询 API | 随执行更新 |
| `agent_action_audit` | 写操作是否执行、谁批的、幂等键 | 合规审计 | **只追加不更新** |

用同一个 `run_id` 串联，物理隔离、职责不重叠。Checkpoint 是"给引擎续跑的执行栈"，agent_run 是"给人看的公告板"，audit 是"给合规留的凭证"。

### 7.3.1 端点收敛：删除与 Agent 路径重叠的旧任务端点（2026-07-23）

自主 Agent 落地后，旧的手工任务端点与 Agent 路径功能重叠，且部分越过护栏。核查调用点后删除三个端点，**保留其底层函数**（单一真相，被 Agent/worker 复用）：

| 删除端点 | 重叠对象 | 底层函数（保留） | 删除理由 |
|---|---|---|---|
| `POST /tasks/pull` | Agent `first_pull` 节点 | `services.pull.create_pull_task` | 建任务逻辑已抽到 services 层，first_pull 直接复用，端点只是薄壳 |
| `POST /tasks/{task_id}/retry` | Agent 补拉路径（`plan_repull`→`human_approval`→`execute`） | 无（`execute_repull_plan` 是另一条路径） | 直接改任务状态投队列，**越过护栏 4 审批**；无代码调用点 |
| `GET /studies/{study_uid}/local-scan` | Checker Worker / Agent 工具 `query_receive_status` | `dicom.scanner.scan_local_dicom` | 本地扫描能力已被 checker/tool 两处复用，独立只读端点多余 |

连带删除仅这些端点使用的 schema（`RetryRequest`、`LocalScanResponse`）与辅助函数（`_sync_local_instances`）。

**第二轮收敛（2026-07-23）**：确认所有任务均从 `POST /agent/chat` 进入、无独立的非 Agent 下载任务后，进一步删除 PACS 探测路由与剩余 Task 路由：

| 删除端点 | 底层依赖（保留） | 删除理由 |
|---|---|---|
| `GET /pacs/sources`、`POST /pacs/{id}/echo`、`GET /pacs/{id}/studies`、`GET /pacs/{id}/studies/{uid}/series` | `PacsClient`（被 services.pull / agent.tools 直接调用） | 手工 PACS 探测/调试壳，零代码引用；study/series 查询已由 Agent 工具层承接 |
| `GET /tasks/{task_id}`、`POST /tasks/{task_id}/cancel` | `DownloadTask` 模型 | 所有任务从 `/agent/chat` 进，无独立下载任务查询/取消需求；Run 取消走 `/agent/runs/{run_id}/cancel` |

连带删除 `routes_pacs.py`、`routes_task.py` 整个文件、`main.py` 的两处挂载、schema（`TaskDetailResponse`、`CancelResponse`）。**保留** `PacsEchoResult`（PacsClient 内部用）、`get_pacs_source`/`pacs_sources` 配置。

**收敛后端点全景**：仅剩 Agent 5 端点（`/agent/chat`、`GET /runs/{id}`、`GET /runs/{id}/stream`、`POST /runs/{id}/action`、`POST /runs/{id}/cancel`）+ `/health`。所有 FastAPI 端点均由外部（前端/用户）调用，Agent 跑在 worker 进程从不 HTTP 调自身。

### 7.3.2 基础设施收敛：删除非必要的表 / Redis 键 / 消息字段（2026-07-23）

对 Redis、MySQL、消息队列做了「只写不读 / 写而无消费者」的核查，删除三处确认冗余项（DICOM 标准元数据字段作为领域建模刻意保留，不在此列）：

| 删除项 | 位置 | 冗余理由 |
|---|---|---|
| `dicom_received:%s` Redis 键 | `storescp.py` | 全仓唯一写点、零读点，每收一个 DICOM 就写一次 + 3600s TTL，无任何消费者 |
| `local_dicom_file` 表 | `models.py` / `schema.sql` / `checker_worker._sync_local_instances` | 唯一写点是 checker 的 `_sync_local_instances`，但计数走实时扫盘（`scan_local_dicom`）不查此表——全表只写不读 |
| `download_queue` 消息体冗余字段 | `pull.task_message` / `tools._submit_repull` | 消息带 `source_id/level/priority/task_body`，但 downloader 消费端只用 `task_id`，其余全从 DB 重查。消息瘦身为 `{task_id}`，消除消息与 DB 双写不一致 |

> **判定口径**：唯一 SOP 计数的权威来源是本地扫盘（`scan_local_dicom` 的 `total_sop_count`），`query_receive_status` 也走扫盘而非查 `local_dicom_file`，故删表不影响任何计数逻辑。storescp 落库仍写 `storescp_image` 表（`received_at` 作接收信号），未受影响。

> **审批闸门用 Checkpoint 做 HITL replay（二期）**：审批暂停态存进同一个 SqliteSaver（`check_same_thread=False`），API 进程创建 Run、worker 进程 `interrupt()` 暂停、API 进程 `Command(resume)` 恢复——三处跨进程共享同一 checkpoint 文件、以 `run_id` 为 thread_id 寻址。checkpoint 初始化失败不静默降级为无闸门执行，而是把 Run 标 `failed` 并注明（降级不伪装）。注意：业务 `thread_id`（会话串行）与 checkpoint 的 thread_id（=run_id）是两个概念，不混用。

### 7.4 会话模型：串行 + 并行（二期）

单 Run 是原子诊断单元，但真实使用是**多轮会话**。二期用 `thread_id`（会话）+ `user_id` 组织并发：

- **同一 thread 同一时刻只允许一个活跃 Run**：新请求进来若本会话已有 Run 在跑 → 返回 409 + 当前活跃 run_id，前端提示"本会话有任务进行中"。**不排队、不打断**——最简单，也契合"一个会话里看着任务跑完才能发下一个"的交互。
- **跨 thread 天然并行**：Agent Worker 无状态，靠 run_id/thread_id 隔离，多会话互不阻塞。
- **一个易混点**：业务 `thread_id`（决定谁和谁串行）与 LangGraph checkpoint 的 thread_id（单 Run 内中断恢复，用 run_id）是**两个概念**，不得混用。

> 面试要点：并发模型选"拒绝"而非"排队"，是刻意的——排队要引入等待队列与唤醒机制，而本场景一个会话本就该"看完一个再发下一个"。**需求驱动的简单，不是偷懒。**

### 7.5 可见过程：流式呈现推理轨迹（二期）

诊断循环耗时不定，用户不该盯着一个转圈等结论。二期把循环的每一步实时推给前端——但呈现的是**过程事件流**，不是逐 token 的"思考文字"。

- **推什么**：每轮 `reason` 的假设、选定的 tool_call（要查什么）、`observe` 的证据摘要（查到什么）、最终 `diagnose`/`reflect`/`plan_repull`。补拉失败后**重新诊断**（§6.3）复用同一 `run_id`，因此跑的是同一套节点、发同款过程事件，进同一 thread 的流——发起补拉到出结论的全程都可见。用户看到的是"Agent 在查什么、发现什么"的**推理轨迹**。
- **怎么推**：LangGraph 原生 `stream(stream_mode="updates")` 逐节点拿状态增量 → 写入 Redis list（per run，带 TTL）→ SSE 端点 `GET /agent/runs/{run_id}/stream` 轮询推前端。
- **为什么不是逐 token**：本设计的决策输出是**结构化 JSON**（护栏 2 要求带引用），结构化与逐 token 思考天然矛盾。过程事件流既保住了结构化与护栏，又比逐字输出更清晰——本质是把护栏 7 的可回溯 trace **实时化**。
- **只读 + 归属校验**：SSE 端点只读推送、绝不触发写；Run 有归属（`user_id`）时**强制校验**，缺 `user_id` 或不匹配一律 403，不能靠不传参绕过——避免跨会话看别人过程。LLM 不可用时过程事件照发但标 `degraded`，不伪装。

- **生产/消费分离（为何 /stream 不冗余）**：Agent 的"节点级流式"是**生产端**——worker 进程跑 `run_agent_streaming`，每过一节点 `publish_event` 写入 Redis；`GET /agent/runs/{run_id}/stream` 是**消费端**——读 Redis 透出 SSE 给前端。两者是同一管道两端，不重叠。在本架构中所有 FastAPI 端点均由外部（前端/用户）调用：Agent 跑在 worker 进程，靠队列/checkpointer/Redis 驱动，从不 HTTP 调自身接口，因此 Agent 是事件生产者而非 /stream 的消费者。

> 面试要点：这体现了对"可解释性"的正确理解——**用户要的是"凭什么这么判断"的证据链，不是模型的意识流**。流式只读呈现、不参与决策，写操作仍只经审批闸门（护栏 4 不变）。

## 8. 记忆与知识（RAG）

### 8.1 三种记忆，别混为一谈

| 记忆 | 载体 | 生命周期 | 作用 |
|---|---|---|---|
| 工作记忆 | 循环内的证据集 + Checkpoint | 单个 Run | 支撑多轮推理、中断恢复 |
| 领域知识 | ChromaDB 静态知识库 | 长期稳定 | DICOM 协议、状态码、故障 SOP，供 `search_knowledge` 检索 |
| 经验记忆 | ChromaDB，标 `source=experience` | 跨 Run 累积 | 人工确认过的真实诊断案例，回灌检索 |

### 8.2 经验记忆是自主 Agent 的"成长"

每当一条诊断被**现实验证通过**，就把"现象—证据—根因—补拉计划"结构化为一条经验记忆写入知识库。它的特殊性：

- **只沉淀被现实验证过的结论**，不是对话历史的堆积——所以能安全回灌，不会污染检索。
- 下次遇到同类故障，`search_knowledge` 能检索到"上次这种情况是这么处理的"。
- 这实现了自主 Agent 的**反思迭代闭环**：错误诊断经人工修正后，成为下一次的证据。

> **写入时机：现实验证通过而非审批通过**：人工审批只表示"我同意执行这个补拉计划"，**不代表补拉执行成功、根因分析正确**。所以经验不在审批时写，而是等 **Checker 验收判定 success**（`integrity_result=="complete"`）**且原诊断置信度为 `confirmed`** 时才写——此时"根因 + 补拉计划"才真正被现实验证。经验正文用**原始诊断**（root_cause/证据/计划，从 `proposed_action` 取），不是 Checker 的验收结论；`unverified`、超时兜底、或原诊断非 confirmed → 一律不写。写入由 Checker 在事务提交后 best-effort 触发（`write_verified_repull_experience`），失败只记日志，不影响已提交的 Task/Archive/Run 终态。
>
> **补拉方案不只记根因（三期补充）**：早期版本正文只有"现象—证据—根因"，检索到经验也不知道当时是怎么解决的。现在写入的 diagnosis 带一个 `repull_plan` 子字典（`strategy`/`level`/`missing_instance_count`/`target_count`/`outcome`，`level` 由 `derive_repull_level(missing_targets)` 现算），`build_experience_content` 把它渲染成正文第四行"补拉方案：strategy=... level=... ..."。之所以落进正文而不是 Chroma metadata，是因为 metadata 只支持标量字段，存不下嵌套结构；正文是唯一能带着完整方案回灌检索的地方。

> 面试要点：这是"长期记忆"最落地的一种实现——**用"现实验证"作为质量门，把被验证的案例变成可检索知识**。不需要复杂的记忆架构，一个带 metadata 标记的向量库就够了。这就是"优雅"。

**去重才让经验"记得住"（二期）**：如果每次审批都新增一条，同类故障会在库里堆成一片近义条目，污染检索。二期给经验记忆加**故障签名**：写入前让 LLM 把自由文本根因归一化为一个规范签名标签，用签名而非 run_id 做记忆的 id。

- 命中已有签名 → 更新计数（`hit_count`）与"最近一次"，而非新增——同类故障沉淀成**一条带频次的经验**。
- 为什么用 LLM 归一化而非结构化字段拼签名：本设计已把代码枚举的故障分类删掉（那是被批判的规则引擎），仅靠 level+完整性拼签名判别力太粗、会误并不同根因。归一化根因换来精准的去重边界，代价是每次写经验多一次 LLM 调用——但写经验只在验证通过后发生、频率低，可接受。
- **签名完全依赖 LLM 语义，不做粗签名兜底（二期修正）**：LLM 不可用或归一化失败 → **跳过本次经验写入**（记日志说明），而不是退回 `level+完整性` 拼一个粗签名。因为粗签名判别力差，会把不同根因误并、污染检索库——"降级不伪装"在这里的诚实形态是"不可用就不记"，而非"记一条粗糙的"。写经验本就低频（只在验证通过时），偶尔跳过不影响长期积累。

### 8.3 检索管道

`元数据过滤 → 向量召回 → 可选重排`。先按 `category/stage` 等结构化标签粗筛，再语义召回 Top-K，必要时重排取 Top-N 注入。重排是否默认启用，由离线评估（Recall@K、MRR）决定——**用数据说话，不凭感觉加组件。**

## 9. 一个完整案例走一遍（把全篇串起来）

用户输入："这个检查怎么没拉全？Study 1.2.840.xxx"

```text
reason①：目标是 Study，先确认 PACS 侧存不存在、期望多少。
  act →  query_pacs_target → observe：pacs_reachable=true, study_exists=true
reason②：PACS 有数据。查本地收了多少。
  act →  query_receive_status → observe：本地部分文件，数量对不上
reason③：部分缺失，需要下钻定位缺哪个 Series。
  act →  query_pacs_hierarchy → observe：3 个 Series，A/B 完整，C 期望 120
  act →  compute_integrity(series=C) → observe：C 本地 0，缺 120（护栏1：代码算）
reason④：Series C 完全没收到。是 C-MOVE 漏传还是接收端问题？查任务状态。
  act →  query_task_context → observe：任务 status=downloaded，无报错
reason⑤：任务称已下载但 C 一张没有 → C-MOVE 对 C 漏传。证据够了。
diagnose：根因=C-MOVE 传输时 Series C 漏传（每条断言带引用，护栏2 校验通过）
reflect：结论能由证据推出？能。无过度归因。（护栏7）
plan_repull：strategy=targeted_cmove, scope=Series C, confidence=high（护栏6）
human_approval：interrupt 暂停，展示诊断+证据+补拉计划 → 用户 approve（Command resume 恢复，护栏4）
execute：图内固定节点投递定向 C-MOVE，记 audit → Run 进 awaiting_repull 等 Checker 验收。
（Checker 判定 success 且原诊断 confirmed → 此时才把这条经验写入知识库，§8.2）
```

注意这里的自主性：**reason③ 决定"下钻"、reason④ 决定"查任务状态"，都不是代码规定的顺序，是模型看到证据后自己选的。** 而每一步的数字、事实、写操作，都被护栏兜住。这就是"自主推理，接地执行"。

## 10. 诚实的代价与适用边界

不讲代价的设计是不可信的。这套自主 Agent 有明确代价，必须主动说：

| 代价 | 缓解手段 |
|---|---|
| **成本/延迟上升**（多轮 ReAct 比单趟贵数倍） | 简单 case 走快速路径（PACS 不可达等强信号直接收敛）；token 预算上限；缓存 |
| **幻觉只能压不能消** | 护栏 2/6 大幅降低；残余风险靠护栏 4（人工闸门）兜底——这正是写操作绝不放开自主的根本原因 |
| **可复现性下降**（同输入可能走不同路径） | 用护栏 7 的全程 trace 换可审计性，弥补确定性损失 |
| **依赖 LLM 可用性** | LLM 不可用时降级为"只跑强信号规则 + 明确告知能力受限"，不假装正常 |

### 10.1 什么场景**不该**用这套设计（反向界定）

- 如果实际 99% 是结构化系统间调用、故障模式高度固定 → **纯后端规则引擎更合适**，自主 Agent 是过度设计。
- 如果写操作可完全自动化、无需人工 → 说明风险低，那 Human-in-the-loop 也是多余的。

> **这套设计的适用前提是：存在"需要探索式调查的真实故障多样性" + "写操作不可逆需人工把关"。** 补拉诊断恰好同时满足这两点，所以它值。能说清"什么时候不该用"，比只会吹"我用了 Agent"更能体现判断力。

这条判断力在二期进一步细化到系统内部：

> **同一系统内也要分流（二期落地）**：这个反向界定不止用于"要不要上 Agent"，也用于"系统内哪一步交给 Agent"。首次拉取和补拉诊断进的是同一个入口、同一套 Run，但首拉无探索空间 → 走确定性直达节点，诊断/补拉有多信号推断 → 走 reason↔act 循环。**把判断力用在"哪些步骤配得上循环"上，比"整个系统要么全 Agent 要么全规则"更成熟。**

## 11. 面试问答速查

**Q：这项目和普通"会调工具的后端"区别在哪？**
A：普通后端是代码决定调用顺序；这里是 LLM 基于每一步证据自主决定下一步查什么。区别在"决策权在谁手里"。

**Q：LLM 会算错、会幻觉，医疗场景怎么敢用？**
A：三层隔离——算数交给确定性工具（护栏1）、断言必须引用工具输出且逐条校验（护栏2）、写操作必须人工批（护栏4）。LLM 只负责"推理"，不负责"计算"和"执行"。

**Q：ReAct 会不会停不下来？**
A：三个代码级终止条件：LLM 主动收敛、预算耗尽、无进展熔断。"何时停"是安全属性，不交给模型。

**Q：为什么不做全自主（模型爱干嘛干嘛）？**
A：医疗高风险场景，全自主会漏调/算错/误写。本设计是"推理自主 + 执行受控"的平衡，不是能力不足。

**Q：为什么补拉要人工批，不自动执行？**
A：补拉是不可逆写操作。自主性到"提出计划"为止。这是所有护栏失效后的最终兜底。

**Q：这套的核心技术含量是什么？**
A：不是"会用 Function Calling"，是**设计了一套 Harness，让自主 Agent 在高风险场景既保留真实推理能力、又不失控**。自主性与安全性的平衡工程。

**Q：既然统一入口交给 Agent，首次拉取也走多轮循环吗？**
A：不。首拉是"建任务→投队列"、没有多信号推断空间，走确定性直达节点、不进循环、不需审批。统一的是入口与 Run 概念，不是让首拉逐轮推理——判断力用在"哪些步骤配得上循环"上（§10.1）。

**Q：怎么知道补拉成功了？用户要手动再查吗？**
A：不用。补拉执行后 Run 进 awaiting_repull，由 **Checker 独家裁定**完整性：success/unverified 直接在同事务里把 Run 收尾（不发消息）；只有 C-MOVE 失败（`move`）或超时仍不完整（`integrity`）才发失败事件，唤醒 Agent **重新跑正常诊断图**产出新提议。Agent 不做第二遍完整性证明——两个裁判一套事实无法仲裁（§6.3）。新提议仍须人工审批，不自动再补拉；事件丢失有 30 分钟超时兜底。

**Q：让用户"看到模型思考"，是流式输出 token 吗？**
A：推的是过程事件流（每轮查什么/发现什么/结论），不是逐 token。因为决策是结构化 JSON、与逐 token 天然矛盾；过程事件既保住结构化与护栏，又比意识流更清晰——本质是把可回溯 trace 实时化。

**Q：经验记忆会越堆越多吗？**
A：按故障签名去重。写入前让 LLM 把根因归一化为规范签名，命中同签名就更新计数而非新增，同类故障沉淀成一条带频次的经验。签名完全靠 LLM 语义，不做粗签名兜底——LLM 不可用就跳过写入（不伪造粗签名污染库）。且经验只在 **Checker 验收判定 success**（完整且原诊断 confirmed）时才写，审批通过≠验证通过。

## 12. 设计信条回顾

- **优雅 = 控制权放在最合适的层**：推理给 LLM，计算给工具，停止给代码，执行给人。
- **不过度设计 = 每个组件都能回答"删掉它会缺什么"**：工具不凑数，护栏不重叠，规模没到不预支复杂度。
- **诚实 = 讲清代价和边界**：知道什么时候不该用这套，比用了更重要。
