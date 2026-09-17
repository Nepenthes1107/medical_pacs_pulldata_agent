"""Minimal HTTP client for the platform Agent Service.

It intentionally mirrors the toolkit's client boundary without embedding PACS
business logic. Applications can use it while the legacy endpoints remain online.
"""
from collections.abc import Iterator
from typing import Any

import requests


class AgentClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def info(self) -> list[dict[str, Any]]:
        response = requests.get(f"{self.base_url}/agents/info", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def invoke(self, message: str, *, agent_id: str = "pacs-diagnostician", thread_id: str | None = None,
               **kwargs: Any) -> dict[str, Any]:
        payload = {"message": message, **kwargs}
        if thread_id is not None:
            payload["thread_id"] = thread_id
        response = requests.post(
            f"{self.base_url}/agents/{agent_id}/invoke", json=payload, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def stream(self, message: str, *, agent_id: str = "pacs-diagnostician", thread_id: str | None = None,
               **kwargs: Any) -> Iterator[str]:
        payload = {"message": message, **kwargs}
        if thread_id is not None:
            payload["thread_id"] = thread_id
        response = requests.post(
            f"{self.base_url}/agents/{agent_id}/stream",
            json=payload,
            timeout=self.timeout,
            stream=True,
        )
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if line:
                yield line if isinstance(line, str) else line.decode()
