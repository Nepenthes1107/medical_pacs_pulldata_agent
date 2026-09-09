SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS pulldata
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE pulldata;

CREATE TABLE IF NOT EXISTS study (
  id BIGINT NOT NULL AUTO_INCREMENT,
  study_instance_uid VARCHAR(128) NOT NULL COMMENT 'DICOM (0020,000D), from tb_study.StudyInstanceUID / dcm_study.StudyInstanceUID',
  accession_number VARCHAR(64) DEFAULT NULL COMMENT 'DICOM (0008,0050)',
  study_id VARCHAR(64) DEFAULT NULL COMMENT 'DICOM (0020,0010)',
  study_date DATE DEFAULT NULL COMMENT 'DICOM (0008,0020)',
  study_time TIME DEFAULT NULL COMMENT 'DICOM (0008,0030)',
  study_description VARCHAR(1024) DEFAULT NULL COMMENT 'DICOM (0008,1030)',
  modalities_in_study VARCHAR(256) DEFAULT NULL COMMENT 'DICOM (0008,0061)',
  institution_name VARCHAR(128) DEFAULT NULL COMMENT 'DICOM (0008,0080)',
  number_of_study_related_instances INT DEFAULT NULL COMMENT 'C-FIND returned expected SOP count at study level',
  source_id VARCHAR(64) NOT NULL DEFAULT 'orthanc-local' COMMENT 'PACS source id',
  find_source JSON DEFAULT NULL COMMENT 'Raw or normalized C-FIND result summary',
  move_source JSON DEFAULT NULL COMMENT 'C-MOVE source configuration summary',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_study_instance_uid (study_instance_uid),
  KEY idx_study_date_time (study_date, study_time),
  KEY idx_source_id (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS series (
  id BIGINT NOT NULL AUTO_INCREMENT,
  study_instance_uid VARCHAR(128) NOT NULL COMMENT 'DICOM (0020,000D), parent StudyInstanceUID',
  series_instance_uid VARCHAR(128) NOT NULL COMMENT 'DICOM (0020,000E)',
  modality VARCHAR(32) DEFAULT NULL COMMENT 'DICOM (0008,0060)',
  series_number INT DEFAULT NULL COMMENT 'DICOM (0020,0011)',
  series_date DATE DEFAULT NULL COMMENT 'DICOM (0008,0021)',
  series_time TIME DEFAULT NULL COMMENT 'DICOM (0008,0031)',
  series_description VARCHAR(255) DEFAULT NULL COMMENT 'DICOM (0008,103E)',
  protocol_name VARCHAR(128) DEFAULT NULL COMMENT 'DICOM (0018,1030)',
  body_part_examined VARCHAR(64) DEFAULT NULL COMMENT 'DICOM (0018,0015)',
  manufacturer VARCHAR(128) DEFAULT NULL COMMENT 'DICOM (0008,0070)',
  number_of_series_related_instances INT DEFAULT NULL COMMENT 'C-FIND returned expected SOP count at series level',
  series_folder_path VARCHAR(512) DEFAULT NULL COMMENT 'Local folder path, simplified from tb_series.SeriesFolderPath',
  source_id VARCHAR(64) NOT NULL DEFAULT 'orthanc-local',
  find_source JSON DEFAULT NULL COMMENT 'Raw or normalized C-FIND series result summary',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_study_series_uid (study_instance_uid, series_instance_uid),
  KEY idx_series_instance_uid (series_instance_uid),
  KEY idx_study_instance_uid (study_instance_uid),
  KEY idx_source_id (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS download_task (
  id BIGINT NOT NULL AUTO_INCREMENT,
  task_id VARCHAR(64) NOT NULL COMMENT 'UUID task id',
  study_instance_uid VARCHAR(128) NOT NULL COMMENT 'From tb_cmove_task.StudyInstanceUID',
  series_instance_uid VARCHAR(128) DEFAULT NULL COMMENT 'From tb_cmove_task.SeriesInstanceUID, nullable for study-level task',
  modality VARCHAR(32) DEFAULT NULL COMMENT 'From tb_cmove_task.Modality',
  expected_image_number INT DEFAULT NULL COMMENT 'From tb_cmove_task.ImageNumber',
  level VARCHAR(32) NOT NULL DEFAULT 'study_level' COMMENT 'study_level / series_level / sop_level',
  status VARCHAR(32) NOT NULL DEFAULT 'in_queue' COMMENT 'in_queue / downloading / downloaded / success / fail / cancel / unverified(期望数缺失,无法严格核对)',
  queued_at DATETIME DEFAULT NULL COMMENT '进入 in_queue 的时间',
  download_started_at DATETIME DEFAULT NULL COMMENT 'Downloader 提交 downloading 的时间',
  move_finished_at DATETIME DEFAULT NULL COMMENT 'C-MOVE 最终状态确认时间',
  checked_at DATETIME DEFAULT NULL COMMENT 'Checker 完成完整性校验的时间',
  failed_at DATETIME DEFAULT NULL COMMENT '任务判定 fail 的时间',
  priority INT NOT NULL DEFAULT 5 COMMENT 'From tb_cmove_task.Priority',
  source_id VARCHAR(64) NOT NULL DEFAULT 'orthanc-local',
  task_body JSON DEFAULT NULL COMMENT 'Normalized pull task body',
  move_result JSON DEFAULT NULL COMMENT 'C-MOVE response summary',
  task_retry_times INT NOT NULL DEFAULT 0,
  last_error TEXT DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_id (task_id),
  KEY idx_study_instance_uid (study_instance_uid),
  KEY idx_series_instance_uid (series_instance_uid),
  KEY idx_status (status),
  KEY idx_source_id (source_id),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS storescp_image (
  id BIGINT NOT NULL AUTO_INCREMENT,
  image_name VARCHAR(255) NOT NULL COMMENT 'From tb_storescp_image.ImageName',
  study_instance_uid VARCHAR(128) DEFAULT NULL COMMENT 'From tb_storescp_image.StudyInstanceUID',
  series_instance_uid VARCHAR(128) NOT NULL COMMENT 'From tb_storescp_image.SeriesInstanceUID',
  series_path VARCHAR(512) DEFAULT NULL COMMENT 'From tb_storescp_image.SeriesPath',
  file_path VARCHAR(512) DEFAULT NULL COMMENT 'Full local DICOM file path',
  received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_image_name (image_name),
  KEY idx_study_series_uid (study_instance_uid, series_instance_uid),
  KEY idx_series_instance_uid (series_instance_uid),
  KEY idx_received_at (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS archive (
  id BIGINT NOT NULL AUTO_INCREMENT,
  data_id VARCHAR(255) NOT NULL COMMENT 'Generated id from study/series uid and level',
  study_instance_uid VARCHAR(128) NOT NULL,
  series_instance_uid VARCHAR(128) DEFAULT NULL,
  level VARCHAR(32) NOT NULL DEFAULT 'study_level',
  status VARCHAR(32) NOT NULL DEFAULT 'unknown' COMMENT 'unknown / archiving / finished / fail / unverified',
  task_id VARCHAR(64) DEFAULT NULL,
  expected_image_count INT DEFAULT NULL,
  archived_image_count INT NOT NULL DEFAULT 0,
  checked TINYINT NOT NULL DEFAULT 0,
  last_error TEXT DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_data_id (data_id),
  KEY idx_study_instance_uid (study_instance_uid),
  KEY idx_series_instance_uid (series_instance_uid),
  KEY idx_task_id (task_id),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_run (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL COMMENT 'UUID，Agent Run 唯一标识，也是隔离边界',
  route VARCHAR(32) DEFAULT NULL COMMENT 'first_pull / diagnosis / knowledge_qa / clarification',
  intent VARCHAR(32) DEFAULT NULL COMMENT '显式意图（first_pull 等），最高优先于规则/LLM',
  thread_id VARCHAR(64) DEFAULT NULL COMMENT '会话标识，同 thread 同一时刻只允许一个活跃 Run（需求 2）',
  user_id VARCHAR(64) DEFAULT NULL COMMENT '用户标识（多用户隔离）',
  source_id VARCHAR(64) NOT NULL DEFAULT 'orthanc-local' COMMENT '本次 Agent Run 使用的 PACS source',
  status VARCHAR(32) NOT NULL DEFAULT 'running' COMMENT 'running / awaiting_approval / awaiting_repull / completed / failed / rejected / cancelled',
  message TEXT DEFAULT NULL COMMENT '用户输入的自然语言 message',
  task_id VARCHAR(64) DEFAULT NULL COMMENT '解析出的任务 ID',
  study_instance_uid VARCHAR(128) DEFAULT NULL,
  study_instance_uid_list JSON DEFAULT NULL,
  batch_mode TINYINT(1) NOT NULL DEFAULT 0,
  batch_summary JSON DEFAULT NULL,
  study_results JSON DEFAULT NULL,
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
  KEY idx_thread_id (thread_id),
  KEY idx_user_id (user_id),
  KEY idx_source_id (source_id),
  KEY idx_agent_run_thread_status_created (thread_id, status, created_at),
  KEY idx_agent_run_task_status_created (task_id, status, created_at),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_action_audit (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL COMMENT '发起该动作的 Agent Run',
  task_id VARCHAR(64) NOT NULL COMMENT '被操作的补拉任务',
  action VARCHAR(32) NOT NULL COMMENT '写操作类型，如 retry_pull_task',
  operator VARCHAR(64) DEFAULT NULL COMMENT '审批人标识',
  idempotency_key VARCHAR(128) NOT NULL COMMENT '目标+策略+范围派生的业务幂等键',
  result VARCHAR(32) NOT NULL COMMENT 'processing / submitted / escalated / failed',
  detail JSON DEFAULT NULL COMMENT '执行结果摘要',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_agent_action_idempotency (idempotency_key),
  KEY idx_run_id (run_id),
  KEY idx_task_id (task_id),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_execution_log (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL COMMENT '关联 agent_run.run_id',
  thread_id VARCHAR(64) NOT NULL COMMENT 'LangGraph checkpoint 会话标识',
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
  tool_id VARCHAR(36) NOT NULL COMMENT '单次工具调用的全局唯一标识，供 evidence_ref 精确引用',
  run_id VARCHAR(64) NOT NULL COMMENT '产生证据的 Agent Run',
  thread_id VARCHAR(64) NOT NULL COMMENT '证据所属会话，用于隔离查询',
  tool_name VARCHAR(64) NOT NULL,
  tool_args JSON NOT NULL COMMENT '该次调用的完整参数',
  output JSON NOT NULL COMMENT '该次调用的完整结构化返回',
  success TINYINT(1) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_tool_id (tool_id),
  KEY idx_run_id (run_id),
  KEY idx_thread_id (thread_id),
  KEY idx_tool_name (tool_name),
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
