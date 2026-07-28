"""Tests for GUI bind-address parsing and port probing."""

from __future__ import annotations

import socket

import pytest

from oxeiosync import net


@pytest.mark.parametrize(
    "address, expected",
    [
        ("127.0.0.1:8384", ("127.0.0.1", 8384)),
        (":8384", ("", 8384)),
        ("0.0.0.0:8384", ("0.0.0.0", 8384)),
        ("localhost:9000", ("localhost", 9000)),
        ("http://127.0.0.1:8384", ("127.0.0.1", 8384)),
        ("http://127.0.0.1:8384/", ("127.0.0.1", 8384)),
        ("[::1]:8384", ("::1", 8384)),
        # No port given: fall back to Syncthing's default.
        ("127.0.0.1", ("127.0.0.1", net.DEFAULT_PORT)),
        ("", ("", net.DEFAULT_PORT)),
        # A bare number is a port, not a host.
        ("8384", ("", 8384)),
    ],
)
def test_split_bind_address(address, expected):
    assert net.split_bind_address(address) == expected


@pytest.mark.parametrize(
    "host, port, expected",
    [
        ("127.0.0.1", 8384, "127.0.0.1:8384"),
        ("", 8384, ":8384"),
        ("::1", 8384, "[::1]:8384"),
    ],
)
def test_join_bind_address(host, port, expected):
    assert net.join_bind_address(host, port) == expected


def test_split_and_join_round_trip():
    for address in ("127.0.0.1:8384", ":8385", "[::1]:9000"):
        host, port = net.split_bind_address(address)
        assert net.join_bind_address(host, port) == address


def test_occupied_port_is_reported_unavailable():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]

        assert not net.is_port_available("127.0.0.1", port)


def test_free_port_is_reported_available():
    # Bind and release to get a port number the OS just told us is usable.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert net.is_port_available("127.0.0.1", port)


def test_find_free_port_skips_an_occupied_one():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]

        found = net.find_free_port("127.0.0.1", port)
        assert found is not None
        assert found > port


def test_find_free_port_returns_the_preferred_one_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert net.find_free_port("127.0.0.1", port) == port


def test_find_free_port_gives_up_rather_than_overflowing():
    assert net.find_free_port("127.0.0.1", 65535, attempts=5) in (65535, None)


def test_ipv6_loopback_is_probed_with_the_right_family():
    """A v6 address must not be reported busy just because a v4 bind fails."""
    if not socket.has_ipv6:
        pytest.skip("no IPv6 support on this host")

    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("::1", 0))
        except OSError:
            pytest.skip("IPv6 loopback is not usable on this host")
        port = probe.getsockname()[1]

    assert net.is_port_available("::1", port)


def test_occupied_ipv6_port_is_reported_unavailable():
    if not socket.has_ipv6:
        pytest.skip("no IPv6 support on this host")

    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as taken:
        try:
            taken.bind(("::1", 0))
        except OSError:
            pytest.skip("IPv6 loopback is not usable on this host")
        taken.listen(1)
        port = taken.getsockname()[1]

        assert not net.is_port_available("::1", port)


def test_empty_host_means_all_interfaces():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]

    assert net.is_port_available("", port)


def test_conflict_description_names_the_address():
    message = net.describe_port_conflict("127.0.0.1", 8384)
    assert "127.0.0.1:8384" in message
    assert "Settings" in message
