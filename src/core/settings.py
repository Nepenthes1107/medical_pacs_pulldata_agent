"""Single application settings entrypoint."""
import os
from functools import lru_cache

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config", "pull_data.local.yml")


class AppConfig(BaseModel):
    name: str = "medical-pacs-pulldata-agent"
    log_level: str = "INFO"
    rule_based_agent: bool = True
    legacy_agent_api_enabled: bool = False


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
    support_find_level: list[str] = Field(default_factory=list)
    support_move_level: list[str] = Field(default_factory=list)


class CheckerConfig(BaseModel):
    interval_seconds: int = 5
    fail_after_seconds: int = 120


class DownloaderConfig(BaseModel):
    max_workers: int = 4
    retry_download_interval: list[int] = Field(default_factory=lambda: [30, 120, 300])


class LLMConfig(BaseModel):
    provider: str = "openai-compatible"
    model: str = "qwen-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str | None = None
    timeout_seconds: int = 60
    max_retries: int = 2
    temperature: float = 0.0


class RAGConfig(BaseModel):
    persist_dir: str = "./data/chroma"
    source_dir: str = "./data/rag/sources"
    lexical_index_dir: str = "./data/rag/bm25"
    embedding_model: str = "text-embedding-v4"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    candidate_k: int = Field(default=20, ge=1)
    rrf_constant: int = Field(default=60, ge=1)
    rerank_model: str = "qwen3-rerank"
    top_k: int = Field(default=5, ge=1, le=10)


class AgentConfig(BaseModel):
    agent_runs_queue: str = "agent_runs"
    repull_events_queue: str = "repull_events"
    approval_lock_ttl_seconds: int = 300
    repull_terminal_timeout_seconds: int = 1800
    max_few_shot: int = 3
    checkpoint_ttl_minutes: int = 1440
    context_token_budget: int = 12000
    context_summary_budget: int = 2000
    recent_message_count: int = 8
    act_max_workers: int = 4
    max_agent_steps: int = 8
    tool_max_retries: int = 1
    max_reflection_revisions: int = 1
    abort_flag_ttl_seconds: int = 3600
    batch_max_studies: int = 20
    batch_concurrency: int = 4
    batch_transient_retries: int = 2


class LangSmithConfig(BaseModel):
    tracing_enabled: bool = False
    project: str = "medical-pacs-pulldata-agent"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PULL_DATA_", extra="ignore")
    app: AppConfig
    database: DatabaseConfig
    redis: RedisConfig
    rabbitmq: RabbitMQConfig
    storage: StorageConfig
    storescp: StoreScpConfig
    pacs_sources: list[PacsSourceConfig]
    checker: CheckerConfig
    downloader: DownloaderConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    rag: RAGConfig = Field(default_factory=lambda: RAGConfig())
    agent: AgentConfig = Field(default_factory=AgentConfig)
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)

    def get_pacs_source(self, source_id: str) -> PacsSourceConfig:
        for source in self.pacs_sources:
            if source.id == source_id:
                return source
        raise KeyError("PACS source not found: %s" % source_id)


_ENV_FILE = os.path.join(BASE_DIR, ".env")


def _load_env_file() -> None:
    """把项目根 .env 补进进程环境（不覆盖已存在的变量）。

    secrets（如 DASHSCOPE_API_KEY）是进程输入：docker compose 会把同目录 .env 注入
    容器环境；本地 python 进程（pytest / CLI / 脚本）默认不读 .env，这里补齐，
    使本地与容器行为一致。.env 已被 .gitignore 忽略，不会被提交。
    """
    if not os.path.exists(_ENV_FILE):
        return
    with open(_ENV_FILE, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            if name and name not in os.environ:
                os.environ[name] = value.strip().strip('"').strip("'")


def _load_yaml_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError("config file not found: %s" % config_path)
    with open(config_path, encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def _apply_env_overrides(raw_config: dict) -> dict:
    for section, env_name, key in (("database", "DATABASE_URL", "url"), ("redis", "REDIS_URL", "url"), ("rabbitmq", "RABBITMQ_URL", "url"), ("storage", "FILESERVER_ROOT", "fileserver_root")):
        raw_config.setdefault(section, {})[key] = os.getenv(env_name, raw_config.get(section, {}).get(key))
    raw_config.setdefault("app", {})["rule_based_agent"] = os.getenv("RULE_BASED_AGENT", str(raw_config.get("app", {}).get("rule_based_agent", True))).lower() in ("1", "true", "yes", "on")
    raw_config["app"]["legacy_agent_api_enabled"] = os.getenv(
        "LEGACY_AGENT_API_ENABLED",
        str(raw_config["app"].get("legacy_agent_api_enabled", False)),
    ).lower() in ("1", "true", "yes", "on")
    llm = raw_config.setdefault("llm", {})
    # Secrets are process inputs only.  Ignore a value accidentally present in
    # YAML instead of allowing it to become part of the loaded settings object.
    llm.pop("api_key", None)
    llm["api_key"] = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    for key, env_name in (("provider", "LLM_PROVIDER"), ("model", "LLM_MODEL"), ("base_url", "LLM_BASE_URL")):
        if os.getenv(env_name):
            llm[key] = os.getenv(env_name)
    langsmith = raw_config.setdefault("langsmith", {})
    if os.getenv("LANGSMITH_TRACING") is not None:
        langsmith["tracing_enabled"] = os.getenv("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes", "on")
    if os.getenv("LANGSMITH_PROJECT"):
        langsmith["project"] = os.getenv("LANGSMITH_PROJECT")
    return raw_config


def resolve_project_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(BASE_DIR, path))


@lru_cache
def get_settings(config_path: str | None = None) -> Settings:
    _load_env_file()
    path = config_path or os.getenv("PULL_DATA_CONFIG") or DEFAULT_CONFIG_PATH
    loaded = Settings(**_apply_env_overrides(_load_yaml_config(path)))
    loaded.storage.fileserver_root = resolve_project_path(loaded.storage.fileserver_root)
    loaded.rag.persist_dir = resolve_project_path(loaded.rag.persist_dir)
    loaded.rag.source_dir = resolve_project_path(loaded.rag.source_dir)
    loaded.rag.lexical_index_dir = resolve_project_path(loaded.rag.lexical_index_dir)
    return loaded


settings = get_settings()
