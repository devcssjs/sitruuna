"""A small Notion API client (API version 2026-03-11).

- Paces itself (1 request a second by default; Notion allows 180 a minute on free plans).
- Waits and retries when Notion says "slow down" (429) or has a hiccup (5xx).
- Raises NotionError with Notion's own message for anything else.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger(__name__)
API = "https://api.notion.com/v1"
VERSION = "2026-03-11"


class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def page_id_from(link_or_id: str) -> str | None:
    """'https://www.notion.so/Sitruuna-HQ-1a2b...32hex?pvs=4' -> '1a2b...' (dashed UUID)."""
    s = (link_or_id or "").split("?")[0].split("#")[0]
    m = re.findall(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s)
    if not m:
        return None
    h = m[-1].replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class NotionClient:
    def __init__(self, token: str, gap: float = 1.0, http: httpx.Client | None = None, version: str = VERSION):
        self.gap = gap
        self._last = 0.0
        self.requests = 0
        self.http = http or httpx.Client(timeout=40.0)
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": version,
                        "Content-Type": "application/json"}

    # ---------- the one place requests happen ----------
    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(6):
            wait = self.gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.requests += 1
            try:
                r = self.http.request(method, API + path, headers=self.headers, json=body)
            except httpx.HTTPError as e:
                if attempt == 5:
                    raise NotionError(0, "network", str(e)) from e
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code < 300:
                return r.json() if r.content else {}
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < 5:
                retry = (data.get("additional_data") or {}).get("retry_after") or r.headers.get("Retry-After")
                try:
                    delay = float(retry)
                except (TypeError, ValueError):
                    delay = min(30, 2 ** attempt)
                log.info("Notion said wait %.0fs (%s)", delay, r.status_code)
                time.sleep(min(delay, 60))
                continue
            raise NotionError(r.status_code, data.get("code", "error"), data.get("message", r.text[:300]))
        raise NotionError(0, "retries", "gave up after several tries")

    # ---------- endpoints ----------
    def me(self) -> dict:
        return self.request("GET", "/users/me")

    def get_page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def update_page(self, page_id: str, **fields) -> dict:
        return self.request("PATCH", f"/pages/{page_id}", fields)

    def archive_page(self, page_id: str) -> dict:
        return self.request("PATCH", f"/pages/{page_id}", {"in_trash": True})

    def create_page(self, body: dict) -> dict:
        return self.request("POST", "/pages", body)

    def create_database(self, body: dict) -> dict:
        return self.request("POST", "/databases", body)

    def get_database(self, database_id: str) -> dict:
        return self.request("GET", f"/databases/{database_id}")

    def update_database(self, database_id: str, **fields) -> dict:
        return self.request("PATCH", f"/databases/{database_id}", fields)

    def list_children(self, block_id: str) -> dict:
        return self.request("GET", f"/blocks/{block_id}/children?page_size=100")

    def get_data_source(self, data_source_id: str) -> dict:
        return self.request("GET", f"/data_sources/{data_source_id}")

    def update_data_source(self, data_source_id: str, properties: dict) -> dict:
        return self.request("PATCH", f"/data_sources/{data_source_id}", {"properties": properties})

    def query(self, data_source_id: str, filter: dict | None = None, cursor: str | None = None) -> dict:
        body = {"page_size": 100}
        if filter:
            body["filter"] = filter
        if cursor:
            body["start_cursor"] = cursor
        return self.request("POST", f"/data_sources/{data_source_id}/query", body)

    def append_blocks(self, block_id: str, children: list) -> dict:
        return self.request("PATCH", f"/blocks/{block_id}/children", {"children": children})

    def update_block(self, block_id: str, body: dict) -> dict:
        return self.request("PATCH", f"/blocks/{block_id}", body)

    def create_view(self, body: dict) -> dict:
        return self.request("POST", "/views", body)

    def delete_block(self, block_id: str) -> dict:
        return self.request("DELETE", f"/blocks/{block_id}")
