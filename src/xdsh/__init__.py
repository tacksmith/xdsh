"""XDSH product shell powered by DeepSeek Harness."""

from xdsh.version import (
    DSH_COMPATIBLE_VERSION,
    DSH_PYTHON_SDK_VERSION,
    XDSH_RELEASE_TAG,
    XDSH_VERSION,
    XDSH_VERSION_DISPLAY,
)

__version__ = XDSH_VERSION

__all__ = [
    "DSH_COMPATIBLE_VERSION",
    "DSH_PYTHON_SDK_VERSION",
    "XDSH_RELEASE_TAG",
    "XDSH_VERSION_DISPLAY",
    "__version__",
]
