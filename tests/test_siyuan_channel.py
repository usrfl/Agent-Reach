# -*- coding: utf-8 -*-
"""Dedicated tests for the ``siyuan`` channel.

SiYuan (思源笔记) exposes a local JSON-RPC kernel API.  These tests stub the
shared ``_post`` helper (and the lower-level ``urllib.request.urlopen``) so the
channel's URL-validation, response-shaping, and error-handling logic runs
offline.  The real network path is exercised only by ``check()`` against a
running desktop app.
"""

import json
from unittest.mock import patch
from urllib.error import URLError

import pytest

from agent_reach.channels import siyuan as s
from agent_reach.channels.siyuan import SiYuanChannel, SiYuanAPIError


# --- can_handle ---

def test_can_handle_matches_siyuan_scheme_and_loopback():
    ch = SiYuanChannel()
    for url in [
        "siyuan://blocks/20210808180117-czj9bvb",
        "siyuan://20210808180117-czj9bvb",
        "http://127.0.0.1:6806/api/notebook/lsNotebooks",
        "http://localhost:6806/api/system/version",
    ]:
        assert ch.can_handle(url) is True, url


def test_can_handle_rejects_non_siyuan_urls():
    ch = SiYuanChannel()
    for url in [
        "https://github.com/panniantong/agent-reach",
        "https://www.v2ex.com/t/123",
        "http://example.com:6806/api/x",
        "http://127.0.0.1:9999/api/x",
        "",
    ]:
        assert ch.can_handle(url) is False, url


# --- URL / host validation ---

@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:6806",
        "https://siyuan.evil.test",
        "http://user:pass@127.0.0.1:6806",
        "ftp://127.0.0.1:6806",
        "not-a-url",
    ],
)
def test_validate_api_url_rejects_non_loopback(url):
    with pytest.raises(ValueError):
        s._validate_api_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6806",
        "http://localhost:6806",
        "http://127.0.0.1:6807",  # custom port allowed
        "https://localhost:6806",
    ],
)
def test_validate_api_url_accepts_loopback(url):
    # Must not raise.
    s._validate_api_url(url)


# --- check() ---

def test_check_ok_sets_active_backend_and_version():
    ch = SiYuanChannel()
    with patch.object(s, "_post", return_value={"version": "3.0.0"}):
        status, message = ch.check()
    assert status == "ok"
    assert "3.0.0" in message
    assert ch.active_backend == ch.backends[0]


def test_check_ok_without_version():
    ch = SiYuanChannel()
    with patch.object(s, "_post", return_value={}):
        status, message = ch.check()
    assert status == "ok"
    assert ch.active_backend == ch.backends[0]


def test_check_warn_on_exception_clears_backend():
    ch = SiYuanChannel()
    ch.active_backend = "stale"
    with patch.object(s, "_post", side_effect=URLError("connection refused")):
        status, message = ch.check()
    assert status == "warn"
    assert "连接失败" in message
    assert ch.active_backend is None


def test_check_when_app_down_returns_warn_not_error(monkeypatch):
    """A stopped SiYuan app must surface as warn, never crash the doctor."""
    ch = SiYuanChannel()
    monkeypatch.setattr(s.urllib.request, "urlopen", _raise_connection_refused)
    status, message = ch.check()
    assert status == "warn"
    assert ch.active_backend is None


def _raise_connection_refused(*_a, **_k):
    raise URLError("Connection refused")


# --- _post: response parsing + error handling ---

def test_post_returns_data_field_on_success():
    fake_resp = _FakeResponse({"code": 0, "msg": "", "data": {"k": "v"}})
    with patch.object(s.urllib.request, "urlopen", return_value=fake_resp):
        assert s._post("/api/x", {}, None) == {"k": "v"}


def test_post_raises_on_nonzero_code():
    fake_resp = _FakeResponse({"code": -1, "msg": "not found", "data": None})
    with patch.object(s.urllib.request, "urlopen", return_value=fake_resp):
        with pytest.raises(SiYuanAPIError, match="not found"):
            s._post("/api/x", {}, None)


def test_post_raises_on_non_object_response():
    fake_resp = _FakeResponse([])
    with patch.object(s.urllib.request, "urlopen", return_value=fake_resp):
        with pytest.raises(SiYuanAPIError):
            s._post("/api/x", {}, None)


def test_post_sends_authorization_header_when_token_configured():
    captured = {}

    class _CaptureResponse:
        def __init__(self, req):
            captured["headers"] = dict(req.header_items())
            captured["data"] = req.data

        def read(self, _n):
            return json.dumps({"code": 0, "msg": "", "data": None}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        return _CaptureResponse(req)

    with patch.object(s.urllib.request, "urlopen", side_effect=fake_urlopen):
        s._post("/api/system/version", {}, {"siyuan_api_token": "secret-123"})

    assert captured["headers"].get("Authorization") == "Token secret-123"
    # urllib normalizes header casing (Content-Type -> Content-type)
    lowered = {k.lower(): v for k, v in captured["headers"].items()}
    assert lowered["content-type"] == "application/json"


def test_post_skips_authorization_without_token():
    captured = {}

    class _CaptureResponse:
        def __init__(self, req):
            captured["headers"] = dict(req.header_items())

        def read(self, _n):
            return json.dumps({"code": 0, "msg": "", "data": None}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        return _CaptureResponse(req)

    with patch.object(s.urllib.request, "urlopen", side_effect=fake_urlopen):
        s._post("/api/system/version", {}, None)

    assert "Authorization" not in captured["headers"]


def test_post_uses_configured_base_url():
    captured = {}

    class _CaptureResponse:
        def __init__(self, req):
            captured["url"] = req.full_url

        def read(self, _n):
            return json.dumps({"code": 0, "msg": "", "data": None}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        return _CaptureResponse(req)

    with patch.object(s.urllib.request, "urlopen", side_effect=fake_urlopen):
        s._post("/api/system/version", {}, {"siyuan_api_url": "http://localhost:6807"})

    assert captured["url"] == "http://localhost:6807/api/system/version"


def test_post_rejects_public_url_before_network(monkeypatch):
    """A misconfigured public URL must never hit the network."""
    monkeypatch.setattr(s.urllib.request, "urlopen", _assert_not_called)
    with pytest.raises(ValueError):
        s._post(
            "/api/system/version",
            {},
            {"siyuan_api_url": "http://evil.example.com:6806"},
        )


def _assert_not_called(*_a, **_k):
    raise AssertionError("urlopen must not be called for a public URL")


# --- list_notebooks ---

def test_list_notebooks_maps_fields():
    ch = SiYuanChannel()
    payload = {
        "notebooks": [
            {"id": "nb-1", "name": "测试", "icon": "1f4d4", "sort": 0, "closed": False},
            {"id": "nb-2", "name": "日记", "icon": "", "sort": 1, "closed": True},
        ]
    }
    with patch.object(s, "_post", return_value=payload):
        notebooks = ch.list_notebooks()
    assert len(notebooks) == 2
    assert notebooks[0]["id"] == "nb-1"
    assert notebooks[0]["name"] == "测试"
    assert notebooks[1]["closed"] is True


def test_list_notebooks_handles_empty():
    ch = SiYuanChannel()
    with patch.object(s, "_post", return_value={"notebooks": []}):
        assert ch.list_notebooks() == []


# --- list_docs ---

def test_list_docs_maps_fields():
    ch = SiYuanChannel()
    payload = {
        "files": [
            {
                "id": "doc-1",
                "name": "欢迎使用",
                "path": "/20210808180117-czj9bvb.sy",
                "subFileCount": 0,
                "isDir": False,
            },
            {
                "id": "dir-1",
                "name": "子目录",
                "path": "/20210808180117-czj9bvb",
                "subFileCount": 3,
                "isDir": True,
            },
        ]
    }
    with patch.object(s, "_post", return_value=payload) as post:
        docs = ch.list_docs("nb-1", "/")
    assert len(docs) == 2
    assert docs[0]["is_dir"] is False
    assert docs[1]["is_dir"] is True
    assert docs[1]["sub_file_count"] == 3
    # Verify the request payload reached the kernel.
    called_payload = post.call_args.args[1]
    assert called_payload == {"notebook": "nb-1", "path": "/"}


# --- get_block_kramdown ---

def test_get_block_kramdown_returns_markdown():
    ch = SiYuanChannel()
    payload = {"id": "blk-1", "kramdown": "# 标题\n\n正文内容"}
    with patch.object(s, "_post", return_value=payload):
        result = ch.get_block_kramdown("blk-1")
    assert result["id"] == "blk-1"
    assert result["kramdown"] == "# 标题\n\n正文内容"


# --- get_child_blocks ---

def test_get_child_blocks_maps_types():
    ch = SiYuanChannel()
    payload = [
        {"id": "c1", "type": "h", "subType": "h1"},
        {"id": "c2", "type": "p", "subType": ""},
    ]
    with patch.object(s, "_post", return_value=payload):
        children = ch.get_child_blocks("parent-1")
    assert len(children) == 2
    assert children[0]["type"] == "h"
    assert children[0]["sub_type"] == "h1"
    assert children[1]["sub_type"] == ""


# --- get_doc_outline ---

def test_get_doc_outline_maps_levels():
    ch = SiYuanChannel()
    payload = [
        {"id": "h1", "title": "第一章", "level": 1},
        {"id": "h2", "title": "1.1 小节", "level": 2},
    ]
    with patch.object(s, "_post", return_value=payload):
        outline = ch.get_doc_outline("doc-1")
    assert outline[0]["level"] == 1
    assert outline[1]["title"] == "1.1 小节"


# --- search ---

def test_search_maps_blocks_and_limits_results():
    ch = SiYuanChannel()
    blocks = [
        {
            "id": f"b{i}",
            "title": f"标题{i}",
            "content": f"内容{i}",
            "path": f"/doc{i}.sy",
            "box": "nb-1",
        }
        for i in range(25)
    ]
    with patch.object(s, "_post", return_value={"blocks": blocks}):
        results = ch.search("关键词")
    assert len(results) == 20  # capped
    assert results[0]["notebook"] == "nb-1"
    assert results[0]["content"] == "内容0"


def test_search_empty_query_returns_empty_without_network():
    ch = SiYuanChannel()
    with patch.object(s, "_post", side_effect=AssertionError("must not call")):
        assert ch.search("") == []


# --- helpers ---

class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, n=-1):
        return self._payload[:n] if n and n > 0 else self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
