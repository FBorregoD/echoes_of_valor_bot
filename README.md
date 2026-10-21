# Echoes of Valor Bot

A Discord bot for running multi-tournament, division-based competitions off Google Sheets. It reads match schedules, standings, and hero-build data directly from public Google Sheets (no Google API credentials needed) and renders them as Discord commands — either as images or as plain text in DMs — plus a scheduler for recurring posts and notifications.

Built for the **Realms Rising** Discord server's communities, currently running the *Echoes of Valor* and *Meridian Ascension* tournaments. The tournament list and aliases are driven by `config.json`, but the bot is **not** a drop-in fit for arbitrary spreadsheets — it requires the source Google Sheet to follow a very specific layout (a `SCHEDULE` marker row, `Player - Hero` formatted match cells, a particular hero-builds sheet column layout, etc. — see [Google Sheet format](#google-sheet-format) below). Adapting it to a different tournament's sheets means matching that layout, not just changing config values.

## Features

- **Match lookups** (`!matches`, `!division`, `!standings`) rendered as PNG images, with automatic fallback to plain text if rendering fails or in DMs.
- **Automatic week resolution** — commands figure out the "current" week on their own from whatever scheduled posting task exists for a tournament, no manual week argument needed.
- **Player subscriptions** (`!subscribe`) — any user can DM the bot to get a player's matches pushed to them automatically on a schedule, without needing to be an admin.
- **A persistent scheduler** (SQLite-backed) for recurring tasks: posting matchups/standings to threads, DMing players with pending matches, reporting misreported scores, and notifying subscribers — each on its own weekly or interval schedule, in any timezone.
- **Per-guild configuration**: multiple Discord servers can run against the same bot, each with its own command prefix and channel-to-tournament mapping.

## Commands

Admin status is controlled by `admin_user_ids` in the config file. `!help` (or `!help <command>`) shows this from inside Discord, always up to date with the code.

**Where commands are accepted:** regular users talk to the bot **by DM**. In a server, a message is only treated as a command if it comes from a bot admin **and** is posted in a channel listed in `guild_channels` (or a thread inside one); everything else is ignored silently — no reply, not logged, not counted in `!usage_report`. Messages from other bots are always ignored, and an unknown command inside a server (e.g. `!roll`, meant for another bot that shares the `!` prefix) gets no reply either. In DMs an unknown command still answers "Command unknown".

### Public

| Command | Description |
|---|---|
| `!matches` / `!m <player> [text]` | A player's matches, as an image (add `text` for plain text — DMs only). |
| `!division` / `!d [division] [week] [text]` | A division's matchups for a given week. |
| `!standings` / `!c [tournament] [division] [text]` | A division's current standings table. |
| `!tournaments` | List configured tournaments and their aliases. |
| `!subscribe` / `!sub <player> [text]` | **DM-only.** Subscribe to a player — get DM'd their matches automatically when an admin's `notify_subscribed_players` task fires. One active subscription per user. |
| `!unsubscribe` / `!unsub` | **DM-only.** Remove your subscription. |

### Admin — tournament

| Command | Description |
|---|---|
| `!sendto <player> [week]` | DM a specific player their matches. |
| `!notify_all [week]` | DM every player with pending matches. |
| `!post_divisions [week] [tournament]` | Post matchups to every division thread. |
| `!post_standings [tournament]` | Post standings to every division thread. |
| `!refresh` | Clear and reload all cached Google Sheets data. |
| `!show_sub` | **DM-only.** List every active `!subscribe` subscription (`Username → Player`). |
| `!status` | **DM-only.** Bot health: uptime, last disconnect, config source (Drive or local fallback), scheduled tasks, subscriptions, commands since start. |
| `!usage_report` / `!us [days\|all] [others]` | **DM-only.** Command usage over the last N days (default 7; `others` leaves out your own commands): per command: uses and average time from the cache vs downloading the sheet (with the difference), unused commands, top users, DM vs server, failures. |

### Admin — scheduler

| Command | Description |
|---|---|
| `!schedule add <action> <when> [options]` | Create a recurring task. |
| `!schedule list` | List scheduled tasks for the current server. |
| `!schedule listall` | List scheduled tasks for every server (works in DMs). |
| `!schedule remove <id> \| all \| tournament=<alias>` | Delete one task, every task on this server, or every task for a tournament. `remove <id>` also works in DMs, for a task on any server (IDs from `!schedule listall`). |
| `!schedule info <id>` | Show a task's details. |
| `!schedule actions` | List available scheduled actions and their parameters. |

`<when>` is either a weekday + time (`monday 09:00 tz=Europe/Madrid`) or an interval (`every=2h`, `every=30m`, `every=1d`). Available actions: `post_divisions`, `notify_all`, `standings`, `report_misreported`, `notify_subscribed_players`, `usage_report` (DMs the `!usage_report` of the last `days=N` days to the alert list — never posted in the channel).

```
!schedule add post_divisions monday 09:00 tz=Europe/Madrid tournament=MA week=4
!schedule add notify_subscribed_players every=6h
!schedule remove tournament=EoV
```

### Admin — debug

| Command | Description |
|---|---|
| `!context_debug` | Show how the bot resolved the current channel (tournament/division/neutral). |
| `!test_map` | Print the full player → Discord ID mapping. |
| `!test_id <player>` | Look up one player's Discord ID. |
| `!dmtest <user_id> <message>` | Send a raw test DM. |

## Setup

**Requirements:** Python 3.11+, a Discord bot application/token, and public Google Sheets for the tournament data (view access, no API key needed — the bot reads them as CSV exports).

```bash
git clone <this repo>
cd echoes_of_valor_bot
pip install -r requirements.txt
```

Provide a bot token (see [Environment variables](#environment-variables)) and a `config.json` (see below), then:

```bash
python main.py
```

## Configuration (`config.json`)

```jsonc
{
  "config_url": "https://drive.google.com/file/d/<id>/view?usp=sharing",
  "tournaments": [
    {
      "name": "Echoes of Valor",
      "alias": "EoV",
      "url": "https://docs.google.com/spreadsheets/d/<id>",
      "builds_sheet": "hero builds Season 10",
      "builds_mapping": { "player_col": 3, "ancestry_col": 4, "class_col": 5 },
      "season": 10
    }
  ],
  "mapping_sheet_url": "https://docs.google.com/spreadsheets/d/<id>",
  "default_week": 2,
  "command_prefix": "!",
  "admin_user_ids": [123456789012345678],
  "alert_user_ids": [123456789012345678],
  "image_scale": 1.0,
  "guild_channels": [
    {
      "guild_id": 111111111111111111,
      "prefix": "! ",
      "channels": [
        { "channel_id": 222222222222222222, "channel_name": "eov-general", "tournament": "EoV" },
        { "channel_id": 333333333333333333, "channel_name": "bot-admin", "tournament": null }
      ]
    }
  ]
}
```

- **`config_url`** *(optional)* — a JSON file (e.g. on Google Drive, shared as "Anyone with the link can view") holding the **full** configuration, with exactly the same structure as this file. When set, the bot loads it **at startup** and uses it *instead of* everything else in the local `config.json` (nothing is merged) — so tournaments, channels, admins, prefixes, etc. can all be changed without a code push, just a bot restart. If the download fails or the remote file is invalid (missing tournament keys, `admin_user_ids` written as text instead of numbers, empty prefix, ...), the bot logs a warning with the reason and falls back to the rest of the local `config.json`; if that isn't a usable config either, it refuses to start. Keep the local copy reasonably in sync so the fallback is sensible.
- **`tournaments`** — one entry per tournament (each needs `name`, `alias`, `url`, `builds_sheet`, `builds_mapping`, `season`). `url` is the tournament's Google Sheet (division sheets are auto-detected by looking for a `SCHEDULE` marker). `builds_sheet`/`builds_mapping` point at the hero-builds/roster sheet used for build lookups and to validate `!subscribe`.
- **`mapping_sheet_url`** — a separate sheet of `player name → Discord ID`, used only by `!sendto`/`!notify_all`/`!test_map`/`!test_id` to DM a specific player. Not the tournament roster (that's the builds sheet above).
- **`alert_user_ids`** *(optional)* — Discord user IDs that receive the scheduled `usage_report` DM. Defaults to `admin_user_ids` when missing.
- **`guild_channels`** — per-server channel-to-tournament binding and an optional per-server prefix override (falls back to the top-level `command_prefix`). A channel with `"tournament": null` is treated as a neutral/admin channel.

## Google Sheet format

The bot doesn't use the Google Sheets API — it fetches sheets as public CSV exports (via `Googlexcel-noPassword`), so **the tournament spreadsheet must be shared as "Anyone with the link can view"**. That also means it can only parse a specific, pre-agreed layout — there's no generic spreadsheet importer here.

**Division sheets** (one tab per division, e.g. `DIAMOND`, `GOLD`) are auto-detected by scanning every tab for this shape (`is_division_sheet` in `match_utils.py`); any tab that doesn't match is ignored (meta tabs like "Format", "Leagues overview", etc. are safely skipped this way):

- Column A contains a cell reading exactly `SCHEDULE`, followed further down by cells reading `Week 1`, `Week 2`, etc.
- Below each `Week N` marker, match rows follow this column layout:

  | Col A | Col B | Col C (player1) | Col D (player2) | Col E (score1) | Col F (score2) | Col G (check) |
  |---|---|---|---|---|---|---|
  | *(week marker)* | Round label | `PlayerName - HeroName` | `PlayerName - HeroName` | number | number | *(ignored by the bot)* |

  Both player cells **must** contain a literal `" - "` separating the player name from their chosen hero — this is how the bot tells a real match row from a header row, and how it later extracts just the player name for `!m`/`!subscribe` lookups.

  A match's state is decided **only from the two score cells** (`read_match_result` in `match_utils.py`) — the check column is edited by hand and isn't trusted. Matches are best-of-two: **finished** = both scores in 0/1/2 adding up to 2 (`2-0`, `0-2`, `1-1`); **not started** = `0-0` or both cells empty; **misreported** = anything else (e.g. `2-2`, `1-0`, only one cell filled, text instead of a number). Past-week matches that aren't finished show up as pending (or misreported).

**Hero-builds / roster sheet** (one per tournament, named e.g. `hero builds Season 10`, referenced by `builds_sheet` in config): a header row containing "tier" or "player" (auto-detected, or matched via the `builds_mapping` column names/indices), then one row per registered player with their `Player - Hero` name in `player_col` and their ancestry/class in `ancestry_col`/`class_col`. This sheet is the actual tournament roster — it's what `!subscribe` validates player names against, independent of whether that player has any matches yet.

**Player mapping sheet** (`mapping_sheet_url`, separate from the tournaments above): two columns, player name and their Discord user ID, used only for `!sendto`/`!notify_all`/`!test_map`/`!test_id`. Rows with no ID in the second column are skipped — this sheet is a much smaller list than the full roster (only players who've bothered to link a Discord ID), so it's deliberately *not* used to validate `!subscribe`.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes* | The bot's Discord token. |
| `CONFIG_FILE` | No | Path to the config file to load. Defaults to `config.json`. |
| `PREFIX_OVERRIDE` | No | **Local dev bot only — never set on the production host.** The dev and production bots share the test servers and load the same config, so both would answer the same prefix there. With e.g. `PREFIX_OVERRIDE=dev` this bot answers `dev ...` in every server instead (a trailing space is added to word prefixes); DMs keep the normal prefix. A warning is logged at startup when it's active. |
| `LOG_LEVEL` | No | Logging level (`DEBUG`, `INFO`, `WARNING`, ...). Defaults to `INFO`; `DEBUG` adds per-step detail (week resolution, image rendering, HTTP requests). |
| `RAILWAY_VOLUME_MOUNT_PATH` | No | Auto-set by Railway once a persistent volume is attached to the service. When present, `tasks.db` (the scheduler/subscriptions database) is stored there instead of the working directory, so it survives redeploys. Unset locally — `tasks.db` just lives in the repo root. |

\* If `DISCORD_BOT_TOKEN` isn't set, the bot falls back to reading a token from `var/env.txt` (gitignored) — convenient for local dev, but the env var is what production (Railway) uses.

## Monitoring commands

Every command a user runs is logged by the `audit` logger (`command_audit.py`): one line when it starts (who, where — `DM` or `Server/#channel` — and the message) and one when it finishes or fails, with its duration. Ordinary chat messages are not logged. With Docker:

```bash
docker logs -f echoes-of-valor 2>&1 | grep audit
```

```
[INFO] audit: fulano (123...) in DM: '!m scorium'
[INFO] audit: fulano !matches done in 2.4s
[INFO] audit: fulano '!mm scorium' failed (CommandNotFound) in DM
```

Each of those commands is also stored in the `command_usage` table of `tasks.db` (time, user, command, DM or server, success/error, duration, and how many Google Sheets downloads it needed — 0 means it was served from the 5-minute cache). Admins can read it from Discord, by DM:

- `!status` — uptime, latency and last disconnect, where the config came from, scheduled tasks, subscriptions, commands since start.
- `!usage_report` / `!us [days|all] [others]` — `others` leaves out your own commands; which commands are used and which never are, by whom, how fast (cache vs download), failures and mistyped commands.

`!schedule add usage_report monday 09:00 tz=Europe/Madrid days=7 others=yes` sends the report automatically each week to the users in `alert_user_ids` (falls back to `admin_user_ids`); with `others=yes` each of them gets it without their own commands.

## Running tests

```bash
python -m pytest tests/ -v
```

Tests mock Discord and Google Sheets entirely — no token or network access needed. A few database tests spin up a throwaway temp SQLite file rather than touching `tasks.db`.

**Season simulation** (needs network, no Discord): `python tools/simulate_season.py [--player Tuff] [--save-images DIR]` runs the real auto-advancing `post_divisions` scheduler task week by week against a real, completed tournament sheet (EoV season 10 by default), hiding results that "haven't been played yet" and reporting a few late so pending matches appear. After each fire it prints what was posted, the task's `current_week`, and what `!m <player>` would show — useful to check the season-end transition without waiting real weeks.

## Deployment

The bot ships as a single worker process (`Procfile`: `worker: python main.py`) and includes a `Dockerfile` for container-based hosting (currently deployed on Railway). For the scheduler/subscriptions database to survive redeploys, attach a persistent volume to the service — see the `RAILWAY_VOLUME_MOUNT_PATH` note above.

## Project layout

| File | Responsibility |
|---|---|
| `main.py` | Bot startup, config/token loading, prefix resolution, global error handling. |
| `commands.py` | Public + admin tournament commands (`!matches`, `!subscribe`, `!sendto`, ...). |
| `schedule_commands.py` | `!schedule` admin commands. |
| `scheduler.py` | Persistent task scheduler (SQLite), subscriptions table, and the actions each scheduled task can run. |
| `tournament_actions.py` | Shared logic (posting matchups/standings, DMing players) used by both direct commands and scheduled tasks. |
| `match_utils.py` | Google Sheets fetching/caching/parsing: matches, standings, hero builds, player-name matching. |
| `channel_context.py` | Resolves a channel/thread to its tournament/division and per-guild prefix. |
| `image_render.py` | PNG rendering for matchups/standings/player-matches. |
| `help.py` | `!help` embed content. |
| `tests/` | Pytest suite (mocked Discord/Sheets; no live credentials needed). |
