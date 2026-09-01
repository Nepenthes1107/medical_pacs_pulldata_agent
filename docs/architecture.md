# Agent memory architecture

The project separates volatile execution state, conversation continuity, durable audit data, and domain knowledge.

```text
Client -- thread_id --> LangGraph AgentState
                           | every graph super-step
                           v
                    Redis Checkpointer

LangGraph nodes ----------> MySQL agent_run / agent_execution_log

Agent RAG tools ----------> Chroma collection pacs_knowledge
```

## Storage boundaries

| Concern | Implementation | Retention and purpose |
| --- | --- | --- |
| Working memory | `AgentState` | Current route, task identifiers, evidence, tool results, retry/iteration counters and diagnosis |
| Short-term memory | Redis `RedisSaver` | Full checkpoint snapshots keyed by `thread_id`; supports multi-turn continuation, interrupt/resume and worker recovery |
| Long-term task records | MySQL | `agent_run` stores inputs, PACS source and final results; `agent_execution_log` stores append-only node/event/time/status summaries and `tool_id` references; `agent_tool_evidence` exclusively stores complete tool arguments, outputs and success state; `agent_action_audit` uses one unique idempotency row per approved write |
| Business knowledge | Chroma | Independent RAG collection for curated DICOM/PACS documents; it is never used as Agent memory or a LangGraph checkpointer |

`run_id` identifies one execution and links MySQL records and SSE events. `thread_id` identifies the conversation and is the only key passed to LangGraph's checkpointer. A new `/agent/chat` call may reuse a completed thread's `thread_id`; LangGraph then restores its messages, summary, and current diagnosis target while resetting run-local result fields. Supplying any new task/Study/Series target replaces the previous target scope. Execution experience stays in MySQL diagnosis and trajectory records instead of being written back into RAG.

Concurrent reuse is not continuation. If `/agent/chat` receives a `thread_id` owned by an active run or currently being created, it returns `409` with the minimal detail `thread_id 已被占用，请使用新的 thread_id 重试`. The client must create a new ID instead of retrying the conflicting ID after the first run finishes.

## Evidence identity and citation

`tool_results` is only a convenience snapshot keyed by tool name; repeated calls overwrite it and it is never used as the citation source of truth. Every invocation in `act` receives a UUID `tool_id` and is written to `agent_tool_evidence` with its complete arguments, output and success flag before the node returns.

`agent_execution_log.payload` is deliberately lightweight. For an `act` event it contains only the current `tool_ids` plus tool name/success summaries; it never copies `args`, `output`, `tool_results`, or the State evidence window. Full tool data has exactly one durable owner: `agent_tool_evidence`.

The State keeps only the latest 20 evidence records to bound checkpoint size. A diagnosis or repull-plan reference has the form `{tool_id, field, value}` and is verified as follows:

```text
evidence_ref.tool_id
  -> locate in recent AgentState evidence
  -> if absent, query agent_tool_evidence by thread_id + tool_id
  -> require success=true
  -> resolve field in the original output
  -> compare the cited value with the original value
```

This preserves a bounded working set in Redis without losing the full audit and citation chain in MySQL. Scoping fallback queries by `thread_id` prevents a model from citing evidence belonging to another conversation.

## Verify and Reflect boundary

`verify_diagnosis` is a deterministic authenticity gate. It checks only that every claim reference resolves to a tool call in the current `thread_id`, that the call succeeded, that the field exists, and that the cited value equals the original value. It does not require a particular tool such as `query_pacs_target`, assess evidence sufficiency, or decide whether a root cause follows from the claims. The first failure returns to `diagnose` once; the second removes ungrounded claims and changes confidence to `uncertain`.

`reflect` is the terminal inference gate. Both Verify and Reflect use the same State-first/MySQL-fallback resolver, so Reflect receives the tool name, arguments, field, and actual value for every cited fact even after that evidence leaves the 20-item State window. Reflection has one result field:

| `inference_status` | Meaning | Confidence effect |
| --- | --- | --- |
| `supported` | Claims sufficiently support summary and root cause | Keep current confidence |
| `overstated` | Some support exists, but wording, causality, or confidence is too strong | `confirmed -> high -> uncertain` |
| `unsupported` | Required evidence is absent or the root cause cannot be inferred | Set `uncertain` |

If reflection is unavailable or fails, the diagnosis is `uncertain`. The existing `plan_repull` gate therefore prevents an inference that was not validated from reaching human approval. Reflect does not rewrite claims, values, summary, or root cause. SSE `reflect` node events expose the same `{inference_status, reason}` object.

## Finite correction loop

`observe` is also the deterministic Rule Validator for the latest tool batch. A failed result with
`retryable=true` is repeated with the same Action and Input at most once; non-retryable failures and
exhausted retries are returned to `reason` as short `rule_findings`. A failed query remains a failed
query and cannot be converted into “Study/Series does not exist”. The loop stops as `no_progress`
only when two consecutive batches have the same Action, Input, and normalized Observation.

Verify and Reflect together are the diagnosis Final Validator rather than a new duplicate node. The
first normal `unsupported` Reflection may return to `reason` once so it can collect the missing
evidence named in `reason`; a second `unsupported` result becomes `uncertain` and cannot produce a
repull plan. An unavailable or failed Reflection call becomes `uncertain` immediately and does not
start another model loop. After an approved repull, the existing Checker remains the final data
integrity validator.

## Context policy

Before routing each turn, `manage_context` enforces the configured token budget:

1. Keep the latest `recent_message_count` raw messages.
2. Summarize older raw messages into `context_summary`.
3. If summary plus recent messages still exceed the budget, remove the oldest remaining raw messages as a sliding-window fallback.

The budgeted `conversation_context` is supplied to LLM nodes. Fixed system rules and tool definitions stay at the start of requests so provider-side prompt-prefix caching can reuse them. DashScope enables implicit context caching automatically for supported `qwen-plus` requests, so no application-side cache store or memory write is added. Prompt caching is an optimization only; no cached prompt is treated as memory.

## Operations

Redis 8 is required because `langgraph-checkpoint-redis` uses RedisJSON and RediSearch. The checkpointer runs `setup()` during process initialization and refreshes a 24-hour checkpoint TTL on reads by default.

For an existing MySQL database, apply `config/migrations/002_agent_memory_audit.sql` and then `003_minimal_redis_mysql_fixes.sql` before deploying the worker. New databases receive the same schema from `config/schema.sql`. Initialize the independent RAG collection explicitly:

```bash
docker compose --profile init run --rm rag-init
```
