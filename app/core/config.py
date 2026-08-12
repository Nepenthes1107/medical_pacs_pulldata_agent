import os
from functools import lru_cache
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config", "pull_data.local.yml")


class AppConfig(BaseModel):
    name: str = "medical-pacs-pulldata-agent"
    log_level: str = "INFO"
    rule_based_agent: bool = True


class DatabaseConfig(BaseModel):
    url: str


class RedisConfig(BaseModel):
    url: str


class RabbitMQConfig(BaseModel):
    url: str
    download_queue: str = "download_tasks"


class StorageConfig(BaseModel):
    fileserver_root: str = "./data/fileserver"


class StoreScpConfig(BaseModel):
    ae_title: str = "PULLDATA_SCP"
    host: str = "0.0.0.0"
    port: int = 11112


class PacsSourceConfig(BaseModel):
    id: str
    name: str
    host: str
    port: int
    ae_title: str
    client_ae_title: str = "PULLDATA_SCU"
    move_destination_ae_title: str = "PULLDATA_SCP"
    support_find_level: List[str] = Field(default_factory=list)
    support_move_level: List[str] = Field(default_factory=list)


class CheckerConfig(BaseModel):
    interval_seconds: int = 5
    fail_after_seconds: int = 120


class DownloaderConfig(BaseModel):
    max_workers: int = 4
    retry_download_interval: List[int] = Field(default_factory=lambda: [30, 120, 300])


class LLMConfig(BaseModel):
    # DashScope 走 OpenAI 兼容模式；api_key 只从环境变量注入，不落 yaml。
    model: str = "qwen-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: Optional[str] = None
    timeout_seconds: int = 60
    max_retries: int = 2
    temperature: float = 0.0


class RAGConfig(BaseModel):
    persist_dir: str = "./data/chroma"
    embedding_model: str = "text-embedding-v4"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    enable_rerank: bool = False
    rerank_model: str = "qwen3-rerank"
    top_k: int = 3


class AgentConfig(BaseModel):
    agent_runs_queue: str = "agent_runs"
    # 补拉失败事件队列：只承载 C-MOVE 失败（move）与超时仍不完整（integrity）。
    # success/unverified 由 Checker 同事务直接收尾 Run，不入队。物理队列名保持不变。
    repull_events_queue: str = "repull_events"
    approval_lock_ttl_seconds: int = 300
    # awaiting_repull 超时兜底（30 分钟）：只负责释放永久卡住的 thread（补拉失败事件
    # best-effort 发布失败或消费者长期不可用），不代表任何完整性判定——完整性只由 Checker 裁定。
    repull_terminal_timeout_seconds: int = 1800
    max_few_shot: int = 3
    checkpoint_path: str = "./data/agent_checkpoints.sqlite"
    act_max_workers: int = 4  # 单 run 内 act() 多个只读工具的并发上限（线程池）
    # 紧急止损标记存活时长：必须长于 PACS 把一批 C-STORE 推完的时间，否则标记先过期、
    # 余下影像照常收下。1 小时覆盖大范围 Study 的推送窗口。
    abort_flag_ttl_seconds: int = 3600


class LangSmithConfig(BaseModel):
    tracing_enabled: bool = False
    project: str = "medical-pacs-pulldata-agent"


class Settings(BaseSettings):
    app: AppConfig
    database: DatabaseConfig
    redis: RedisConfig
    rabbitmq: RabbitMQConfig
    storage: StorageConfig
    storescp: StoreScpConfig
    pacs_sources: List[PacsSourceConfig]
    checker: CheckerConfig
    downloader: DownloaderConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)

    def get_pacs_source(self, source_id: str) -> PacsSourceConfig:
        for source in self.pacs_sources:
            if source.id == source_id:
                return source
        raise KeyError("PACS source not found: %s" % source_id)


def _load_yaml_config(config_path: str) -> Dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError("config file not found: %s" % config_path)
    with open(config_path, "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def _apply_env_overrides(raw_config: Dict) -> Dict:
    raw_config.setdefault("database", {})["url"] = os.getenv(
        "DATABASE_URL", raw_config.get("database", {}).get("url")
    )
    raw_config.setdefault("redis", {})["url"] = os.getenv(
        "REDIS_URL", raw_config.get("redis", {}).get("url")
    )
    raw_config.setdefault("rabbitmq", {})["url"] = os.getenv(
        "RABBITMQ_URL", raw_config.get("rabbitmq", {}).get("url")
    )
    raw_config.setdefault("storage", {})["fileserver_root"] = os.getenv(
        "FILESERVER_ROOT", raw_config.get("storage", {}).get("fileserver_root")
    )
    raw_config.setdefault("app", {})["rule_based_agent"] = os.getenv(
        "RULE_BASED_AGENT", str(raw_config.get("app", {}).get("rule_based_agent", True))
    ).lower() in ("1", "true", "yes", "on")

    # LLM / RAG 的 api_key 一律走环境变量，绝不写入 yaml。
    llm_cfg = raw_config.setdefault("llm", {})
    llm_cfg["api_key"] = os.getenv("DASHSCOPE_API_KEY", llm_cfg.get("api_key"))
    if os.getenv("LLM_MODEL"):
        llm_cfg["model"] = os.getenv("LLM_MODEL")
    if os.getenv("LLM_BASE_URL"):
        llm_cfg["base_url"] = os.getenv("LLM_BASE_URL")

    # LangSmith tracing 开关与 project 名可由 env 覆盖（key 走 LANGSMITH_API_KEY，SDK 直接读环境变量）。
    ls_cfg = raw_config.setdefault("langsmith", {})
    if os.getenv("LANGSMITH_TRACING") is not None:
        ls_cfg["tracing_enabled"] = os.getenv("LANGSMITH_TRACING", "").lower() in (
            "1", "true", "yes", "on",
        )
    if os.getenv("LANGSMITH_PROJECT"):
        ls_cfg["project"] = os.getenv("LANGSMITH_PROJECT")
    return raw_config


def resolve_project_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(BASE_DIR, path))


@lru_cache()
def get_settings(config_path: Optional[str] = None) -> Settings:
    raw_config = _load_yaml_config(config_path or os.getenv("PULL_DATA_CONFIG", DEFAULT_CONFIG_PATH))
    return Settings(**_apply_env_overrides(raw_config))


settings = get_settings()
