# tg-qbit-bot

Send a `.torrent` file or magnet link to a private Telegram bot and have it added to your qBittorrent seedbox automatically.

It is a small native Python CLI. No Docker required.

## Features

- Accepts `.torrent` files sent as Telegram documents
- Accepts magnet links sent as plain text
- Restricts access to allowed Telegram user IDs
- Adds torrents through qBittorrent Web API
- Supports qBittorrent category, tags, save path, and paused mode
- Sends progress updates after new torrents are added
- Shows a live auto-refreshing dashboard with `/status`
- Pauses/resumes torrents by fuzzy-matched name
- Sends disk usage alerts at configurable thresholds
- Sends ratio alerts when torrents hit your target ratio
- Inline buttons for pause, resume, delete, delete data, recheck, and force start
- `/search`, `/stats`, `/top`, `/queue`, `/recent`, `/disk`, `/health`, `/logs`, `/backup`
- Stalled and low-speed torrent alerts
- Completion actions and per-category ratio targets
- Freeleech mode and torrent profiles
- RSS feed watcher with title filters
- Owner/admin/viewer roles and optional approval flow
- Optional webhook mode for reverse-proxy HTTPS deployments
- qBittorrent preference controls for alt speed, speed limits, and queueing
- Persists Telegram update offset so restarts do not replay old uploads
- Can run under a user-level systemd service

## Quick Start

Create a bot with [@BotFather](https://t.me/BotFather), then install from this repo:

```bash
python3 -m venv ~/.local/share/tg-qbit-bot/venv
~/.local/share/tg-qbit-bot/venv/bin/pip install .
mkdir -p ~/.local/bin
ln -sf ~/.local/share/tg-qbit-bot/venv/bin/tg-qbit-bot ~/.local/bin/tg-qbit-bot
```

Create the config file:

```bash
mkdir -p ~/.config/tg-qbit-bot
cp config.example.env ~/.config/tg-qbit-bot/config.env
nano ~/.config/tg-qbit-bot/config.env
```

Set at least these values:

```env
TG_BOT_TOKEN=123456:your_bot_token
TG_ALLOWED_USER_IDS=123456789
QBIT_URL=http://127.0.0.1:8080
QBIT_USER=admin
QBIT_PASS=your_qbit_password
```

Check the config without starting the polling loop:

```bash
tg-qbit-bot --check-config
```

Start the bot:

```bash
tg-qbit-bot
```

Send `/start` to the bot. If your Telegram ID is not allowlisted yet, the bot will reply with the ID to put in `TG_ALLOWED_USER_IDS`.

## Bot Usage

Send a `.torrent` file directly to the bot, or send a magnet link as plain text. New torrents get a live progress card that edits in place instead of spamming the chat.

Commands:

```text
/status
/search breaking bad
/stats
/top
/queue
/recent
/disk
/health
/pause breaking bad
/resume breaking bad
/add tv magnet:?xt=urn:btih:...
/freeleech on
/pref alt
/pref dl 5MiB
/pref ul 1MiB
/pref queue on
```

`/status` sends a clean live dashboard, then refreshes the same Telegram message for the configured live window.

For category routing with torrent files, add a Telegram caption:

```text
category: tv
```

or simply:

```text
tv
```

If `CATEGORY_SAVE_PATHS` maps that category, the bot sends the torrent to the matching save path and sets the qBittorrent category.

Inline buttons appear under torrent cards for direct remote control:

```text
Pause | Resume | Recheck
Force Start | Delete | Delete + Data
```

Profiles let short labels carry routing policy:

```env
TORRENT_PROFILES=tv:category=tv,save_path=/srv/tv,ratio=2.0;movies:category=movies,save_path=/srv/movies,ratio=1.5
```

RSS watcher format:

```env
RSS_FEEDS=anime|https://example.com/rss|1080p|anime;movies|https://example.com/movies.xml||movies
```

## Run As A User Service

This keeps the bot running in the background without a root-owned service.

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/tg-qbit-bot.user.service ~/.config/systemd/user/tg-qbit-bot.service
systemctl --user daemon-reload
systemctl --user enable --now tg-qbit-bot
journalctl --user -u tg-qbit-bot -f
```

If your host stops user services after logout, enable lingering for your account or ask your provider to enable it.

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TG_BOT_TOKEN` | yes | | Token from BotFather |
| `TG_ALLOWED_USER_IDS` | yes | | Comma-separated Telegram user IDs |
| `OWNER_USER_IDS` | no | `TG_ALLOWED_USER_IDS` | Users allowed to approve, view logs, export config |
| `ADMIN_USER_IDS` | no | `TG_ALLOWED_USER_IDS` | Users allowed to control qBittorrent |
| `VIEWER_USER_IDS` | no | | Users allowed to view dashboards |
| `APPROVAL_ENABLED` | no | `false` | Let non-admin submissions wait for owner approval |
| `QBIT_URL` | yes | | qBittorrent Web UI base URL |
| `QBIT_USER` | yes | | qBittorrent Web UI username |
| `QBIT_PASS` | yes | | qBittorrent Web UI password |
| `QBIT_VERIFY_TLS` | no | `true` | Verify HTTPS certificates for qBittorrent |
| `QBIT_SAVE_PATH` | no | | qBittorrent save path |
| `QBIT_CATEGORY` | no | | qBittorrent category |
| `QBIT_TAGS` | no | | Comma-separated qBittorrent tags |
| `QBIT_PAUSED` | no | `false` | Add torrents paused |
| `CATEGORY_SAVE_PATHS` | no | | Comma-separated mappings like `tv=/srv/tv,movies=/srv/movies` |
| `CATEGORY_AS_TAG` | no | `false` | Also add the selected category as a qBittorrent tag |
| `TORRENT_PROFILES` | no | | Label profiles for category, save path, tags, ratio |
| `PER_CATEGORY_RATIO_TARGETS` | no | | Mappings like `movies=2.0,private=3.0` |
| `FREELEECH_TAGS` | no | `freeleech` | Extra tags when `/freeleech on` is active |
| `FREELEECH_CATEGORY` | no | | Optional category override in freeleech mode |
| `MAX_TORRENT_BYTES` | no | `20971520` | Max accepted Telegram file size |
| `PROGRESS_UPDATE_INTERVAL_SECONDS` | no | `180` | How often to DM progress for newly added torrents |
| `PROGRESS_UPDATE_MAX_HOURS` | no | `24` | Stop progress tracking after this many hours |
| `STATUS_LIMIT` | no | `15` | Max torrents shown by `/status` |
| `STATUS_LIVE_ENABLED` | no | `true` | Edit `/status` in place as a live dashboard |
| `STATUS_REFRESH_SECONDS` | no | `30` | Live dashboard refresh interval |
| `STATUS_LIVE_DURATION_SECONDS` | no | `600` | How long each live dashboard keeps refreshing |
| `FUZZY_MATCH_MIN_SCORE` | no | `0.35` | Minimum name-match score for `/pause` and `/resume` |
| `FUZZY_MATCH_LIMIT` | no | `5` | Max fuzzy matches to pause/resume |
| `DISK_WATCH_ENABLED` | no | `true` | Enable disk space alerts |
| `DISK_WATCH_PATH` | no | `QBIT_SAVE_PATH` or `/` | Path to check for disk usage |
| `DISK_WATCH_THRESHOLDS` | no | `80,90,95` | Percent thresholds that trigger DMs |
| `DISK_WATCH_INTERVAL_SECONDS` | no | `300` | Disk check interval |
| `RATIO_ALERT_TARGET` | no | `1.0` | DM when a torrent ratio reaches this value; set `0` to disable |
| `RATIO_ALERT_INTERVAL_SECONDS` | no | `300` | Ratio check interval |
| `RATIO_ACTION` | no | `notify` | `notify`, `pause`, `delete`, `delete_data`, or `category` |
| `STALLED_ALERT_ENABLED` | no | `true` | Alert when torrents stop progressing |
| `STALLED_ALERT_MINUTES` | no | `30` | Stalled threshold |
| `LOW_SPEED_ALERT_ENABLED` | no | `true` | Alert when active torrents stay below a speed |
| `LOW_SPEED_THRESHOLD_BYTES` | no | `51200` | Low-speed threshold |
| `LOW_SPEED_MINUTES` | no | `30` | Low-speed duration threshold |
| `COMPLETION_ACTION` | no | `notify` | Action when tracked torrents complete |
| `RSS_FEEDS` | no | | Feed specs: `name|url|filter|category` separated by `;` |
| `WEBHOOK_ENABLED` | no | `false` | Run webhook server instead of long polling |
| `WEBHOOK_URL` | no | | Public HTTPS URL for Telegram webhook |
| `STATE_DIR` | no | `~/.local/state/tg-qbit-bot` | Directory for bot state |
| `LOG_LEVEL` | no | `INFO` | Python logging level |

## Updating On A VPS

```bash
cd ~/tg-qbit-bot
git pull
~/.local/share/tg-qbit-bot/venv/bin/pip install .
systemctl --user restart tg-qbit-bot
```

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp config.example.env config.env
tg-qbit-bot --config config.env
```

Run checks:

```bash
python -m compileall src
ruff check .
```

## More Install Notes

See [docs/systemd.md](docs/systemd.md) for a slightly more detailed native install guide.

## Security

- Keep the bot private and always use `TG_ALLOWED_USER_IDS`.
- Prefer running qBittorrent Web UI on a private network, VPN, or localhost.
- Do not commit `.env`; it contains credentials.
- Use HTTPS with valid certificates if qBittorrent is reachable over the public internet.

## Legal

This project only automates sending torrent metadata to your qBittorrent client. Use it only for content you have the right to download or share.
