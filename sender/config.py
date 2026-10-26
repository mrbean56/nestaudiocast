"""Configuration of the owner app — ``/etc/castsender.conf``, contracts §6.

The keys are **exactly** §6's, and §6 says nothing else may be added without approval. Paths
and the offline switch are therefore command-line/environment knobs of the service, not
configuration keys; they exist so the offline self-test can run outside ``/opt/castrelay``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, replace

from v020.contracts import CONFIG_DEFAULTS, QUIET_POLICIES

from . import ROOT
from .quiet import QuietWindow, parse_hhmm

_APP_ID = re.compile(r"^[0-9A-Fa-f]{8}$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

#: §6, in file order — the example file and the parser share this list.
CONFIG_KEYS = (
    "CHROMECAST", "APP_ID", "GROUP", "HTTP_BIND", "HTTP_PORT", "DELTA_V2", "RATE_MAX",
    "MAX_HEIGHT", "QUIET_START", "QUIET_END", "QUIET_POLICY",
)

#: Where the service looks when no --config is given (§6 says /etc/castsender.conf).
DEFAULT_CONFIG_PATH = "/etc/castsender.conf"


class ConfigError(ValueError):
    """A configuration value is unusable; the service refuses to start rather than guess."""


def _as_number(key: str, raw: str, cast, low: float, high: float) -> float:
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: not a number ({raw!r})") from exc
    if not (low <= float(value) <= high):
        raise ConfigError(f"{key}: {value} is outside {low}…{high}")
    return value


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a systemd ``EnvironmentFile``: ``KEY=VALUE``, ``#`` comments, optional quotes."""
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                raise ConfigError(f"{path}:{lineno}: not KEY=VALUE: {line!r}")
            key, _, value = line.partition("=")
            key = key.strip()
            if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
                raise ConfigError(f"{path}:{lineno}: bad key {key!r}")
            value = value.strip()
            # systemd keeps a trailing " # comment" in the value; strip it the way humans read it
            if " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


@dataclass(frozen=True)
class Config:
    """The §6 configuration, validated. Immutable so a session cannot be reconfigured under it."""

    chromecast: str = CONFIG_DEFAULTS["CHROMECAST"]
    app_id: str = CONFIG_DEFAULTS["APP_ID"]
    group: str = CONFIG_DEFAULTS["GROUP"]
    http_bind: str = CONFIG_DEFAULTS["HTTP_BIND"]
    http_port: int = int(CONFIG_DEFAULTS["HTTP_PORT"])
    delta_v2: float = float(CONFIG_DEFAULTS["DELTA_V2"])
    rate_max: float = float(CONFIG_DEFAULTS["RATE_MAX"])
    max_height: int = int(CONFIG_DEFAULTS["MAX_HEIGHT"])
    quiet_start: str = CONFIG_DEFAULTS["QUIET_START"]
    quiet_end: str = CONFIG_DEFAULTS["QUIET_END"]
    quiet_policy: str = CONFIG_DEFAULTS["QUIET_POLICY"]

    # Paths: not §6 keys, not read from /etc/castsender.conf.
    state_dir: str = field(default_factory=lambda: os.path.join(ROOT, "state"))
    evidence_dir: str = field(default_factory=lambda: os.path.join(ROOT, "evidence"))
    receiver_dir: str = field(default_factory=lambda: os.path.join(ROOT, "receiver"))
    assets_dir: str = field(default_factory=lambda: os.path.join(ROOT, "assets"))

    # -- construction -----------------------------------------------------------------------
    @classmethod
    def from_mapping(cls, values: dict[str, str], **overrides) -> "Config":
        """Build from §6 ``KEY=VALUE`` strings; unknown keys are refused (no silent typo)."""
        unknown = sorted(set(values) - set(CONFIG_KEYS) - set(f.name for f in fields(cls)))
        if unknown:
            raise ConfigError(f"unknown configuration key(s): {', '.join(unknown)}")
        casts = {"http_port": int, "max_height": int, "delta_v2": float, "rate_max": float}
        kw: dict[str, object] = {}
        for key, value in values.items():
            if key not in CONFIG_KEYS:
                continue
            name = key.lower()
            cast = casts.get(name, str)
            try:
                kw[name] = cast(str(value).strip()) if cast is not str else str(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key}: {value!r} is not a {cast.__name__}") from exc
        cfg = cls(**kw)
        if overrides:
            cfg = replace(cfg, **overrides)
        return cfg.validate()

    @classmethod
    def from_file(cls, path: str, **overrides) -> "Config":
        if not os.path.isfile(path):
            raise ConfigError(f"no configuration file at {path}")
        return cls.from_mapping(parse_env_file(path), **overrides)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **overrides) -> "Config":
        """§6 keys taken from the process environment (what systemd's EnvironmentFile does)."""
        env = os.environ if env is None else env
        values = {key: env[key] for key in CONFIG_KEYS if key in env}
        return cls.from_mapping(values, **overrides)

    # -- validation -------------------------------------------------------------------------
    def validate(self) -> "Config":
        if not _APP_ID.match(str(self.app_id)):
            raise ConfigError(f"APP_ID: expected 8 hex characters, got {self.app_id!r}")
        for key in ("chromecast", "group", "http_bind"):
            value = str(getattr(self, key))
            if not value or not _HOST.match(value):
                raise ConfigError(f"{key.upper()}: not a host name or address ({value!r})")
        if not (0 <= int(self.http_port) <= 65535):
            # 0 = let the OS pick a port: only the offline self-test and the tests use it.
            raise ConfigError(f"HTTP_PORT: {self.http_port} is not a TCP port")
        if not (-1.0 <= float(self.delta_v2) <= 1.0):
            raise ConfigError(f"DELTA_V2: {self.delta_v2} s is not a plausible calibration")
        if not (0.0 < float(self.rate_max) <= 0.5):
            raise ConfigError(f"RATE_MAX: {self.rate_max} must be in (0, 0.5]")
        if not (144 <= int(self.max_height) <= 4320):
            raise ConfigError(f"MAX_HEIGHT: {self.max_height} is not a video height")
        if self.quiet_policy not in QUIET_POLICIES:
            raise ConfigError(f"QUIET_POLICY: must be one of {QUIET_POLICIES}")
        try:
            parse_hhmm(self.quiet_start)
            parse_hhmm(self.quiet_end)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if (parse_hhmm(self.quiet_start) is None) != (parse_hhmm(self.quiet_end) is None):
            raise ConfigError("QUIET_START and QUIET_END go together (both set, or both empty)")
        return self

    # -- helpers ----------------------------------------------------------------------------
    @property
    def quiet(self) -> QuietWindow:
        return QuietWindow(self.quiet_start, self.quiet_end, self.quiet_policy)

    def as_mapping(self) -> dict[str, str]:
        """The §6 keys as strings, for a log line or the example file."""
        return {
            "CHROMECAST": self.chromecast,
            "APP_ID": self.app_id,
            "GROUP": self.group,
            "HTTP_BIND": self.http_bind,
            "HTTP_PORT": str(self.http_port),
            "DELTA_V2": f"{self.delta_v2}",
            "RATE_MAX": f"{self.rate_max}",
            "MAX_HEIGHT": str(self.max_height),
            "QUIET_START": self.quiet_start,
            "QUIET_END": self.quiet_end,
            "QUIET_POLICY": self.quiet_policy,
        }


def load_config(path: str | None = None, *, env: dict[str, str] | None = None,
                **overrides) -> Config:
    """``--config`` if given, else /etc/castsender.conf if it exists, else the environment."""
    if path:
        return Config.from_file(path, **overrides)
    if os.path.isfile(DEFAULT_CONFIG_PATH):
        return Config.from_file(DEFAULT_CONFIG_PATH, **overrides)
    return Config.from_env(env, **overrides)
