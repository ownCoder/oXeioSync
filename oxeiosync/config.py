"""oXeioSync's own settings, persisted as JSON next to the Syncthing database."""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

DEFAULT_GUI_ADDRESS = "127.0.0.1:8384"


@dataclass
class Config:
    """User-visible configuration.

    Anything Syncthing owns (folders, devices, ignore patterns) is deliberately
    absent — that lives in Syncthing's own config and is edited through its web
    UI. This holds only what the host application needs to know.
    """

    # --- Syncthing process ---------------------------------------------------
    #: Override for the Syncthing binary. Empty means "discover automatically".
    syncthing_path: str = ""
    #: Override for Syncthing's home directory. Empty means the default.
    syncthing_home: str = ""
    #: Extra command-line arguments appended to the Syncthing invocation.
    syncthing_extra_args: list[str] = field(default_factory=list)
    #: Address Syncthing's web GUI is bound to.
    gui_address: str = DEFAULT_GUI_ADDRESS
    #: API key forced onto Syncthing at launch, so we always know it.
    api_key: str = ""
    start_syncthing_automatically: bool = True

    # --- Window and tray behaviour ------------------------------------------
    start_on_login: bool = False
    start_minimized: bool = False
    close_to_tray: bool = True
    minimize_to_tray: bool = True

    # --- Notifications -------------------------------------------------------
    notify_folder_synced: bool = True
    notify_device_connections: bool = True
    notify_conflicts: bool = True
    notify_folder_errors: bool = True

    # --- Persisted UI state --------------------------------------------------
    #: Base64-encoded QMainWindow.saveGeometry() blob.
    window_geometry: str = ""
    #: How many lines of Syncthing output to keep in the in-app log viewer.
    log_lines_kept: int = 2000

    # ------------------------------------------------------------------ helpers
    def ensure_api_key(self) -> str:
        """Generate an API key on first run so we never have to guess it."""
        if not self.api_key:
            self.api_key = secrets.token_hex(16)
        return self.api_key

    def resolved_syncthing_home(self) -> Path:
        return Path(self.syncthing_home) if self.syncthing_home else paths.syncthing_home()

    def gui_url(self) -> str:
        """The base URL to reach Syncthing's GUI and REST API on."""
        from .syncthing.api import normalise_gui_address

        return normalise_gui_address(self.gui_address or DEFAULT_GUI_ADDRESS)

    # ----------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        """Build a Config from parsed JSON, ignoring unknown or mistyped keys.

        Being forgiving here means a config written by a newer version, or one
        a user has hand-edited badly, degrades to defaults instead of stopping
        the application from starting.
        """
        known = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in raw.items():
            spec = known.get(key)
            if spec is None:
                log.debug("Ignoring unknown config key %r", key)
                continue
            if spec.type == "bool" and not isinstance(value, bool):
                log.warning("Ignoring config key %r: expected a boolean", key)
                continue
            if spec.type == "int" and not isinstance(value, int):
                log.warning("Ignoring config key %r: expected an integer", key)
                continue
            if spec.type == "str" and not isinstance(value, str):
                log.warning("Ignoring config key %r: expected a string", key)
                continue
            if spec.type == "list[str]" and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            ):
                log.warning("Ignoring config key %r: expected a list of strings", key)
                continue
            kwargs[key] = value
        return cls(**kwargs)


def load(path: Path | None = None) -> Config:
    """Read the config file, falling back to defaults if it is missing or bad."""
    path = path or paths.config_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("No config at %s; using defaults", path)
        return Config()
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read config at %s (%s); using defaults", path, exc)
        return Config()

    if not isinstance(raw, dict):
        log.error("Config at %s is not a JSON object; using defaults", path)
        return Config()
    return Config.from_dict(raw)


def save(config: Config, path: Path | None = None) -> None:
    """Write the config file atomically, so a crash cannot truncate it."""
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    log.debug("Wrote config to %s", path)
