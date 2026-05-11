from __future__ import annotations

import argparse
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


LOGGER = logging.getLogger("tg_qbit_bot")


class BotError(RuntimeError):
    """Expected runtime error that should be reported to the Telegram user."""


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: set[int]
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    qbit_verify_tls: bool
    qbit_save_path: str | None
    qbit_category: str | None
    qbit_tags: str | None
    qbit_paused: bool
    max_torrent_bytes: int
    state_dir: Path
    log_level: str

    @property
    def offset_file(self) -> Path:
        return self.state_dir / "offset"

    @property
    def telegram_api(self) -> str:
        return f"https://api.telegram.org/bot{self.telegram_token}"

    @property
    def telegram_file_api(self) -> str:
        return f"https://api.telegram.org/file/bot{self.telegram_token}"


def getenv_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise BotError(f"{name} is required")
    return value


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def parse_allowed_user_ids(value: str) -> set[int]:
    ids: set[int] = set()
    for raw_id in value.split(","):
        raw_id = raw_id.strip()
        if raw_id:
            try:
                ids.add(int(raw_id))
            except ValueError as error:
                raise BotError(f"Invalid Telegram user ID: {raw_id}") from error
    if not ids:
        raise BotError("TG_ALLOWED_USER_IDS must contain at least one numeric user ID")
    return ids


def optional_env(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def default_state_dir() -> Path:
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "tg-qbit-bot"


def default_config_path() -> Path:
    configured = os.getenv("TG_QBIT_BOT_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "tg-qbit-bot" / "config.env"


def unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: Path) -> None:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError as error:
        raise BotError(f"Config file not found: {path}") from error

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if "=" not in stripped:
            raise BotError(f"Invalid config line {path}:{line_number}")
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            raise BotError(f"Invalid empty config key {path}:{line_number}")
        os.environ.setdefault(key, unquote_env_value(value.strip()))


def load_config() -> Config:
    max_torrent_bytes = os.getenv("MAX_TORRENT_BYTES", str(20 * 1024 * 1024))
    try:
        parsed_max_torrent_bytes = int(max_torrent_bytes)
    except ValueError as error:
        raise BotError("MAX_TORRENT_BYTES must be an integer") from error
    configured_state_dir = optional_env("STATE_DIR")
    state_dir = Path(configured_state_dir).expanduser() if configured_state_dir else default_state_dir()

    return Config(
        telegram_token=getenv_required("TG_BOT_TOKEN"),
        allowed_user_ids=parse_allowed_user_ids(getenv_required("TG_ALLOWED_USER_IDS")),
        qbit_url=getenv_required("QBIT_URL").rstrip("/"),
        qbit_user=getenv_required("QBIT_USER"),
        qbit_pass=getenv_required("QBIT_PASS"),
        qbit_verify_tls=parse_bool(os.getenv("QBIT_VERIFY_TLS"), default=True),
        qbit_save_path=optional_env("QBIT_SAVE_PATH"),
        qbit_category=optional_env("QBIT_CATEGORY"),
        qbit_tags=optional_env("QBIT_TAGS"),
        qbit_paused=parse_bool(os.getenv("QBIT_PAUSED"), default=False),
        max_torrent_bytes=parsed_max_torrent_bytes,
        state_dir=state_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


class TelegramClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def call(self, method: str, **kwargs: Any) -> Any:
        response = requests.post(
            f"{self.config.telegram_api}/{method}",
            timeout=65,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise BotError(f"Telegram {method} failed: {payload}")
        return payload["result"]

    def reply(self, chat_id: int, text: str) -> None:
        try:
            self.call("sendMessage", json={"chat_id": chat_id, "text": text})
        except Exception:
            LOGGER.exception("Could not send Telegram reply")

    def download_file(self, file_id: str, output_path: Path) -> None:
        file_info = self.call("getFile", json={"file_id": file_id})
        file_path = file_info.get("file_path")
        if not file_path:
            raise BotError("Telegram did not return a file path for this upload")

        with requests.get(
            f"{self.config.telegram_file_api}/{file_path}",
            stream=True,
            timeout=60,
        ) as response:
            response.raise_for_status()
            with output_path.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        output.write(chunk)


class QBittorrentClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def add_payload(self) -> dict[str, str]:
        data = {"paused": str(self.config.qbit_paused).lower()}
        if self.config.qbit_save_path:
            data["savepath"] = self.config.qbit_save_path
        if self.config.qbit_category:
            data["category"] = self.config.qbit_category
        if self.config.qbit_tags:
            data["tags"] = self.config.qbit_tags
        return data

    def login(self) -> tuple[requests.Session, dict[str, str]]:
        session = requests.Session()
        headers = {
            "Referer": self.config.qbit_url,
            "Origin": self.config.qbit_url,
        }
        response = session.post(
            f"{self.config.qbit_url}/api/v2/auth/login",
            data={"username": self.config.qbit_user, "password": self.config.qbit_pass},
            headers=headers,
            timeout=20,
            verify=self.config.qbit_verify_tls,
        )
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise BotError(
                f"qBittorrent login failed: HTTP {response.status_code} "
                f"{response.text[:120]}"
            )
        return session, headers

    def check_add_response(self, response: requests.Response) -> None:
        text = response.text.strip()
        if response.status_code == 415:
            raise BotError("qBittorrent rejected this as an invalid torrent file")
        if response.status_code != 200 or text.lower().startswith("fail"):
            raise BotError(f"qBittorrent add failed: HTTP {response.status_code} {text[:120]}")

    def add_file(self, path: Path, original_name: str) -> None:
        session, headers = self.login()
        with path.open("rb") as torrent_file:
            response = session.post(
                f"{self.config.qbit_url}/api/v2/torrents/add",
                data=self.add_payload(),
                files={
                    "torrents": (
                        original_name,
                        torrent_file,
                        "application/x-bittorrent",
                    )
                },
                headers=headers,
                timeout=60,
                verify=self.config.qbit_verify_tls,
            )
        self.check_add_response(response)

    def add_url(self, url: str) -> None:
        session, headers = self.login()
        data = self.add_payload()
        data["urls"] = url
        response = session.post(
            f"{self.config.qbit_url}/api/v2/torrents/add",
            data=data,
            headers=headers,
            timeout=30,
            verify=self.config.qbit_verify_tls,
        )
        self.check_add_response(response)


class TorrentBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.telegram = TelegramClient(config)
        self.qbit = QBittorrentClient(config)

    def load_offset(self) -> int | None:
        try:
            return int(self.config.offset_file.read_text().strip())
        except FileNotFoundError:
            return None
        except ValueError:
            return None

    def save_offset(self, offset: int) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.offset_file.write_text(str(offset))

    def run(self) -> None:
        offset = self.load_offset()
        LOGGER.info("Bot started")

        while True:
            try:
                payload: dict[str, int] = {"timeout": 50}
                if offset is not None:
                    payload["offset"] = offset

                updates = self.telegram.call("getUpdates", json=payload)
                for update in updates:
                    next_offset = update["update_id"] + 1
                    try:
                        self.process_update(update)
                    except Exception:
                        LOGGER.exception("Unhandled update processing failure")
                    finally:
                        offset = next_offset
                        self.save_offset(offset)
            except Exception:
                LOGGER.exception("Polling failed")
                time.sleep(5)

    def process_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        if user_id not in self.config.allowed_user_ids:
            LOGGER.warning("Rejected unauthorized Telegram user id: %s", user_id)
            self.telegram.reply(
                chat_id,
                f"Not authorized. Your Telegram user ID is: {user_id}",
            )
            return

        try:
            if "document" in message:
                self.handle_document(message)
            else:
                self.handle_text(message)
        except BotError as error:
            LOGGER.warning("User-facing error: %s", error)
            self.telegram.reply(chat_id, str(error))
        except Exception:
            LOGGER.exception("Failed to process update")
            self.telegram.reply(chat_id, "Failed to add torrent. Check the bot logs.")

    def handle_document(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        document = message["document"]
        file_name = document.get("file_name") or "upload.torrent"
        mime_type = document.get("mime_type") or ""
        file_size = document.get("file_size") or 0

        if not file_name.lower().endswith(".torrent") and mime_type != "application/x-bittorrent":
            raise BotError("Please send a .torrent file.")

        if file_size and file_size > self.config.max_torrent_bytes:
            raise BotError(
                f"That file is too large. Limit: {self.config.max_torrent_bytes} bytes."
            )

        with tempfile.TemporaryDirectory(prefix="tg-qbit-") as temp_dir:
            torrent_path = Path(temp_dir) / safe_filename(file_name)
            self.telegram.download_file(document["file_id"], torrent_path)
            self.qbit.add_file(torrent_path, torrent_path.name)

        self.telegram.reply(chat_id, f"Added torrent: {file_name}")

    def handle_text(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()

        if text == "/start":
            self.telegram.reply(chat_id, "Send me a .torrent file or a magnet link.")
            return

        if text.startswith("magnet:"):
            self.qbit.add_url(text)
            self.telegram.reply(chat_id, "Added magnet link.")
            return

        raise BotError("Send a .torrent file or a magnet link.")


def safe_filename(name: str) -> str:
    cleaned = Path(name or "upload.torrent").name
    return cleaned if cleaned.lower().endswith(".torrent") else f"{cleaned}.torrent"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward Telegram torrents to qBittorrent.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config env file; defaults to ~/.config/tg-qbit-bot/config.env",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="load configuration and exit without polling Telegram",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = (args.config or default_config_path()).expanduser()
    if args.config or config_path.exists():
        load_env_file(config_path)

    try:
        config = load_config()
    except BotError as error:
        raise SystemExit(f"tg-qbit-bot: {error}") from error

    configure_logging(config.log_level)

    if args.check_config:
        LOGGER.info("Configuration loaded successfully from %s", config_path)
        return

    TorrentBot(config).run()
