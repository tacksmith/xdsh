from __future__ import annotations

import asyncio
import json
import threading

import pytest

from xdsh.core.external import (
    ExternalPolicyError,
    ExternalProtocolError,
    HttpRequest,
    HttpResponse,
    WebDocument,
)
from xdsh.core.external_gateway import RestrictedMcpGateway
from xdsh.core.external_runtime import (
    build_external_clients,
    external_config_from_env,
)
from xdsh.core.public_web import (
    PinnedPublicTransport,
    PublicDnsResolver,
    PublicSearchUnavailable,
    PublicWebClient,
    SearchResultsParser,
)


@pytest.mark.parametrize('body', ['<html>No results</html>', '<form id="b_captcha"></form>'])
async def test_blocked_search_never_reports_successful_empty_evidence(body: str) -> None:
    class Client(PublicWebClient):
        async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
            return WebDocument(url, url, 200, 'text/html', body, 'test')

    with pytest.raises(PublicSearchUnavailable):
        await Client().search('public documentation')


def test_search_parser_preserves_sources_and_rejects_private_links() -> None:
    parser = SearchResultsParser()
    for url in ['https://docs.python.org/', 'http://bad.example/', 'https://127.0.0.1/',
                'https://' + 'user:' + 'password@bad.example/', 'https://docs.python.org/']:
        parser.feed(f'<li class="b_algo"><h2><a href="{url}">Python docs</a></h2>'
                    '<p>Reference material</p></li>')
    assert len(parser.results) == 1
    assert parser.results[0]['url'] == 'https://docs.python.org/'
    assert 'Reference material' in parser.results[0]['snippet']


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '198.18.0.1'])
def test_doh_rejects_nonpublic_answers(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    def get(*args: object) -> HttpResponse:
        return HttpResponse(status=200, url='https://dns.google/resolve', headers={},
                            body=json.dumps({'Status': 0, 'Answer': [
                                {'type': 1, 'data': address, 'TTL': 60}]}).encode())
    monkeypatch.setattr('xdsh.core.public_web._get', get)
    with pytest.raises(ExternalPolicyError):
        PublicDnsResolver().resolve('example.com', 443)


@pytest.mark.parametrize('url', ['http://example.com/', 'https://127.0.0.1/',
                                 'https://169.254.169.254/', 'file:///etc/passwd'])
async def test_fetch_ssrf_blocked_before_transport(url: str) -> None:
    with pytest.raises((ExternalPolicyError, ValueError)):
        await PublicWebClient().fetch(url)


@pytest.mark.parametrize('proxy', ['http://10.0.0.1:1234', 'https://127.0.0.1:1234',
                                   'http://u:p@127.0.0.1:1234', 'http://localhost:1234'])
def test_proxy_is_explicit_loopback_without_auth(proxy: str) -> None:
    with pytest.raises(ValueError):
        PublicWebClient(proxy=proxy)


async def test_independent_batches_preserve_priority_dedupe_and_partial_evidence() -> None:
    active = peak = 0
    started: list[str] = []

    class Client(PublicWebClient):
        async def search(self, query: str) -> list[dict[str, str]]:
            nonlocal active, peak
            started.append(query)
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                if query == 'fail':
                    raise ExternalProtocolError('blocked')
                return [{'url': 'https://example.com/', 'title': query}]
            finally:
                active -= 1

    client = Client()
    with pytest.raises(ExternalPolicyError):
        await client.search_many(['a', 'b'], independent=False)
    assert started == []
    result = await client.search_many(
        ['official', 'secondary', 'official', 'fail'], independent=True,
    )
    assert started == ['official', 'secondary', 'fail']
    assert peak == 2
    assert result['complete'] is False
    rows = result['results']
    assert isinstance(rows, list) and len(rows) == 3


async def test_low_yield_batch_compacts_duplicate_sources_and_reports_missing_model() -> None:
    class Client(PublicWebClient):
        async def search(self, query: str) -> list[dict[str, str]]:
            return [{'url': 'https://example.com/volcano', 'title': 'Geology',
                     'snippet': 'A volcano is a mountain.'}]

    result = await Client().search_many(
        ['vendor deepseek-v4.1-flash plan', 'deepseek-v4.1-flash release'],
        independent=True,
    )
    rows = result['results']
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    assert first['sources']
    second = rows[1]
    assert isinstance(second, dict)
    assert second['sources'] == []
    assert second['sources_from_query'] == first['query']
    first_quality = first['quality']
    assert isinstance(first_quality, dict)
    assert first_quality['unmatched_identifiers'] == ['deepseek-v4.1-flash']
    total_quality = result['quality']
    assert isinstance(total_quality, dict)
    assert total_quality['duplicate_source_lists'] == 1
    next_action = total_quality['next_action']
    assert isinstance(next_action, str)
    assert 'not evidence of absence' in next_action
    assert 'different source' in next_action


async def test_batch_keeps_distinct_excerpts_and_does_not_invent_semantic_absence() -> None:
    class Client(PublicWebClient):
        async def search(self, query: str) -> list[dict[str, str]]:
            return [{'url': 'https://example.com/docs', 'title': 'Model docs',
                     'snippet': query}]

    result = await Client().search_many(
        ['deepseek-v4.1-flash release', 'deepseek-v4.1-flash plan'], independent=True,
    )
    rows = result['results']
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        assert row['sources']
    total_quality = result['quality']
    assert isinstance(total_quality, dict)
    assert total_quality['duplicate_source_lists'] == 0
    for row in rows:
        quality = row['quality']
        assert isinstance(quality, dict)
        assert not quality['unmatched_identifiers']


async def test_gateway_exposes_public_tools_only_after_opt_in() -> None:
    assert build_external_clients(external_config_from_env({})).public_web is None
    clients = build_external_clients(external_config_from_env({'XDSH_PUBLIC_WEB_ENABLED': '1'}))
    gateway = RestrictedMcpGateway(clients)
    response = await gateway.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert response is not None
    result = response['result']
    assert isinstance(result, dict) and isinstance(result['tools'], list)
    assert {row['name'] for row in result['tools'] if isinstance(row, dict)} == {
        'web_search', 'web_fetch',
    }
    invalid = await gateway.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                      'params': {'name': 'web_search', 'arguments': {}}})
    assert invalid is not None and 'error' in invalid


@pytest.mark.parametrize('wrapped', [False, True])
async def test_gateway_batch_replay_reaches_concurrent_search(
    monkeypatch: pytest.MonkeyPatch, wrapped: bool,
) -> None:
    active = peak = 0
    started: list[str] = []

    async def search(self: PublicWebClient, query: str) -> list[dict[str, str]]:
        nonlocal active, peak
        started.append(query)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return [{'url': 'https://example.com/' + query, 'title': query}]
        finally:
            active -= 1

    monkeypatch.setattr(PublicWebClient, 'search', search)
    gateway = RestrictedMcpGateway(build_external_clients(
        external_config_from_env({'XDSH_PUBLIC_WEB_ENABLED': '1'}),
    ))
    queries = ['official', 'plan', 'release', 'official']
    response = await gateway.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'web_search', 'arguments': {
            'queries': [{'query': q} for q in queries] if wrapped else queries,
            'independent': True,
        }}})
    assert response is not None and 'result' in response, response
    assert started == ['official', 'plan', 'release']
    assert peak == 2


@pytest.mark.parametrize('arguments', [
    {'queries': [{'query': 'a', 'headers': {'Authorization': 'private'}}], 'independent': True},
    {'queries': [{'query': 123}], 'independent': True},
    {'queries': ['a'], 'independent': False},
    {'queries': ['a']},
    {'queries': ['a'] * 5, 'independent': True},
    {'queries': ['a'], 'query': 'b', 'independent': True},
])
async def test_search_schema_failure_is_actionable_without_network(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object],
) -> None:
    async def search(*args: object, **kwargs: object) -> object:
        pytest.fail('Invalid batch must not reach search')

    monkeypatch.setattr(PublicWebClient, 'search_many', search)
    gateway = RestrictedMcpGateway(build_external_clients(
        external_config_from_env({'XDSH_PUBLIC_WEB_ENABLED': '1'}),
    ))
    response = await gateway.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'web_search', 'arguments': arguments}})
    assert response is not None
    error = response['error']
    assert isinstance(error, dict)
    assert error['code'] == -32602
    message = error['message']
    assert isinstance(message, str)
    assert 'independent' in message
    assert 'private' not in json.dumps(response)


def test_pinned_transport_does_not_forward_caller_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    from xdsh.core.external import StaticHostResolver

    calls: list[tuple[object, ...]] = []

    def get(*args: object) -> HttpResponse:
        calls.append(args)
        return HttpResponse(status=200, url='https://example.com/', headers={}, body=b'ok')

    monkeypatch.setattr('xdsh.core.public_web._get', get)
    transport = PinnedPublicTransport(StaticHostResolver({'example.com': ['93.184.216.34']}), None)
    transport.request(HttpRequest(method='GET', url='https://example.com/',
                                  headers={'Authorization': 'not-for-forwarding'},
                                  max_response_bytes=1000), timeout_seconds=5)
    assert calls[0][1] == '93.184.216.34'
    assert 'not-for-forwarding' not in repr(calls)


def test_cancelled_http_loop_returns_while_blocking_socket_keeps_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release, returned = threading.Event(), threading.Event(), threading.Event()

    async def blocked(_self: object, url: str, **_kwargs: object) -> WebDocument:
        started.set()
        release.wait(2)
        return WebDocument(url, url, 200, 'text/plain', 'done', 'test')

    monkeypatch.setattr('xdsh.core.public_web.SafeWebFetcher.fetch', blocked)
    client = PublicWebClient()

    async def short_request() -> None:
        # Python 3.10 has a distinct asyncio timeout type; 3.11+ aliases it.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.fetch('https://example.com/'), timeout=0.05)

    def handler() -> None:
        asyncio.run(short_request())
        returned.set()

    thread = threading.Thread(target=handler)
    thread.start()
    try:
        assert started.wait(1)
        assert returned.wait(0.3), 'HTTP loop waited for the cancelled blocking socket'
    finally:
        release.set()
        thread.join(3)
