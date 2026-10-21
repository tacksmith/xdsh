from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from xdsh.core.plugins import (
    PLUGIN_INVOCATION_CONTRACT,
    DshPluginManager,
    PluginApprovalRequired,
    PluginControl,
    PluginError,
    PluginInstallError,
    PluginInvocationReason,
    PluginInvocationReport,
    PluginInvocationState,
    PluginInvocationStatus,
    PluginInvocationUnavailable,
    PluginProfileError,
    PluginSurface,
    derive_plugin_command,
)


def _profile(root: Path, name: str, *, dependencies: dict[str, str], bundles: list[str]) -> None:
    profile = root / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "name": f"xdsh-{name}",
                "dependencies": dependencies,
                "dsh": {"profile": {"bundles": bundles}},
            }
        ),
        encoding="utf-8",
    )
    (profile / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")


def test_profile_inventory_is_bounded_and_classifies_surfaces(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "web",
        dependencies={
            "@deepseek-ai/dsh-tool-web-search": "0.1.1-rc.2",
            "@acme/dsh-command-calendar": "1.2.3",
            "plain-library": "^2.0.0",
        },
        bundles=["@deepseek-ai/dsh-tool-web-search", "@acme/dsh-command-calendar"],
    )
    manager = DshPluginManager(tmp_path, dsh_command=(sys.executable, "-c", "pass"))

    inventory = manager.inventory("web")

    assert [
        (item.package, item.version, item.surface, item.bundle)
        for item in inventory.plugins
    ] == [
        ("@acme/dsh-command-calendar", "1.2.3", PluginSurface.COMMAND, True),
        ("@deepseek-ai/dsh-tool-web-search", "0.1.1-rc.2", PluginSurface.TOOL, True),
        ("plain-library", "^2.0.0", PluginSurface.UNKNOWN, False),
    ]


def test_inventory_includes_shipped_in_box_bundles_not_listed_as_dependencies(
    tmp_path: Path,
) -> None:
    _profile(
        tmp_path,
        "sdk",
        dependencies={"@acme/dsh-tool-extra": "1.0.0"},
        bundles=["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-sdk-app", "@acme/dsh-tool-extra"],
    )
    manager = DshPluginManager(tmp_path, dsh_command=None, default_profile="sdk")

    inventory = manager.inventory()

    assert [(item.package, item.version, item.bundle) for item in inventory.plugins] == [
        ("@acme/dsh-tool-extra", "1.0.0", True),
        ("@deepseek-ai/dsh-base", "in-box", True),
        ("@deepseek-ai/dsh-sdk-app", "in-box", True),
    ]
    assert [item.package for item in inventory.bundles] == [
        "@acme/dsh-tool-extra",
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-sdk-app",
    ]


def test_inventory_projects_explicit_plugin_capabilities_without_executing_manifest(
    tmp_path: Path,
) -> None:
    _profile(
        tmp_path,
        "sdk",
        dependencies={"@acme/dsh-command-calendar": "1.2.3"},
        bundles=["@acme/dsh-command-calendar"],
    )
    package_root = (
        tmp_path
        / "profiles"
        / "sdk"
        / "node_modules"
        / "@acme"
        / "dsh-command-calendar"
    )
    package_root.mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@acme/dsh-command-calendar",
                "version": "1.2.3",
                "dsh": {
                    "surface": "command",
                    "capabilities": ["calendar.read", "calendar.write", "rm -rf /"],
                    "commands": [
                        {"name": "calendar.list", "description": "List events"},
                        {"name": "calendar.write", "description": "Write events"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    manager = DshPluginManager(tmp_path, dsh_command=None, default_profile="sdk")

    inventory = manager.inventory()
    plugin = inventory.find("@acme/dsh-command-calendar")

    assert plugin is not None
    assert plugin.surface is PluginSurface.COMMAND
    assert plugin.capabilities == ("calendar.read", "calendar.write")
    assert plugin.commands == ("calendar.list", "calendar.write")
    assert inventory.find_capability("calendar.read") == (plugin,)
    assert inventory.find_capability("rm -rf /") == ()


def test_inventory_rejects_malformed_bundle_entries(tmp_path: Path) -> None:
    _profile(tmp_path, "sdk", dependencies={}, bundles=["not a package"])
    manager = DshPluginManager(tmp_path, dsh_command=None, default_profile="sdk")

    with pytest.raises(PluginError, match="bundle package name"):
        manager.inventory()


def test_sync_plan_uses_exact_specs_and_never_invents_stale_plugins(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "web",
        dependencies={"@acme/dsh-tool-a": "1.2.3", "@acme/dsh-tool-b": "^2.0.0"},
        bundles=["@acme/dsh-tool-a", "@acme/dsh-tool-b"],
    )
    _profile(tmp_path, "xdsh", dependencies={}, bundles=[])
    manager = DshPluginManager(tmp_path, dsh_command=(sys.executable, "-c", "pass"))

    plans = manager.sync_plan(source_profile="web", target_profile="xdsh")

    assert [plan.package_spec for plan in plans] == ["@acme/dsh-tool-a@1.2.3"]
    assert all(plan.requires_approval for plan in plans)


def test_install_requires_explicit_approval_and_uses_argv_without_shell(tmp_path: Path) -> None:
    _profile(tmp_path, "xdsh", dependencies={}, bundles=[])
    marker = tmp_path / "argv.json"
    script = (
        "import json,os,sys; "
        f"json.dump(sys.argv[1:],open({str(marker)!r},'w')); "
        "sys.exit(0)"
    )
    manager = DshPluginManager(tmp_path, dsh_command=(sys.executable, "-c", script))
    plan = manager.plan_install("@acme/dsh-tool-a@1.2.3", profile="xdsh")

    with pytest.raises(PluginApprovalRequired):
        asyncio.run(manager.install(plan))
    result = asyncio.run(manager.install(plan, approved=True))

    assert result.exit_code == 0
    assert json.loads(marker.read_text(encoding="utf-8")) == [
        "plugin",
        "--profile",
        "xdsh",
        "add",
        "@acme/dsh-tool-a@1.2.3",
    ]
    assert "@acme/dsh-tool-a@1.2.3" in result.command_display


def test_manager_rejects_unpinned_or_unsafe_package_specs(tmp_path: Path) -> None:
    _profile(tmp_path, "xdsh", dependencies={}, bundles=[])
    manager = DshPluginManager(tmp_path, dsh_command=(sys.executable, "-c", "pass"))
    for value in ("@acme/dsh-tool-a", "@acme/dsh-tool-a@^1.2.3", "--help", "x;y@1.0.0"):
        with pytest.raises(PluginInstallError):
            manager.plan_install(value, profile="xdsh")


def test_profile_manager_is_read_only_without_cli_and_accepts_build_metadata(
    tmp_path: Path,
) -> None:
    _profile(tmp_path, "sdk", dependencies={}, bundles=[])
    manager = DshPluginManager(tmp_path, dsh_command=None, default_profile="sdk")

    assert manager.can_mutate is False
    assert manager.inventory().profile == "sdk"
    plan = manager.plan_install("@acme/dsh-tool-a@1.2.3+linux.1", profile="sdk")
    assert plan.package_version == "1.2.3+linux.1"
    with pytest.raises(PluginError):
        asyncio.run(manager.install(plan, approved=True))


def test_invocation_status_is_explicit_and_fail_closed_without_gateway(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "xdsh",
        dependencies={
            "@acme/dsh-tool-calendar": "1.0.0",
            "@acme/dsh-command-calendar": "1.0.0",
        },
        bundles=[],
    )
    manager = DshPluginManager(tmp_path, dsh_command=None)
    control = PluginControl(manager)

    tool_status = control.invocation_status("@acme/dsh-tool-calendar")
    command_status = control.invocation_status("@acme/dsh-command-calendar")
    missing_status = control.invocation_status("@acme/dsh-tool-missing")

    assert tool_status.state is PluginInvocationState.AGENT_TOOL
    assert tool_status.available is False
    assert command_status.state is PluginInvocationState.UNAVAILABLE
    assert missing_status.state is PluginInvocationState.NOT_INSTALLED


def test_invocation_status_requires_exact_gateway_capability(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "xdsh",
        dependencies={"@acme/dsh-command-calendar": "1.0.0"},
        bundles=[],
    )

    class Gateway:
        @property
        def capabilities(self) -> frozenset[str]:
            return frozenset({"plugin:@acme/dsh-command-other"})

        async def invoke(
            self,
            *,
            plugin: object,
            arguments: str,
            session_id: str,
            cwd: Path,
        ) -> str:
            del plugin, arguments, session_id, cwd
            return "never"

    control = PluginControl(
        DshPluginManager(tmp_path, dsh_command=None),
        invocation_gateway=Gateway(),
    )

    status = control.invocation_status("@acme/dsh-command-calendar")

    assert status.state is PluginInvocationState.UNAVAILABLE
    assert status.capability == "plugin:@acme/dsh-command-calendar"


def test_invocation_report_is_one_coherent_machine_readable_snapshot(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "xdsh",
        dependencies={
            "@acme/dsh-tool-calendar": "1.0.0",
            "@acme/dsh-command-calendar": "1.0.0",
        },
        bundles=[],
    )

    class Gateway:
        contract = PLUGIN_INVOCATION_CONTRACT

        @property
        def capabilities(self) -> frozenset[str]:
            return frozenset({"plugin:@acme/dsh-command-calendar"})

        async def invoke(
            self,
            *,
            plugin: object,
            arguments: str,
            session_id: str,
            cwd: Path,
        ) -> str:
            del plugin, arguments, session_id, cwd
            return "ok"

    report = PluginControl(
        DshPluginManager(tmp_path, dsh_command=None),
        invocation_gateway=Gateway(),
    ).invocation_report()

    assert isinstance(report, PluginInvocationReport)
    assert report.inventory is not None
    assert report.counts == {
        "available": 1,
        "agent_tool": 1,
        "unavailable": 0,
        "not_installed": 0,
    }
    direct = report.status_for("@acme/dsh-command-calendar")
    assert direct is not None
    assert direct.reason is PluginInvocationReason.DIRECT_GATEWAY
    assert direct.gateway_contract == PLUGIN_INVOCATION_CONTRACT
    assert direct.as_dict()["reason_code"] == "direct_gateway"


def test_unknown_gateway_contract_fails_closed_and_is_diagnosable(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "xdsh",
        dependencies={"@acme/dsh-command-calendar": "1.0.0"},
        bundles=[],
    )

    class Gateway:
        contract = "future-unknown-contract"

        @property
        def capabilities(self) -> frozenset[str]:
            return frozenset({"plugin:@acme/dsh-command-calendar"})

        async def invoke(
            self,
            *,
            plugin: object,
            arguments: str,
            session_id: str,
            cwd: Path,
        ) -> str:
            del plugin, arguments, session_id, cwd
            return "must not run"

    status = PluginControl(
        DshPluginManager(tmp_path, dsh_command=None),
        invocation_gateway=Gateway(),
    ).invocation_status("@acme/dsh-command-calendar")

    assert status.state is PluginInvocationState.UNAVAILABLE
    assert status.reason is PluginInvocationReason.GATEWAY_INVALID
    assert status.as_dict()["reason_code"] == "gateway_invalid"


def test_gateway_invalid_reason_does_not_depend_on_human_detail() -> None:
    status = PluginInvocationStatus(
        package="command-plugin",
        state=PluginInvocationState.UNAVAILABLE,
        detail="localized diagnostic text",
        gateway_invalid=True,
    )

    assert status.reason is PluginInvocationReason.GATEWAY_INVALID
    with pytest.raises(PluginError, match="validity"):
        PluginInvocationStatus(
            package="command-plugin",
            state=PluginInvocationState.AVAILABLE,
            gateway_invalid=True,
        )


def test_direct_invocation_redacts_and_bounds_gateway_output(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "xdsh",
        dependencies={"@acme/dsh-command-calendar": "1.0.0"},
        bundles=[],
    )

    class Gateway:
        @property
        def capabilities(self) -> frozenset[str]:
            return frozenset({"plugin:@acme/dsh-command-calendar"})

        async def invoke(
            self,
            *,
            plugin: object,
            arguments: str,
            session_id: str,
            cwd: Path,
        ) -> str:
            del plugin, arguments, session_id, cwd
            return "TOKEN=super-secret-value\n" + ("x" * 40_000)

    control = PluginControl(
        DshPluginManager(tmp_path, dsh_command=None),
        invocation_gateway=Gateway(),
    )
    result = asyncio.run(
        control.invoke(
            "@acme/dsh-command-calendar",
            arguments="today",
            session_id="session-a",
            cwd=tmp_path,
        )
    )

    assert len(result) <= 32 * 1024
    assert "super-secret-value" not in result
    assert "[REDACTED]" in result


def test_direct_invocation_gateway_errors_are_bounded(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "xdsh",
        dependencies={"@acme/dsh-command-calendar": "1.0.0"},
        bundles=[],
    )

    class Gateway:
        @property
        def capabilities(self) -> frozenset[str]:
            return frozenset({"plugin:@acme/dsh-command-calendar"})

        async def invoke(
            self,
            *,
            plugin: object,
            arguments: str,
            session_id: str,
            cwd: Path,
        ) -> str:
            del plugin, arguments, session_id, cwd
            raise RuntimeError("provider TOKEN=secret-value body")

    control = PluginControl(
        DshPluginManager(tmp_path, dsh_command=None),
        invocation_gateway=Gateway(),
    )
    with pytest.raises(PluginInvocationUnavailable, match="gateway invocation failed"):
        asyncio.run(
            control.invoke(
                "@acme/dsh-command-calendar",
                arguments="today",
                session_id="session-a",
                cwd=tmp_path,
            )
        )


def test_profile_path_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "sdk").symlink_to(outside, target_is_directory=True)
    manager = DshPluginManager(tmp_path, dsh_command=None, default_profile="sdk")

    with pytest.raises(PluginProfileError):
        manager.inventory()


def test_plugin_command_derivation_only_accepts_profile_launchers() -> None:
    assert derive_plugin_command(("dsh", "--profile", "sdk")) == ("dsh",)
    assert derive_plugin_command(
        ("node", "/opt/node_modules/@deepseek-ai/dsh/lib/bin.js", "--profile", "sdk")
    ) == ("node", "/opt/node_modules/@deepseek-ai/dsh/lib/bin.js")
    assert derive_plugin_command(("node", "packaged-bin.js", "--profile", "sdk")) is None
