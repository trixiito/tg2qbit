# User systemd install

This runs the bot as your normal Linux user. It does not require a root-owned service.

```bash
python3 -m venv ~/.local/share/tg-qbit-bot/venv
~/.local/share/tg-qbit-bot/venv/bin/pip install .

mkdir -p ~/.local/bin
ln -sf ~/.local/share/tg-qbit-bot/venv/bin/tg-qbit-bot ~/.local/bin/tg-qbit-bot

mkdir -p ~/.config/tg-qbit-bot
cp config.example.env ~/.config/tg-qbit-bot/config.env
nano ~/.config/tg-qbit-bot/config.env

mkdir -p ~/.config/systemd/user
cp packaging/systemd/tg-qbit-bot.user.service ~/.config/systemd/user/tg-qbit-bot.service

systemctl --user daemon-reload
systemctl --user enable --now tg-qbit-bot
journalctl --user -u tg-qbit-bot -f
```

Some servers stop user services when the user logs out. If that happens, enable linger for your account if your provider allows it:

```bash
loginctl enable-linger "$USER"
```
