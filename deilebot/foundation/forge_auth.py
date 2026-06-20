"""forge_auth — autenticação multi-forge (GitHub + GitLab) para as operações git do bot.

ABC ``ForgeAuthService`` com concretes ``GitHubForgeAuth`` e ``GitLabForgeAuth``.

Dois métodos de login por forge:
  * PAT — o operador cola um Personal Access Token.
  * OAuth device flow (GitHub apenas em V1; GitLab OAuth é V3).

O token resultante é instalado em ``~/.git-credentials`` (modo 0600) com uma
linha por host — múltiplos forges coexistem no mesmo arquivo.

Metadados não-sensíveis ficam em ``~/.deile/forge_auth.json`` com chaves
top-level ``github`` e ``gitlab``. Migração automática: se existir o legado
``~/.deile/github_auth.json``, é lido como bloco ``github`` e renomeado para
``.bak``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

_logger = logging.getLogger("deilebot.forge_auth")

_USER_AGENT = "deilebot-forge-auth"
_RFC8628_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class ForgeKind(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


# ----- typed errors ----------------------------------------------------------

class ForgeAuthError(Exception):
    """Falha de autenticação num forge — a mensagem é pt-BR, vai para o usuário."""


class ForgeTimeoutError(ForgeAuthError):
    """Timeout numa chamada HTTP ao forge — evento loop nunca trava."""


# ----- Identity dataclass ----------------------------------------------------

@dataclass(frozen=True)
class Identity:
    """Identidade resolvida a partir de um token válido."""

    login: str
    name: str = ""
    scopes: str = ""
    token_type: str = "token"
    forge: str = ""


# ----- token classification --------------------------------------------------

_GITHUB_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghs_", "ghr_")
_GITLAB_PREFIXES = ("glpat-", "gldt-", "glptt-", "glsoat-")


def classify_token(token: str) -> Tuple[str, str]:
    """Retorna ``(forge_kind, label)`` pelo prefixo.

    forge_kind: ``"github"`` | ``"gitlab"`` | ``"unknown"``
    label: string descritiva (ex.: ``"PAT clássico"``).
    """
    token = token or ""
    if token.startswith("github_pat_"):
        return ("github", "PAT fine-grained")
    if token.startswith("ghp_"):
        return ("github", "PAT clássico")
    if token.startswith("gho_"):
        return ("github", "token OAuth")
    if token.startswith("ghs_"):
        return ("github", "token server-to-server")
    if token.startswith("ghr_"):
        return ("github", "token refresh")
    if token.startswith("glpat-"):
        return ("gitlab", "PAT pessoal")
    if token.startswith("gldt-"):
        return ("gitlab", "deploy token")
    if token.startswith("glptt-"):
        return ("gitlab", "pipeline trigger token")
    if token.startswith("glsoat-"):
        return ("gitlab", "service account token")
    return ("unknown", "token")


# ----- device flow grant (GitHub only in V1) --------------------------------

@dataclass(frozen=True)
class DeviceCodeGrant:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int = 5
    expires_in: int = 900


# ----- ABC -------------------------------------------------------------------

class ForgeAuthService(ABC):
    """Interface de autenticação para um forge."""

    @abstractmethod
    async def validate_token(self, token: str) -> Identity:
        """Valida o token fazendo uma chamada live ao forge. Levanta ForgeAuthError."""

    @abstractmethod
    async def install_credentials(self, token: str, *, method: str) -> Identity:
        """Valida o token e instala no credential store do git."""

    @abstractmethod
    async def current_identity(self) -> Optional[Identity]:
        """Revalida o token persistido ao vivo. None se ausente/inválido."""

    @abstractmethod
    def stored_metadata(self) -> Optional[Dict[str, Any]]:
        """Lê os metadados não-sensíveis do forge. None se ausentes/corrompidos."""

    @abstractmethod
    async def logout(self) -> bool:
        """Remove a credencial + metadados do forge. True se removeu algo."""


# ----- shared base implementation -------------------------------------------

class _BaseForgeAuth(ForgeAuthService):
    """Implementação base: I/O de rede, credential store, metadados multi-forge."""

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        timeout: float = 15.0,
    ) -> None:
        self._home = Path(home) if home else Path(os.environ.get("HOME", "/home/deile"))
        self._timeout = timeout

    # ----- caminhos -----

    @property
    def credentials_path(self) -> Path:
        return self._home / ".git-credentials"

    @property
    def _forge_auth_path(self) -> Path:
        return self._home / ".deile" / "forge_auth.json"

    @property
    def _legacy_path(self) -> Path:
        return self._home / ".deile" / "github_auth.json"

    @property
    @abstractmethod
    def _forge_kind(self) -> ForgeKind:
        """ForgeKind desta implementação."""

    @property
    @abstractmethod
    def _host(self) -> str:
        """Hostname deste forge (ex.: ``github.com``, ``gitlab.com``)."""

    # ----- rede monkeypatchável -----

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        data: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        """Único ponto de saída HTTP."""
        client_timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.request(method, url, headers=headers, data=data) as resp:
                    resp_headers = {k: v for k, v in resp.headers.items()}
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
                    return resp.status, body, resp_headers
        except asyncio.TimeoutError as exc:
            _logger.warning(
                "forge_auth: timeout ao contatar %s (timeout=%.1fs)",
                self._host, self._timeout,
            )
            raise ForgeTimeoutError(
                f"{self._forge_kind.value} self-hosted não respondeu em {self._timeout:.0f}s"
            ) from exc
        except aiohttp.ClientError as exc:
            _logger.warning("forge_auth: falha de rede ao contatar %s: %s", self._host, exc)
            raise ForgeAuthError(f"falha de rede ao contatar o {self._forge_kind.value}: {exc}") from exc

    # ----- credential store -----

    def _is_my_line(self, line: str) -> bool:
        """True se a linha do ~/.git-credentials aponta para o host deste forge."""
        line = line.strip()
        if not line:
            return False
        try:
            return (urlsplit(line).hostname or "").lower() == self._host.lower()
        except ValueError:
            return False

    def _read_credential_lines(self) -> List[str]:
        try:
            raw = self.credentials_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            _logger.warning("forge_auth: não consegui ler ~/.git-credentials: %s", exc)
            return []
        return [ln for ln in raw.splitlines() if ln.strip()]

    def _write_credential(self, token: str) -> None:
        """Substitui a linha deste forge no ~/.git-credentials (atômico, 0600).

        Não toca linhas de outros hosts — múltiplos forges coexistem.
        """
        lines = self._read_credential_lines()
        kept = [ln for ln in lines if not self._is_my_line(ln)]
        new_line = f"https://oauth2:{token}@{self._host}"
        content = "\n".join([new_line, *kept]).strip() + "\n"
        self._atomic_write(self.credentials_path, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def stored_token(self) -> Optional[str]:
        """Extrai o token deste forge do ~/.git-credentials, se houver."""
        for line in self._read_credential_lines():
            if self._is_my_line(line):
                parts = urlsplit(line.strip())
                if parts.password:
                    return parts.password
        return None

    async def _configure_git_helper(self) -> None:
        """``git config --global credential.helper store`` (idempotente)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "config", "--global", "credential.helper", "store",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                _logger.warning(
                    "forge_auth: 'git config credential.helper' saiu %s: %s",
                    proc.returncode, stderr.decode("utf-8", "replace")[:200],
                )
        except FileNotFoundError:
            _logger.warning("forge_auth: git não encontrado — credential.helper não configurado")
        except OSError as exc:
            _logger.warning("forge_auth: não consegui rodar git config: %s", exc)

    # ----- metadados multi-forge -----

    def _load_forge_data(self) -> Dict[str, Any]:
        """Carrega forge_auth.json; migra legado github_auth.json se necessário."""
        # Migração: legado github_auth.json → forge_auth.json["github"]
        if self._legacy_path.is_file() and not self._forge_auth_path.is_file():
            try:
                raw = self._legacy_path.read_text(encoding="utf-8")
                legacy_data = json.loads(raw)
                if isinstance(legacy_data, dict):
                    merged = {"github": legacy_data}
                    self._atomic_write(self._forge_auth_path, json.dumps(merged, indent=2) + "\n")
                    # Rename legado para .bak (rollback)
                    try:
                        self._legacy_path.rename(self._legacy_path.with_suffix(".json.bak"))
                    except OSError as exc:
                        _logger.warning("forge_auth: não consegui renomear legado: %s", exc)
            except Exception as exc:
                _logger.warning("forge_auth: migração do legado github_auth.json falhou: %s", exc)

        try:
            raw = self._forge_auth_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError):
            return {}
        except json.JSONDecodeError:
            return {}

    def _save_forge_data(self, data: Dict[str, Any]) -> None:
        self._atomic_write(self._forge_auth_path, json.dumps(data, indent=2) + "\n")

    def stored_metadata(self) -> Optional[Dict[str, Any]]:
        data = self._load_forge_data()
        section = data.get(self._forge_kind.value)
        return section if isinstance(section, dict) else None

    def _write_metadata(self, identity: Identity, *, method: str) -> None:
        data = self._load_forge_data()
        data[self._forge_kind.value] = {
            "login": identity.login,
            "name": identity.name,
            "method": method,
            "scopes": identity.scopes,
            "token_type": identity.token_type,
            "obtained_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_forge_data(data)

    def _remove_metadata(self) -> bool:
        data = self._load_forge_data()
        if self._forge_kind.value in data:
            del data[self._forge_kind.value]
            if data:
                self._save_forge_data(data)
            else:
                try:
                    self._forge_auth_path.unlink()
                except FileNotFoundError:
                    pass
            return True
        return False

    # ----- install + logout -----

    async def install_credentials(self, token: str, *, method: str) -> Identity:
        identity = await self.validate_token(token)
        self._write_credential(token.strip())
        await self._configure_git_helper()
        self._write_metadata(identity, method=method)
        _logger.info(
            "forge_auth: credencial %s instalada para login=%s método=%s",
            self._forge_kind.value, identity.login, method,
        )
        return identity

    async def current_identity(self) -> Optional[Identity]:
        token = self.stored_token()
        if not token:
            return None
        try:
            return await self.validate_token(token)
        except ForgeAuthError:
            return None

    async def logout(self) -> bool:
        removed = False
        lines = self._read_credential_lines()
        kept = [ln for ln in lines if not self._is_my_line(ln)]
        if len(kept) != len(lines):
            removed = True
            if kept:
                self._atomic_write(self.credentials_path, "\n".join(kept) + "\n")
            else:
                try:
                    self.credentials_path.unlink()
                except OSError:
                    pass
        if self._remove_metadata():
            removed = True
        return removed


# ----- GitHub ----------------------------------------------------------------

class GitHubForgeAuth(_BaseForgeAuth):
    """Autenticação GitHub: PAT + OAuth device flow (RFC 8628)."""

    _GITHUB_API_BASE = "https://api.github.com"
    _GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
    _GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        timeout: float = 15.0,
        api_base: str = _GITHUB_API_BASE,
        device_code_url: str = _GITHUB_DEVICE_CODE_URL,
        access_token_url: str = _GITHUB_ACCESS_TOKEN_URL,
    ) -> None:
        super().__init__(home=home, timeout=timeout)
        self._api_base = api_base.rstrip("/")
        self._device_code_url = device_code_url
        self._access_token_url = access_token_url

    @property
    def _forge_kind(self) -> ForgeKind:
        return ForgeKind.GITHUB

    @property
    def _host(self) -> str:
        return "github.com"

    async def validate_token(self, token: str) -> Identity:
        token = (token or "").strip()
        if not token:
            raise ForgeAuthError("token vazio")
        status, body, headers = await self._request(
            "GET",
            f"{self._api_base}/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": _USER_AGENT,
            },
        )
        if status == 401:
            _logger.warning("forge_auth: token GitHub inválido/expirado (HTTP 401)")
            raise ForgeAuthError("token GitHub inválido ou expirado (HTTP 401)")
        if status != 200:
            _logger.warning("forge_auth: GitHub recusou token (HTTP %s)", status)
            raise ForgeAuthError(
                f"o GitHub recusou o token (HTTP {status}) — "
                "verifique se ele tem os escopos necessários"
            )
        login = str(body.get("login") or "").strip()
        if not login:
            raise ForgeAuthError("resposta do GitHub sem o campo 'login'")
        _, token_type = classify_token(token)
        return Identity(
            login=login,
            name=str(body.get("name") or ""),
            scopes=str(headers.get("X-OAuth-Scopes") or "").strip(),
            token_type=token_type,
            forge="github",
        )

    async def start_device_flow(self, client_id: str, scope: str) -> DeviceCodeGrant:
        if not client_id:
            raise ForgeAuthError(
                "OAuth não configurado — o operador precisa registrar um GitHub "
                "OAuth App e definir `forge.github.oauth_client_id` no deilebot.yaml. "
                "Use o método PAT enquanto isso."
            )
        status, body, _ = await self._request(
            "POST",
            self._device_code_url,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            data={"client_id": client_id, "scope": scope},
        )
        if status != 200 or "device_code" not in body:
            err = body.get("error_description") or body.get("error") or f"HTTP {status}"
            raise ForgeAuthError(f"o GitHub recusou iniciar o device flow: {err}")
        return DeviceCodeGrant(
            device_code=str(body["device_code"]),
            user_code=str(body.get("user_code", "")),
            verification_uri=str(
                body.get("verification_uri", "https://github.com/login/device")
            ),
            interval=int(body.get("interval", 5) or 5),
            expires_in=int(body.get("expires_in", 900) or 900),
        )

    async def poll_device_flow(self, client_id: str, grant: DeviceCodeGrant) -> str:
        interval = max(grant.interval, 1)
        deadline = time.monotonic() + grant.expires_in
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            status, body, _ = await self._request(
                "POST",
                self._access_token_url,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
                data={
                    "client_id": client_id,
                    "device_code": grant.device_code,
                    "grant_type": _RFC8628_GRANT_TYPE,
                },
            )
            token = body.get("access_token")
            if token:
                return str(token)
            error = str(body.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "expired_token":
                break
            if error == "access_denied":
                raise ForgeAuthError("autorização negada no GitHub")
            detail = body.get("error_description") or error or f"HTTP {status}"
            raise ForgeAuthError(f"o GitHub recusou o device flow: {detail}")
        raise ForgeAuthError("o código expirou antes da autorização — refaça o /git login")


# ----- GitLab ----------------------------------------------------------------

class GitLabForgeAuth(_BaseForgeAuth):
    """Autenticação GitLab: PAT como caminho primário (OAuth GitLab é V3)."""

    _DEFAULT_HOST = "gitlab.com"

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        timeout: float = 15.0,
        host: str = _DEFAULT_HOST,
    ) -> None:
        super().__init__(home=home, timeout=timeout)
        self._gitlab_host = host.rstrip("/")

    @property
    def _forge_kind(self) -> ForgeKind:
        return ForgeKind.GITLAB

    @property
    def _host(self) -> str:
        return self._gitlab_host

    async def validate_token(self, token: str) -> Identity:
        token = (token or "").strip()
        if not token:
            raise ForgeAuthError("token vazio")
        status, body, headers = await self._request(
            "GET",
            f"https://{self._gitlab_host}/api/v4/user",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": _USER_AGENT,
            },
        )
        if status == 401:
            _logger.warning(
                "forge_auth: token GitLab inválido/expirado (HTTP 401) host=%s",
                self._gitlab_host,
            )
            raise ForgeAuthError("token GitLab inválido ou expirado (HTTP 401)")
        if status != 200:
            _logger.warning(
                "forge_auth: GitLab recusou token (HTTP %s) host=%s",
                status, self._gitlab_host,
            )
            raise ForgeAuthError(
                f"o GitLab recusou o token (HTTP {status}) — "
                "verifique se ele tem os escopos necessários"
            )
        login = str(body.get("username") or "").strip()
        if not login:
            raise ForgeAuthError("resposta do GitLab sem o campo 'username'")
        _, token_type = classify_token(token)
        return Identity(
            login=login,
            name=str(body.get("name") or ""),
            scopes=str(body.get("scopes") or headers.get("X-OAuth-Scopes") or "").strip(),
            token_type=token_type,
            forge="gitlab",
        )


# ----- router ----------------------------------------------------------------

def get_forge_auth(
    kind: ForgeKind,
    *,
    home: Optional[Path] = None,
    timeout: float = 15.0,
    host: Optional[str] = None,
) -> ForgeAuthService:
    """Resolve o ForgeAuthService pelo ForgeKind."""
    if kind == ForgeKind.GITHUB:
        return GitHubForgeAuth(home=home, timeout=timeout)
    if kind == ForgeKind.GITLAB:
        return GitLabForgeAuth(
            home=home, timeout=timeout,
            host=host or GitLabForgeAuth._DEFAULT_HOST,
        )
    raise ValueError(f"forge kind desconhecido: {kind!r}")
