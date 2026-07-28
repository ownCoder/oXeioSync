"""Tests for configuration loading, saving and URL normalisation."""

from __future__ import annotations

import json

import pytest

from oxeiosync import config as config_module
from oxeiosync.config import DEFAULT_GUI_ADDRESS, Config


def test_defaults_are_usable_without_a_file(tmp_path):
    loaded = config_module.load(tmp_path / "missing.json")
    assert loaded == Config()
    assert loaded.gui_address == DEFAULT_GUI_ADDRESS


def test_round_trip(tmp_path):
    path = tmp_path / "config.json"
    original = Config(
        gui_address="127.0.0.1:9999",
        api_key="deadbeef",
        syncthing_extra_args=["--no-default-folder"],
        close_to_tray=False,
        log_lines_kept=500,
    )

    config_module.save(original, path)
    assert config_module.load(path) == original


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "config.json"
    config_module.save(Config(), path)

    assert path.exists()
    assert list(tmp_path.iterdir()) == [path]


def test_unknown_keys_are_ignored():
    """A config from a newer version must not stop this one from starting."""
    loaded = Config.from_dict({"api_key": "abc", "some_future_setting": 42})

    assert loaded.api_key == "abc"
    assert not hasattr(loaded, "some_future_setting")


@pytest.mark.parametrize(
    "key, bad_value",
    [
        ("close_to_tray", "yes"),  # str where bool expected
        ("log_lines_kept", "many"),  # str where int expected
        ("api_key", 12345),  # int where str expected
        ("syncthing_extra_args", "--flag"),  # str where list expected
        ("syncthing_extra_args", [1, 2]),  # list of the wrong element type
    ],
)
def test_mistyped_values_fall_back_to_defaults(key, bad_value):
    loaded = Config.from_dict({key: bad_value})
    assert getattr(loaded, key) == getattr(Config(), key)


def test_malformed_json_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json at all", encoding="utf-8")

    assert config_module.load(path) == Config()


def test_json_array_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert config_module.load(path) == Config()


def test_ensure_api_key_is_generated_once():
    config = Config()
    assert config.api_key == ""

    first = config.ensure_api_key()
    assert len(first) == 32
    assert config.ensure_api_key() == first


@pytest.mark.parametrize(
    "address, expected",
    [
        ("127.0.0.1:8384", "http://127.0.0.1:8384"),
        # A bind address meaning "all interfaces" is not a usable client target.
        (":8384", "http://127.0.0.1:8384"),
        ("0.0.0.0:8384", "http://127.0.0.1:8384"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("https://127.0.0.1:8384", "https://127.0.0.1:8384"),
        ("http://127.0.0.1:8384/", "http://127.0.0.1:8384"),
    ],
)
def test_gui_url_normalisation(address, expected):
    assert Config(gui_address=address).gui_url() == expected


def test_empty_gui_address_uses_the_default():
    assert Config(gui_address="").gui_url() == f"http://{DEFAULT_GUI_ADDRESS}"
