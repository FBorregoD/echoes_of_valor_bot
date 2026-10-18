import json

import pytest
import requests

import remote_config as rc
from remote_config import drive_download_url, load_config, parse_config


def _tournament(name="Echoes of Valor", alias="EoV"):
    return {
        "name": name,
        "alias": alias,
        "url": f"https://docs.google.com/spreadsheets/d/{alias}",
        "builds_sheet": "hero builds",
        "builds_mapping": {"player_col": 3, "ancestry_col": 4, "class_col": 5},
        "season": 1,
    }


def _config(*tournaments, **overrides):
    config = {
        "tournaments": list(tournaments) or [_tournament()],
        "mapping_sheet_url": "https://docs.google.com/spreadsheets/d/map",
        "default_week": 2,
        "command_prefix": "!",
        "admin_user_ids": [111, 222],
        "image_scale": 1.0,
        "guild_channels": [
            {
                "guild_id": 1,
                "prefix": "lol ",
                "channels": [
                    {"channel_id": 10, "channel_name": "eov", "tournament": "EoV"},
                    {"channel_id": 11, "channel_name": "general", "tournament": None},
                ],
            }
        ],
    }
    config.update(overrides)
    return config


REMOTE = _config(_tournament(), _tournament("Meridian Ascension", "MA"), admin_user_ids=[999])
LOCAL = _config(_tournament("Local Only", "LO"), config_url="https://x/config.json")


class FakeResponse:
    def __init__(self, body: bytes, content_type="application/octet-stream", status=200):
        self.content = body
        self.headers = {"Content-Type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


@pytest.fixture
def fake_get(monkeypatch):
    """Patch requests.get to return a configurable response and record the URL."""
    state = {"response": None, "exc": None, "url": None}

    def _get(url, timeout):
        state["url"] = url
        if state["exc"]:
            raise state["exc"]
        return state["response"]

    monkeypatch.setattr(rc.requests, "get", _get)
    return state


# ---------- drive_download_url ----------

def test_drive_share_link_converted():
    url = "https://drive.google.com/file/d/1su6h71enAIZSKKlCSOoLZ1tpob1b3YvP/view?usp=sharing"
    assert drive_download_url(url) == (
        "https://drive.google.com/uc?export=download&id=1su6h71enAIZSKKlCSOoLZ1tpob1b3YvP"
    )


def test_drive_open_link_converted():
    assert drive_download_url("https://drive.google.com/open?id=abc-123") == (
        "https://drive.google.com/uc?export=download&id=abc-123"
    )


def test_non_drive_url_unchanged():
    url = "https://example.com/config.json"
    assert drive_download_url(url) == url


# ---------- parse_config ----------

def test_parse_valid_config():
    assert parse_config(REMOTE) is REMOTE


def test_parse_minimal_config_only_required_keys():
    minimal = {"tournaments": [_tournament()], "mapping_sheet_url": "https://x"}
    assert parse_config(minimal) is minimal


@pytest.mark.parametrize("payload", [None, "x", [_tournament()]])
def test_parse_rejects_non_object(payload):
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_config(payload)


@pytest.mark.parametrize("tournaments", [None, [], "x"])
def test_parse_rejects_missing_or_empty_tournaments(tournaments):
    with pytest.raises(ValueError, match="tournaments"):
        parse_config(_config(tournaments=tournaments))


def test_parse_reports_missing_tournament_keys_by_name():
    broken = _tournament()
    del broken["builds_sheet"]
    del broken["season"]
    with pytest.raises(ValueError, match="Echoes of Valor.*builds_sheet, season"):
        parse_config(_config(broken))


def test_parse_requires_mapping_sheet_url():
    config = _config()
    del config["mapping_sheet_url"]
    with pytest.raises(ValueError, match="mapping_sheet_url"):
        parse_config(config)


@pytest.mark.parametrize("overrides, error", [
    ({"admin_user_ids": ["254177975417700352"]}, "admin_user_ids"),  # IDs quoted as text
    ({"admin_user_ids": 111}, "admin_user_ids"),
    ({"command_prefix": ""}, "command_prefix"),
    ({"default_week": "2"}, "default_week"),
    ({"image_scale": 0}, "image_scale"),
    ({"guild_channels": {}}, "guild_channels"),
    ({"guild_channels": [{"guild_id": "1", "channels": []}]}, "guild_id"),
    ({"guild_channels": [{"guild_id": 1, "prefix": "", "channels": []}]}, "prefix"),
    ({"guild_channels": [{"guild_id": 1, "channels": [{"channel_name": "x"}]}]}, "channel_id"),
])
def test_parse_rejects_bad_optional_keys(overrides, error):
    with pytest.raises(ValueError, match=error):
        parse_config(_config(**overrides))


# ---------- load_config ----------

def test_no_url_uses_local(fake_get):
    local = _config()
    assert load_config(local) is local
    assert fake_get["url"] is None  # never hit the network


def test_remote_replaces_whole_local_config(fake_get):
    fake_get["response"] = FakeResponse(json.dumps(REMOTE).encode())
    local = dict(LOCAL, config_url="https://drive.google.com/file/d/XYZ/view")
    config = load_config(local)
    assert config == REMOTE
    assert config["admin_user_ids"] == [999]  # not merged with local's
    assert fake_get["url"] == "https://drive.google.com/uc?export=download&id=XYZ"


def test_remote_with_utf8_bom(fake_get):
    fake_get["response"] = FakeResponse(b"\xef\xbb\xbf" + json.dumps(REMOTE).encode())
    assert load_config(LOCAL) == REMOTE


@pytest.mark.parametrize("response, exc", [
    (FakeResponse(b"<html>Sign-in</html>", content_type="text/html; charset=utf-8"), None),
    (FakeResponse(b"{not json"), None),
    (FakeResponse(json.dumps(_config(tournaments=[{"name": "x"}])).encode()), None),
    (FakeResponse(json.dumps(_config(command_prefix="")).encode()), None),
    (FakeResponse(b"", status=404), None),
    (None, requests.ConnectionError("down")),
    (None, requests.Timeout("slow")),
])
def test_remote_failure_falls_back_to_local(fake_get, response, exc):
    fake_get["response"], fake_get["exc"] = response, exc
    assert load_config(LOCAL) is LOCAL


def test_remote_failure_with_bootstrap_only_local_raises(fake_get):
    fake_get["exc"] = requests.ConnectionError("down")
    with pytest.raises(RuntimeError, match="local fallback config is not usable"):
        load_config({"config_url": "https://x/config.json"})


# ---------- alert_user_ids / config source ----------

def test_parse_rejects_bad_alert_user_ids():
    with pytest.raises(ValueError, match="alert_user_ids"):
        parse_config(_config(alert_user_ids=["254177975417700352"]))
    assert parse_config(_config(alert_user_ids=[1, 2]))["alert_user_ids"] == [1, 2]


def test_load_config_with_source(fake_get):
    from remote_config import (load_config_with_source, CONFIG_SOURCE_LOCAL,
                               CONFIG_SOURCE_REMOTE, CONFIG_SOURCE_FALLBACK)
    assert load_config_with_source(_config())[1] == CONFIG_SOURCE_LOCAL
    fake_get["response"] = FakeResponse(json.dumps(REMOTE).encode())
    assert load_config_with_source(LOCAL) == (REMOTE, CONFIG_SOURCE_REMOTE)
    fake_get["response"], fake_get["exc"] = None, requests.ConnectionError("down")
    assert load_config_with_source(LOCAL) == (LOCAL, CONFIG_SOURCE_FALLBACK)
