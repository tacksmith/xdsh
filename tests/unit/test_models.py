from __future__ import annotations

import asyncio
import json
from email.message import Message
from types import TracebackType
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from xdsh.core.models import (
    MODEL_CAPABILITIES,
    FixedModelCatalog,
    LiteLlmExecutionProbe,
    LiteLlmModelCatalog,
    LiteLlmRouteVerifier,
    ModelAvailability,
    ModelCatalogError,
    ModelDirectory,
    ModelProfile,
    ModelProvider,
    ModelRouteHealth,
    ProbedModelCatalog,
    parse_model_provider_aliases,
)


class FakeResponse:
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback

    def read(self, size: int = -1) -> bytes:
        return json.dumps(
            {
                "data": [
                    {
                        "model_name": "z-model",
                        "model_info": {"litellm_provider": "anthropic"},
                    },
                    {
                        "model_name": "a/model",
                        "model_info": {"litellm_provider": "openai"},
                    },
                    {
                        "model_name": "invalid model",
                        "model_info": {"litellm_provider": "openai"},
                    },
                    {
                        "model_name": "*",
                        "model_info": {"litellm_provider": "openai"},
                    },
                ]
            }
        ).encode()


class FakeOpener:
    def __init__(self) -> None:
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float) -> FakeResponse:
        assert timeout == 5.0
        self.requests.append(request)
        return FakeResponse()


async def test_litellm_catalog_is_loopback_only_and_ignores_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeOpener()
    proxy_configurations: list[dict[str, str]] = []

    def fake_proxy_handler(proxies: dict[str, str]) -> object:
        proxy_configurations.append(proxies)
        return object()

    monkeypatch.setenv("XDSH_LITELLM_API_KEY", "unit-test-placeholder")
    monkeypatch.setattr("xdsh.core.models.ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: opener)
    catalog = LiteLlmModelCatalog("http://127.0.0.1:4000")

    directory = await catalog.snapshot()

    assert [(provider.id, provider.models) for provider in directory.providers] == [
        ("anthropic", ("z-model",)),
        ("openai", ("a/model",)),
    ]
    assert directory.models == ("a/model", "z-model")
    anthropic = directory.providers[0]
    assert directory.resolve_provider(anthropic.token) == anthropic
    assert directory.resolve_model(directory.model_token("z-model")) == "z-model"
    assert directory.provider_for("z-model") == anthropic
    assert proxy_configurations == [{}]
    assert opener.requests[0].full_url == "http://127.0.0.1:4000/model/info"
    assert opener.requests[0].headers["Authorization"] == "Bearer unit-test-placeholder"


async def test_litellm_catalog_identifies_hermes_provider_from_route_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {
                            "model_name": "doubao-seed-2.0-pro",
                            "model_info": {"litellm_provider": "anthropic"},
                            "litellm_params": {
                                "api_base": "https://ark.cn-beijing.volces.com/api/coding/v3",
                            },
                        },
                        {
                            "model_name": "qwen-local",
                            "model_info": {"litellm_provider": "openai"},
                            "litellm_params": {"api_base": "http://127.0.0.1:4000/v1"},
                        },
                    ]
                }
            ).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response()

    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())

    directory = await LiteLlmModelCatalog("http://127.0.0.1:4000").snapshot()

    assert [(provider.id, provider.models) for provider in directory.providers] == [
        ("local-llama", ("qwen-local",)),
        ("volc-coding", ("doubao-seed-2.0-pro",)),
    ]


async def test_litellm_catalog_keeps_unclassified_models_and_capability_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {"model_name": "vendor-alias"},
                        {
                            "model_name": "reasoning-model",
                            "model_info": {
                                "litellm_provider": "openai",
                                "supports_reasoning": True,
                                "max_input_tokens": 123_456,
                                "max_output_tokens": 4_096,
                            },
                        },
                    ]
                }
            ).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response()

    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())

    directory = await LiteLlmModelCatalog("http://127.0.0.1:4000").snapshot()

    assert [(provider.id, provider.models) for provider in directory.providers] == [
        ("openai", ("reasoning-model",)),
        ("unclassified", ("vendor-alias",)),
    ]
    profile = directory.profile_for("reasoning-model")
    assert profile.reasoning is True
    assert profile.context_window_tokens == 123_456
    assert profile.max_output_tokens == 4_096


async def test_litellm_catalog_preserves_explicit_multimodal_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability metadata is tri-state and never inferred from a model name."""

    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {
                            "model_name": "capability-model",
                            "model_info": {
                                "litellm_provider": "openai",
                                "supports_reasoning": True,
                                "supports_thinking": False,
                                "supports_function_calling": True,
                                "supports_vision": True,
                                "supports_audio_input": False,
                                "supports_structured_output": True,
                            },
                        },
                        {"model_name": "unknown-model"},
                    ]
                }
            ).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response()

    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())

    directory = await LiteLlmModelCatalog("http://127.0.0.1:4000").snapshot()
    profile = directory.profile_for("capability-model")

    assert MODEL_CAPABILITIES == frozenset(
        {"reasoning", "thinking", "tools", "vision", "audio", "structured_output"}
    )
    assert profile.reasoning is True
    assert profile.thinking is False
    assert profile.tools is True
    assert profile.vision is True
    assert profile.audio is False
    assert profile.structured_output is True
    assert directory.capability_for("capability-model", "thinking") is False
    assert directory.capability_for("unknown-model", "thinking") is None
    assert directory.models_for_capability("vision") == ("capability-model",)


@pytest.mark.parametrize(
    "api_base",
    [
        "https://ark.cn-beijing.volces.com.evil.example/api",
        "https://volces.com/api",
        "https://user:password@ark.cn-beijing.volces.com/api",
        "https://ark.cn-beijing.volces.com:not-a-port/api",
    ],
)
async def test_litellm_catalog_does_not_trust_lookalike_route_hosts(
    monkeypatch: pytest.MonkeyPatch,
    api_base: str,
) -> None:
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {
                            "model_name": "model-a",
                            "model_info": {"litellm_provider": "anthropic"},
                            "litellm_params": {"api_base": api_base},
                        }
                    ]
                }
            ).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response()

    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())

    directory = await LiteLlmModelCatalog("http://127.0.0.1:4000").snapshot()

    assert directory.providers == (ModelProvider("anthropic", ("model-a",)),)


async def test_litellm_catalog_aliases_override_wire_adapter_and_ignore_unknown_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {
                            "model_name": "doubao-seed-2.0-pro",
                            "model_info": {"litellm_provider": "anthropic"},
                        },
                        {
                            "model_name": "qwen-local",
                            "model_info": {"litellm_provider": "openai"},
                        },
                    ]
                }
            ).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response()

    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())

    directory = await LiteLlmModelCatalog(
        "http://127.0.0.1:4000",
        provider_aliases=parse_model_provider_aliases(
            "volc-coding=doubao-seed-2.0-pro,not-live;local-llama=qwen-local"
        ),
    ).snapshot()

    assert [(provider.id, provider.models) for provider in directory.providers] == [
        ("local-llama", ("qwen-local",)),
        ("volc-coding", ("doubao-seed-2.0-pro",)),
    ]


async def test_litellm_catalog_can_show_explicit_routes_missing_from_model_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-declared alias is selectable without being mistaken for healthy."""

    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps({"data": [{"model_name": "live-model"}]}).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response()

    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())

    directory = await LiteLlmModelCatalog(
        "http://127.0.0.1:4000",
        provider_aliases=parse_model_provider_aliases(
            "volc-coding=ark-code-latest,stale-model"
        ),
        configured_model="ark-code-latest",
        include_configured_aliases=True,
    ).snapshot()

    assert directory.models == ("ark-code-latest", "live-model", "stale-model")
    provider = directory.provider_for("ark-code-latest")
    assert provider is not None
    assert provider.id == "volc-coding"
    assert provider.display_label == "🔥 火山引擎 ARK · volc-coding"


async def test_litellm_catalog_can_show_a_configured_model_without_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps({"data": []}).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response()

    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())

    directory = await LiteLlmModelCatalog(
        "http://127.0.0.1:4000",
        configured_model="company-route",
        include_configured_aliases=True,
    ).snapshot()

    assert directory.providers == (
        ModelProvider("configured", ("company-route",), "Configured route", "⚙️"),
    )


async def test_unconfigured_alias_must_not_be_verified_by_a_catch_all_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful local catch-all must not certify a requested online alias."""
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps({"data": [
                {"model_name": "local-model"},
                {"model_name": "*", "litellm_params": {"model": "openai/local-model"}},
            ]}).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            return Response()

    class Probe:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def probe(self, model: str) -> ModelRouteHealth:
            self.calls.append(model)
            return ModelRouteHealth(model, ModelAvailability.AVAILABLE, "execution_ok")

    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())
    catalog = LiteLlmModelCatalog(
        "http://127.0.0.1:4000", configured_model="remote-alias",
        include_configured_aliases=True,
    )
    directory = await catalog.snapshot()
    health = directory.health_for("remote-alias")
    assert health.availability is ModelAvailability.UNAVAILABLE
    assert health.detail == "unconfigured_wildcard_route"
    probe = Probe()
    checked = await ProbedModelCatalog(catalog, probe).snapshot()
    assert checked.health_for("remote-alias") == health
    assert probe.calls == ["local-model"]


async def test_configured_model_already_in_live_routes_is_not_added_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return json.dumps({"data": [{
                "model_name": "deepseek-v4-flash",
                "litellm_params": {"model": "anthropic/deepseek-v4-flash"},
            }]}).encode()

    class Opener:
        def open(self, request: Request, timeout: float) -> Response:
            return Response()

    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: Opener())
    directory = await LiteLlmModelCatalog(
        "http://127.0.0.1:4001", configured_model="deepseek-v4-flash",
        include_configured_aliases=True,
    ).snapshot()
    assert directory.models == ("deepseek-v4-flash",)


async def test_route_verifier_is_cached_and_keeps_known_rejections_during_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    now = [10.0]
    monkeypatch.setenv("XDSH_LITELLM_BASE_URL", "http://127.0.0.1:4000")
    monkeypatch.setattr("xdsh.core.models.time.monotonic", lambda: now[0])

    async def snapshot(self: LiteLlmModelCatalog) -> ModelDirectory:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ModelCatalogError("metadata unavailable")
        names = ("local-model", "*") if calls == 1 else ("local-model", "remote-alias", "*")
        return ModelDirectory(
            (ModelProvider("local", ("local-model",)),), route_names=names,
        )

    monkeypatch.setattr(LiteLlmModelCatalog, "snapshot", snapshot)
    verifier = LiteLlmRouteVerifier(ttl_seconds=30)
    assert await verifier.check("remote-alias") is not None
    assert await verifier.check("local-model") is None
    assert calls == 1
    now[0] = 41
    assert await verifier.check("remote-alias") is not None
    now[0] = 72
    assert await verifier.check("remote-alias") is None
    assert calls == 3


def test_provider_display_labels_keep_stable_ids_and_add_operator_context() -> None:
    assert ModelProvider("volc-coding", ("doubao-seed-2.0-pro",)).display_label == (
        "🔥 火山引擎 ARK · volc-coding"
    )
    assert ModelProvider("local-llama", ("qwen-local",)).display_label == (
        "🖥️ 本地 LLM · local-llama"
    )


def test_model_capability_state_preserves_unknown_and_explicit_negative() -> None:
    directory = ModelDirectory(
        (ModelProvider("provider", ("known", "unknown")),),
        profiles=(ModelProfile("known", tools=False),),
    )

    assert directory.capability_state("known", "tools") == "unsupported"
    assert directory.capability_state("unknown", "tools") == "unknown"
    assert directory.capability_allowed("known", "tools") is False
    assert directory.capability_allowed("unknown", "tools") is True
    assert directory.capability_allowed("unknown", "tools", allow_unknown=False) is False


def test_hermes_style_provider_aliases_group_only_live_models() -> None:
    aliases = parse_model_provider_aliases(
        "volc-coding=doubao-seed-2.0-pro,deepseek-v4-pro;"
        "local-llama=qwen-local"
    )
    assert aliases == (
        ModelProvider("volc-coding", ("doubao-seed-2.0-pro", "deepseek-v4-pro")),
        ModelProvider("local-llama", ("qwen-local",)),
    )


@pytest.mark.parametrize(
    "value",
    [
        "volc-coding=",
        "volc-coding=doubao-seed-2.0-pro;volc-coding=deepseek-v4-pro",
        "volc coding=doubao-seed-2.0-pro",
        "volc-coding=doubao seed",
    ],
)
def test_hermes_style_provider_aliases_fail_closed(value: str) -> None:
    with pytest.raises(ValueError):
        parse_model_provider_aliases(value)


def test_litellm_catalog_rejects_external_or_credentialed_urls() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LiteLlmModelCatalog("https://example.com")
    with pytest.raises(ValueError, match="credential-free"):
        LiteLlmModelCatalog("http://user:password@127.0.0.1:4000")


async def test_execution_health_is_attached_and_cached() -> None:
    class Probe:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def probe(self, model: str) -> ModelRouteHealth:
            self.calls.append(model)
            return ModelRouteHealth(
                model=model,
                availability=(
                    ModelAvailability.UNAVAILABLE
                    if model == "broken-model"
                    else ModelAvailability.AVAILABLE
                ),
                detail="execution_probe",
            )

    probe = Probe()
    catalog = ProbedModelCatalog(
        FixedModelCatalog.from_models("broken-model", "healthy-model"),
        probe,
        ttl_seconds=60.0,
    )

    first = await catalog.snapshot()
    second = await catalog.snapshot()

    assert first.health_for("healthy-model").availability is ModelAvailability.AVAILABLE
    assert first.health_for("broken-model").availability is ModelAvailability.UNAVAILABLE
    assert second.health == first.health
    assert sorted(probe.calls) == ["broken-model", "healthy-model"]


async def test_interactive_catalog_never_generates_hidden_probe_tasks() -> None:
    class ForbiddenProbe:
        calls = 0

        async def probe(self, model: str) -> ModelRouteHealth:
            self.calls += 1
            raise AssertionError("A settings menu must not execute the model")

    probe = ForbiddenProbe()
    catalog = ProbedModelCatalog(
        FixedModelCatalog.from_models("model-one", "model-two"),
        probe, probe_on_snapshot=False,
    )
    directory = await catalog.snapshot()
    assert probe.calls == 0
    assert directory.models == ("model-one", "model-two")
    assert directory.health_for("model-one").availability is ModelAvailability.UNKNOWN


async def test_execution_health_probe_is_bounded_and_marks_deferred_routes() -> None:
    class Probe:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def probe(self, model: str) -> ModelRouteHealth:
            self.calls.append(model)
            return ModelRouteHealth(model, ModelAvailability.AVAILABLE, "ok")

    probe = Probe()
    catalog = ProbedModelCatalog(
        FixedModelCatalog.from_models("model-a", "model-b", "model-c"),
        probe,
        ttl_seconds=60.0,
        max_probe_models=2,
        max_concurrent_probes=1,
    )

    directory = await catalog.snapshot()

    assert len(probe.calls) == 2
    assert directory.health_for("model-c").availability is ModelAvailability.UNKNOWN
    assert directory.health_for("model-c").detail == "probe_deferred"


@pytest.mark.asyncio
async def test_execution_health_probe_does_not_swallow_cancellation() -> None:
    started = asyncio.Event()

    class Probe:
        async def probe(self, model: str) -> ModelRouteHealth:
            del model
            started.set()
            await asyncio.Future[None]()
            raise AssertionError("probe should remain pending")

    catalog = ProbedModelCatalog(
        FixedModelCatalog.from_models("slow-model"),
        Probe(),
        ttl_seconds=60.0,
    )
    task = asyncio.create_task(catalog.snapshot())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_execution_probe_requires_a_real_anthropic_message_and_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            return b'{"type":"message","content":[{"type":"text","text":"OK"}]}'

    class Opener(FakeOpener):
        def open(self, request: Request, timeout: float) -> Response:
            assert timeout == 7.0
            self.requests.append(request)
            return Response()

    opener = Opener()
    proxy_configurations: list[dict[str, str]] = []

    def fake_proxy_handler(proxies: dict[str, str]) -> object:
        proxy_configurations.append(proxies)
        return object()

    monkeypatch.setenv("XDSH_LITELLM_API_KEY", "probe-placeholder")
    monkeypatch.setattr(
        "xdsh.core.models.ProxyHandler",
        fake_proxy_handler,
    )
    monkeypatch.setattr("xdsh.core.models.build_opener", lambda *_handlers: opener)

    health = await LiteLlmExecutionProbe(
        "http://127.0.0.1:4000",
        timeout_seconds=7.0,
    ).probe("model-a")

    assert health.availability is ModelAvailability.AVAILABLE
    assert proxy_configurations == [{}]
    request = opener.requests[0]
    assert request.full_url == "http://127.0.0.1:4000/v1/messages"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {
        "model": "model-a",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply only OK."}],
        "stream": False,
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ModelAvailability.UNAVAILABLE),
        (404, ModelAvailability.UNAVAILABLE),
        (429, ModelAvailability.UNKNOWN),
        (500, ModelAvailability.UNKNOWN),
    ],
)
async def test_execution_probe_does_not_cache_transient_http_failures_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected: ModelAvailability,
) -> None:
    class ErrorOpener:
        def open(self, request: Request, timeout: float) -> FakeResponse:
            del timeout
            raise HTTPError(request.full_url, status, "failure", Message(), None)

    monkeypatch.setenv("XDSH_LITELLM_API_KEY", "probe-placeholder")
    monkeypatch.setattr("xdsh.core.models.ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr(
        "xdsh.core.models.build_opener",
        lambda *_handlers: ErrorOpener(),
    )

    health = await LiteLlmExecutionProbe("http://127.0.0.1:4000").probe("model-a")

    assert health.availability is expected


async def test_execution_probe_bounds_response_and_installs_no_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdsh.core.models import _NoCredentialRedirect

    class LargeResponse(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            assert size == 65_537
            return b"x" * size

    class Opener(FakeOpener):
        def open(self, request: Request, timeout: float) -> LargeResponse:
            return LargeResponse()

    def build(*handlers: object) -> Opener:
        assert any(isinstance(handler, _NoCredentialRedirect) for handler in handlers)
        return Opener()

    monkeypatch.setenv("XDSH_LITELLM_API_KEY", "probe-placeholder")
    monkeypatch.setattr("xdsh.core.models.build_opener", build)
    health = await LiteLlmExecutionProbe("http://127.0.0.1:4000").probe("model-a")
    assert health.availability is ModelAvailability.UNKNOWN
    assert health.detail == "response_too_large"
