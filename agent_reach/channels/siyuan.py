# -*- coding: utf-8 -*-
"""SiYuan (思源笔记) — local HTTP API for notebooks, docs, blocks, and search.

SiYuan is a local-first note-taking app.  When running, it exposes a kernel
HTTP API (default ``http://127.0.0.1:6806``) that uses JSON-RPC-style POST
requests.  An optional API token (Settings → About → API Token) is sent as
``Authorization: Token <token>``.

This channel is read-heavy: it lists notebooks and documents, fetches block
Kramdown (Markdown) source, runs full-text search, and renders a document
outline.  Mutating endpoints (create/update/delete block) are intentionally
excluded — agents that need to write should call the SiYuan API directly.
"""

import ipaddress
import json
import os
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit

from .base import Channel

_DEFAULT_API_URL = "http://127.0.0.1:6806"
_UA = "agent-reach/1.0"
_TIMEOUT = 10
_MAX_RESPONSE_BYTES = 1024 * 1024

# Loopback is the safe default.  Remote targets (phone on LAN, tunnel, etc.)
# are allowed only when the user explicitly configures ``siyuan_api_url`` —
# never the default.  Embedded credentials are always rejected; the API token
# travels in the Authorization header instead.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class SiYuanAPIError(RuntimeError):
    """Raised when the SiYuan kernel returns a non-zero ``code``."""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_api_url(base_url: str, *, allow_remote: bool = False) -> None:
    """Validate a SiYuan API base URL.

    Loopback targets are always accepted.  Remote targets are accepted only
    when ``allow_remote`` is True (i.e. the user explicitly set the URL in
    config).  Embedded credentials are rejected regardless.
    """
    try:
        parsed = urlsplit(base_url)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid SiYuan API URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "SiYuan API URL must be http(s) without embedded credentials"
        )
    if not allow_remote and not _is_loopback(host):
        raise ValueError(
            "SiYuan API default target must be loopback "
            "(http://127.0.0.1:6806).  To connect to a phone or remote "
            "instance, set siyuan_api_url in config explicitly."
        )
    # Port may be None (default) or any int — SiYuan can run on custom ports.


def _api_base(config=None) -> str:
    """Return the configured SiYuan API base URL (config → env → default)."""
    if config is not None:
        url = config.get("siyuan_api_url")
        if url:
            return url.rstrip("/")
    env_url = os.environ.get("SIYUAN_API_URL")
    if env_url:
        return env_url.rstrip("/")
    return _DEFAULT_API_URL


def _allow_remote(config=None) -> bool:
    """Remote targets are allowed only when the URL was explicitly set.

    A configured URL means the user opted into a non-default (likely remote)
    target; the default URL is always loopback and needs no flag.
    """
    if config is not None and config.get("siyuan_api_url"):
        return True
    if os.environ.get("SIYUAN_API_URL"):
        return True
    return False


def _api_token(config=None) -> Optional[str]:
    """Return the configured SiYuan API token (config → env → None)."""
    if config is not None:
        token = config.get("siyuan_api_token")
        if token:
            return token
    env_token = os.environ.get("SIYUAN_API_TOKEN")
    if env_token:
        return env_token
    return None


def _post(endpoint: str, payload: dict, config=None) -> Any:
    """POST a JSON-RPC request to the SiYuan kernel and return ``data``.

    Raises ``SiYuanAPIError`` when the kernel reports ``code != 0``.
    """
    base = _api_base(config)
    _validate_api_url(base, allow_remote=_allow_remote(config))
    url = f"{base}{endpoint}"
    body = json.dumps(payload or {}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": _UA}
    token = _api_token(config)
    if token:
        headers["Authorization"] = f"Token {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("SiYuan API response exceeds the 1 MiB safety limit")

    result = json.loads(raw.decode("utf-8"))
    if not isinstance(result, dict):
        raise SiYuanAPIError("SiYuan API returned a non-object response")
    code = result.get("code", 0)
    if code != 0:
        raise SiYuanAPIError(result.get("msg") or f"SiYuan API error (code={code})")
    return result.get("data")


class SiYuanChannel(Channel):
    name = "siyuan"
    description = "思源笔记本地 API（笔记本、文档、块、搜索）"
    backends = ["SiYuan API (本地)"]
    tier = 1  # needs the desktop app running; token optional but recommended

    # ------------------------------------------------------------------ #
    # URL routing
    # ------------------------------------------------------------------ #

    def can_handle(self, url: str) -> bool:
        """SiYuan block/doc links use the ``siyuan://`` scheme.

        Local kernel URLs (``http://127.0.0.1:6806/...``) are also accepted so
        that agents pasting a SiYuan API URL land here.
        """
        try:
            parsed = urlsplit(url)
        except (TypeError, ValueError):
            return False
        if parsed.scheme == "siyuan":
            return True
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        return host in _LOOPBACK_HOSTS and (port is None or port == 6806)

    # ------------------------------------------------------------------ #
    # Health check
    # ------------------------------------------------------------------ #

    def check(self, config=None):
        try:
            data = _post("/api/system/version", {}, config)
            version = (data or {}).get("version", "")
            self.active_backend = self.backends[0]
            if version:
                return "ok", f"思源笔记 API 可用（版本 {version}）"
            return "ok", "思源笔记 API 可用"
        except Exception as e:
            self.active_backend = None
            from agent_reach.utils.text import scrub_url_credentials

            detail = scrub_url_credentials(e).rstrip(": ")
            return (
                "warn",
                f"思源笔记 API 连接失败：{detail}。"
                "请确认思源笔记已启动，并在 设置 → 关于 → API Token 中"
                "获取令牌后运行：agent-reach configure siyuan-token（隐藏输入）",
            )

    # ------------------------------------------------------------------ #
    # Read-only data methods
    # ------------------------------------------------------------------ #

    def list_notebooks(self, config=None) -> list:
        """列出所有笔记本。

        Returns a list of dicts with keys:
          id, name, icon, sort, closed
        """
        data = _post("/api/notebook/lsNotebooks", {}, config)
        notebooks = (data or {}).get("notebooks") or []
        return [
            {
                "id": nb.get("id", ""),
                "name": nb.get("name", ""),
                "icon": nb.get("icon", ""),
                "sort": nb.get("sort", 0),
                "closed": nb.get("closed", False),
            }
            for nb in notebooks
        ]

    def list_docs(self, notebook_id: str, path: str = "/", config=None) -> list:
        """列出指定笔记本路径下的文档与子目录。

        Args:
            notebook_id: 笔记本 ID
            path:        文档树路径，以 / 开头

        Returns a list of dicts with keys:
          id, name, path, sub_file_count, is_dir
        """
        data = _post(
            "/api/filetree/listDocsByPath",
            {"notebook": notebook_id, "path": path},
            config,
        )
        files = (data or {}).get("files") or []
        return [
            {
                "id": f.get("id", ""),
                "name": f.get("name", ""),
                "path": f.get("path", ""),
                "sub_file_count": f.get("subFileCount", 0),
                "is_dir": f.get("isDir", False),
            }
            for f in files
        ]

    def get_block_kramdown(self, block_id: str, config=None) -> dict:
        """获取指定块的 Kramdown（Markdown）源码。

        Args:
            block_id: 块 ID

        Returns a dict with keys: id, kramdown
        """
        data = _post("/api/block/getBlockKramdown", {"id": block_id}, config)
        return {
            "id": block_id,
            "kramdown": (data or {}).get("kramdown", ""),
        }

    def get_child_blocks(self, block_id: str, config=None) -> list:
        """获取指定块的直接子块列表。

        Args:
            block_id: 父块 ID

        Returns a list of dicts with keys: id, type, sub_type
        """
        data = _post("/api/block/getChildBlocks", {"id": block_id}, config)
        children = data or []
        return [
            {
                "id": c.get("id", ""),
                "type": c.get("type", ""),
                "sub_type": c.get("subType", ""),
            }
            for c in children
        ]

    def get_doc_outline(self, doc_id: str, config=None) -> list:
        """获取文档的标题大纲。

        Args:
            doc_id: 文档块 ID

        Returns a list of dicts with keys: id, title, level
        """
        data = _post("/api/outline/getDocOutline", {"id": doc_id}, config)
        outline = data or []
        return [
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "level": item.get("level", 0),
            }
            for item in outline
        ]

    def search(self, query: str, config=None) -> list:
        """全文搜索笔记内容块。

        Args:
            query: 搜索关键词

        Returns a list of dicts with keys:
          id, title, content, path, notebook
        """
        if not query:
            return []
        data = _post(
            "/api/search/fullTextSearchBlock",
            {"query": query},
            config,
        )
        blocks = (data or {}).get("blocks") or []
        results = []
        for b in blocks[:20]:
            results.append(
                {
                    "id": b.get("id", ""),
                    "title": b.get("title", ""),
                    "content": b.get("content", ""),
                    "path": b.get("path", ""),
                    "notebook": b.get("box", ""),
                }
            )
        return results
