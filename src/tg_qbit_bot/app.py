from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
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
    disk_watch_enabled: bool
    disk_watch_path: Path
    disk_watch_thresholds: list[int]
    disk_watch_interval_seconds: int
    progress_update_interval_seconds: int
    progress_update_max_hours: int
    ratio_alert_target: float | None
    ratio_alert_interval_seconds: int
    status_limit: int
    fuzzy_match_min_score: float
    fuzzy_match_limit: int
    category_save_paths: dict[str, str]
    category_as_tag: bool

    @property
    def offset_file(self) -> Path:
        return self.state_dir / "offset"

    @property
    def disk_alerts_file(self) -> Path:
        return self.state_dir / "disk-alerts.json"

    @property
    def ratio_alerts_file(self) -> Path:
        return self.state_dir / "ratio-alerts.json"

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


def parse_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as error:
        raise BotError(f"{name} must be an integer") from error


def parse_float_env(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as error:
        raise BotError(f"{name} must be a number") from error


def parse_thresholds(value: str | None) -> list[int]:
    if not value:
        return [80, 90, 95]

    thresholds: list[int] = []
    for raw_threshold in value.split(","):
        raw_threshold = raw_threshold.strip()
        if not raw_threshold:
            continue
        try:
            threshold = int(raw_threshold)
        except ValueError as error:
            raise BotError(f"Invalid disk threshold: {raw_threshold}") from error
        if threshold <= 0 or threshold > 100:
            raise BotError("Disk thresholds must be between 1 and 100")
        thresholds.append(threshold)

    return sorted(set(thresholds)) or [80, 90, 95]


def parse_mapping(value: str | None) -> dict[str, str]:
    if not value:
        return {}

    mapping: dict[str, str] = {}
    for raw_pair in value.split(","):
        raw_pair = raw_pair.strip()
        if not raw_pair:
            continue
        if "=" not in raw_pair:
            raise BotError(f"Invalid category save path mapping: {raw_pair}")
        key, mapped_value = raw_pair.split("=", 1)
        key = key.strip()
        mapped_value = mapped_value.strip()
        if not key or not mapped_value:
            raise BotError(f"Invalid category save path mapping: {raw_pair}")
        mapping[key.lower()] = mapped_value
    return mapping


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
    config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "tg-qbit-bot" / "config.env"


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
    configured_state_dir = optional_env("STATE_DIR")
    if configured_state_dir:
        state_dir = Path(configured_state_dir).expanduser()
    else:
        state_dir = default_state_dir()
    disk_watch_path = optional_env("DISK_WATCH_PATH") or optional_env("QBIT_SAVE_PATH") or "/"

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
        max_torrent_bytes=parse_int_env("MAX_TORRENT_BYTES", 20 * 1024 * 1024),
        state_dir=state_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        disk_watch_enabled=parse_bool(os.getenv("DISK_WATCH_ENABLED"), default=True),
        disk_watch_path=Path(disk_watch_path).expanduser(),
        disk_watch_thresholds=parse_thresholds(os.getenv("DISK_WATCH_THRESHOLDS")),
        disk_watch_interval_seconds=parse_int_env("DISK_WATCH_INTERVAL_SECONDS", 300),
        progress_update_interval_seconds=parse_int_env("PROGRESS_UPDATE_INTERVAL_SECONDS", 180),
        progress_update_max_hours=parse_int_env("PROGRESS_UPDATE_MAX_HOURS", 24),
        ratio_alert_target=parse_float_env("RATIO_ALERT_TARGET", 1.0),
        ratio_alert_interval_seconds=parse_int_env("RATIO_ALERT_INTERVAL_SECONDS", 300),
        status_limit=parse_int_env("STATUS_LIMIT", 15),
        fuzzy_match_min_score=parse_float_env("FUZZY_MATCH_MIN_SCORE", 0.35) or 0.35,
        fuzzy_match_limit=parse_int_env("FUZZY_MATCH_LIMIT", 5),
        category_save_paths=parse_mapping(os.getenv("CATEGORY_SAVE_PATHS")),
        category_as_tag=parse_bool(os.getenv("CATEGORY_AS_TAG"), default=False),
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

    def notify_allowed_users(self, user_ids: set[int], text: str) -> None:
        for user_id in user_ids:
            self.reply(user_id, text)

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

    def add_payload(
        self,
        category: str | None = None,
        save_path: str | None = None,
        tags: str | None = None,
    ) -> dict[str, str]:
        data = {"paused": str(self.config.qbit_paused).lower()}
        final_save_path = save_path or self.config.qbit_save_path
        final_category = category or self.config.qbit_category
        final_tags = tags or self.config.qbit_tags

        if final_save_path:
            data["savepath"] = final_save_path
        if final_category:
            data["category"] = final_category
        if final_tags:
            data["tags"] = final_tags
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

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, str | int] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, Any, str]] | None = None,
        timeout: int = 30,
    ) -> requests.Response:
        session, headers = self.login()
        url = f"{self.config.qbit_url}/api/v2/{endpoint.lstrip('/')}"
        if method == "GET":
            response = session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                verify=self.config.qbit_verify_tls,
            )
        else:
            response = session.post(
                url,
                params=params,
                data=data,
                files=files,
                headers=headers,
                timeout=timeout,
                verify=self.config.qbit_verify_tls,
            )
        return response

    def check_add_response(self, response: requests.Response) -> None:
        text = response.text.strip()
        if response.status_code == 415:
            raise BotError("qBittorrent rejected this as an invalid torrent file")
        if response.status_code != 200 or text.lower().startswith("fail"):
            raise BotError(f"qBittorrent add failed: HTTP {response.status_code} {text[:120]}")

    def add_file(
        self,
        path: Path,
        original_name: str,
        *,
        category: str | None = None,
        save_path: str | None = None,
        tags: str | None = None,
    ) -> None:
        session, headers = self.login()
        with path.open("rb") as torrent_file:
            response = session.post(
                f"{self.config.qbit_url}/api/v2/torrents/add",
                data=self.add_payload(category=category, save_path=save_path, tags=tags),
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

    def add_url(
        self,
        url: str,
        *,
        category: str | None = None,
        save_path: str | None = None,
        tags: str | None = None,
    ) -> None:
        session, headers = self.login()
        data = self.add_payload(category=category, save_path=save_path, tags=tags)
        data["urls"] = url
        response = session.post(
            f"{self.config.qbit_url}/api/v2/torrents/add",
            data=data,
            headers=headers,
            timeout=30,
            verify=self.config.qbit_verify_tls,
        )
        self.check_add_response(response)

    def torrents_info(
        self,
        *,
        status_filter: str | None = None,
        hashes: list[str] | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if status_filter:
            params["filter"] = status_filter
        if hashes:
            params["hashes"] = "|".join(hashes)
        if sort:
            params["sort"] = sort

        response = self.request("GET", "torrents/info", params=params)
        response.raise_for_status()
        return response.json()

    def torrent_hashes(self) -> set[str]:
        return {torrent["hash"] for torrent in self.torrents_info() if torrent.get("hash")}

    def wait_for_new_torrents(
        self,
        previous_hashes: set[str],
        *,
        timeout_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            torrents = self.torrents_info()
            new_torrents = [
                torrent
                for torrent in torrents
                if torrent.get("hash") and torrent["hash"] not in previous_hashes
            ]
            if new_torrents:
                return new_torrents
            time.sleep(2)
        return []

    def control_torrents(self, action: str, hashes: list[str]) -> None:
        joined_hashes = "|".join(hashes)
        endpoints = {
            "pause": ["torrents/pause", "torrents/stop"],
            "resume": ["torrents/resume", "torrents/start"],
        }[action]

        last_response: requests.Response | None = None
        for endpoint in endpoints:
            response = self.request(
                "POST",
                endpoint,
                data={"hashes": joined_hashes},
                timeout=20,
            )
            if response.status_code == 404:
                last_response = response
                continue
            response.raise_for_status()
            return

        if last_response is not None:
            last_response.raise_for_status()


@dataclass
class TrackedTorrent:
    torrent_hash: str
    chat_id: int
    name: str
    added_at: float
    last_sent_at: float = 0


def read_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring invalid JSON state file: %s", path)
        return default


def write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True))


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "0 B"
    size = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def format_speed(value: int | float | None) -> str:
    return f"{format_bytes(value)}/s"


def format_eta(seconds: int | float | None) -> str:
    if seconds is None or seconds < 0 or seconds >= 8_640_000:
        return "unknown"

    seconds = int(seconds)
    if seconds == 0:
        return "done"

    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, _ = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


def format_percent(value: int | float | None) -> str:
    if value is None:
        return "0.0%"
    return f"{float(value) * 100:.1f}%"


def format_ratio(value: int | float | None) -> str:
    if value is None:
        return "0.00"
    return f"{float(value):.2f}"


def torrent_line(torrent: dict[str, Any]) -> str:
    name = torrent.get("name") or "Unnamed torrent"
    progress = format_percent(torrent.get("progress"))
    dlspeed = format_speed(torrent.get("dlspeed"))
    upspeed = format_speed(torrent.get("upspeed"))
    eta = format_eta(torrent.get("eta"))
    ratio = format_ratio(torrent.get("ratio"))
    state = torrent.get("state") or "unknown"
    details = f"{progress} | down {dlspeed} up {upspeed} | ETA {eta}"
    return f"{name}\n  {details} | ratio {ratio} | {state}"


def is_active_torrent(torrent: dict[str, Any]) -> bool:
    state = str(torrent.get("state") or "").lower()
    if "paused" in state or "stopped" in state:
        return False
    return bool(torrent.get("dlspeed") or torrent.get("upspeed") or torrent.get("progress", 0) < 1)


def category_from_caption(caption: str | None) -> str | None:
    if not caption:
        return None

    text = caption.strip()
    lower_text = text.lower()
    for prefix in ("category:", "cat:"):
        if lower_text.startswith(prefix):
            category = text[len(prefix) :].strip()
            return category or None

    if lower_text.startswith("/add"):
        category = text[4:].strip()
        return category or None

    return text or None


def parse_command(text: str) -> tuple[str | None, str]:
    if not text.startswith("/"):
        return None, ""
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return command, args


def parse_add_command(text: str) -> tuple[str | None, str]:
    parts = text.split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    magnet_index = rest.find("magnet:")
    if magnet_index < 0:
        raise BotError("Usage: /add category magnet-link")

    category = rest[:magnet_index].strip() or None
    magnet = rest[magnet_index:].strip()
    if not magnet:
        raise BotError("Usage: /add category magnet-link")
    return category, magnet


def normalize_category(category: str | None) -> str | None:
    if not category:
        return None
    cleaned = " ".join(category.strip().split())
    return cleaned or None


def combined_tags(base_tags: str | None, category: str | None, category_as_tag: bool) -> str | None:
    tags = [tag.strip() for tag in (base_tags or "").split(",") if tag.strip()]
    if category_as_tag and category:
        tags.append(category)
    seen: set[str] = set()
    unique_tags: list[str] = []
    for tag in tags:
        lowered = tag.lower()
        if lowered not in seen:
            seen.add(lowered)
            unique_tags.append(tag)
    return ",".join(unique_tags) or None


def fuzzy_score(query: str, candidate: str) -> float:
    query = query.lower()
    candidate = candidate.lower()
    if query in candidate:
        return 1.0
    return SequenceMatcher(None, query, candidate).ratio()


def help_text() -> str:
    return "\n".join(
        [
            "Send a .torrent file or magnet link to add it to qBittorrent.",
            "",
            "Commands:",
            "/status - show active torrents",
            "/pause name - fuzzy-match and pause torrents",
            "/resume name - fuzzy-match and resume torrents",
            "/add category magnet-link - add a magnet under a category",
            "",
            "Torrent file captions:",
            "category: tv",
            "movies",
        ]
    )


class TorrentBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.telegram = TelegramClient(config)
        self.qbit = QBittorrentClient(config)
        self.stop_event = threading.Event()
        self.tracked_torrents: dict[str, TrackedTorrent] = {}
        self.tracked_lock = threading.Lock()

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
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.start_background_threads()
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

    def start_background_threads(self) -> None:
        threads = [
            ("progress-watch", self.progress_watch_loop),
            ("ratio-alerts", self.ratio_alert_loop),
        ]
        if self.config.disk_watch_enabled:
            threads.append(("disk-watch", self.disk_watch_loop))

        for name, target in threads:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()

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
        category = normalize_category(category_from_caption(message.get("caption")))

        if not file_name.lower().endswith(".torrent") and mime_type != "application/x-bittorrent":
            raise BotError("Please send a .torrent file.")

        if file_size and file_size > self.config.max_torrent_bytes:
            raise BotError(
                f"That file is too large. Limit: {self.config.max_torrent_bytes} bytes."
            )

        with tempfile.TemporaryDirectory(prefix="tg-qbit-") as temp_dir:
            torrent_path = Path(temp_dir) / safe_filename(file_name)
            self.telegram.download_file(document["file_id"], torrent_path)
            previous_hashes = self.qbit.torrent_hashes()
            add_category, save_path, tags = self.resolve_add_options(category)
            self.qbit.add_file(
                torrent_path,
                torrent_path.name,
                category=add_category,
                save_path=save_path,
                tags=tags,
            )
            new_torrents = self.qbit.wait_for_new_torrents(previous_hashes)

        self.announce_added_torrents(chat_id, file_name, new_torrents, category)

    def handle_text(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        command, args = parse_command(text)

        if command in {"/start", "/help"}:
            self.telegram.reply(chat_id, help_text())
            return

        if command == "/status":
            self.handle_status(chat_id)
            return

        if command == "/pause":
            self.handle_control(chat_id, "pause", args)
            return

        if command == "/resume":
            self.handle_control(chat_id, "resume", args)
            return

        if command == "/add":
            category, magnet = parse_add_command(text)
            self.add_magnet(chat_id, magnet, category)
            return

        if text.startswith("magnet:"):
            self.add_magnet(chat_id, text, None)
            return

        raise BotError("Send a .torrent file or a magnet link.")

    def resolve_add_options(
        self,
        category: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        category = normalize_category(category)
        save_path = None
        if category:
            save_path = self.config.category_save_paths.get(category.lower())
        tags = combined_tags(self.config.qbit_tags, category, self.config.category_as_tag)
        return category, save_path, tags

    def add_magnet(self, chat_id: int, magnet: str, category: str | None) -> None:
        category = normalize_category(category)
        previous_hashes = self.qbit.torrent_hashes()
        add_category, save_path, tags = self.resolve_add_options(category)
        self.qbit.add_url(magnet, category=add_category, save_path=save_path, tags=tags)
        new_torrents = self.qbit.wait_for_new_torrents(previous_hashes)
        self.announce_added_torrents(chat_id, "magnet link", new_torrents, category)

    def announce_added_torrents(
        self,
        chat_id: int,
        label: str,
        torrents: list[dict[str, Any]],
        category: str | None,
    ) -> None:
        category_text = f" in category {category}" if category else ""
        if not torrents:
            self.telegram.reply(
                chat_id,
                f"qBittorrent accepted {label}{category_text}, but I could not identify "
                "a new torrent hash. Use /status to check it.",
            )
            return

        now = time.time()
        lines = [f"Added {len(torrents)} torrent(s){category_text}:"]
        for torrent in torrents:
            torrent_hash = torrent.get("hash")
            name = torrent.get("name") or label
            if torrent_hash:
                with self.tracked_lock:
                    self.tracked_torrents[torrent_hash] = TrackedTorrent(
                        torrent_hash=torrent_hash,
                        chat_id=chat_id,
                        name=name,
                        added_at=now,
                        last_sent_at=now,
                    )
            lines.append(torrent_line(torrent))

        interval = format_eta(self.config.progress_update_interval_seconds)
        lines.append(f"I will send progress updates every {interval}.")
        self.telegram.reply(chat_id, "\n\n".join(lines))

    def handle_status(self, chat_id: int) -> None:
        torrents = self.qbit.torrents_info(sort="name")
        active_torrents = [torrent for torrent in torrents if is_active_torrent(torrent)]
        if not active_torrents:
            self.telegram.reply(chat_id, "No active torrents right now.")
            return

        shown = active_torrents[: self.config.status_limit]
        lines = [f"Active torrents: {len(active_torrents)}"]
        lines.extend(torrent_line(torrent) for torrent in shown)
        if len(active_torrents) > len(shown):
            lines.append(f"...and {len(active_torrents) - len(shown)} more.")
        self.telegram.reply(chat_id, "\n\n".join(lines))

    def handle_control(self, chat_id: int, action: str, query: str) -> None:
        query = query.strip()
        if not query:
            raise BotError(f"Usage: /{action} torrent name")

        torrents = self.qbit.torrents_info(sort="name")
        matches = self.find_matches(query, torrents)
        if not matches:
            raise BotError(f"No torrents matched: {query}")

        hashes = [torrent["hash"] for torrent in matches if torrent.get("hash")]
        self.qbit.control_torrents(action, hashes)
        names = "\n".join(f"- {torrent.get('name') or torrent['hash']}" for torrent in matches)
        self.telegram.reply(chat_id, f"{action.title()}d {len(matches)} torrent(s):\n{names}")

    def find_matches(self, query: str, torrents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if query.lower() == "all":
            return torrents

        scored: list[tuple[float, dict[str, Any]]] = []
        for torrent in torrents:
            name = str(torrent.get("name") or "")
            score = fuzzy_score(query, name)
            scored.append((score, torrent))

        substring_matches = [
            torrent for score, torrent in scored if score == 1.0 and torrent.get("hash")
        ]
        if substring_matches:
            return substring_matches[: self.config.fuzzy_match_limit]

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            torrent
            for score, torrent in scored[: self.config.fuzzy_match_limit]
            if score >= self.config.fuzzy_match_min_score and torrent.get("hash")
        ]

    def progress_watch_loop(self) -> None:
        while not self.stop_event.wait(10):
            now = time.time()
            with self.tracked_lock:
                tracked = list(self.tracked_torrents.values())

            for item in tracked:
                if now - item.last_sent_at < self.config.progress_update_interval_seconds:
                    continue
                if now - item.added_at > self.config.progress_update_max_hours * 3600:
                    self.untrack_torrent(item.torrent_hash)
                    continue

                try:
                    torrents = self.qbit.torrents_info(hashes=[item.torrent_hash])
                except Exception:
                    LOGGER.exception("Could not fetch progress for %s", item.torrent_hash)
                    continue

                if not torrents:
                    self.telegram.reply(
                        item.chat_id,
                        f"Stopped tracking missing torrent: {item.name}",
                    )
                    self.untrack_torrent(item.torrent_hash)
                    continue

                torrent = torrents[0]
                if float(torrent.get("progress") or 0) >= 1:
                    self.telegram.reply(item.chat_id, f"Completed:\n{torrent_line(torrent)}")
                    self.untrack_torrent(item.torrent_hash)
                    continue

                self.telegram.reply(item.chat_id, f"Progress update:\n{torrent_line(torrent)}")
                with self.tracked_lock:
                    if item.torrent_hash in self.tracked_torrents:
                        self.tracked_torrents[item.torrent_hash].last_sent_at = now

    def untrack_torrent(self, torrent_hash: str) -> None:
        with self.tracked_lock:
            self.tracked_torrents.pop(torrent_hash, None)

    def disk_watch_loop(self) -> None:
        while not self.stop_event.wait(1):
            try:
                self.check_disk_usage()
            except Exception:
                LOGGER.exception("Disk watchdog failed")
            self.stop_event.wait(self.config.disk_watch_interval_seconds)

    def check_disk_usage(self) -> None:
        path = self.config.disk_watch_path
        if not path.exists():
            LOGGER.warning("Disk watch path does not exist: %s", path)
            return

        usage = shutil.disk_usage(path)
        used_percent = usage.used / usage.total * 100
        state = read_json_file(self.config.disk_alerts_file, {"alerted": []})
        alerted = {int(threshold) for threshold in state.get("alerted", [])}

        for threshold in self.config.disk_watch_thresholds:
            if used_percent >= threshold and threshold not in alerted:
                self.telegram.notify_allowed_users(
                    self.config.allowed_user_ids,
                    (
                        f"Disk alert: {path} is {used_percent:.1f}% full "
                        f"({format_bytes(usage.free)} free of {format_bytes(usage.total)})."
                    ),
                )
                alerted.add(threshold)
            elif used_percent < threshold:
                alerted.discard(threshold)

        write_json_file(self.config.disk_alerts_file, {"alerted": sorted(alerted)})

    def ratio_alert_loop(self) -> None:
        if self.config.ratio_alert_target is None or self.config.ratio_alert_target <= 0:
            return

        while not self.stop_event.wait(15):
            try:
                self.check_ratio_alerts()
            except Exception:
                LOGGER.exception("Ratio alert poll failed")
            self.stop_event.wait(self.config.ratio_alert_interval_seconds)

    def check_ratio_alerts(self) -> None:
        target = self.config.ratio_alert_target
        if target is None:
            return

        torrents = self.qbit.torrents_info()
        state = read_json_file(self.config.ratio_alerts_file, {"alerted": []})
        alerted = {str(torrent_hash) for torrent_hash in state.get("alerted", [])}
        active_hashes = {torrent["hash"] for torrent in torrents if torrent.get("hash")}

        for torrent in torrents:
            torrent_hash = torrent.get("hash")
            if not torrent_hash or torrent_hash in alerted:
                continue
            ratio = float(torrent.get("ratio") or 0)
            if ratio >= target:
                self.telegram.notify_allowed_users(
                    self.config.allowed_user_ids,
                    (
                        f"Ratio target reached ({ratio:.2f} >= {target:.2f}):\n"
                        f"{torrent.get('name') or torrent_hash}"
                    ),
                )
                alerted.add(torrent_hash)

        write_json_file(self.config.ratio_alerts_file, {"alerted": sorted(alerted & active_hashes)})


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
