"""Env-derivation tests for the SSRF Protection level — no external deps.

Covers the legacy per-path SSRF env vars collapsing into a single
``SSRFProtectionLevel`` so existing deployments keep their behavior.
"""

import pytest

from onyx.server.security import store
from onyx.server.security.models import SSRFProtectionLevel


def _set_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_url_validate_ssrf: bool = True,
    mcp_allow_private_network: bool = False,
    mcp_allow_loopback: bool = False,
    web_connector_validate_urls: str | None = None,
) -> None:
    monkeypatch.setattr(store._cfg, "OPEN_URL_VALIDATE_SSRF", open_url_validate_ssrf)
    monkeypatch.setattr(
        store._cfg, "MCP_SERVER_ALLOW_PRIVATE_NETWORK", mcp_allow_private_network
    )
    monkeypatch.setattr(store._cfg, "MCP_SERVER_ALLOW_LOOPBACK", mcp_allow_loopback)
    monkeypatch.setattr(
        store._cfg, "WEB_CONNECTOR_VALIDATE_URLS", web_connector_validate_urls
    )


def test_all_defaults_derive_validate_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    assert store._derive_ssrf_level_from_env() == SSRFProtectionLevel.VALIDATE_LLM


def test_mcp_private_network_derives_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, mcp_allow_private_network=True)
    assert store._derive_ssrf_level_from_env() == SSRFProtectionLevel.DISABLED


def test_mcp_loopback_alone_derives_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local-MCP operator who set only MCP_SERVER_ALLOW_LOOPBACK keeps loopback
    access — only DISABLED permits it, so it must not collapse to VALIDATE_LLM."""
    _set_env(monkeypatch, mcp_allow_loopback=True)
    assert store._derive_ssrf_level_from_env() == SSRFProtectionLevel.DISABLED


def test_open_url_opt_out_derives_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, open_url_validate_ssrf=False)
    assert store._derive_ssrf_level_from_env() == SSRFProtectionLevel.DISABLED


def test_web_connector_validate_true_derives_validate_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, web_connector_validate_urls="true")
    assert store._derive_ssrf_level_from_env() == SSRFProtectionLevel.VALIDATE_ALL


def test_web_connector_validate_explicit_false_is_not_validate_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var is a free-form string; an explicit "false" must parse as
    falsey rather than enabling VALIDATE_ALL via truthy-string."""
    _set_env(monkeypatch, web_connector_validate_urls="false")
    assert store._derive_ssrf_level_from_env() == SSRFProtectionLevel.VALIDATE_LLM


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("True", True),
        ("  TRUE  ", True),
        ("false", False),
        ("", False),
        (None, False),
        ("1", False),
    ],
)
def test_env_flag(raw: str | None, expected: bool) -> None:
    assert store._env_flag(raw) is expected
