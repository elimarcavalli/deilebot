"""Testes do forge_auth — ABC + GitHub/GitLab concretes.

Cobre AC-1..AC-4, AC-6, AC-18 (timeout + erro tipado), AC-20 (log estruturado).
Sem rede real; HTTP mockado via monkeypatch. Filesystem usa tmp_path como HOME.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from deilebot.foundation.forge_auth import (
    ForgeAuthError,
    ForgeKind,
    ForgeTimeoutError,
    GitHubForgeAuth,
    GitLabForgeAuth,
    Identity,
    classify_token,
    get_forge_auth,
)


# ── AC-1: classify_token (table-driven, ≥9 prefixos + 1 desconhecido) ───────

@pytest.mark.parametrize("token,expected_forge,expected_label_substr", [
    # GitHub
    ("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxx", "github", "clássico"),
    ("github_pat_abcdefghijklmno", "github", "fine-grained"),
    ("gho_abcdefghijklmnop", "github", "OAuth"),
    ("ghs_abcdefghijklmnop", "github", "server-to-server"),
    ("ghr_abcdefghijklmnop", "github", "refresh"),
    # GitLab
    ("glpat-abcdefghijklmno", "gitlab", "PAT"),
    ("gldt-abcdefghijklmnop", "gitlab", "deploy"),
    ("glptt-abcdefghijklmno", "gitlab", "pipeline trigger"),
    ("glsoat-abcdefghijklmn", "gitlab", "service account"),
    # Desconhecido
    ("Bearer xxxxxxxxxxxxxxx", "unknown", "token"),
])
def test_classify_token_ac1(token, expected_forge, expected_label_substr):
    forge, label = classify_token(token)
    assert forge == expected_forge, f"token={token!r}: esperado forge={expected_forge!r}, obtido {forge!r}"
    assert expected_label_substr.lower() in label.lower(), f"label={label!r} não contém {expected_label_substr!r}"


# ── AC-2: GitLabForgeAuth.validate_token (HTTP mockado) ─────────────────────

async def test_gitlab_validate_token_success(monkeypatch, tmp_path):
    svc = GitLabForgeAuth(home=tmp_path, host="gitlab.com")

    async def fake_req(method, url, *, headers, data=None):
        assert method == "GET"
        assert "api/v4/user" in url
        assert "gitlab.com" in url
        return 200, {"username": "gituser", "name": "Git User"}, {}

    monkeypatch.setattr(svc, "_request", fake_req)
    identity = await svc.validate_token("glpat-validtokenxxx")
    assert identity.login == "gituser"
    assert identity.forge == "gitlab"


async def test_gitlab_validate_token_self_hosted(monkeypatch, tmp_path):
    svc = GitLabForgeAuth(home=tmp_path, host="gitlab.mycompany.com")

    async def fake_req(method, url, *, headers, data=None):
        assert "gitlab.mycompany.com" in url, f"URL errada: {url}"
        return 200, {"username": "devuser", "name": "Dev"}, {}

    monkeypatch.setattr(svc, "_request", fake_req)
    identity = await svc.validate_token("glpat-selfhostedtoken")
    assert identity.login == "devuser"


async def test_gitlab_validate_token_401(monkeypatch, tmp_path):
    svc = GitLabForgeAuth(home=tmp_path)

    async def fake_req(method, url, *, headers, data=None):
        return 401, {}, {}

    monkeypatch.setattr(svc, "_request", fake_req)
    with pytest.raises(ForgeAuthError, match="401"):
        await svc.validate_token("glpat-invalid")


# ── AC-3: _is_my_line isolamento por host ───────────────────────────────────

def test_github_is_my_line():
    svc = GitHubForgeAuth()
    assert svc._is_my_line("https://oauth2:tok@github.com") is True
    assert svc._is_my_line("https://oauth2:tok@gitlab.com") is False
    assert svc._is_my_line("") is False


def test_gitlab_is_my_line():
    svc = GitLabForgeAuth()
    assert svc._is_my_line("https://oauth2:tok@gitlab.com") is True
    assert svc._is_my_line("https://oauth2:tok@github.com") is False


def test_install_gitlab_preserves_github_line(tmp_path):
    """Instalar token GitLab preserva a linha GitHub existente e vice-versa."""
    # Escrever linha GitHub manualmente.
    creds = tmp_path / ".git-credentials"
    creds.write_text("https://oauth2:ghp_OLD@github.com\n")

    svc_gl = GitLabForgeAuth(home=tmp_path)
    svc_gl._write_credential("glpat-NEWTOKEN")

    lines = creds.read_text().splitlines()
    assert any("github.com" in ln for ln in lines), "Linha GitHub foi apagada!"
    assert any("gitlab.com" in ln for ln in lines), "Linha GitLab não foi escrita!"


def test_install_github_preserves_gitlab_line(tmp_path):
    """Instalar token GitHub preserva a linha GitLab existente."""
    creds = tmp_path / ".git-credentials"
    creds.write_text("https://oauth2:glpat-OLD@gitlab.com\n")

    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gh._write_credential("ghp_NEWTOKEN")

    lines = creds.read_text().splitlines()
    assert any("gitlab.com" in ln for ln in lines), "Linha GitLab foi apagada!"
    assert any("github.com" in ln for ln in lines), "Linha GitHub não foi escrita!"


# ── AC-4: metadados multi-forge + migração legado ───────────────────────────

def test_metadata_multi_forge_independent(tmp_path):
    """stored_metadata() de github e gitlab são independentes."""
    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gl = GitLabForgeAuth(home=tmp_path)

    id_gh = Identity(login="octocat", forge="github", token_type="PAT clássico")
    id_gl = Identity(login="gitcat", forge="gitlab", token_type="PAT pessoal")

    svc_gh._write_metadata(id_gh, method="pat")
    svc_gl._write_metadata(id_gl, method="pat")

    meta_gh = svc_gh.stored_metadata()
    meta_gl = svc_gl.stored_metadata()

    assert meta_gh is not None
    assert meta_gl is not None
    assert meta_gh["login"] == "octocat"
    assert meta_gl["login"] == "gitcat"


def test_metadata_migration_from_legacy(tmp_path):
    """Se só o legado ~/.deile/github_auth.json existe, é lido como github e renomeado .bak."""
    legacy_path = tmp_path / ".deile" / "github_auth.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps({"login": "legacyuser", "method": "pat"}))

    svc = GitHubForgeAuth(home=tmp_path)
    meta = svc.stored_metadata()

    assert meta is not None
    assert meta["login"] == "legacyuser"

    # Legado deve ter sido renomeado para .bak
    assert legacy_path.with_suffix(".json.bak").exists(), "Legado não foi renomeado para .bak"
    assert not legacy_path.exists(), "Legado não foi removido"


# ── AC-6: validação ANTES de gravar ─────────────────────────────────────────

async def test_invalid_token_not_written_to_credentials(monkeypatch, tmp_path):
    """Token inválido (401) não deve ser escrito em ~/.git-credentials."""
    svc = GitLabForgeAuth(home=tmp_path)

    async def fake_req(method, url, *, headers, data=None):
        return 401, {}, {}

    monkeypatch.setattr(svc, "_request", fake_req)

    with pytest.raises(ForgeAuthError):
        await svc.install_credentials("glpat-INVALIDO", method="pat")

    creds = tmp_path / ".git-credentials"
    assert not creds.exists(), "Credencial foi escrita apesar de token inválido!"


# ── AC-18: timeout configurável → ForgeTimeoutError tipado ──────────────────

async def test_request_converts_asyncio_timeout_to_forge_timeout_error(monkeypatch, tmp_path):
    """_request converte asyncio.TimeoutError → ForgeTimeoutError tipado."""
    import unittest.mock as mock

    svc = GitLabForgeAuth(home=tmp_path, timeout=5.0)

    # Patch aiohttp.ClientSession to raise asyncio.TimeoutError
    timeout_exc = asyncio.TimeoutError()
    mock_session = mock.AsyncMock()
    mock_session.__aenter__ = mock.AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = mock.AsyncMock(return_value=False)
    mock_session.request = mock.MagicMock(side_effect=timeout_exc)

    with mock.patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(ForgeTimeoutError, match="5s"):
            await svc._request("GET", "https://gitlab.com/api/v4/user", headers={})


async def test_timeout_raises_typed_error(monkeypatch, tmp_path):
    """validate_token propagates ForgeTimeoutError from _request (sem traceback cru)."""
    svc = GitLabForgeAuth(home=tmp_path, timeout=5.0)

    async def fake_req(method, url, *, headers, data=None):
        raise ForgeTimeoutError(f"GitLab não respondeu em {svc._timeout:.0f}s")

    monkeypatch.setattr(svc, "_request", fake_req)

    with pytest.raises(ForgeTimeoutError, match="5s"):
        await svc.validate_token("glpat-qualquer")


async def test_github_timeout_raises_typed_error(monkeypatch, tmp_path):
    svc = GitHubForgeAuth(home=tmp_path, timeout=10.0)

    async def fake_req(method, url, *, headers, data=None):
        raise ForgeTimeoutError(f"GitHub não respondeu em {svc._timeout:.0f}s")

    monkeypatch.setattr(svc, "_request", fake_req)

    with pytest.raises(ForgeTimeoutError):
        await svc.validate_token("ghp_qualquer")


# ── AC-20: log estruturado em falha de auth ──────────────────────────────────

async def test_log_on_gitlab_401(monkeypatch, tmp_path, caplog):
    svc = GitLabForgeAuth(home=tmp_path)

    async def fake_req(method, url, *, headers, data=None):
        return 401, {}, {}

    monkeypatch.setattr(svc, "_request", fake_req)

    with caplog.at_level(logging.WARNING, logger="deilebot.forge_auth"):
        with pytest.raises(ForgeAuthError):
            await svc.validate_token("glpat-INVALIDO")

    assert any("401" in r.message or "inválido" in r.message for r in caplog.records), \
        "Log estruturado não emitido para 401 GitLab"


async def test_log_on_timeout(monkeypatch, tmp_path, caplog):
    """_request emite log estruturado para timeout antes de levantar ForgeTimeoutError."""
    import unittest.mock as mock

    svc = GitLabForgeAuth(home=tmp_path, timeout=3.0)

    # Patch aiohttp.ClientSession to raise asyncio.TimeoutError
    mock_cm = mock.MagicMock()
    mock_cm.__aenter__ = mock.AsyncMock(side_effect=asyncio.TimeoutError())
    mock_cm.__aexit__ = mock.AsyncMock(return_value=False)

    with mock.patch("aiohttp.ClientSession", return_value=mock_cm):
        with caplog.at_level(logging.WARNING, logger="deilebot.forge_auth"):
            with pytest.raises(ForgeTimeoutError):
                await svc._request("GET", "https://gitlab.com/api/v4/user", headers={})

    assert any("timeout" in r.message.lower() for r in caplog.records), \
        "Log estruturado não emitido para timeout"


# ── get_forge_auth router ─────────────────────────────────────────────────────

def test_get_forge_auth_returns_github(tmp_path):
    svc = get_forge_auth(ForgeKind.GITHUB, home=tmp_path)
    assert isinstance(svc, GitHubForgeAuth)


def test_get_forge_auth_returns_gitlab(tmp_path):
    svc = get_forge_auth(ForgeKind.GITLAB, home=tmp_path)
    assert isinstance(svc, GitLabForgeAuth)


def test_get_forge_auth_gitlab_custom_host(tmp_path):
    svc = get_forge_auth(ForgeKind.GITLAB, home=tmp_path, host="gitlab.myorg.com")
    assert isinstance(svc, GitLabForgeAuth)
    assert svc._host == "gitlab.myorg.com"


# ── GitHub: validação e install ───────────────────────────────────────────────

async def test_github_validate_token_success(monkeypatch, tmp_path):
    svc = GitHubForgeAuth(home=tmp_path)

    async def fake_req(method, url, *, headers, data=None):
        return 200, {"login": "octocat", "name": "Octocat"}, {"X-OAuth-Scopes": "repo"}

    monkeypatch.setattr(svc, "_request", fake_req)
    identity = await svc.validate_token("ghp_xxx")
    assert identity.login == "octocat"
    assert identity.forge == "github"


async def test_github_install_credentials(monkeypatch, tmp_path):
    svc = GitHubForgeAuth(home=tmp_path)

    async def fake_validate(token):
        return Identity(login="octocat", token_type="PAT clássico", forge="github")

    async def fake_helper():
        pass

    monkeypatch.setattr(svc, "validate_token", fake_validate)
    monkeypatch.setattr(svc, "_configure_git_helper", fake_helper)

    identity = await svc.install_credentials("ghp_TOKEN", method="pat")
    assert identity.login == "octocat"
    assert svc.stored_token() == "ghp_TOKEN"


# ── logout multi-forge ────────────────────────────────────────────────────────

async def test_logout_github_preserves_gitlab(tmp_path):
    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gl = GitLabForgeAuth(home=tmp_path)

    creds = tmp_path / ".git-credentials"
    creds.write_text(
        "https://oauth2:ghp_X@github.com\nhttps://oauth2:glpat-X@gitlab.com\n"
    )

    removed = await svc_gh.logout()
    assert removed is True

    lines = creds.read_text().splitlines()
    assert all("github.com" not in ln for ln in lines), "github.com ainda presente!"
    assert any("gitlab.com" in ln for ln in lines), "gitlab.com foi removido indevidamente!"


async def test_logout_nothing_to_remove(tmp_path):
    svc = GitLabForgeAuth(home=tmp_path)
    assert await svc.logout() is False
