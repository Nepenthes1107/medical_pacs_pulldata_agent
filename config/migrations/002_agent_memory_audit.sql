-- Existing deployments: add append-only Agent trajectory and immutable tool evidence tables.
CREATE TABLE IF NOT EXISTS agent_execution_log (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL,
  thread_id VARCHAR(64) NOT NULL,
  event_type VARCHAR(32) NOT NULL COMMENT 'node / tool_call / exception',
  node_name VARCHAR(64) DEFAULT NULL,
  payload JSON DEFAULT NULL COMMENT '轻量状态摘要与 tool_id 引用；不保存完整工具参数或输出',
  error TEXT DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_run_id (run_id),
  KEY idx_thread_id (thread_id),
  KEY idx_event_type (event_type),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_tool_evidence (
  id BIGINT NOT NULL AUTO_INCREMENT,
  tool_id VARCHAR(36) NOT NULL,
  run_id VARCHAR(64) NOT NULL,
  thread_id VARCHAR(64) NOT NULL,
  tool_name VARCHAR(64) NOT NULL,
  tool_args JSON NOT NULL,
  output JSON NOT NULL,
  success TINYINT(1) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_tool_id (tool_id),
  KEY idx_run_id (run_id),
  KEY idx_thread_id (thread_id),
  KEY idx_tool_name (tool_name),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
