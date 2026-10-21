from __future__ import annotations

from xdsh import (
    DSH_COMPATIBLE_VERSION,
    XDSH_RELEASE_TAG,
    XDSH_VERSION_DISPLAY,
    __version__,
)
from xdsh.version import normalize_release_version, versions_align


def test_active_xdsh_and_dsh_versions_share_the_same_release_line() -> None:
    assert __version__ == "0.1.1rc2"
    assert XDSH_VERSION_DISPLAY == "0.1.1-rc.2"
    assert XDSH_RELEASE_TAG == "xdsh-0.1.1-rc.2"
    assert versions_align(__version__, DSH_COMPATIBLE_VERSION)


def test_release_version_normalization_accepts_pep440_and_npm_spellings() -> None:
    assert normalize_release_version("0.1.1rc2") == "0.1.1rc2"
    assert normalize_release_version("0.1.1-rc.2") == "0.1.1rc2"
    assert not versions_align("0.1.0", DSH_COMPATIBLE_VERSION)
