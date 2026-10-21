"""Safe discovery and management of official DSH profile plugins.

The module deliberately treats a DSH profile as the source of truth.  XDSH
never edits a profile manifest or invokes a package manager directly: installs
are delegated to the official ``dsh plugin`` command after an explicit
approval.  This keeps standalone DSH Web/CLI usage and XDSH usage on the same
profile while preserving a clear supply-chain boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, cast

from xdsh.core.product_control import safe_outbound_text

_PROFILE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
_PACKAGE_NAME = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,99}$"
)
_EXACT_SPEC = re.compile(
    r"^(?P<name>(?:@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,99})@"
    r"(?P<version>0|[1-9][0-9]{0,8}\.([0-9]{1,9})\.([0-9]{1,9})"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$"
)
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_OUTPUT_BYTES = 32 * 1024
_MAX_PLUGINS = 2_048
_MAX_PLUGIN_CAPABILITIES = 64
_MAX_PLUGIN_COMMANDS = 64
_MAX_INVOCATION_OUTPUT_CHARS = 32 * 1024
_MAX_GATEWAY_CAPABILITIES = 4_096
_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_.:/-]{0,95}$")
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_.:/-]{0,95}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")

# This is an XDSH-side handshake label, not a claim that RC2 implements the
# method.  An authenticated future DSH Host adapter may advertise it alongside
# exact ``plugin:<package>`` capabilities.  A legacy capability-only adapter is
# retained for backwards compatibility with the first experimental seam.
PLUGIN_INVOCATION_CONTRACT = "xdsh-plugin-invoke-v1"
_LEGACY_GATEWAY_CONTRACT = "capability-only"


class PluginError(ValueError):
    """A DSH plugin profile or operation is invalid."""


class PluginProfileError(PluginError):
    """A DSH profile path is missing, malformed, or escapes DSH_HOME."""


class PluginInstallError(PluginError):
    """An install specification is not safe for unattended forwarding."""


class PluginApprovalRequired(PluginError):
    """The caller must obtain a human approval before package mutation."""


class PluginInvocationUnavailable(PluginError):
    """The selected runtime does not expose a typed direct invocation port."""


class PluginSurface(str, Enum):
    """Best-effort package surface classification for user-facing inventory."""

    TOOL = "tool"
    COMMAND = "command"
    WEB = "web"
    SERVICE = "service"
    UNKNOWN = "unknown"


class PluginInvocationState(str, Enum):
    """Why a discovered plugin can or cannot be called directly."""

    AVAILABLE = "available"
    AGENT_TOOL = "agent_tool"
    UNAVAILABLE = "unavailable"
    NOT_INSTALLED = "not_installed"


class PluginInvocationReason(str, Enum):
    """Stable machine-readable explanation for an invocation state."""

    DIRECT_GATEWAY = "direct_gateway"
    AGENT_TOOL_ONLY = "agent_tool_only"
    DIRECT_UNAVAILABLE = "direct_unavailable"
    GATEWAY_INVALID = "gateway_invalid"
    NOT_INSTALLED = "not_installed"


@dataclass(frozen=True, slots=True)
class DshPlugin:
    """One dependency observed in a DSH profile."""

    package: str
    version: str
    bundle: bool
    surface: PluginSurface
    capabilities: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _PACKAGE_NAME.fullmatch(self.package) is None:
            raise PluginError("plugin package name is invalid")
        if not isinstance(self.version, str) or not self.version or len(self.version) > 128:
            raise PluginError("plugin version is invalid")
        if len(self.capabilities) > _MAX_PLUGIN_CAPABILITIES:
            raise PluginError("plugin declares too many capabilities")
        if len(self.commands) > _MAX_PLUGIN_COMMANDS:
            raise PluginError("plugin declares too many commands")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise PluginError("plugin capabilities must be unique")
        if len(self.commands) != len(set(self.commands)):
            raise PluginError("plugin commands must be unique")
        if any(_CAPABILITY_NAME.fullmatch(value) is None for value in self.capabilities):
            raise PluginError("plugin capability name is invalid")
        if any(_COMMAND_NAME.fullmatch(value) is None for value in self.commands):
            raise PluginError("plugin command name is invalid")


@dataclass(frozen=True, slots=True)
class DshPluginInventory:
    """Point-in-time profile inventory; no credentials or source code included."""

    profile: str
    root: Path
    plugins: tuple[DshPlugin, ...]

    def find(self, package: str) -> DshPlugin | None:
        return next((item for item in self.plugins if item.package == package), None)

    @property
    def bundles(self) -> tuple[DshPlugin, ...]:
        """Return only packages explicitly mounted by the DSH profile."""

        return tuple(item for item in self.plugins if item.bundle)

    def find_capability(self, capability: str) -> tuple[DshPlugin, ...]:
        """Return plugins that explicitly advertise a safe capability name."""

        if _CAPABILITY_NAME.fullmatch(capability) is None:
            return ()
        return tuple(item for item in self.plugins if capability in item.capabilities)


@dataclass(frozen=True, slots=True)
class PluginInstallPlan:
    """A no-side-effect description of one exact package installation."""

    profile: str
    package_name: str
    package_version: str
    package_spec: str
    requires_approval: bool = True


@dataclass(frozen=True, slots=True)
class PluginInstallResult:
    """Bounded, credential-redacted result of the official CLI invocation."""

    plan: PluginInstallPlan
    exit_code: int
    stdout: str
    stderr: str
    command_display: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class PluginInvocationStatus:
    """Credential-free, deterministic direct-invocation discovery result."""

    package: str
    state: PluginInvocationState
    capability: str | None = None
    detail: str = ""
    gateway_contract: str | None = None
    gateway_invalid: bool = False

    def __post_init__(self) -> None:
        if _PACKAGE_NAME.fullmatch(self.package) is None:
            raise PluginError("plugin invocation status package is invalid")
        if not isinstance(self.state, PluginInvocationState):
            raise PluginError("plugin invocation status state is invalid")
        if self.capability is not None and (
            not isinstance(self.capability, str)
            or len(self.capability) > 256
            or any(character in self.capability for character in "\r\n\x00")
        ):
            raise PluginError("plugin invocation capability is invalid")
        if not isinstance(self.detail, str) or len(self.detail) > 256 or any(
            character in self.detail for character in "\r\n\x00"
        ):
            raise PluginError("plugin invocation detail is invalid")
        if self.gateway_contract is not None and (
            not isinstance(self.gateway_contract, str)
            or len(self.gateway_contract) > 64
            or any(character in self.gateway_contract for character in "\r\n\x00")
        ):
            raise PluginError("plugin invocation gateway contract is invalid")
        if not isinstance(self.gateway_invalid, bool) or (
            self.gateway_invalid
            and (
                self.state is not PluginInvocationState.UNAVAILABLE
                or self.gateway_contract is not None
            )
        ):
            raise PluginError("plugin invocation gateway validity is inconsistent")

    @property
    def available(self) -> bool:
        return self.state is PluginInvocationState.AVAILABLE

    @property
    def reason(self) -> PluginInvocationReason:
        """Return a stable code suitable for logs and channel adapters."""

        if self.state is PluginInvocationState.AVAILABLE:
            return PluginInvocationReason.DIRECT_GATEWAY
        if self.state is PluginInvocationState.AGENT_TOOL:
            return PluginInvocationReason.AGENT_TOOL_ONLY
        if self.state is PluginInvocationState.NOT_INSTALLED:
            return PluginInvocationReason.NOT_INSTALLED
        if self.gateway_invalid:
            return PluginInvocationReason.GATEWAY_INVALID
        return PluginInvocationReason.DIRECT_UNAVAILABLE

    @property
    def reason_code(self) -> str:
        """String form kept convenient for JSON and text transports."""

        return self.reason.value

    def as_dict(self) -> dict[str, object]:
        """Return a bounded, credential-free diagnostic projection."""

        result: dict[str, object] = {
            "package": self.package,
            "state": self.state.value,
            "reason_code": self.reason_code,
        }
        if self.capability is not None:
            result["capability"] = self.capability
        if self.gateway_contract is not None:
            result["gateway_contract"] = self.gateway_contract
        if self.detail:
            result["detail"] = safe_outbound_text(self.detail, max_chars=256)
        return result


@dataclass(frozen=True, slots=True)
class PluginInvocationReport:
    """One coherent inventory/route snapshot for an operator or adapter."""

    profile: str
    statuses: tuple[PluginInvocationStatus, ...]
    inventory: DshPluginInventory | None = None

    def __post_init__(self) -> None:
        if _PROFILE_NAME.fullmatch(self.profile) is None:
            raise PluginError("plugin invocation report profile is invalid")
        packages = [status.package for status in self.statuses]
        if len(packages) != len(set(packages)):
            raise PluginError("plugin invocation report contains duplicate packages")
        if self.inventory is not None:
            if self.inventory.profile != self.profile:
                raise PluginError("plugin invocation report inventory profile differs")
            inventory_packages = {plugin.package for plugin in self.inventory.plugins}
            if set(packages) != inventory_packages:
                raise PluginError("plugin invocation report does not match inventory")

    @property
    def counts(self) -> dict[str, int]:
        """Return deterministic state counts without exposing plugin content."""

        counts = {state.value: 0 for state in PluginInvocationState}
        for status in self.statuses:
            counts[status.state.value] += 1
        return counts

    def as_dict(self) -> dict[str, object]:
        """Return a bounded machine-readable snapshot."""

        return {
            "profile": self.profile,
            "counts": self.counts,
            "plugins": [status.as_dict() for status in self.statuses],
        }

    def status_for(self, package: str) -> PluginInvocationStatus | None:
        """Resolve one status without requiring a second inventory read."""

        return next((status for status in self.statuses if status.package == package), None)


class PluginInvocationGateway(Protocol):
    """Optional typed Host bridge supplied by a DSH Web/Remote adapter.

    Implementations may expose a ``contract`` string attribute.  The current
    RC2-compatible bridge can omit it and is treated as the legacy
    capability-only contract; unknown explicit values fail closed.
    """

    @property
    def capabilities(self) -> frozenset[str]: ...

    async def invoke(
        self,
        *,
        plugin: DshPlugin,
        arguments: str,
        session_id: str,
        cwd: Path,
    ) -> str: ...


class DshPluginManager:
    """Inspect profiles and delegate approved mutations to official DSH CLI."""

    def __init__(
        self,
        dsh_home: Path,
        *,
        dsh_command: tuple[str, ...] | None,
        default_profile: str = "xdsh",
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
        command_timeout_seconds: float = 300.0,
    ) -> None:
        self.dsh_home = _safe_home(dsh_home)
        # A read-only manager is useful when the runtime exposes a profile
        # manifest but no safely derivable CLI prefix (notably rc.2). Mutation
        # remains unavailable until an explicit command is configured.
        self.dsh_command = (
            _validate_command(dsh_command) if dsh_command is not None else None
        )
        self.default_profile = _validate_profile(default_profile)
        if not 1_024 <= max_output_bytes <= 1_000_000:
            raise PluginError("plugin command output bound is invalid")
        if not 1.0 <= command_timeout_seconds <= 3_600.0:
            raise PluginError("plugin command timeout is invalid")
        self.max_output_bytes = max_output_bytes
        self.command_timeout_seconds = command_timeout_seconds

    @property
    def can_mutate(self) -> bool:
        """Whether this manager may delegate an approved install/remove."""

        return self.dsh_command is not None

    def profile_root(self, profile: str | None = None) -> Path:
        selected = _validate_profile(profile or self.default_profile)
        profiles_root = self.dsh_home / "profiles"
        if profiles_root.is_symlink() or (profiles_root.exists() and not profiles_root.is_dir()):
            raise PluginProfileError("DSH profiles directory is not a regular directory")
        candidate = profiles_root / selected
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise PluginProfileError("DSH profile directory is not a regular directory")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.dsh_home):
            raise PluginProfileError("DSH profile escapes the XDSH-owned DSH_HOME")
        return resolved

    def profiles(self) -> tuple[str, ...]:
        root = self.dsh_home / "profiles"
        if root.is_symlink():
            raise PluginError("DSH profiles directory is not a regular directory")
        if not root.exists():
            return ()
        if not root.is_dir():
            raise PluginError("DSH profiles directory is not a regular directory")
        names: list[str] = []
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink() and _PROFILE_NAME.fullmatch(child.name):
                names.append(child.name)
        return tuple(sorted(names))

    def inventory(self, profile: str | None = None) -> DshPluginInventory:
        selected = _validate_profile(profile or self.default_profile)
        root = self.profile_root(selected)
        manifest_path = root / "package.json"
        if not manifest_path.exists():
            return DshPluginInventory(selected, root, ())
        document = _read_json(manifest_path)
        dependencies_value = document.get("dependencies", {})
        if not isinstance(dependencies_value, dict):
            raise PluginError("DSH profile dependencies must be an object")
        raw_bundles: object = {}
        dsh_section = document.get("dsh")
        if isinstance(dsh_section, dict):
            profile_section = dsh_section.get("profile")
            if isinstance(profile_section, dict):
                raw_bundles = profile_section.get("bundles", [])
        if not isinstance(raw_bundles, list):
            raise PluginError("DSH profile bundles must be a list")
        bundles: set[str] = set()
        for item in cast(list[object], raw_bundles):
            if not isinstance(item, str) or _PACKAGE_NAME.fullmatch(item) is None:
                raise PluginError("DSH profile contains an invalid bundle package name")
            bundles.add(item)
        plugins: list[DshPlugin] = []
        dependency_items = cast(dict[object, object], dependencies_value)
        for raw_name, raw_version in dependency_items.items():
            if not isinstance(raw_name, str) or _PACKAGE_NAME.fullmatch(raw_name) is None:
                raise PluginError("DSH profile contains an invalid package name")
            if not isinstance(raw_version, str) or not raw_version or len(raw_version) > 128:
                raise PluginError("DSH profile contains an invalid package version")
            metadata = _plugin_metadata(root, raw_name)
            plugins.append(
                DshPlugin(
                    package=raw_name,
                    version=raw_version,
                    bundle=raw_name in bundles,
                    surface=metadata.surface or _surface_for(raw_name),
                    capabilities=metadata.capabilities,
                    commands=metadata.commands,
                )
            )
            if len(plugins) > _MAX_PLUGINS:
                raise PluginError("DSH profile contains too many dependencies")
        # Shipped DSH profiles deliberately keep their in-box bundles out of
        # ``dependencies``. They are still active plugin layers and must be
        # visible to XDSH; use an explicit label instead of inventing semver.
        for package in sorted(bundles.difference(dependency_items)):
            metadata = _plugin_metadata(root, package)
            plugins.append(
                DshPlugin(
                    package=package,
                    version="in-box",
                    bundle=True,
                    surface=metadata.surface or _surface_for(package),
                    capabilities=metadata.capabilities,
                    commands=metadata.commands,
                )
            )
            if len(plugins) > _MAX_PLUGINS:
                raise PluginError("DSH profile contains too many dependencies")
        return DshPluginInventory(
            selected,
            root,
            tuple(sorted(plugins, key=lambda item: item.package)),
        )

    def plan_install(self, package_spec: str, *, profile: str | None = None) -> PluginInstallPlan:
        selected = _validate_profile(profile or self.default_profile)
        name, version = _parse_exact_spec(package_spec)
        current = self.inventory(selected).find(name)
        if current is not None and current.version == version:
            raise PluginInstallError(f"plugin {name}@{version} is already installed")
        return PluginInstallPlan(
            profile=selected,
            package_name=name,
            package_version=version,
            package_spec=f"{name}@{version}",
        )

    def sync_plan(
        self,
        *,
        source_profile: str,
        target_profile: str | None = None,
    ) -> tuple[PluginInstallPlan, ...]:
        """Plan exact bundle copies between independent DSH profiles."""

        source = self.inventory(source_profile)
        target_name = _validate_profile(target_profile or self.default_profile)
        target = self.inventory(target_name)
        target_versions = {item.package: item.version for item in target.plugins}
        plans: list[PluginInstallPlan] = []
        for item in source.plugins:
            if not item.bundle or target_versions.get(item.package) == item.version:
                continue
            if _EXACT_SPEC.fullmatch(f"{item.package}@{item.version}") is None:
                # A range in the source profile is intentionally not copied;
                # the operator can approve an exact version from its lockfile.
                continue
            plans.append(self.plan_install(f"{item.package}@{item.version}", profile=target_name))
        return tuple(plans)

    async def install(
        self,
        plan: PluginInstallPlan,
        *,
        approved: bool = False,
    ) -> PluginInstallResult:
        if not approved:
            raise PluginApprovalRequired("plugin installation requires explicit approval")
        return await self._run(plan, "add")

    def plan_remove(self, package: str, *, profile: str | None = None) -> PluginInstallPlan:
        """Create a no-side-effect removal plan for an installed package."""

        selected = _validate_profile(profile or self.default_profile)
        if _PACKAGE_NAME.fullmatch(package) is None:
            raise PluginInstallError("plugin package name is invalid")
        current = self.inventory(selected).find(package)
        if current is None:
            raise PluginInstallError(f"plugin {package} is not installed")
        return PluginInstallPlan(
            profile=selected,
            package_name=package,
            package_version=current.version,
            package_spec=package,
        )

    async def remove(
        self,
        package: str,
        *,
        profile: str | None = None,
        approved: bool = False,
    ) -> PluginInstallResult:
        if not approved:
            raise PluginApprovalRequired("plugin removal requires explicit approval")
        plan = self.plan_remove(package, profile=profile)
        return await self._run(plan, "remove")

    async def _run(self, plan: PluginInstallPlan, operation: str) -> PluginInstallResult:
        if operation not in {"add", "remove"}:
            raise PluginError("unsupported plugin operation")
        if self.dsh_command is None:
            raise PluginError(
                "目前 runtime 沒有可驗證的 DSH CLI; 僅能讀取 inventory, "
                "請配置 XDSH_DSH_PLUGIN_COMMAND 後再批准變更"
            )
        _validate_profile(plan.profile)
        if _PACKAGE_NAME.fullmatch(plan.package_name) is None:
            raise PluginInstallError("plugin package name is invalid")
        if operation == "add":
            parsed_name, parsed_version = _parse_exact_spec(plan.package_spec)
            if (parsed_name, parsed_version) != (
                plan.package_name,
                plan.package_version,
            ):
                raise PluginInstallError("plugin install plan does not match its exact spec")
        elif plan.package_spec != plan.package_name:
            raise PluginInstallError("plugin removal plan must use a package name")
        self._prepare_home()
        args = (
            *self.dsh_command,
            "plugin",
            "--profile",
            plan.profile,
            operation,
            plan.package_spec,
        )
        environment = os.environ.copy()
        environment["DSH_HOME"] = str(self.dsh_home)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self.dsh_home),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise PluginError("official DSH plugin command could not start") from error
        try:
            stdout_task = asyncio.create_task(_read_bounded(process.stdout, self.max_output_bytes))
            stderr_task = asyncio.create_task(_read_bounded(process.stderr, self.max_output_bytes))
            await asyncio.wait_for(process.wait(), timeout=self.command_timeout_seconds)
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise PluginError("official DSH plugin command timed out") from None
        return PluginInstallResult(
            plan=plan,
            exit_code=process.returncode if process.returncode is not None else 1,
            stdout=_redact(stdout),
            stderr=_redact(stderr),
            command_display=" ".join(_quote_display(item) for item in args),
        )

    def _prepare_home(self) -> None:
        """Create only the XDSH-owned root needed by the official CLI."""

        self.dsh_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.dsh_home.is_symlink() or not self.dsh_home.is_dir():
            raise PluginProfileError("DSH home is not a regular directory")
        self.dsh_home.chmod(0o700)
        profiles = self.dsh_home / "profiles"
        profiles.mkdir(mode=0o700, exist_ok=True)
        if profiles.is_symlink() or not profiles.is_dir():
            raise PluginProfileError("DSH profiles directory is not a regular directory")
        profiles.chmod(0o700)


class PluginControl:
    """Runner-neutral channel facade for inventory, approval, and invocation."""

    def __init__(
        self,
        manager: DshPluginManager,
        *,
        invocation_gateway: PluginInvocationGateway | None = None,
    ) -> None:
        self.manager = manager
        self.invocation_gateway = invocation_gateway

    def inventory(self, profile: str | None = None) -> DshPluginInventory:
        return self.manager.inventory(profile)

    def invocation_report(self, profile: str | None = None) -> PluginInvocationReport:
        """Build one inventory and one gateway handshake for a coherent view."""

        inventory = self.manager.inventory(profile)
        capabilities, contract, gateway_error = self._gateway_surface()
        statuses = tuple(
            self._status_for_plugin(
                plugin,
                capabilities=capabilities,
                gateway_contract=contract,
                gateway_error=gateway_error,
            )
            for plugin in inventory.plugins
        )
        return PluginInvocationReport(inventory.profile, statuses, inventory)

    def invocation_status(
        self,
        package: str,
        *,
        profile: str | None = None,
    ) -> PluginInvocationStatus:
        """Describe the typed invocation seam without attempting execution.

        Exact package capabilities are required.  Wildcards, package metadata
        guesses, and the DSH agent's natural-language tool path never authorize
        a direct channel invocation.
        """

        if _PACKAGE_NAME.fullmatch(package) is None:
            raise PluginInvocationUnavailable("plugin package name is invalid")
        inventory = self.manager.inventory(profile)
        plugin = inventory.find(package)
        if plugin is None:
            return PluginInvocationStatus(
                package=package,
                state=PluginInvocationState.NOT_INSTALLED,
                detail=f"plugin {package} is not installed",
            )
        capabilities, contract, gateway_error = self._gateway_surface()
        return self._status_for_plugin(
            plugin,
            capabilities=capabilities,
            gateway_contract=contract,
            gateway_error=gateway_error,
        )

    def _status_for_plugin(
        self,
        plugin: DshPlugin,
        *,
        capabilities: frozenset[str],
        gateway_contract: str | None,
        gateway_error: bool,
    ) -> PluginInvocationStatus:
        capability = f"plugin:{plugin.package}"
        if capability in capabilities:
            return PluginInvocationStatus(
                package=plugin.package,
                state=PluginInvocationState.AVAILABLE,
                capability=capability,
                gateway_contract=gateway_contract,
                detail="authenticated direct gateway advertised exact package capability",
            )
        if plugin.surface is PluginSurface.TOOL:
            return PluginInvocationStatus(
                package=plugin.package,
                state=PluginInvocationState.AGENT_TOOL,
                capability=capability,
                gateway_contract=gateway_contract,
                detail="tool plugin is callable by the DSH agent tool chain",
            )
        return PluginInvocationStatus(
            package=plugin.package,
            state=PluginInvocationState.UNAVAILABLE,
            capability=capability,
            gateway_contract=gateway_contract,
            gateway_invalid=gateway_error,
            detail=(
                "authenticated direct plugin gateway could not be inspected"
                if gateway_error
                else "no authenticated direct plugin gateway is available"
            ),
        )

    def _gateway_surface(self) -> tuple[frozenset[str], str | None, bool]:
        """Read and validate an optional gateway handshake exactly once."""

        gateway = self.invocation_gateway
        if gateway is None:
            return frozenset(), None, False
        try:
            raw_contract = getattr(gateway, "contract", None)
            contract = _validate_gateway_contract(raw_contract)
            raw_capabilities = gateway.capabilities
            if not isinstance(raw_capabilities, set | frozenset | tuple | list):
                raise TypeError("gateway capabilities are not a finite collection")
            if len(raw_capabilities) > _MAX_GATEWAY_CAPABILITIES:
                raise ValueError("gateway advertises too many capabilities")
            capabilities = frozenset(raw_capabilities)
            if any(
                not isinstance(value, str)
                or len(value) > 256
                or any(character in value for character in "\r\n\x00")
                for value in capabilities
            ):
                raise ValueError("gateway capability contains unsafe text")
            return capabilities, contract, False
        except Exception:
            # Optional integrations are never allowed to widen the route when
            # their handshake is malformed or unavailable.
            return frozenset(), None, True

    async def invoke(
        self,
        package: str,
        *,
        arguments: str,
        session_id: str,
        cwd: Path,
        profile: str | None = None,
    ) -> str:
        if not isinstance(arguments, str) or len(arguments) > 8_000:
            raise PluginInvocationUnavailable("plugin invocation arguments exceed the bound")
        if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
            raise PluginInvocationUnavailable("plugin invocation session ID is invalid")
        try:
            resolved_cwd = cwd.expanduser().resolve()
        except (OSError, RuntimeError) as error:
            raise PluginInvocationUnavailable("plugin invocation workspace is invalid") from error
        if not resolved_cwd.is_dir():
            raise PluginInvocationUnavailable("plugin invocation workspace is unavailable")
        inventory = self.manager.inventory(profile)
        plugin = inventory.find(package)
        if plugin is None:
            raise PluginInvocationUnavailable(f"plugin {package} is not installed")
        capabilities, contract, gateway_error = self._gateway_surface()
        status = self._status_for_plugin(
            plugin,
            capabilities=capabilities,
            gateway_contract=contract,
            gateway_error=gateway_error,
        )
        if not status.available:
            if status.state is PluginInvocationState.AGENT_TOOL:
                raise PluginInvocationUnavailable(
                    "此 tool 由 DSH agent 工具鏈調用; RC2 JSON-RPC 沒有直接 tool-call 方法"
                )
            raise PluginInvocationUnavailable(status.detail)
        gateway = self.invocation_gateway
        if gateway is None:
            # Keep the invariant explicit even if a mutable gateway disappears
            # between discovery and invocation.
            raise PluginInvocationUnavailable("authenticated direct plugin gateway is unavailable")
        try:
            result = await gateway.invoke(
                plugin=plugin,
                arguments=arguments,
                session_id=session_id,
                cwd=resolved_cwd,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise PluginInvocationUnavailable("direct plugin gateway invocation failed") from error
        if not isinstance(result, str):
            raise PluginInvocationUnavailable("direct plugin gateway returned non-text output")
        return safe_outbound_text(result, max_chars=_MAX_INVOCATION_OUTPUT_CHARS)


def _safe_home(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise PluginError("DSH home must not be a symlink")
    resolved = expanded.resolve()
    if (
        resolved == Path(resolved.anchor)
        or resolved == Path.home().expanduser().resolve()
        or ".xclaude" in resolved.parts
        or ".dsh" in resolved.parts
    ):
        raise PluginError("DSH home must be a dedicated XDSH-owned root")
    return resolved


def derive_plugin_command(runtime_command: tuple[str, ...]) -> tuple[str, ...] | None:
    """Derive the official DSH CLI prefix from a profile runtime argv.

    The alpha launcher is commonly either ``dsh --profile sdk`` or
    ``node .../@deepseek-ai/dsh/lib/bin.js --profile sdk``. A legacy
    ``packaged-bin.js`` JSON-RPC demo is intentionally not guessed: callers
    may provide an explicit plugin command when that runtime exposes one.
    """

    if not runtime_command or any(not item or "\x00" in item for item in runtime_command):
        return None
    argv = tuple(runtime_command)
    profile_index: int | None = None
    for index, item in enumerate(argv):
        if item == "--profile" and index + 1 < len(argv):
            profile_index = index
            break
        if item.startswith("--profile=") and len(item) > len("--profile="):
            profile_index = index
            break
    if profile_index is None:
        return None
    prefix = argv[:profile_index]
    if not prefix:
        return None
    normalized = tuple(item.replace("\\", "/") for item in prefix)
    if any(item.endswith("packaged-bin.js") for item in normalized):
        return None
    if prefix[0] in {"dsh", "dsh.cmd", "dsh.exe"}:
        return (prefix[0],)
    if len(prefix) >= 2 and prefix[0].casefold() in {"node", "nodejs"}:
        launcher = normalized[-1]
        if launcher.endswith("/@deepseek-ai/dsh/lib/bin.js") or launcher.endswith(
            "/dsh/lib/bin.js"
        ):
            return prefix[:2]
    if normalized[-1].endswith("/@deepseek-ai/dsh/lib/bin.js"):
        return prefix
    return None


def _validate_profile(value: str) -> str:
    if _PROFILE_NAME.fullmatch(value) is None:
        raise PluginError("DSH profile name is invalid")
    return value


def _validate_gateway_contract(value: object) -> str:
    """Accept the current handshake or the compatibility-era omission."""

    if value is None:
        return _LEGACY_GATEWAY_CONTRACT
    if not isinstance(value, str) or value not in {
        PLUGIN_INVOCATION_CONTRACT,
        _LEGACY_GATEWAY_CONTRACT,
    }:
        raise ValueError("plugin gateway contract is not supported")
    return value


def _validate_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command or any(not item or "\x00" in item for item in command):
        raise PluginError("DSH command argv must be non-empty")
    return command


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PluginError("DSH profile manifest must be a regular file")
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise PluginError("DSH profile manifest is too large")
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PluginError("DSH profile manifest is invalid JSON") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PluginError("DSH profile manifest must be an object")
    return cast(dict[str, object], value)


def _parse_exact_spec(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 256:
        raise PluginInstallError("plugin package spec is too large")
    match = _EXACT_SPEC.fullmatch(value)
    if match is None:
        raise PluginInstallError("plugin installs require an exact npm name@version spec")
    return match.group("name"), match.group("version")


@dataclass(frozen=True, slots=True)
class _PluginMetadata:
    """Safe, declarative metadata read from an installed plugin package."""

    surface: PluginSurface | None = None
    capabilities: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()


def _plugin_metadata(profile_root: Path, package: str) -> _PluginMetadata:
    """Read an installed package's optional DSH declaration, fail closed.

    Only a package's own ``package.json`` is inspected.  No JavaScript is
    imported or executed, and symlinked package trees are ignored so a plugin
    cannot make XDSH read outside the selected DSH profile.
    """

    manifest = _package_manifest_path(profile_root, package)
    if manifest is None:
        return _PluginMetadata()
    try:
        document = _read_json(manifest)
    except PluginError:
        return _PluginMetadata()
    if document.get("name") != package:
        return _PluginMetadata()
    raw_dsh = document.get("dsh")
    if not isinstance(raw_dsh, dict):
        return _PluginMetadata()
    sections: list[dict[str, object]] = [cast(dict[str, object], raw_dsh)]
    nested = raw_dsh.get("plugin")
    if isinstance(nested, dict):
        sections.append(cast(dict[str, object], nested))

    surface: PluginSurface | None = None
    capabilities: list[str] = []
    commands: list[str] = []
    for section in sections:
        raw_surface = section.get("surface", section.get("kind"))
        if surface is None and isinstance(raw_surface, str):
            try:
                surface = PluginSurface(raw_surface.casefold())
            except ValueError:
                pass
        _append_declared_names(
            capabilities,
            section.get("capabilities"),
            pattern=_CAPABILITY_NAME,
            maximum=_MAX_PLUGIN_CAPABILITIES,
        )
        _append_declared_names(
            commands,
            section.get("commands", section.get("command")),
            pattern=_COMMAND_NAME,
            maximum=_MAX_PLUGIN_COMMANDS,
        )
        # Some manifests model tools separately.  Names are still declarative
        # and are deliberately not turned into executable argv here.
        _append_declared_names(
            capabilities,
            section.get("tools"),
            pattern=_CAPABILITY_NAME,
            maximum=_MAX_PLUGIN_CAPABILITIES,
        )
    return _PluginMetadata(
        surface=surface,
        capabilities=tuple(capabilities),
        commands=tuple(commands),
    )


def _append_declared_names(
    output: list[str],
    raw: object,
    *,
    pattern: re.Pattern[str],
    maximum: int,
) -> None:
    """Append bounded string or ``{"name": ...}`` declarations."""

    if isinstance(raw, dict):
        # A capability map may use true values as an explicit declaration;
        # false/unknown values are intentionally omitted.
        values: list[object] = [key for key, value in raw.items() if value is True]
    elif isinstance(raw, list):
        values = cast(list[object], raw)
    elif isinstance(raw, str):
        values = [raw]
    else:
        return
    for value in values:
        if isinstance(value, dict):
            value = value.get("name")
        if (
            not isinstance(value, str)
            or len(output) >= maximum
            or pattern.fullmatch(value) is None
            or value in output
        ):
            continue
        output.append(value)


def _package_manifest_path(profile_root: Path, package: str) -> Path | None:
    """Resolve a package manifest below ``profile_root/node_modules`` only."""

    parts = package.split("/")
    relative = Path(*parts)
    candidate = profile_root / "node_modules" / relative
    try:
        root = profile_root.resolve(strict=True)
        current = profile_root / "node_modules"
        # Check every path component before resolving.  Internal links (as used
        # by some package managers) are acceptable when they stay below the
        # selected profile; links into a global cache or outside the profile
        # are ignored rather than followed.
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                linked = current.resolve(strict=True)
                if not linked.is_relative_to(root):
                    return None
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_dir():
            return None
        manifest = resolved / "package.json"
        if manifest.is_symlink() or not manifest.is_file():
            return None
        return manifest
    except OSError:
        return None


def _surface_for(package: str) -> PluginSurface:
    normalized = package.casefold().split("/", maxsplit=1)[-1]
    if normalized.startswith("dsh-tool-"):
        return PluginSurface.TOOL
    if normalized.startswith("dsh-command-"):
        return PluginSurface.COMMAND
    if normalized.startswith("dsh-client-") or normalized.startswith("dsh-web-"):
        return PluginSurface.WEB
    if normalized.startswith("dsh-"):
        return PluginSurface.SERVICE
    return PluginSurface.UNKNOWN


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    maximum: int,
) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if total < maximum:
            kept = chunk[: maximum - total]
            chunks.append(kept)
            total += len(kept)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return text + ("\n[…output truncated]" if total >= maximum else "")


def _redact(value: str) -> str:
    # Keep command diagnostics useful while avoiding common bearer/key forms.
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", value)
    value = re.sub(r"(?i)\b(?:sk-|key-)[A-Za-z0-9._~+/=-]{8,}", "***", value)
    return value[: _MAX_OUTPUT_BYTES + 64]


def _quote_display(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value) else "<arg>"


__all__ = [
    "PLUGIN_INVOCATION_CONTRACT",
    "DshPlugin",
    "DshPluginInventory",
    "DshPluginManager",
    "PluginApprovalRequired",
    "PluginControl",
    "PluginError",
    "PluginInstallError",
    "PluginInstallPlan",
    "PluginInstallResult",
    "PluginInvocationGateway",
    "PluginInvocationReason",
    "PluginInvocationReport",
    "PluginInvocationState",
    "PluginInvocationStatus",
    "PluginInvocationUnavailable",
    "PluginProfileError",
    "PluginSurface",
    "derive_plugin_command",
]
