"""tests/integration 专用夹具。

集成测试直连真实外部服务（由项目根 docker-compose 提供，端口映射宿主机）。
设计约束：
- 仅在本子目录生效，不往顶层 tests/conftest.py 放重夹具；
- 不修改全局 os.environ / 顶层 settings 的 DATABASE_URL，避免污染单测进程；
- 每个服务探测不可用即 pytest.skip，无 Docker 环境也能优雅收集；
- 本模块顶层只 import 标准库，重依赖在 fixture 内按需导入。
"""
import os
import socket
import subprocess
import uuid
from contextlib import contextmanager

import pytest

# ---- 连接参数（可用环境变量覆盖） ----
MYSQL_HOST = os.getenv("MYSQL_TEST_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_TEST_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_TEST_USER", "uii")
MYSQL_PASSWORD = os.getenv("MYSQL_TEST_PASSWORD", "011107")
MYSQL_CONTAINER = os.getenv("MYSQL_CONTAINER", "pulldata-mysql")

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://127.0.0.1:6379/0")
RABBITMQ_TEST_URL = os.getenv("RABBITMQ_TEST_URL", "amqp://guest:guest@127.0.0.1:5672/%2F")
ORTHANC_HOST = os.getenv("ORTHANC_TEST_HOST", "127.0.0.1")
ORTHANC_PORT = int(os.getenv("ORTHANC_TEST_PORT", "4242"))
ORTHANC_SOURCE_ID = os.getenv("ORTHANC_SOURCE_ID", "orthanc-local")


def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _redis_reachable(url: str) -> bool:
    import redis

    try:
        return bool(redis.Redis.from_url(url).ping())
    except Exception:
        return False


def _rabbitmq_reachable(url: str) -> bool:
    import pika

    try:
        connection = pika.BlockingConnection(pika.URLParameters(url))
        connection.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- MySQL

@pytest.fixture(scope="session")
def mysql_db():
    """用 root（docker exec）建一次性数据库并授权测试用户，测试后删除。"""
    if not _tcp_open(MYSQL_HOST, MYSQL_PORT):
        pytest.skip("MySQL 未就绪，需先 `docker compose up -d mysql`")
    dbname = "pulldata_it_%s" % uuid.uuid4().hex[:8]

    def _mysql_root(sql: str) -> None:
        subprocess.run(
            ["docker", "exec", MYSQL_CONTAINER, "mysql", "-uroot", "-proot", "--execute", sql],
            check=True,
            capture_output=True,
            text=True,
        )

    try:
        _mysql_root(
            "CREATE DATABASE `%s` CHARACTER SET utf8mb4;"
            "GRANT ALL PRIVILEGES ON `%s`.* TO '%s'@'%%'; FLUSH PRIVILEGES;"
            % (dbname, dbname, MYSQL_USER)
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip("无法用 root 建一次性测试库（%s）" % type(exc).__name__)

    url = "mysql+pymysql://%s:%s@%s:%s/%s?charset=utf8mb4" % (
        MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST, MYSQL_PORT, dbname,
    )
    info = {"name": dbname, "url": url}
    yield info
    try:
        _mysql_root("DROP DATABASE IF EXISTS `%s`;" % dbname)
    except Exception:
        pass


@pytest.fixture(scope="session")
def mysql_engine(mysql_db):
    """一次性库 + 当前 models 全量表（create_all 以 models 为权威 schema）。"""
    from sqlalchemy import create_engine

    import src.infrastructure.db.models  # noqa: F401  # 注册全部表
    from src.infrastructure.db.database import Base

    engine = create_engine(mysql_db["url"], pool_pre_ping=True, future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def mysql_session(mysql_engine):
    """每次测试独立事务会话（调用方负责 commit/close 语义）。"""
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(bind=mysql_engine, autoflush=False, autocommit=False, future=True)
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def mysql_audit_scope(mysql_engine, monkeypatch):
    """把 db/audit 模块的 session_scope 指到一次性测试库，使落库函数真跑完整路径。"""
    from sqlalchemy.orm import sessionmaker

    from src.infrastructure.db import audit as audit_module

    session_factory = sessionmaker(bind=mysql_engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def test_scope():
        db = session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(audit_module, "session_scope", test_scope)
    return audit_module


# ---------------------------------------------------------------- Redis

@pytest.fixture(scope="session")
def redis_url():
    if not _redis_reachable(REDIS_TEST_URL):
        pytest.skip("Redis 未就绪，需先 `docker compose up -d redis`")
    return REDIS_TEST_URL


@pytest.fixture
def redis_settings_url(redis_url, monkeypatch):
    """把 settings.redis.url 指到测试 Redis（idempotency/events 运行时读取）。"""
    from src.core.settings import settings

    monkeypatch.setattr(settings.redis, "url", redis_url)
    return redis_url


# ---------------------------------------------------------------- RabbitMQ

@pytest.fixture
def rabbit_scope(redis_settings_url, monkeypatch):
    """用唯一后缀队列名隔离，避免触碰真实 download_tasks/agent_runs 队列。"""
    url = RABBITMQ_TEST_URL
    if not _rabbitmq_reachable(url):
        pytest.skip("RabbitMQ 未就绪，需先 `docker compose up -d rabbitmq`")

    from src.core.settings import settings

    suffix = uuid.uuid4().hex[:8]
    names = {
        "download": "it_download_%s" % suffix,
        "agent_run": "it_agent_run_%s" % suffix,
        "repull": "it_repull_%s" % suffix,
    }
    monkeypatch.setattr(settings.rabbitmq, "url", url)
    monkeypatch.setattr(settings.rabbitmq, "download_queue", names["download"])
    monkeypatch.setattr(settings.agent, "agent_runs_queue", names["agent_run"])
    monkeypatch.setattr(settings.agent, "repull_events_queue", names["repull"])

    yield {"url": url, "names": names}

    try:
        import pika

        connection = pika.BlockingConnection(pika.URLParameters(url))
        channel = connection.channel()
        for queue in names.values():
            channel.queue_delete(queue=queue)
        connection.close()
    except Exception:
        pass


# ---------------------------------------------------------------- PACS / Orthanc

@pytest.fixture
def pacs_settings(monkeypatch):
    """把本地 settings 里 orthanc-local 源指到宿主机映射端口。"""
    if not _tcp_open(ORTHANC_HOST, ORTHANC_PORT):
        pytest.skip("Orthanc DICOM 未就绪，需先 `docker compose up -d orthanc`")
    from src.core.settings import settings

    source = settings.get_pacs_source(ORTHANC_SOURCE_ID)
    monkeypatch.setattr(source, "host", ORTHANC_HOST)
    monkeypatch.setattr(source, "port", ORTHANC_PORT)
    return ORTHANC_SOURCE_ID


@pytest.fixture
def storescp_available():
    """本机 11112 是否起着 C-STORE 接收端（pull-data-storescp 容器）。"""
    return _tcp_open("127.0.0.1", 11112, timeout=0.5)


# ---------------------------------------------------------------- RAG（本地 Chroma/BM25）

@pytest.fixture
def rag_dirs(redis_settings_url, tmp_path, monkeypatch):
    """把 RAG store 指向临时目录，并用离线确定性 embedding 替换 dashscope 段。"""
    from src.core.settings import settings
    from src.infrastructure import rag_lexical, rag_store
    from tests.integration.fakes import FakeEmbedding

    chroma_dir = tmp_path / "chroma"
    bm25_dir = tmp_path / "bm25"

    monkeypatch.setattr(settings.rag, "persist_dir", str(chroma_dir))
    monkeypatch.setattr(settings.rag, "lexical_index_dir", str(bm25_dir))
    monkeypatch.setattr(rag_store, "DashScopeEmbeddingFunction", FakeEmbedding)
    rag_store.get_collection.cache_clear()
    rag_lexical.load_index.cache_clear()

    yield {
        "chroma_dir": str(chroma_dir),
        "bm25_dir": str(bm25_dir),
        "store": rag_store,
        "lexical": rag_lexical,
        "fake_embedding": FakeEmbedding,
    }

    rag_store.get_collection.cache_clear()
    rag_lexical.load_index.cache_clear()
