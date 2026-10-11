"""Load the bot configuration from a remote JSON file (Google Drive).

The local config.json sets "config_url" to a publicly shared JSON file
holding the full configuration (tournaments, guild_channels, admins, ...).
That remote file is the source of truth: everything can be changed there
without a code push — the bot picks it up on its next restart.

If the remote fetch fails or the remote file is invalid, the rest of the
local config.json is used as a fallback so the bot still starts.
"""
import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

REMOTE_FETCH_TIMEOUT = 30  # seconds
REQUIRED_TOURNAMENT_KEYS = ("name", "alias", "url", "builds_sheet", "builds_mapping", "season")

_DRIVE_FILE_ID_RE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:.*&)?id=)([\w-]+)")


def drive_download_url(url: str) -> str:
    """Turn a Google Drive share link into a direct-download URL.

    Non-Drive URLs are returned unchanged, so any plain JSON URL also works.
    """
    match = _DRIVE_FILE_ID_RE.search(url)
    if not match:
        return url
    return f"https://drive.google.com/uc?export=download&id={match.group(1)}"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_tournaments(tournaments) -> None:
    if not isinstance(tournaments, list) or not tournaments:
        raise ValueError("no non-empty 'tournaments' list found")
    for i, tournament in enumerate(tournaments):
        if not isinstance(tournament, dict):
            raise ValueError(f"tournament #{i + 1} is not an object")
        missing = [k for k in REQUIRED_TOURNAMENT_KEYS if k not in tournament]
        if missing:
            label = tournament.get("name") or f"#{i + 1}"
            raise ValueError(f"tournament {label} is missing keys: {', '.join(missing)}")


def _validate_guild_channels(guild_channels) -> None:
    if not isinstance(guild_channels, list):
        raise ValueError("'guild_channels' must be a list")
    for i, guild in enumerate(guild_channels):
        if not isinstance(guild, dict) or not _is_int(guild.get("guild_id")):
            raise ValueError(f"guild_channels #{i + 1} needs an integer 'guild_id'")
        if "prefix" in guild and (not isinstance(guild["prefix"], str) or not guild["prefix"]):
            raise ValueError(f"guild {guild['guild_id']} has an empty or non-text 'prefix'")
        channels = guild.get("channels", [])
        if not isinstance(channels, list):
            raise ValueError(f"guild {guild['guild_id']} 'channels' must be a list")
        for channel in channels:
            if not isinstance(channel, dict) or not _is_int(channel.get("channel_id")):
                raise ValueError(f"guild {guild['guild_id']} has a channel without an integer 'channel_id'")


def parse_config(payload) -> dict:
    """Validate a config payload and return it unchanged.

    Required: "tournaments" and "mapping_sheet_url". The optional keys are
    type-checked only when present (main.py applies the defaults).
    Raises ValueError with a readable message on the first problem found.
    """
    if not isinstance(payload, dict):
        raise ValueError("config is not a JSON object")

    _validate_tournaments(payload.get("tournaments"))

    mapping_url = payload.get("mapping_sheet_url")
    if not isinstance(mapping_url, str) or not mapping_url:
        raise ValueError("'mapping_sheet_url' is missing or empty")

    if "guild_channels" in payload:
        _validate_guild_channels(payload["guild_channels"])
    if "admin_user_ids" in payload:
        admins = payload["admin_user_ids"]
        if not isinstance(admins, list) or not all(_is_int(a) for a in admins):
            raise ValueError("'admin_user_ids' must be a list of integer Discord IDs")
    if "command_prefix" in payload:
        prefix = payload["command_prefix"]
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("'command_prefix' must be non-empty text")
    if "default_week" in payload and not _is_int(payload["default_week"]):
        raise ValueError("'default_week' must be an integer")
    if "image_scale" in payload:
        scale = payload["image_scale"]
        if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
            raise ValueError("'image_scale' must be a positive number")

    return payload


def fetch_remote_config(url: str, timeout: float = REMOTE_FETCH_TIMEOUT) -> dict:
    """Download and validate the config from `url`.

    Raises requests.RequestException on network/HTTP errors and ValueError
    on a non-JSON response (e.g. Drive's sign-in page when the file isn't
    shared publicly) or invalid content.
    """
    response = requests.get(drive_download_url(url), timeout=timeout)
    response.raise_for_status()
    if "text/html" in response.headers.get("Content-Type", ""):
        raise ValueError(
            "got an HTML page instead of JSON — is the file shared as "
            "'Anyone with the link can view'?"
        )
    payload = json.loads(response.content.decode("utf-8-sig"))
    return parse_config(payload)


def load_config(local_config: dict) -> dict:
    """Return the effective config: the remote one when "config_url" is set,
    otherwise (or if the remote one can't be loaded) the local one."""
    remote_url = local_config.get("config_url")
    if not remote_url:
        return parse_config(local_config)

    try:
        config = fetch_remote_config(remote_url)
        logger.info(
            f"Loaded config from {remote_url} "
            f"({len(config['tournaments'])} tournament(s), "
            f"{len(config.get('guild_channels', []))} guild(s))"
        )
        return config
    except (requests.RequestException, ValueError) as remote_error:
        try:
            config = parse_config(local_config)
        except ValueError as local_error:
            raise RuntimeError(
                f"Could not load config from {remote_url} ({remote_error}) "
                f"and the local fallback config is not usable ({local_error})."
            ) from remote_error
        logger.warning(
            f"Could not load config from {remote_url} ({remote_error}); "
            "falling back to the local config."
        )
        return config
