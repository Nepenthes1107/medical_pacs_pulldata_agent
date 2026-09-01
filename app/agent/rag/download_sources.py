"""按固定清单下载 RAG 官方资料；文档本体写入 data/，不提交仓库。"""
import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List

import requests
import yaml

from app.core.config import resolve_project_path, settings

logger = logging.getLogger(__name__)

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "sources.yml")
DOWNLOAD_RECORD = "downloads.json"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: str = MANIFEST_PATH) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp)["sources"]


def download_sources(force: bool = False, manifest_path: str = MANIFEST_PATH) -> Dict:
    """下载清单文件并记录实际哈希；已有文件默认不重复下载。"""
    source_dir = resolve_project_path(settings.rag.source_dir)
    os.makedirs(source_dir, exist_ok=True)
    record_path = os.path.join(source_dir, DOWNLOAD_RECORD)
    previous = {}
    if os.path.isfile(record_path):
        with open(record_path, "r", encoding="utf-8") as fp:
            previous = {row["id"]: row for row in json.load(fp)["sources"]}
    records = []
    for source in load_manifest(manifest_path):
        filename = os.path.basename(source["filename"])
        if filename != source["filename"]:
            raise ValueError("source filename must not contain a directory: %s" % source["filename"])
        target = os.path.join(source_dir, filename)
        downloaded = force or not os.path.isfile(target)
        if downloaded:
            temp_path = target + ".part"
            logger.info("下载 RAG 文档: %s", source["url"])
            with requests.get(source["url"], stream=True, timeout=(20, 300)) as response:
                response.raise_for_status()
                with open(temp_path, "wb") as fp:
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            fp.write(block)
            os.replace(temp_path, target)
        records.append({
            **source,
            "path": target,
            "sha256": _sha256(target),
            "downloaded": downloaded,
            "fetched_at": (
                datetime.now(timezone.utc).isoformat()
                if downloaded else (
                    previous.get(source["id"], {}).get("fetched_at")
                    or datetime.fromtimestamp(os.path.getmtime(target), timezone.utc).isoformat()
                )
            ),
        })

    with open(record_path, "w", encoding="utf-8") as fp:
        json.dump({"sources": records}, fp, ensure_ascii=False, indent=2)
    return {"source_dir": source_dir, "downloaded": sum(r["downloaded"] for r in records),
            "total": len(records), "record": record_path}


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="重新下载已有文件")
    args = parser.parse_args()
    print(json.dumps(download_sources(force=args.force), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
