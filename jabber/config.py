"""Jabber configuration: one TOML file, validated at startup.

mode = "solo"   one person, loopback by default, no login.
mode = "multi"  several people; refuses to start without a real auth provider.

auth.provider:
  none    solo only.
  local   built-in accounts (scrypt password hashes in the data dir).
  oidc    any OpenID Connect issuer (Keycloak, Authentik, Zitadel, Entra, ...).
  proxy   identity asserted by a trusted reverse proxy (header or signed JWT).
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("solo", "multi")
PROVIDERS = ("none", "local", "oidc", "proxy")
BACKENDS = ("auto", "ollama", "openai")
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class ConfigError(ValueError):
    pass


@dataclass
class Auth:
    provider: str = "none"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret_env: str = "JABBER_OIDC_SECRET"
    proxy_header: str = "X-Forwarded-Email"
    proxy_trusted_ips: list = field(default_factory=lambda: ["127.0.0.1"])
    allow_signup: bool = False


@dataclass
class Backend:
    kind: str = "auto"
    url: str = "http://127.0.0.1:11434"
    api_key_env: str = ""


@dataclass
class Config:
    mode: str = "solo"
    instance_name: str = "Jabber"
    bind: str = "127.0.0.1"
    port: int = 8420
    data_dir: str = "~/.local/share/jabber"
    system_prompt: str = ""
    model_allowlist: list = field(default_factory=list)
    default_model: str = ""
    model_choice: bool = True
    max_upload_bytes: int = 2 * 1024**3     # per file
    user_quota_bytes: int = 20 * 1024**3    # per user, all projects
    chunk_bytes: int = 512 * 1024           # upload piece size; must fit the reverse proxy's body limit (nginx default 1 MB)
    web: bool = False                       # allow web search/page fetch (users still switch it on per chat)
    search_url: str = ""                    # optional SearXNG base URL (preferred over DuckDuckGo)
    obscura_path: str = ""                  # optional Obscura binary, fallback when DuckDuckGo blocks plain requests
    host_note: str = ""                    # one line about where this runs, e.g. "a single 24 GB GPU in the owner's home"
    max_thinking_chars: int = 12000         # stop endless reasoning and make the model answer (0 = no cap)
    tools: bool = True                      # let the model read project files
    secure_cookies: bool = False
    context_tokens: int = 16384
    think: bool = True
    log_enabled: bool = True
    log_dir: str = ""
    auth: Auth = field(default_factory=Auth)
    backend: Backend = field(default_factory=Backend)

    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    def log_path(self) -> Path:
        return Path(self.log_dir).expanduser() if self.log_dir else self.data_path() / "logs"


def _section(raw: dict, name: str, cls):
    sec = raw.get(name, {})
    unknown = set(sec) - set(cls.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown keys in [{name}]: {sorted(unknown)}")
    return cls(**sec)


def validate(cfg: Config) -> Config:
    if cfg.mode not in MODES:
        raise ConfigError(f"mode must be one of {MODES}, got {cfg.mode!r}")
    if cfg.auth.provider not in PROVIDERS:
        raise ConfigError(f"auth.provider must be one of {PROVIDERS}")
    if cfg.backend.kind not in BACKENDS:
        raise ConfigError(f"backend.kind must be one of {BACKENDS}")
    if not 0 <= cfg.port <= 65535:
        raise ConfigError("port out of range")
    if cfg.mode == "multi" and cfg.auth.provider == "none":
        raise ConfigError("mode=multi requires auth.provider = local, oidc or proxy")
    if cfg.mode == "solo" and cfg.auth.provider not in ("none", "local"):
        raise ConfigError("mode=solo supports auth.provider = none or local")
    if cfg.bind not in LOOPBACK and cfg.auth.provider == "none":
        raise ConfigError("a non-loopback bind needs an auth provider")
    if not 16 * 1024 <= cfg.chunk_bytes <= 16 * 1024 * 1024:
        raise ConfigError("chunk_bytes must be between 16384 and 16777216")
    if cfg.max_upload_bytes < 1 or cfg.user_quota_bytes < cfg.max_upload_bytes:
        raise ConfigError("user_quota_bytes must be at least max_upload_bytes")
    if cfg.context_tokens != 0 and not 2048 <= cfg.context_tokens <= 2_000_000:
        raise ConfigError("context_tokens must be 0 (no fitting) or between 2048 and 2000000")
    if not cfg.model_choice and not cfg.default_model:
        raise ConfigError("model_choice = false requires default_model")
    if cfg.auth.provider == "oidc" and not (cfg.auth.oidc_issuer and cfg.auth.oidc_client_id):
        raise ConfigError("auth.provider=oidc needs oidc_issuer and oidc_client_id")
    if cfg.auth.provider == "proxy" and not cfg.auth.proxy_trusted_ips:
        raise ConfigError("auth.provider=proxy needs proxy_trusted_ips")
    return cfg


def load(path=None) -> Config:
    raw = {}
    if path:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    top = {k: v for k, v in raw.items() if k not in ("auth", "backend")}
    unknown = set(top) - set(Config.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown top-level keys: {sorted(unknown)}")
    cfg = Config(**top)
    cfg.auth = _section(raw, "auth", Auth)
    cfg.backend = _section(raw, "backend", Backend)
    return validate(cfg)
