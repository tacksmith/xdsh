"""Agent runtime adapters."""

from xdsh.runners.compat import ADAPTER_CONTRACT, DshSdkSurface, installed_sdk_version
from xdsh.runners.dsh import (
    DSH_NPM_VERSION,
    DSH_PYTHON_SDK_VERSION,
    DshRunner,
    DshRunnerConfig,
)
from xdsh.runners.runtime import (
    ALPHA2_DSH_NPM_VERSION,
    LEGACY_DSH_NPM_VERSION,
    VALIDATED_DSH_NPM_VERSIONS,
    DshRuntimeSpec,
    ProfileFiles,
    RuntimeCompatibilityReport,
    RuntimeCompatibilityStatus,
    RuntimeMode,
    classify_runtime_command,
    runtime_compatibility_report,
    validate_packaged_release_runtime,
)

__all__ = [
    "ADAPTER_CONTRACT",
    "ALPHA2_DSH_NPM_VERSION",
    "DSH_NPM_VERSION",
    "DSH_PYTHON_SDK_VERSION",
    "LEGACY_DSH_NPM_VERSION",
    "VALIDATED_DSH_NPM_VERSIONS",
    "DshRunner",
    "DshRunnerConfig",
    "DshRuntimeSpec",
    "DshSdkSurface",
    "ProfileFiles",
    "RuntimeCompatibilityReport",
    "RuntimeCompatibilityStatus",
    "RuntimeMode",
    "classify_runtime_command",
    "installed_sdk_version",
    "runtime_compatibility_report",
    "validate_packaged_release_runtime",
]
