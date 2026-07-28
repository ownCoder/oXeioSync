"""Bind-address helpers for Syncthing's web GUI.

Syncthing's default GUI port, 8384, is very often already taken — by another
Syncthing, by SyncTrayzor, or by a copy of oXeioSync that did not exit cleanly.
Syncthing's response is to log a bind error and exit, so the port has to be
checked before launching it rather than diagnosed afterwards.
"""

from __future__ import annotations

import logging
import socket

log = logging.getLogger(__name__)

DEFAULT_PORT = 8384
#: How many consecutive ports to try when the preferred one is taken.
PORT_SEARCH_RANGE = 40


def split_bind_address(address: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """Split a Syncthing GUI address into a bindable ``(host, port)`` pair.

    An empty host means "every interface", which is what Syncthing itself
    understands from an address like ``:8384``.
    """
    address = address.strip()
    if "://" in address:
        address = address.split("://", 1)[1]
    address = address.rstrip("/")

    if not address:
        return "", default_port

    # Bracketed IPv6 literal, e.g. "[::1]:8384".
    if address.startswith("["):
        host, _, rest = address.partition("]")
        host = host[1:]
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else default_port

    host, sep, port = address.rpartition(":")
    if not sep:
        # No colon at all: a bare host or a bare port.
        return ("", int(address)) if address.isdigit() else (address, default_port)

    return host, int(port) if port.isdigit() else default_port


def join_bind_address(host: str, port: int) -> str:
    """Rebuild an address string from a host and port."""
    if not host:
        return f":{port}"
    if ":" in host:  # IPv6 literal
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def is_port_available(host: str, port: int) -> bool:
    """True if a TCP listener could be opened on ``host:port`` right now.

    Deliberately does not set ``SO_REUSEADDR``: on Windows that flag lets a
    bind succeed against a port already in use, which is exactly the case we
    are trying to detect.
    """
    bind_host = host or "0.0.0.0"
    # Pick the one family the address actually belongs to. Trying both would
    # mean an IPv6 address always failed the IPv4 attempt and got reported as
    # occupied when it is free.
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET

    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((bind_host, port))
            sock.listen(1)
    except OSError as exc:
        log.debug("Port %s unavailable: %s", join_bind_address(host, port), exc)
        return False
    except (ValueError, socket.gaierror) as exc:
        log.debug("Cannot evaluate %s: %s", join_bind_address(host, port), exc)
        return False
    return True


def find_free_port(host: str, preferred: int, attempts: int = PORT_SEARCH_RANGE) -> int | None:
    """Return ``preferred`` if it is free, else the next free port above it."""
    for offset in range(attempts):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if is_port_available(host, candidate):
            return candidate
    return None


def describe_port_conflict(host: str, port: int) -> str:
    """A message explaining a bind conflict, in terms the user can act on."""
    where = join_bind_address(host, port)
    return (
        f"Another program is already listening on {where}, so the sync engine "
        "cannot start its web interface.\n\n"
        "This usually means another sync client is already running. "
        "Either close it, or change the engine address in Settings."
    )
