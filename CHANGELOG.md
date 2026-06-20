# Changelog

## [Unreleased]

### Breaking

- **`/git` replaces `/github_*`** — os comandos `/github_login`, `/github_status` e `/github_logout` foram **removidos** e substituídos pelo grupo nativo `/git login`, `/git status`, `/git logout`. Tentar os comandos antigos retorna uma mensagem de migração.
- **`ForgeSettings` substitui `GitHubSettings`** — o bloco `github:` top-level no `deilebot.yaml` foi substituído por `forge.github:` (e `forge.gitlab:`). O `_build_settings` ainda lê o legado `github:` como fallback de migração.
- **`BotSettings.github` removido** — use `BotSettings.forge.github` e `BotSettings.forge.gitlab`.

### Added

- **`deilebot/foundation/forge_auth.py`** — ABC `ForgeAuthService` com concretes `GitHubForgeAuth` e `GitLabForgeAuth`.
  - `classify_token()` reconhece 5 prefixos GitHub e 4 prefixos GitLab.
  - Metadados multi-forge em `~/.deile/forge_auth.json` (chaves `github`/`gitlab`).
  - Migração automática: `~/.deile/github_auth.json` → `forge_auth.json["github"]` + `.bak`.
  - Timeout configurável por forge (`ForgeSettings.<forge>.timeout`, default 15.0s).
  - `ForgeTimeoutError` tipado — timeout nunca trava o event loop Discord.
  - Log estruturado via `logging.getLogger("deilebot.forge_auth")`.
- **`deilebot/providers/discord/cogs/git_cog.py`** — grupo `/git` com subcomandos `login`/`status`/`logout`/`ideia`.
  - `/git login` abre Modal com campo Forge + PAT; valida ao vivo antes de gravar.
  - `/git status` revalida ao vivo a cada invocação (3 estados: ✅/⚠️/⛔).
  - `/git ideia` integra o antigo `IdeaCog`; seleciona forge automaticamente; templates por forge.
  - `on_command_error` responde mensagem de migração para `/github_*` legados.
- **`ForgeSettings`** em `settings.py` — sub-blocos `github` e `gitlab` com `host`/`oauth_client_id`/`oauth_scope`/`timeout`.
- **`_step_forge`** no wizard — substitui `_step_github`; idempotente (merge por forge no re-run).
- Suporte a `GITLAB_TOKEN`/`GL_TOKEN` em `.env`.
- Exemplos GitLab em `config/deilebot.example.yaml` e `.env.example`.

### Removed

- `deilebot/providers/discord/cogs/github_auth_cog.py` — substituído por `git_cog.py`.
- `deilebot/providers/discord/cogs/idea_cog.py` — integrado em `/git ideia`.
- Registro de `IdeaCog` e `GitHubAuthCog` em `adapter.py`.
