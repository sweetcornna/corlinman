"""Tests for :mod:`corlinman_server.system.marketplace.accel`.

Exhaustive rewrite table for :class:`GithubAccelerator`:

* ``mode`` off / on / auto enablement (incl. ``assume_region`` config and
  the ``TZ`` / ``CORLINMAN_REGION`` env signals for ``auto``).
* Per-preset rewrites: ``ghproxy`` (prefix), ``jsdelivr`` (raw → cdn map,
  non-raw passthrough), ``mirror`` (host swap, empty host no-op), and
  ``custom`` (prefix).
* Non-GitHub URLs are a no-op under every preset.
* :meth:`GithubAccelerator.is_trusted_host` trusts GitHub + the configured
  mirror host, distrusts public-proxy rewrites.

The accelerator is pure (no I/O, no clock), so every case is a direct
string assertion.
"""

from __future__ import annotations

import pytest
from corlinman_server.system.marketplace.accel import (
    AccelSettings,
    GithubAccelerator,
)

RAW = "https://raw.githubusercontent.com/o/r/main/skills/a.tar.gz"
GH = "https://github.com/o/r/releases/download/v1/asset.bin"
CODELOAD = "https://codeload.github.com/o/r/tar.gz/main"
NON_GH = "https://example.com/o/r/main/file.txt"


# ---------------------------------------------------------------------------
# Enablement — mode off / on / auto
# ---------------------------------------------------------------------------


def test_mode_off_disabled() -> None:
    accel = GithubAccelerator(AccelSettings(mode="off"))
    assert accel.enabled is False
    # ... and accelerate is a no-op while disabled.
    assert accel.accelerate(RAW) == RAW


def test_mode_on_enabled() -> None:
    accel = GithubAccelerator(AccelSettings(mode="on", preset="ghproxy"))
    assert accel.enabled is True


def test_mode_defaults_to_off() -> None:
    # A bare accelerator (no settings) is off by default.
    accel = GithubAccelerator()
    assert accel.enabled is False
    assert accel.accelerate(RAW) == RAW


def test_mode_is_case_insensitive() -> None:
    assert GithubAccelerator(AccelSettings(mode="ON")).enabled is True
    assert GithubAccelerator(AccelSettings(mode="OFF")).enabled is False


# ---------------------------------------------------------------------------
# Enablement — auto detection
# ---------------------------------------------------------------------------


def test_auto_assume_region_cn_enables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear env signals so only the config field decides.
    monkeypatch.delenv("CORLINMAN_REGION", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    accel = GithubAccelerator(AccelSettings(mode="auto", assume_region="cn"))
    assert accel.enabled is True


def test_auto_assume_region_global_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``global`` wins even when the env would otherwise signal CN.
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    accel = GithubAccelerator(
        AccelSettings(mode="auto", assume_region="global")
    )
    assert accel.enabled is False


def test_auto_env_region_cn_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_REGION", "cn")
    monkeypatch.delenv("TZ", raising=False)
    accel = GithubAccelerator(AccelSettings(mode="auto"))
    assert accel.enabled is True


def test_auto_env_region_global_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORLINMAN_REGION", "global")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    accel = GithubAccelerator(AccelSettings(mode="auto"))
    assert accel.enabled is False


def test_auto_tz_asia_shanghai_enables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_REGION", raising=False)
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    accel = GithubAccelerator(AccelSettings(mode="auto"))
    assert accel.enabled is True


def test_auto_tz_non_cn_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_REGION", raising=False)
    monkeypatch.setenv("TZ", "America/New_York")
    accel = GithubAccelerator(AccelSettings(mode="auto"))
    assert accel.enabled is False


def test_auto_no_signal_defaults_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing matches → never silently route through a third-party mirror.
    monkeypatch.delenv("CORLINMAN_REGION", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    accel = GithubAccelerator(AccelSettings(mode="auto"))
    assert accel.enabled is False


# ---------------------------------------------------------------------------
# Preset: ghproxy (prefix style)
# ---------------------------------------------------------------------------


def test_ghproxy_prefixes_base_and_full_url() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="ghproxy", base="https://ghproxy.com/")
    )
    assert accel.accelerate(RAW) == "https://ghproxy.com/" + RAW


def test_ghproxy_strips_trailing_slash_then_joins() -> None:
    # A base with extra trailing slashes still yields exactly one separator.
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="ghproxy", base="https://ghproxy.com///")
    )
    assert accel.accelerate(GH) == "https://ghproxy.com/" + GH


def test_ghproxy_empty_base_is_noop() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="ghproxy", base="   ")
    )
    assert accel.accelerate(RAW) == RAW


def test_ghproxy_applies_to_codeload_and_releases() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="ghproxy", base="https://gh.io/")
    )
    assert accel.accelerate(CODELOAD) == "https://gh.io/" + CODELOAD
    assert accel.accelerate(GH) == "https://gh.io/" + GH


# ---------------------------------------------------------------------------
# Preset: jsdelivr (raw → cdn map; non-raw passthrough)
# ---------------------------------------------------------------------------


def test_jsdelivr_maps_raw_to_cdn() -> None:
    accel = GithubAccelerator(AccelSettings(mode="on", preset="jsdelivr"))
    assert (
        accel.accelerate(RAW)
        == "https://cdn.jsdelivr.net/gh/o/r@main/skills/a.tar.gz"
    )


def test_jsdelivr_maps_nested_path() -> None:
    accel = GithubAccelerator(AccelSettings(mode="on", preset="jsdelivr"))
    url = "https://raw.githubusercontent.com/owner/repo/v2.1/a/b/c.json"
    assert (
        accel.accelerate(url)
        == "https://cdn.jsdelivr.net/gh/owner/repo@v2.1/a/b/c.json"
    )


def test_jsdelivr_passes_through_non_raw_github_url() -> None:
    # A non-raw GitHub url (release asset, API, codeload) is unchanged —
    # jsdelivr only fronts raw repo content.
    accel = GithubAccelerator(AccelSettings(mode="on", preset="jsdelivr"))
    assert accel.accelerate(GH) == GH
    assert accel.accelerate(CODELOAD) == CODELOAD


def test_jsdelivr_passes_through_short_raw_path() -> None:
    # Fewer than 4 path segments can't be mapped — returned unchanged.
    accel = GithubAccelerator(AccelSettings(mode="on", preset="jsdelivr"))
    short = "https://raw.githubusercontent.com/o/r/main"
    assert accel.accelerate(short) == short


# ---------------------------------------------------------------------------
# Preset: mirror (host swap; empty host no-op)
# ---------------------------------------------------------------------------


def test_mirror_swaps_host_preserving_path() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="mirror", mirror_host="gh.mycorp.cn")
    )
    assert (
        accel.accelerate(RAW)
        == "https://gh.mycorp.cn/o/r/main/skills/a.tar.gz"
    )


def test_mirror_accepts_scheme_prefixed_host() -> None:
    accel = GithubAccelerator(
        AccelSettings(
            mode="on", preset="mirror", mirror_host="https://gh.mycorp.cn/"
        )
    )
    assert (
        accel.accelerate(GH)
        == "https://gh.mycorp.cn/o/r/releases/download/v1/asset.bin"
    )


def test_mirror_empty_host_is_noop() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="mirror", mirror_host="   ")
    )
    assert accel.accelerate(RAW) == RAW


# ---------------------------------------------------------------------------
# Preset: custom (prefix style, same machinery as ghproxy)
# ---------------------------------------------------------------------------


def test_custom_prefixes_base() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="custom", base="https://my.proxy/")
    )
    assert accel.accelerate(RAW) == "https://my.proxy/" + RAW


def test_custom_empty_base_is_noop() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="custom", base="")
    )
    assert accel.accelerate(RAW) == RAW


# ---------------------------------------------------------------------------
# Non-GitHub URL is a no-op under every preset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["ghproxy", "jsdelivr", "mirror", "custom"])
def test_non_github_url_is_noop(preset: str) -> None:
    accel = GithubAccelerator(
        AccelSettings(
            mode="on",
            preset=preset,
            base="https://ghproxy.com/",
            mirror_host="gh.mycorp.cn",
        )
    )
    assert accel.accelerate(NON_GH) == NON_GH


def test_unknown_preset_is_noop() -> None:
    accel = GithubAccelerator(AccelSettings(mode="on", preset="bogus"))
    assert accel.accelerate(RAW) == RAW


# ---------------------------------------------------------------------------
# is_trusted_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r",
        "https://raw.githubusercontent.com/o/r/main/x",
        "https://codeload.github.com/o/r/tar.gz/main",
        "https://objects.githubusercontent.com/blob",
        "https://api.github.com/repos/o/r",
    ],
)
def test_is_trusted_host_true_for_github_hosts(url: str) -> None:
    accel = GithubAccelerator(AccelSettings(mode="on", preset="ghproxy"))
    assert accel.is_trusted_host(url) is True


def test_is_trusted_host_true_for_matching_mirror() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="mirror", mirror_host="gh.mycorp.cn")
    )
    rewritten = accel.accelerate(RAW)
    assert rewritten == "https://gh.mycorp.cn/o/r/main/skills/a.tar.gz"
    assert accel.is_trusted_host(rewritten) is True


def test_is_trusted_host_false_for_ghproxy_rewrite() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="ghproxy", base="https://ghproxy.com/")
    )
    # The bare ghproxy host (not a github host) is not trusted.
    assert accel.is_trusted_host("https://ghproxy.com/anything") is False


def test_is_trusted_host_false_for_jsdelivr_rewrite() -> None:
    accel = GithubAccelerator(AccelSettings(mode="on", preset="jsdelivr"))
    rewritten = accel.accelerate(RAW)
    assert rewritten.startswith("https://cdn.jsdelivr.net/")
    assert accel.is_trusted_host(rewritten) is False


def test_is_trusted_host_false_for_non_matching_mirror_host() -> None:
    accel = GithubAccelerator(
        AccelSettings(mode="on", preset="mirror", mirror_host="gh.mycorp.cn")
    )
    assert accel.is_trusted_host("https://other.proxy/o/r") is False
