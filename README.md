# tg-qbit-bot

Send a `.torrent` file or magnet link to a private Telegram bot and have it added to your qBittorrent seedbox automatically.

It is a small native Python CLI. No Docker required.

## Features

- Accepts `.torrent` files sent as Telegram documents
- Accepts magnet links sent as plain text
- Restricts access to allowed Telegram user IDs
- Adds torrents through qBittorrent Web API
- Supports qBittorrent category, tags, save path, and paused mode
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
| `QBIT_URL` | yes | | qBittorrent Web UI base URL |
| `QBIT_USER` | yes | | qBittorrent Web UI username |
| `QBIT_PASS` | yes | | qBittorrent Web UI password |
| `QBIT_VERIFY_TLS` | no | `true` | Verify HTTPS certificates for qBittorrent |
| `QBIT_SAVE_PATH` | no | | qBittorrent save path |
| `QBIT_CATEGORY` | no | | qBittorrent category |
| `QBIT_TAGS` | no | | Comma-separated qBittorrent tags |
| `QBIT_PAUSED` | no | `false` | Add torrents paused |
| `MAX_TORRENT_BYTES` | no | `20971520` | Max accepted Telegram file size |
| `STATE_DIR` | no | `~/.local/state/tg-qbit-bot` | Directory for bot state |
| `LOG_LEVEL` | no | `INFO` | Python logging level |

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
