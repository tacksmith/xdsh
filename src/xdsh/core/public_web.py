"""Opt-in public HTML search/read: pinned public IPs, no cookies or credentials."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import parse_qs, urlencode, urlsplit

from xdsh.core.admission import BoundedAdmission
from xdsh.core.external import (
    AuditSink,
    ExternalBusyError,
    ExternalPolicyError,
    ExternalProtocolError,
    ExternalResourceLimitError,
    ExternalTimeoutError,
    HostResolver,
    HttpRequest,
    HttpResponse,
    NetworkPolicy,
    SafeWebFetcher,
    UrlPolicy,
    WebDocument,
)
from xdsh.core.learning import contains_sensitive_text
from xdsh.core.retrieval import assess_search_batch, retrieval_failure


class PublicSearchUnavailable(ExternalProtocolError):
    """Safe, content-free explanation suitable for a tool response."""


class _PinnedTLS(HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float, proxy: str | None) -> None:
        super().__init__(host, port=443, timeout=timeout)
        self.address = address
        self.proxy = proxy

    def connect(self) -> None:
        if self.proxy:
            parsed = urlsplit(self.proxy)
            tunnel = HTTPConnection(parsed.hostname or '127.0.0.1', parsed.port,
                                    timeout=self.timeout)
            tunnel.set_tunnel(self.address, 443)
            try:
                tunnel.connect()
                raw = tunnel.sock
                tunnel.sock = None
            except Exception:
                tunnel.close()
                raise
        else:
            raw = socket.create_connection((self.address, 443), self.timeout)
        assert raw is not None
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _get(host: str, address: str, path: str, limit: int, timeout: float,
         proxy: str | None, accept: str) -> HttpResponse:
    connection = _PinnedTLS(host, address, timeout, proxy)
    try:
        connection.request('GET', path, headers={
            'Host': host, 'Accept': accept, 'Accept-Encoding': 'identity',
            'User-Agent': 'XDSH-public-reader/1.0',
        })
        response = connection.getresponse()
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ExternalResourceLimitError('Public response exceeds size limit')
        return HttpResponse(status=response.status, url='https://' + host + path,
                            headers=dict(response.getheaders()), body=body)
    except TimeoutError as error:
        raise ExternalTimeoutError('Public HTTPS time budget exceeded') from error
    except (OSError, ValueError, HTTPException) as error:
        raise ExternalProtocolError('Public HTTPS connection failed') from error
    finally:
        connection.close()


class PublicDnsResolver:
    """Explicit Google DoH, TLS authenticated and IP-pinned; short bounded cache."""
    def __init__(self, proxy: str | None = None) -> None:
        self.proxy = proxy
        self._cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._lock = threading.Lock()

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del port
        with self._lock:
            cached = self._cache.get(hostname)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        response = _get('dns.google', '8.8.8.8', '/resolve?' + urlencode({
            'name': hostname, 'type': 'A',
        }), 32_768, 10, self.proxy, 'application/dns-json')
        if response.status != 200:
            raise ExternalProtocolError('Public DNS unavailable')
        try:
            payload = json.loads(response.body)
            if payload.get('Status') != 0:
                raise ValueError('DNS failure')
            records = payload.get('Answer', [])
            addresses = tuple(dict.fromkeys(str(row['data']) for row in records
                                            if row.get('type') == 1))
            if not addresses or len(addresses) > 32:
                raise ValueError('No bounded IPv4 answer')
            if any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise ValueError('Non-public DNS answer')
            ttl = min(60, max(0, min(int(row.get('TTL', 0)) for row in records)))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ExternalPolicyError('Invalid or non-public DNS answer') from None
        with self._lock:
            if len(self._cache) >= 256:
                self._cache.clear()
            self._cache[hostname] = (time.monotonic() + ttl, addresses)
        return addresses


class PinnedPublicTransport:
    def __init__(self, resolver: HostResolver, proxy: str | None) -> None:
        self.resolver = resolver
        self.proxy = proxy

    def request(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse:
        if request.method != 'GET' or request.body is not None:
            raise ExternalPolicyError('Public reader is GET-only')
        host = urlsplit(request.url).hostname or ''
        checked = UrlPolicy(NetworkPolicy(allowed_hosts=(host,)),
                            resolver=self.resolver).validate(request.url)
        parsed = urlsplit(checked.normalized)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        # Ignore caller headers entirely: no secret/header can leave through this client.
        accept = ('application/vnd.github+json' if host == 'api.github.com' else
                  'application/json, text/html;q=0.9, text/plain;q=0.9, application/xml;q=0.8')
        return _get(host, checked.addresses[0], path, request.max_response_bytes,
                    timeout_seconds, self.proxy, accept)


def _source_url(value: str) -> str | None:
    if len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.hostname in {'www.bing.com', 'cn.bing.com'} and parsed.path == '/ck/a':
            wrapped = parse_qs(parsed.query).get('u', [''])[0]
            if wrapped.startswith('a1'):
                encoded = wrapped[2:] + '=' * (-len(wrapped[2:]) % 4)
                value = base64.urlsafe_b64decode(encoded).decode()
                parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in {None, 443} or len(value) > 2048):
            return None
        if '.' not in parsed.hostname or parsed.hostname.endswith(('.local', '.localhost')):
            return None
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return None
        except ValueError:
            pass
        return value
    except (ValueError, UnicodeError):
        return None


class SearchResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self.active = False
        self.heading = False
        self.title = ''
        self.snippet = ''
        self.url = ''

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == 'li' and 'b_algo' in (attributes.get('class') or '').split():
            self.active = len(self.results) < 8
            self.title = self.snippet = self.url = ''
        if self.active and tag == 'h2':
            self.heading = True
        if self.active and self.heading and tag == 'a':
            self.url = _source_url(attributes.get('href') or '') or ''

    def handle_data(self, data: str) -> None:
        if self.active:
            self.snippet = (self.snippet + ' ' + data)[:1600]
            if self.heading:
                self.title = (self.title + data)[:300]

    def handle_endtag(self, tag: str) -> None:
        if tag == 'h2':
            self.heading = False
        if tag == 'li' and self.active:
            if self.url and self.title.strip() and not any(
                row['url'] == self.url for row in self.results
            ):
                self.results.append({'title': self.title.strip(), 'url': self.url,
                                     'snippet': ' '.join(self.snippet.split())[:1000]})
            self.active = False


class PublicWebClient:
    """Public HTTPS read/search only, explicitly enabled by the operator."""
    def __init__(self, *, proxy: str | None = None, audit: AuditSink | None = None) -> None:
        if proxy:
            parsed = urlsplit(proxy)
            if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
                    or parsed.username or parsed.password or not parsed.port
                    or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
                raise ValueError('Public Web proxy must be literal loopback without credentials')
        self.resolver: HostResolver = PublicDnsResolver(proxy)
        self.transport = PinnedPublicTransport(self.resolver, proxy)
        self.audit = audit
        self._workers = BoundedAdmission(workers=2, pending=4, wait_seconds=5)
        # asyncio.run waits for its default executor even on cancellation. Keep
        # sockets outside per-request HTTP loops so partial replies survive.
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix='xdsh-public-web')
        self._inflight = threading.BoundedSemaphore(6)

    async def fetch(self, url: str, *, html: bool = False) -> WebDocument:
        if contains_sensitive_text(url):
            raise ExternalPolicyError('Public URL contains credential-like content')
        host = urlsplit(url).hostname or ''
        fetcher = SafeWebFetcher(
            UrlPolicy(NetworkPolicy(allowed_hosts=(host,), max_response_bytes=500_000,
                                    max_decompressed_bytes=500_000, timeout_seconds=20),
                      resolver=self.resolver),
            transport=self.transport, audit=self.audit, max_text_chars=100_000,
            preserve_html=html,
        )
        # Thread-safe across gateway event loops; admission and queue time are bounded.
        # A cancelled await must not release the slot while its socket is still active.
        stopped = threading.Event()

        def work() -> WebDocument:
            if not self._workers.acquire():
                raise ExternalBusyError('Public reader busy')
            try:
                if stopped.is_set():
                    raise ExternalBusyError('Public reader request was cancelled before start')
                return asyncio.run(fetcher.fetch(url))
            finally:
                self._workers.release()
        if not self._inflight.acquire(blocking=False):
            raise ExternalBusyError('Public reader queue full')
        try:
            future = self._executor.submit(work)
        except BaseException:
            self._inflight.release()
            raise
        # Real completion (or cancellation before start), not awaiter cancellation.
        future.add_done_callback(lambda _future: self._inflight.release())
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=25)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            stopped.set()
            future.cancel()
            raise

    async def search(self, query: str) -> list[dict[str, str]]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ExternalPolicyError('Search query must have 1-500 characters')
        if contains_sensitive_text(query):
            raise ExternalPolicyError('Search query contains credential-like content')
        document = await self.fetch(
            'https://cn.bing.com/search?' + urlencode({'q': query}), html=True,
        )
        lowered = document.text.lower()
        if any(marker in lowered for marker in ('challenge-form', 'anomaly.js', 'id="b_captcha"')):
            raise PublicSearchUnavailable('Search engine requires human verification; not bypassed')
        parser = SearchResultsParser()
        parser.feed(document.text)
        if not parser.results:
            raise PublicSearchUnavailable('No usable results: blocked, empty, or changed page')
        return parser.results

    async def search_many(self, queries: list[str], *, independent: bool) -> dict[str, object]:
        """Caller orders evidence priority; only explicitly independent queries batch."""
        if independent is not True or not 1 <= len(queries) <= 4:
            raise ExternalPolicyError('Batch requires 1-4 explicitly independent queries')
        if any(not isinstance(query, str) or not 1 <= len(query.strip()) <= 500
               or contains_sensitive_text(query) for query in queries):
            raise ExternalPolicyError('Invalid public search query')
        unique = list(dict.fromkeys(query.strip() for query in queries))
        slots = asyncio.Semaphore(2)

        async def one(query: str) -> dict[str, object]:
            async with slots:
                try:
                    return {'query': query, 'sources': await self.search(query), 'ok': True}
                except (ExternalPolicyError, ExternalProtocolError,
                        ExternalResourceLimitError, TimeoutError, asyncio.TimeoutError) as error:
                    return {'query': query, 'sources': [], 'ok': False,
                            'error': retrieval_failure(error)}

        tasks = [asyncio.create_task(one(query)) for query in unique]
        try:
            _done, pending = await asyncio.wait(tasks, timeout=25)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            results = [
                {'query': query, 'sources': [], 'ok': False,
                 'error': retrieval_failure(TimeoutError())}
                if task in pending else task.result()
                for query, task in zip(unique, tasks, strict=True)
            ]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        quality = assess_search_batch(results)
        return {'results': results, 'complete': all(row['ok'] for row in results),
                'quality': quality,
                'source_warning': 'Untrusted data; syndicated copies are not independent.'}
