"""Regressions at the real MCP boundary from the paired September trials."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xdsh.core.evidence import exact_json_evidence
from xdsh.core.external import ExternalHttpError, ExternalPolicyError, WebDocument
from xdsh.core.external_gateway import RestrictedMcpGateway
from xdsh.core.external_runtime import ExternalClients
from xdsh.core.public_web import PublicWebClient


def test_retrieval_plan_gates_dependencies_and_caps_parallelism() -> None:
    from xdsh.core.retrieval import plan_retrieval, retrieval_plan_context

    dependent = plan_retrieval("先查官方版本, 然后打开发布说明详情")
    assert dependent is not None
    assert dependent.mode == "dependent"
    assert dependent.max_parallel == 2

    independent = plan_retrieval("对比多个官方来源, 分别核实版本和发布日期")
    assert independent is not None
    assert independent.mode == "independent"
    context = retrieval_plan_context("对比多个官方来源, 分别核实版本和发布日期")
    assert context is not None
    assert "at most two concurrent reads" in context
    assert plan_retrieval("请写一个 Python 函数") is None


def request(arguments: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "web_fetch", "arguments": arguments}}


def test_public_transport_negotiates_github_json_without_forwarding_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core import public_web
    from xdsh.core.external import HttpRequest, HttpResponse, StaticHostResolver

    seen = []

    def get(*args: Any) -> HttpResponse:
        seen.append(args)
        return HttpResponse(status=200, url="https://api.github.com/repos", headers={}, body=b"[]")

    monkeypatch.setattr(public_web, "_get", get)
    transport = public_web.PinnedPublicTransport(
        StaticHostResolver({"api.github.com": ("93.184.216.34",)}), None,
    )
    transport.request(HttpRequest(method="GET", url="https://api.github.com/repos",
                               headers={"Authorization": "Bearer secret"},
                               max_response_bytes=1000), timeout_seconds=5)
    assert seen[0][-1] == "application/vnd.github+json"
    assert "secret" not in str(seen)


def test_terminal_progress_never_reintroduces_eta_on_nonfinal_refresh() -> None:
    from xdsh.core.events import FinishEvent, TodoEvent, TodoItem
    from xdsh.telegram.render import ProgressRenderer

    renderer = ProgressRenderer(language="en")
    renderer.accept(TodoEvent(session_id="test", items=(
        TodoItem(content="Read", status="completed"),
        TodoItem(content="Verify", status="in_progress"),
    )))
    renderer.accept(FinishEvent(session_id="test", reason="completed"))
    text = renderer.render(final=False, elapsed_seconds=120)
    assert text is not None
    assert "ETA" not in text
    assert "checklist not reconciled" in text


@pytest.mark.parametrize("language", ["zh-CN", "zh-TW", "en", "ja", "de"])
@pytest.mark.parametrize("final", [False, True])
def test_progress_omits_tool_error_counter_but_keeps_diagnostics(
    language: str, final: bool,
) -> None:
    from xdsh.core.events import ToolResultEvent, ToolStartEvent
    from xdsh.telegram.render import ProgressRenderer

    renderer = ProgressRenderer(language=language)
    renderer.accept(ToolStartEvent(session_id="test", call_id="w", name="web_fetch"))
    renderer.accept(ToolResultEvent(session_id="test", call_id="w", name="web_fetch",
                                    is_error=True, content="private provider error"))
    text = renderer.render(final=final, elapsed_seconds=3) or ""
    assert sum(renderer.tool_errors.values()) == 1
    assert not any(label in text for label in ("工具错误", "工具錯誤", "Tool errors"))
    assert "private provider error" not in text


def test_length_diagnostic_is_only_in_final_progress_not_answer() -> None:
    from xdsh.telegram.render import ProgressRenderer, TurnRenderer

    progress = ProgressRenderer(language="en", answer_length_overrun=(14, 10))
    assert "14/10" not in (progress.render(final=False, elapsed_seconds=1) or "")
    assert "14/10" in (progress.render(final=True, elapsed_seconds=1) or "")
    answer = TurnRenderer(max_answer_chars=1000, original_request="只输出JSON")
    answer.text_parts = ['{"value":true}']
    answer.apply_output_contract()
    assert json.loads("".join(answer.text_parts)) == {"value": True}


def test_incomplete_web_evidence_blocks_unqualified_absence_claim() -> None:
    from xdsh.core.events import TextEvent, ToolResultEvent
    from xdsh.telegram.render import TurnRenderer

    renderer = TurnRenderer(
        max_answer_chars=4_000,
        language="zh-CN",
        original_request="请检索这个频道最近一周的动态",
    )
    renderer.accept(ToolResultEvent(
        session_id="s",
        call_id="c",
        name="web_search",
        content='{"complete": false, "results": []}',
    ))
    renderer.accept(TextEvent(session_id="s", text="该频道不存在。"))
    renderer.apply_output_contract()
    answer = "".join(renderer.text_parts)
    assert "搜索无结果不等于对象不存在" in answer


def test_qualified_retrieval_uncertainty_is_not_repeated() -> None:
    from xdsh.core.events import TextEvent, ToolResultEvent
    from xdsh.telegram.render import TurnRenderer

    renderer = TurnRenderer(
        max_answer_chars=4_000,
        language="zh-CN",
        original_request="请检索这个频道最近一周的动态",
    )
    renderer.accept(ToolResultEvent(
        session_id="s", call_id="c", name="web_search",
        content='{"complete": false, "results": []}',
    ))
    renderer.accept(TextEvent(session_id="s", text="无法确认该频道最近一周的动态。"))
    renderer.apply_output_contract()
    assert "搜索无结果不等于对象不存在" not in "".join(renderer.text_parts)


class Reader(PublicWebClient):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []

    async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
        self.calls.append(url)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if url.endswith("/denied"):
                raise ExternalPolicyError("secret provider detail must not escape")
            return WebDocument(url, url, 200, "text/plain", "evidence " * 2_000, "hash")
        finally:
            self.active -= 1


def test_truncated_npm_dist_tags_offers_small_official_endpoint() -> None:
    from xdsh.core.external_gateway import _public_excerpt

    url = "https://registry.npmjs.org/@deepseek-ai/dsh"
    result = _public_excerpt(WebDocument(url, url, 200, "application/json", '{"x":',
                                        "hash", truncated=True), 12000, None, "/dist-tags")
    assert "https://registry.npmjs.org/-/package/@deepseek-ai/dsh/dist-tags" in str(result)
    assert "not a complete version inventory" in str(result)


@pytest.mark.asyncio
async def test_public_fetch_marks_truncated_evidence_explicitly() -> None:
    gateway = RestrictedMcpGateway(ExternalClients(public_web=Reader()))
    response: Any = await gateway.dispatch(request({"url": "https://example.org/a",
                                                    "max_bytes": 100}))
    assert response["result"]["structuredContent"]["truncated"] is True
    assert "not evidence of absence" in response["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_independent_fetch_batch_really_overlaps_and_preserves_partial_results() -> None:
    reader = Reader()
    gateway = RestrictedMcpGateway(ExternalClients(public_web=reader))
    response: Any = await gateway.dispatch(request({
        "urls": ["https://example.org/a", "https://example.org/b", "https://example.org/denied"],
        "independent": True, "max_bytes": 500,
    }))
    assert "result" in response
    assert reader.peak == 2
    result = response["result"]["structuredContent"]
    assert result["complete"] is False
    assert [r["ok"] for r in result["results"]] == [True, True, False]
    assert "secret provider detail" not in str(response)


@pytest.mark.asyncio
async def test_policy_rejection_is_actionable_without_disclosing_private_detail() -> None:
    gateway = RestrictedMcpGateway(ExternalClients(public_web=Reader()))
    response: Any = await gateway.dispatch(request({"url": "https://example.org/denied"}))
    assert response["result"]["isError"] is True
    error = response["result"]["structuredContent"]["error"]
    assert error["kind"] == "policy_denied"
    assert error["retryable"] is False
    assert "secret provider detail" not in str(response)


@pytest.mark.asyncio
async def test_batch_deadline_retains_completed_page_and_cancels_pending() -> None:
    class SlowReader(Reader):
        async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
            if url.endswith("/slow"):
                await asyncio.sleep(60)
            return await super().fetch(url, html=html)

    reader = SlowReader()
    gateway = RestrictedMcpGateway(ExternalClients(public_web=reader),
                                   public_fetch_timeout_seconds=0.05)
    response: Any = await gateway.dispatch(request({
        "urls": ["https://example.org/a", "https://example.org/slow"], "independent": True,
    }))
    rows = response["result"]["structuredContent"]["results"]
    assert rows[0]["ok"] is True
    assert rows[1]["error"]["kind"] == "timeout"
    assert response["result"]["isError"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"urls": ["https://example.org/a"], "independent": False},
    {"urls": ["https://example.org/a"] * 5, "independent": True},
    {"url": "https://example.org/a", "credential": "not-allowed"},
    {"url": "https://example.org/a", "max_bytes": True},
])
async def test_invalid_batch_never_fetches(args: dict[str, object]) -> None:
    reader = Reader()
    response: Any = await RestrictedMcpGateway(ExternalClients(public_web=reader)).dispatch(
        request(args),
    )
    assert response["error"]["code"] == -32602
    assert not reader.calls


@pytest.mark.asyncio
async def test_batch_deduplication_and_shared_utf8_budget() -> None:
    reader = Reader()
    response: Any = await RestrictedMcpGateway(ExternalClients(public_web=reader)).dispatch(
        request({"urls": ["https://example.org/a", "https://example.org/b",
                          "https://example.org/a"], "independent": True, "max_bytes": 3,
                 "focus": "not in document"}),
    )
    rows = response["result"]["structuredContent"]["results"]
    assert len(rows) == len(reader.calls) == 2
    assert sum(row["returned_bytes"] for row in rows) <= 3
    assert all(row["focus_matched"] is False and row["truncated"] for row in rows)


@pytest.mark.asyncio
async def test_non_retryable_failure_cached_even_when_only_output_size_changes() -> None:
    reader = Reader()
    gateway = RestrictedMcpGateway(ExternalClients(public_web=reader))
    for size in (100, 200):
        response: Any = await gateway.dispatch(request({"url": "https://example.org/denied",
                                                        "max_bytes": size}))
        assert response["result"]["isError"]
    assert len(reader.calls) == 1
    assert response["result"]["structuredContent"]["cached_failure"]


@pytest.mark.asyncio
async def test_failure_cache_collapses_only_wire_equivalent_urls() -> None:
    class Missing(Reader):
        async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
            self.calls.append(url)
            raise ExternalHttpError(404)

    reader = Missing()
    gateway = RestrictedMcpGateway(ExternalClients(public_web=reader))
    for url in ("https://example.org/a?x=1", "https://EXAMPLE.org:443/a?x=1#next"):
        await gateway.dispatch(request({"url": url}))
    assert len(reader.calls) == 1
    for url in ("https://example.org/a?x=2", "https://example.org/A?x=1",
                "https://user@example.org/a?x=1"):
        await gateway.dispatch(request({"url": url}))
    assert len(reader.calls) == 4  # Query, path case and invalid credentials stay distinct.


def test_invalid_urls_cannot_poison_valid_failure_keys() -> None:
    from xdsh.core.external_gateway import _failure_key

    valid = "https://example.org/a"
    for invalid in ("\n" + valid, "https://exam\nple.org/a", valid + "?",
                    "https://u@example.org/a", "https://example.org:bad/a"):
        assert _failure_key(invalid) != _failure_key(valid)
    assert len(_failure_key(valid + "\ud800")) == 64


@pytest.mark.asyncio
async def test_transient_http_failure_not_cached_or_retried_automatically() -> None:
    class Transient(Reader):
        async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
            self.calls.append(url)
            raise ExternalHttpError(429)

    reader = Transient()
    gateway = RestrictedMcpGateway(ExternalClients(public_web=reader))
    for _ in range(2):
        response: Any = await gateway.dispatch(request({"url": "https://example.org/a"}))
        failure = response["result"]["structuredContent"]["error"]
        assert failure["http_status"] == 429 and failure["max_retries"] == 1
    assert len(reader.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kind", "instruction"), [
    (415, "request_incompatible", "request headers"),
    (406, "request_incompatible", "request headers"),
    (404, "not_found", "Do not guess"),
])
async def test_permanent_http_failure_explains_correct_recovery(
    status: int, kind: str, instruction: str,
) -> None:
    class FailedReader(Reader):
        async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
            self.calls.append(url)
            raise ExternalHttpError(status)

    reader = FailedReader()
    gateway = RestrictedMcpGateway(ExternalClients(public_web=reader))
    for _ in range(2):
        response: Any = await gateway.dispatch(request({"url": "https://example.org/a"}))
        failure = response["result"]["structuredContent"]["error"]
        assert failure["kind"] == kind
        assert failure["http_status"] == status
        assert failure["max_retries"] == 0
        assert instruction in failure["next_action"]
    assert len(reader.calls) == 1


@pytest.mark.parametrize(("pointer", "found", "selected"), [
    ("/time/v2.7.4", False, ""), ("/time/2.7.4", True, '"2026-08-03"'),
    ("/a~1b/0", True, '"value"'), ("/a~1b/01", False, ""),
])
def test_exact_json_keys_never_gain_prefix_or_numeric_alias(
    pointer: str, found: bool, selected: str,
) -> None:
    metadata, text = exact_json_evidence(
        '{"time":{"2.7.4":"2026-08-03"},"a/b":["value"]}', pointer, complete=True,
    )
    assert metadata["found"] is found and text == selected
    if pointer == "/time/v2.7.4":
        assert metadata["parent_keys"] == ["2.7.4"]


@pytest.mark.parametrize(("body", "complete"), [
    ('{"a":1,"a":2}', True), ('{"a":NaN}', True), ('{"a":1}', False), ('<html>', True),
])
def test_ambiguous_or_partial_json_is_unknown_not_absent(body: str, complete: bool) -> None:
    result, _ = exact_json_evidence(body, "/missing", complete=complete)
    assert result["found"] is None


@pytest.mark.asyncio
async def test_gateway_json_pointer_returns_verified_absent_key_not_absent_record() -> None:
    class JsonReader(Reader):
        async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
            return WebDocument(url, url, 200, "application/json",
                               '{"time":{"2.7.4":"2026-08-03"}}', "json-hash")

    gateway = RestrictedMcpGateway(ExternalClients(public_web=JsonReader()))
    response: Any = await gateway.dispatch(request({
        "url": "https://example.org/data", "json_pointer": "/time/v2.7.4",
    }))
    assert response["result"]["structuredContent"]["json_lookup"]["found"] is False
    assert '"found": false' in response["result"]["content"][0]["text"]
    assert "2.7.4" in response["result"]["content"][0]["text"]
