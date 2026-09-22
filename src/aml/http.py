"""记忆服务客户端（mcp-memory-service 的 HTTP 接口）。

只依赖标准库，减少安装负担；所有请求带重试与超时，失败时抛 MemoryAPIError 并保留原文。

**可选鉴权**（2026-09-22 加）：后端默认没有鉴权，只靠"绑在 127.0.0.1"来保护 ——
本机任何进程都能读整库（实测一条 `GET /api/memories` 就够）。本客户端补的是**客户端那一半**：
配置里填 `memory_api_token` 之后，所有请求带 `Authorization: Bearer <token>`，
这样"前面挂反代 / 后端自己加 token"才不需要改代码。
所有调用点统一用 `client_for(cfg)` 构造，别在 27 处各写一遍（那样加鉴权必然漏掉某条路）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class MemoryAPIError(RuntimeError):
    pass


class MemoryClient:
    def __init__(self, api: str, timeout: int = 60, tries: int = 3, backoff: float = 1.5,
                 token: str | None = None):
        self.api = api.rstrip("/")
        self.timeout = timeout
        self.tries = tries
        self.backoff = backoff
        self.token = str(token or "").strip()

    # ---- 底层 ----
    def headers(self, content_type: bool = True) -> dict:
        """请求头：有 token 就带上 Bearer（没配就与老行为完全一致）。"""
        headers = {"Content-Type": "application/json"} if content_type else {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, path: str, payload: dict | None = None, method: str = "POST"):
        url = self.api + path
        data = json.dumps(payload).encode() if payload is not None else None
        headers = self.headers()
        last = None
        for attempt in range(max(1, self.tries)):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as f:
                    body = f.read()
                return json.loads(body) if body else {}
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt + 1 < max(1, self.tries):
                    time.sleep(self.backoff)
        raise MemoryAPIError(f"{method} {path} 失败：{type(last).__name__} {last}")

    # ---- 健康 ----
    def health(self, timeout: int = 5) -> dict:
        try:
            req = urllib.request.Request(self.api + "/api/health", headers=self.headers(False))
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return json.loads(f.read() or b"{}")
        except Exception as e:  # noqa: BLE001
            raise MemoryAPIError(f"服务不可达：{type(e).__name__} {e}") from e

    def alive(self) -> bool:
        try:
            self.health()
            return True
        except MemoryAPIError:
            return False

    # ---- 写入 ----
    def store(self, content: str, tags=None, metadata=None, conversation_id: str | None = None) -> dict:
        payload = {"content": content, "tags": list(tags or []), "metadata": dict(metadata or {})}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return self._request("/api/memories", payload)

    # ---- 检索 ----
    def search(self, query: str, n_results: int = 10) -> list[tuple[float, dict]]:
        """语义检索，返回 [(分数, 记忆)]，按分数降序。"""
        r = self._request("/api/search", {"query": query, "n_results": n_results})
        out = []
        for item in (r.get("results") or []):
            memory = item.get("memory", item)
            try:
                score = float(item.get("similarity_score") or memory.get("similarity_score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            out.append((score, memory))
        out.sort(key=lambda x: -x[0])
        return out

    def search_by_tag(self, tags, match_all: bool = False, n: int = 20) -> list[dict]:
        r = self._request("/api/search/by-tag", {"tags": list(tags), "match_all": match_all,
                                                 "time_filter": None})
        return [item.get("memory", item) for item in (r.get("results") or [])][:n]

    def search_by_time(self, time_expr: str, semantic_query: str | None = None, n: int = 10):
        payload = {"query": time_expr, "n_results": n}
        if semantic_query:
            payload["semantic_query"] = semantic_query
        r = self._request("/api/search/by-time", payload)
        return r.get("results") or []

    # ---- 遍历 / 修改 / 删除（去重、导出、补向量都要用） ----
    def list_memories(self, page: int = 1, page_size: int = 100, tag: str | None = None) -> dict:
        from urllib.parse import quote
        path = f"/api/memories?page={page}&page_size={page_size}"
        if tag:
            path += f"&tag={quote(tag)}"
        return self._request(path, None, method="GET")

    def iter_memories(self, tag: str | None = None, max_pages: int = 500):
        """分页遍历全部（或某个标签的）记忆。"""
        page = 1
        while page <= max_pages:
            data = self.list_memories(page=page, tag=tag)
            items = data.get("memories") or data.get("results") or []
            if not items:
                return
            yield from items
            if not data.get("has_more") and len(items) < 100:
                return
            page += 1

    def get(self, content_hash: str) -> dict:
        """按 hash 取一条记忆（后端不一定有这个端点，取不到会抛 MemoryAPIError）。"""
        from urllib.parse import quote
        return self._request(f"/api/memories/{quote(content_hash, safe='')}", None, method="GET")

    def delete(self, content_hash: str) -> dict:
        from urllib.parse import quote
        return self._request(f"/api/memories/{quote(content_hash, safe='')}", {}, method="DELETE")

    def update(self, content_hash: str, updates: dict) -> dict:
        """原地更新一条记忆（tags / memory_type / metadata）。

        正确端点是 `PUT /api/memories/{content_hash}`（body 只接受这三个字段）。
        曾经的写法是 `POST /api/memories/update` —— **405 Method Not Allowed**，
        而且因为没人真跑过 `review --postpone`，这个错藏了很久（2026-09-17 用 OpenAPI 核对后修掉）。
        """
        from urllib.parse import quote
        payload = {k: v for k, v in (updates or {}).items()
                   if k in ("tags", "memory_type", "metadata")}
        return self._request(f"/api/memories/{quote(content_hash, safe='')}", payload, method="PUT")

    def rate(self, content_hash: str, rating: int, feedback: str = "") -> dict:
        """服务的**原生质量评分**（rating ∈ -1/0/1）。用它可以让 Dashboard 的 Analytics 也反映反馈。"""
        from urllib.parse import quote
        return self._request(f"/api/quality/memories/{quote(content_hash, safe='')}/rate",
                             {"rating": rating, "feedback": feedback[:500]})


def client_for(cfg) -> MemoryClient:
    """按配置建客户端（**所有调用点的统一入口**）。

    为什么要有这个函数：`MemoryClient(cfg.api)` 这种写法散在 12 个模块 25 处，
    加鉴权时"改一部分、漏一部分"是必然的 —— 而漏掉的那条路会静默 401 或被后端放过。
    统一从配置里取 `memory_api_token`，只有这一个地方知道 token 从哪来。
    """
    return MemoryClient(cfg.api, token=getattr(cfg, "api_token", "") or None)
