-- Persist PACS source, enforce durable action idempotency, and optimize active-run lookups.
ALTER TABLE agent_run
  ADD COLUMN source_id VARCHAR(64) NOT NULL DEFAULT 'orthanc-local' AFTER user_id,
  ADD KEY idx_source_id (source_id),
  ADD KEY idx_agent_run_thread_status_created (thread_id, status, created_at),
  ADD KEY idx_agent_run_task_status_created (task_id, status, created_at);

ALTER TABLE agent_action_audit
  MODIFY COLUMN idempotency_key VARCHAR(128) NOT NULL,
  ADD UNIQUE KEY uk_agent_action_idempotency (idempotency_key);
