from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests


LOGGER = logging.getLogger("tg_qbit_bot")
LOG_BUFFER: deque[str] = deque(maxlen=300)


class MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


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
    status_live_enabled: bool
    status_refresh_seconds: int
    status_live_duration_seconds: int
    fuzzy_match_min_score: float
    fuzzy_match_limit: int
    category_save_paths: dict[str, str]
    category_as_tag: bool
    owner_user_ids: set[int]
    admin_user_ids: set[int]
    viewer_user_ids: set[int]
    approval_enabled: bool
    approval_timeout_seconds: int
    torrent_profiles: dict[str, dict[str, str]]
    per_category_ratio_targets: dict[str, float]
    ratio_action: str
    ratio_action_category: str | None
    stalled_alert_enabled: bool
    stalled_alert_minutes: int
    low_speed_alert_enabled: bool
    low_speed_threshold_bytes: int
    low_speed_minutes: int
    torrent_alert_interval_seconds: int
    completion_action: str
    completion_action_category: str | None
    freeleech_tags: str | None
    freeleech_category: str | None
    rss_feeds: list[dict[str, str]]
    rss_interval_seconds: int
    webhook_enabled: bool
    webhook_host: str
    webhook_port: int
    webhook_path: str
    webhook_url: str | None

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
    def torrent_alerts_file(self) -> Path:
        return self.state_dir / "torrent-alerts.json"

    @property
    def bot_state_file(self) -> Path:
        return self.state_dir / "bot-state.json"

    @property
    def rss_state_file(self) -> Path:
        return self.state_dir / "rss-seen.json"

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


def parse_optional_user_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    return parse_allowed_user_ids(value)


def parse_float_mapping(value: str | None) -> dict[str, float]:
    mapping: dict[str, float] = {}
    for key, raw_value in parse_mapping(value).items():
        try:
            mapping[key] = float(raw_value)
        except ValueError as error:
            raise BotError(f"Invalid numeric mapping value for {key}: {raw_value}") from error
    return mapping


def parse_profiles(value: str | None) -> dict[str, dict[str, str]]:
    if not value:
        return {}

    profiles: dict[str, dict[str, str]] = {}
    for raw_profile in value.split(";"):
        raw_profile = raw_profile.strip()
        if not raw_profile:
            continue
        if ":" not in raw_profile:
            raise BotError(f"Invalid profile: {raw_profile}")
        name, raw_options = raw_profile.split(":", 1)
        name = name.strip().lower()
        if not name:
            raise BotError(f"Invalid profile name: {raw_profile}")
        options: dict[str, str] = {}
        for raw_option in raw_options.split(","):
            raw_option = raw_option.strip()
            if not raw_option:
                continue
            if "=" not in raw_option:
                raise BotError(f"Invalid profile option: {raw_option}")
            key, option_value = raw_option.split("=", 1)
            options[key.strip().lower()] = option_value.strip()
        profiles[name] = options
    return profiles


def parse_rss_feeds(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []

    feeds: list[dict[str, str]] = []
    for raw_feed in value.split(";"):
        raw_feed = raw_feed.strip()
        if not raw_feed:
            continue
        parts = [part.strip() for part in raw_feed.split("|")]
        if len(parts) < 2:
            raise BotError(f"Invalid RSS feed spec: {raw_feed}")
        name, url = parts[0], parts[1]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise BotError(f"Invalid RSS URL: {url}")
        feeds.append(
            {
                "name": name or parsed.netloc,
                "url": url,
                "include": parts[2] if len(parts) > 2 else "",
                "category": parts[3] if len(parts) > 3 else "",
            }
        )
    return feeds


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
    base_allowed_user_ids = parse_allowed_user_ids(getenv_required("TG_ALLOWED_USER_IDS"))
    owner_user_ids = parse_optional_user_ids(os.getenv("OWNER_USER_IDS")) or base_allowed_user_ids
    admin_user_ids = (
        parse_optional_user_ids(os.getenv("ADMIN_USER_IDS"))
        or base_allowed_user_ids
        or owner_user_ids
    )
    viewer_user_ids = parse_optional_user_ids(os.getenv("VIEWER_USER_IDS"))
    allowed_user_ids = base_allowed_user_ids | owner_user_ids | admin_user_ids | viewer_user_ids
    configured_state_dir = optional_env("STATE_DIR")
    if configured_state_dir:
        state_dir = Path(configured_state_dir).expanduser()
    else:
        state_dir = default_state_dir()
    disk_watch_path = optional_env("DISK_WATCH_PATH") or optional_env("QBIT_SAVE_PATH") or "/"

    return Config(
        telegram_token=getenv_required("TG_BOT_TOKEN"),
        allowed_user_ids=allowed_user_ids,
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
        status_live_enabled=parse_bool(os.getenv("STATUS_LIVE_ENABLED"), default=True),
        status_refresh_seconds=parse_int_env("STATUS_REFRESH_SECONDS", 30),
        status_live_duration_seconds=parse_int_env("STATUS_LIVE_DURATION_SECONDS", 600),
        fuzzy_match_min_score=parse_float_env("FUZZY_MATCH_MIN_SCORE", 0.35) or 0.35,
        fuzzy_match_limit=parse_int_env("FUZZY_MATCH_LIMIT", 5),
        category_save_paths=parse_mapping(os.getenv("CATEGORY_SAVE_PATHS")),
        category_as_tag=parse_bool(os.getenv("CATEGORY_AS_TAG"), default=False),
        owner_user_ids=owner_user_ids,
        admin_user_ids=admin_user_ids | owner_user_ids,
        viewer_user_ids=viewer_user_ids,
        approval_enabled=parse_bool(os.getenv("APPROVAL_ENABLED"), default=False),
        approval_timeout_seconds=parse_int_env("APPROVAL_TIMEOUT_SECONDS", 3600),
        torrent_profiles=parse_profiles(os.getenv("TORRENT_PROFILES")),
        per_category_ratio_targets=parse_float_mapping(os.getenv("PER_CATEGORY_RATIO_TARGETS")),
        ratio_action=os.getenv("RATIO_ACTION", "notify"),
        ratio_action_category=optional_env("RATIO_ACTION_CATEGORY"),
        stalled_alert_enabled=parse_bool(os.getenv("STALLED_ALERT_ENABLED"), default=True),
        stalled_alert_minutes=parse_int_env("STALLED_ALERT_MINUTES", 30),
        low_speed_alert_enabled=parse_bool(os.getenv("LOW_SPEED_ALERT_ENABLED"), default=True),
        low_speed_threshold_bytes=parse_int_env("LOW_SPEED_THRESHOLD_BYTES", 50 * 1024),
        low_speed_minutes=parse_int_env("LOW_SPEED_MINUTES", 30),
        torrent_alert_interval_seconds=parse_int_env("TORRENT_ALERT_INTERVAL_SECONDS", 900),
        completion_action=os.getenv("COMPLETION_ACTION", "notify"),
        completion_action_category=optional_env("COMPLETION_ACTION_CATEGORY"),
        freeleech_tags=optional_env("FREELEECH_TAGS"),
        freeleech_category=optional_env("FREELEECH_CATEGORY"),
        rss_feeds=parse_rss_feeds(os.getenv("RSS_FEEDS")),
        rss_interval_seconds=parse_int_env("RSS_INTERVAL_SECONDS", 900),
        webhook_enabled=parse_bool(os.getenv("WEBHOOK_ENABLED"), default=False),
        webhook_host=os.getenv("WEBHOOK_HOST", "127.0.0.1"),
        webhook_port=parse_int_env("WEBHOOK_PORT", 8088),
        webhook_path=os.getenv("WEBHOOK_PATH", "/telegram"),
        webhook_url=optional_env("WEBHOOK_URL"),
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

    def reply(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
                payload["disable_web_page_preview"] = True
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return self.call("sendMessage", json=payload)
        except Exception:
            LOGGER.exception("Could not send Telegram reply")
            return None

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
                payload["disable_web_page_preview"] = True
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            return self.call("editMessageText", json=payload)
        except Exception:
            LOGGER.exception("Could not edit Telegram message")
            return None

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self.call("answerCallbackQuery", json=payload)
        except Exception:
            LOGGER.exception("Could not answer callback query")

    def set_webhook(self, url: str) -> None:
        self.call("setWebhook", json={"url": url, "drop_pending_updates": False})

    def delete_webhook(self) -> None:
        self.call("deleteWebhook", json={"drop_pending_updates": False})

    def set_commands(self) -> None:
        commands = [
            {"command": "status", "description": "Live seedbox dashboard"},
            {"command": "search", "description": "Search torrents by name"},
            {"command": "stats", "description": "Seedbox overview"},
            {"command": "top", "description": "Top/worst torrent board"},
            {"command": "queue", "description": "Recently added torrents"},
            {"command": "recent", "description": "Recent completions"},
            {"command": "disk", "description": "Disk usage panel"},
            {"command": "health", "description": "Bot and qBit health"},
            {"command": "pause", "description": "Pause torrents by name"},
            {"command": "resume", "description": "Resume torrents by name"},
            {"command": "add", "description": "Add magnet with category"},
            {"command": "freeleech", "description": "Toggle freeleech mode"},
            {"command": "pref", "description": "qBittorrent preferences"},
            {"command": "help", "description": "Show bot commands"},
        ]
        try:
            self.call("setMyCommands", json={"commands": commands})
        except Exception:
            LOGGER.exception("Could not set Telegram command menu")

    def notify_allowed_users(
        self,
        user_ids: set[int],
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        for user_id in user_ids:
            self.reply(user_id, text, parse_mode=parse_mode)

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
        ratio_limit: float | None = None,
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
        if ratio_limit is not None:
            data["ratioLimit"] = str(ratio_limit)
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
        ratio_limit: float | None = None,
    ) -> None:
        session, headers = self.login()
        with path.open("rb") as torrent_file:
            response = session.post(
                f"{self.config.qbit_url}/api/v2/torrents/add",
                data=self.add_payload(
                    category=category,
                    save_path=save_path,
                    tags=tags,
                    ratio_limit=ratio_limit,
                ),
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
        ratio_limit: float | None = None,
    ) -> None:
        session, headers = self.login()
        data = self.add_payload(
            category=category,
            save_path=save_path,
            tags=tags,
            ratio_limit=ratio_limit,
        )
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

    def delete_torrents(self, hashes: list[str], *, delete_files: bool = False) -> None:
        response = self.request(
            "POST",
            "torrents/delete",
            data={"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()},
            timeout=30,
        )
        response.raise_for_status()

    def recheck_torrents(self, hashes: list[str]) -> None:
        response = self.request(
            "POST",
            "torrents/recheck",
            data={"hashes": "|".join(hashes)},
            timeout=30,
        )
        response.raise_for_status()

    def set_force_start(self, hashes: list[str], value: bool = True) -> None:
        response = self.request(
            "POST",
            "torrents/setForceStart",
            data={"hashes": "|".join(hashes), "value": str(value).lower()},
            timeout=30,
        )
        response.raise_for_status()

    def set_category(self, hashes: list[str], category: str) -> None:
        response = self.request(
            "POST",
            "torrents/setCategory",
            data={"hashes": "|".join(hashes), "category": category},
            timeout=30,
        )
        response.raise_for_status()

    def transfer_info(self) -> dict[str, Any]:
        response = self.request("GET", "transfer/info")
        response.raise_for_status()
        return response.json()

    def app_text(self, endpoint: str) -> str:
        response = self.request("GET", f"app/{endpoint}")
        response.raise_for_status()
        return response.text.strip()

    def preferences(self) -> dict[str, Any]:
        response = self.request("GET", "app/preferences")
        response.raise_for_status()
        return response.json()

    def set_preferences(self, values: dict[str, Any]) -> None:
        response = self.request(
            "POST",
            "app/setPreferences",
            data={"json": json.dumps(values)},
            timeout=30,
        )
        response.raise_for_status()

    def toggle_alt_speed(self) -> None:
        response = self.request("POST", "transfer/toggleSpeedLimitsMode")
        response.raise_for_status()

    def set_download_limit(self, bytes_per_second: int) -> None:
        response = self.request(
            "POST",
            "transfer/setDownloadLimit",
            data={"limit": str(bytes_per_second)},
        )
        response.raise_for_status()

    def set_upload_limit(self, bytes_per_second: int) -> None:
        response = self.request(
            "POST",
            "transfer/setUploadLimit",
            data={"limit": str(bytes_per_second)},
        )
        response.raise_for_status()

    def main_log(self, lines: int = 30) -> list[dict[str, Any]]:
        response = self.request("GET", "log/main", params={"normal": "true"})
        response.raise_for_status()
        return response.json()[-lines:]


@dataclass
class TrackedTorrent:
    torrent_hash: str
    chat_id: int
    name: str
    added_at: float
    last_sent_at: float = 0
    message_id: int | None = None
    last_text: str = ""


@dataclass
class LiveStatusPanel:
    chat_id: int
    message_id: int
    created_at: float
    last_sent_at: float = 0
    last_text: str = ""


@dataclass
class PendingApproval:
    pending_id: str
    requester_id: int
    chat_id: int
    kind: str
    value: str
    category: str | None
    file_name: str | None
    file_path: Path | None
    created_at: float


@dataclass
class TorrentVitals:
    progress: float
    downloaded: int
    dlspeed: int
    upspeed: int
    seen_at: float
    stagnant_since: float
    low_speed_since: float | None = None


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


def short_id(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:12]


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}...{value[-3:]}"


def parse_size(value: str) -> int:
    text = value.strip().lower()
    if not text:
        raise BotError("Size value is required")
    units = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
    }
    number = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    unit = text.replace(number, "").strip() or "b"
    if not number or unit not in units:
        raise BotError(f"Invalid size: {value}")
    return int(float(number) * units[unit])


def torrent_hashes(torrents: list[dict[str, Any]]) -> list[str]:
    return [torrent["hash"] for torrent in torrents if torrent.get("hash")]


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


def escape_html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def truncate_text(value: Any, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}..."


def progress_bar(value: int | float | None, width: int = 18) -> str:
    progress = max(0.0, min(1.0, float(value or 0)))
    filled = round(progress * width)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def disk_bar(used_percent: float, width: int = 18) -> str:
    used = max(0.0, min(100.0, used_percent))
    filled = round(used / 100 * width)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def fit_telegram_text(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 32].rstrip() + "\n\n<pre>...trimmed</pre>"


def render_torrent_block(torrent: dict[str, Any], index: int | None = None) -> str:
    prefix = f"{index:02d}. " if index is not None else ""
    name = escape_html(truncate_text(torrent.get("name") or "Unnamed torrent", 72))
    progress = torrent.get("progress")
    state = escape_html(torrent.get("state") or "unknown")
    eta = escape_html(format_eta(torrent.get("eta")))
    lines = [
        f"<b>{prefix}{name}</b>",
        "<pre>"
        f"{progress_bar(progress)} {format_percent(progress)}\n"
        f"DOWN {format_speed(torrent.get('dlspeed')):<14} "
        f"UP {format_speed(torrent.get('upspeed')):<14}\n"
        f"ETA  {eta:<14} "
        f"RATIO {format_ratio(torrent.get('ratio')):<6} {state}"
        "</pre>",
    ]
    return "\n".join(lines)


def render_torrent_card(
    torrent: dict[str, Any],
    *,
    title: str,
    footer: str | None = None,
) -> str:
    body = [
        f"<b>{escape_html(title)}</b>",
        render_torrent_block(torrent),
    ]
    if footer:
        body.append(f"<pre>{escape_html(footer)}</pre>")
    return fit_telegram_text("\n".join(body))


def torrent_keyboard(torrent_hash: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Pause", "callback_data": f"t:pause:{torrent_hash}"},
                {"text": "Resume", "callback_data": f"t:resume:{torrent_hash}"},
                {"text": "Recheck", "callback_data": f"t:recheck:{torrent_hash}"},
            ],
            [
                {"text": "Force Start", "callback_data": f"t:force:{torrent_hash}"},
                {"text": "Delete", "callback_data": f"t:delete:{torrent_hash}"},
                {"text": "Delete + Data", "callback_data": f"t:delete_data:{torrent_hash}"},
            ],
        ]
    }


def approval_keyboard(pending_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"a:yes:{pending_id}"},
                {"text": "Reject", "callback_data": f"a:no:{pending_id}"},
            ]
        ]
    }


def render_torrent_cards(
    torrents: list[dict[str, Any]],
    *,
    title: str,
    limit: int,
) -> str:
    lines = [f"<b>{escape_html(title)}</b>"]
    for index, torrent in enumerate(torrents[:limit], start=1):
        lines.append(render_torrent_block(torrent, index))
    if len(torrents) > limit:
        lines.append(f"<pre>+ {len(torrents) - limit} more</pre>")
    return fit_telegram_text("\n\n".join(lines))


def render_disk_panel(path: Path) -> str:
    try:
        usage = shutil.disk_usage(path)
    except FileNotFoundError:
        return f"DISK {escape_html(path)} unavailable"
    used_percent = usage.used / usage.total * 100
    return (
        f"DISK {escape_html(path)} {used_percent:5.1f}% {disk_bar(used_percent)}\n"
        f"FREE {format_bytes(usage.free)} / {format_bytes(usage.total)}"
    )


def render_status_dashboard(
    torrents: list[dict[str, Any]],
    *,
    config: Config,
    live_until: float | None = None,
) -> str:
    active_torrents = [torrent for torrent in torrents if is_active_torrent(torrent)]
    total_down = sum(int(torrent.get("dlspeed") or 0) for torrent in torrents)
    total_up = sum(int(torrent.get("upspeed") or 0) for torrent in torrents)
    completed = sum(1 for torrent in torrents if float(torrent.get("progress") or 0) >= 1)
    header = [
        "<b>TG2QBIT // LIVE DASHBOARD</b>",
        "<pre>"
        f"ACTIVE {len(active_torrents):02d}  TOTAL {len(torrents):02d}  DONE {completed:02d}\n"
        f"DOWN   {format_speed(total_down):<14} UP {format_speed(total_up):<14}\n"
        f"{render_disk_panel(config.disk_watch_path)}"
        "</pre>",
    ]

    if live_until:
        remaining = max(0, int(live_until - time.time()))
        remaining_text = escape_html(format_eta(remaining))
        header.append(
            f"<pre>REFRESH {config.status_refresh_seconds}s | LIVE {remaining_text}</pre>"
        )

    if not active_torrents:
        header.append("<pre>No active torrents right now.</pre>")
        return "\n".join(header)

    shown_count = 0
    for index, torrent in enumerate(active_torrents[: config.status_limit], start=1):
        candidate = "\n\n".join([*header, render_torrent_block(torrent, index)])
        if len(candidate) > 3800:
            break
        header.append(render_torrent_block(torrent, index))
        shown_count += 1

    hidden_count = len(active_torrents) - shown_count
    if hidden_count > 0:
        header.append(f"<pre>+ {hidden_count} more active torrent(s)</pre>")

    return fit_telegram_text("\n\n".join(header))


def render_stats_panel(
    torrents: list[dict[str, Any]],
    transfer: dict[str, Any],
    *,
    config: Config,
) -> str:
    total_size = sum(int(torrent.get("size") or 0) for torrent in torrents)
    total_uploaded = sum(int(torrent.get("uploaded") or 0) for torrent in torrents)
    total_downloaded = sum(int(torrent.get("downloaded") or 0) for torrent in torrents)
    ratio = total_uploaded / total_downloaded if total_downloaded else 0
    active = [torrent for torrent in torrents if is_active_torrent(torrent)]
    text = (
        "<b>TG2QBIT // SEEDBOX STATS</b>\n"
        "<pre>"
        f"TORRENTS {len(torrents):<5} ACTIVE {len(active):<5}\n"
        f"DOWN     {format_speed(transfer.get('dl_info_speed')):<14}\n"
        f"UP       {format_speed(transfer.get('up_info_speed')):<14}\n"
        f"SESSION  D {format_bytes(transfer.get('dl_info_data')):<10} "
        f"U {format_bytes(transfer.get('up_info_data')):<10}\n"
        f"LIBRARY  {format_bytes(total_size):<10} RATIO {ratio:.2f}\n"
        f"{render_disk_panel(config.disk_watch_path)}"
        "</pre>"
    )
    return fit_telegram_text(text)


def render_top_panel(torrents: list[dict[str, Any]], limit: int) -> str:
    def top_lines(title: str, items: list[dict[str, Any]], metric: str) -> list[str]:
        lines = [title]
        for torrent in items[:limit]:
            name = truncate_text(torrent.get("name") or "unnamed", 42)
            value = torrent.get(metric) or 0
            rendered = format_bytes(value) if metric != "ratio" else format_ratio(value)
            lines.append(f"{rendered:<10} {name}")
        return lines

    top_upload = sorted(torrents, key=lambda t: int(t.get("uploaded") or 0), reverse=True)
    biggest = sorted(torrents, key=lambda t: int(t.get("size") or 0), reverse=True)
    worst_ratio = sorted(torrents, key=lambda t: float(t.get("ratio") or 0))
    stalled = [torrent for torrent in torrents if "stalled" in str(torrent.get("state") or "")]
    lines = ["TG2QBIT // TOP BOARD", ""]
    lines.extend(top_lines("TOP UPLOAD", top_upload, "uploaded"))
    lines.append("")
    lines.extend(top_lines("BIGGEST", biggest, "size"))
    lines.append("")
    lines.extend(top_lines("WORST RATIO", worst_ratio, "ratio"))
    lines.append("")
    lines.append(f"STALLED {len(stalled)}")
    return fit_telegram_text("<b>TOP BOARD</b>\n<pre>" + escape_html("\n".join(lines)) + "</pre>")


def render_health_panel(config: Config, qbit: QBittorrentClient, telegram_ok: bool) -> str:
    checks: list[str] = []
    try:
        version = qbit.app_text("version")
        api_version = qbit.app_text("webapiVersion")
        checks.append(f"QBIT     ok {version} api {api_version}")
    except Exception as error:
        checks.append(f"QBIT     fail {type(error).__name__}")

    checks.append(f"TELEGRAM {'ok' if telegram_ok else 'fail'}")
    disk_status = "ok" if config.disk_watch_path.exists() else "missing"
    checks.append(f"DISK     {disk_status} {config.disk_watch_path}")
    checks.append(f"STATE    {config.state_dir}")
    return "<b>TG2QBIT // HEALTH</b>\n<pre>" + escape_html("\n".join(checks)) + "</pre>"


def find_rss_link(item: ElementTree.Element) -> str | None:
    for enclosure in item.findall("enclosure"):
        url = enclosure.attrib.get("url")
        if url and (url.startswith("magnet:") or url.startswith("http")):
            return url
    for child_name in ("link", "guid"):
        child = item.find(child_name)
        if child is not None and child.text:
            text = child.text.strip()
            if text.startswith("magnet:") or text.startswith("http"):
                return text
    return None


def parse_rss_items(xml_text: str) -> list[dict[str, str]]:
    root = ElementTree.fromstring(xml_text)
    items = root.findall(".//item") or root.findall(".//{*}entry")
    parsed_items: list[dict[str, str]] = []
    for item in items:
        title_node = item.find("title") or item.find("{*}title")
        title = title_node.text.strip() if title_node is not None and title_node.text else "rss item"
        link = find_rss_link(item)
        if link:
            parsed_items.append({"title": title, "link": link})
    return parsed_items


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


def merge_tag_strings(*tag_sets: str | None) -> str | None:
    tags: list[str] = []
    for tag_set in tag_sets:
        tags.extend(tag.strip() for tag in (tag_set or "").split(",") if tag.strip())
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
            "<b>TG2QBIT // CONTROL DECK</b>",
            "<pre>Send a .torrent file or magnet link.</pre>",
            "",
            "<b>Commands</b>",
            "<pre>",
            "/status - show active torrents",
            "/search name - show matching torrent cards",
            "/stats - seedbox overview",
            "/top - top upload, biggest, worst ratio",
            "/queue - recently added torrents",
            "/recent - recent completions",
            "/disk - disk panel",
            "/health - integration health",
            "/logs - recent bot logs",
            "/backup - sanitized config export",
            "/pause name - fuzzy-match and pause torrents",
            "/resume name - fuzzy-match and resume torrents",
            "/add category magnet-link - add a magnet under a category",
            "/freeleech on|off - route adds through freeleech profile",
            "/pref alt|dl|ul|queue - qBittorrent preferences",
            "</pre>",
            "",
            "<b>Torrent file captions</b>",
            "<pre>",
            "category: tv",
            "movies",
            "</pre>",
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
        self.live_status_panels: dict[tuple[int, int], LiveStatusPanel] = {}
        self.live_status_lock = threading.Lock()
        self.pending_approvals: dict[str, PendingApproval] = {}
        self.pending_lock = threading.Lock()
        self.vitals: dict[str, TorrentVitals] = {}
        self.vitals_lock = threading.Lock()

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

    def bot_state(self) -> dict[str, Any]:
        return read_json_file(
            self.config.bot_state_file,
            {"freeleech": False, "recent_added": [], "recent_completed": []},
        )

    def save_bot_state(self, state: dict[str, Any]) -> None:
        write_json_file(self.config.bot_state_file, state)

    def remember_recent(self, key: str, item: dict[str, Any], limit: int = 25) -> None:
        state = self.bot_state()
        items = [existing for existing in state.get(key, []) if existing.get("hash") != item.get("hash")]
        items.insert(0, item)
        state[key] = items[:limit]
        self.save_bot_state(state)

    def role_for_user(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        if user_id in self.config.owner_user_ids:
            return "owner"
        if user_id in self.config.admin_user_ids:
            return "admin"
        if user_id in self.config.viewer_user_ids or user_id in self.config.allowed_user_ids:
            return "viewer"
        return None

    def require_role(self, user_id: int | None, minimum: str) -> str:
        role = self.role_for_user(user_id)
        rank = {None: 0, "viewer": 1, "admin": 2, "owner": 3}
        if rank[role] < rank[minimum]:
            raise BotError(f"{minimum.title()} access required.")
        return role or ""

    def can_submit_with_approval(self, user_id: int | None) -> bool:
        return bool(self.config.approval_enabled and user_id is not None)

    def run(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.telegram.set_commands()
        self.start_background_threads()
        if self.config.webhook_enabled:
            self.run_webhook()
            return
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

    def run_webhook(self) -> None:
        if self.config.webhook_url:
            self.telegram.set_webhook(self.config.webhook_url)
        bot = self
        path = self.config.webhook_path

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != path:
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    update = json.loads(body.decode())
                    bot.process_update(update)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception:
                    LOGGER.exception("Webhook update failed")
                    self.send_response(500)
                    self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                LOGGER.debug("webhook: " + format, *args)

        server = ThreadingHTTPServer(
            (self.config.webhook_host, self.config.webhook_port),
            Handler,
        )
        LOGGER.info(
            "Webhook server listening on %s:%s%s",
            self.config.webhook_host,
            self.config.webhook_port,
            self.config.webhook_path,
        )
        server.serve_forever()

    def start_background_threads(self) -> None:
        threads = [
            ("progress-watch", self.progress_watch_loop),
            ("ratio-alerts", self.ratio_alert_loop),
            ("torrent-alerts", self.torrent_alert_loop),
        ]
        if self.config.status_live_enabled:
            threads.append(("status-live", self.status_live_loop))
        if self.config.disk_watch_enabled:
            threads.append(("disk-watch", self.disk_watch_loop))
        if self.config.rss_feeds:
            threads.append(("rss-watch", self.rss_watch_loop))

        for name, target in threads:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()

    def process_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        if self.role_for_user(user_id) is None and not self.can_submit_with_approval(user_id):
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

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        user_id = callback.get("from", {}).get("id")
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        try:
            if data.startswith("t:"):
                self.require_role(user_id, "admin")
                self.handle_torrent_callback(data, chat_id, message_id)
                self.telegram.answer_callback(callback_id, "Done")
            elif data.startswith("a:"):
                self.require_role(user_id, "owner")
                self.handle_approval_callback(data)
                self.telegram.answer_callback(callback_id, "Recorded")
            else:
                self.telegram.answer_callback(callback_id, "Unknown action")
        except BotError as error:
            self.telegram.answer_callback(callback_id, str(error))
        except Exception:
            LOGGER.exception("Callback failed")
            self.telegram.answer_callback(callback_id, "Action failed")

    def handle_torrent_callback(
        self,
        data: str,
        chat_id: int | None,
        message_id: int | None,
    ) -> None:
        _, action, torrent_hash = data.split(":", 2)
        if action == "pause":
            self.qbit.control_torrents("pause", [torrent_hash])
        elif action == "resume":
            self.qbit.control_torrents("resume", [torrent_hash])
        elif action == "recheck":
            self.qbit.recheck_torrents([torrent_hash])
        elif action == "force":
            self.qbit.set_force_start([torrent_hash], True)
        elif action == "delete":
            self.qbit.delete_torrents([torrent_hash], delete_files=False)
        elif action == "delete_data":
            self.qbit.delete_torrents([torrent_hash], delete_files=True)
        else:
            raise BotError("Unknown torrent action")

        if chat_id and message_id and action.startswith("delete"):
            self.telegram.edit_message(
                chat_id,
                message_id,
                f"<b>REMOVED</b>\n<pre>{escape_html(torrent_hash)}</pre>",
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )

    def handle_document(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
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
            if self.role_for_user(user_id) not in {"owner", "admin"}:
                if not self.can_submit_with_approval(user_id):
                    raise BotError("Admin access required.")
                self.queue_approval(
                    requester_id=user_id or 0,
                    chat_id=chat_id,
                    kind="file",
                    value="",
                    category=category,
                    file_name=file_name,
                    source_path=torrent_path,
                )
                self.telegram.reply(chat_id, "Submitted for owner approval.")
                return

            previous_hashes = self.qbit.torrent_hashes()
            add_category, save_path, tags, ratio_limit = self.resolve_add_options(category)
            self.qbit.add_file(
                torrent_path,
                torrent_path.name,
                category=add_category,
                save_path=save_path,
                tags=tags,
                ratio_limit=ratio_limit,
            )
            new_torrents = self.qbit.wait_for_new_torrents(previous_hashes)

        self.announce_added_torrents(chat_id, file_name, new_torrents, category)

    def handle_text(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        text = (message.get("text") or "").strip()
        command, args = parse_command(text)

        if command in {"/start", "/help"}:
            self.telegram.reply(chat_id, help_text(), parse_mode="HTML")
            return

        if command == "/status":
            self.require_role(user_id, "viewer")
            self.handle_status(chat_id)
            return

        if command == "/search":
            self.require_role(user_id, "viewer")
            self.handle_search(chat_id, args)
            return

        if command == "/stats":
            self.require_role(user_id, "viewer")
            self.handle_stats(chat_id)
            return

        if command == "/top":
            self.require_role(user_id, "viewer")
            self.handle_top(chat_id)
            return

        if command == "/queue":
            self.require_role(user_id, "viewer")
            self.handle_queue(chat_id)
            return

        if command == "/recent":
            self.require_role(user_id, "viewer")
            self.handle_recent(chat_id)
            return

        if command == "/disk":
            self.require_role(user_id, "viewer")
            self.handle_disk(chat_id)
            return

        if command == "/health":
            self.require_role(user_id, "admin")
            self.handle_health(chat_id)
            return

        if command == "/logs":
            self.require_role(user_id, "owner")
            self.handle_logs(chat_id)
            return

        if command in {"/config", "/backup"}:
            self.require_role(user_id, "owner")
            self.handle_config(chat_id)
            return

        if command == "/freeleech":
            self.require_role(user_id, "admin")
            self.handle_freeleech(chat_id, args)
            return

        if command == "/pref":
            self.require_role(user_id, "admin")
            self.handle_pref(chat_id, args)
            return

        if command == "/pause":
            self.require_role(user_id, "admin")
            self.handle_control(chat_id, "pause", args)
            return

        if command == "/resume":
            self.require_role(user_id, "admin")
            self.handle_control(chat_id, "resume", args)
            return

        if command == "/add":
            category, magnet = parse_add_command(text)
            self.submit_magnet(chat_id, user_id, magnet, category)
            return

        if text.startswith("magnet:"):
            self.submit_magnet(chat_id, user_id, text, None)
            return

        raise BotError("Send a .torrent file or a magnet link.")

    def resolve_add_options(
        self,
        category: str | None,
    ) -> tuple[str | None, str | None, str | None, float | None]:
        category = normalize_category(category)
        profile = self.config.torrent_profiles.get((category or "").lower(), {})
        add_category = normalize_category(profile.get("category") or category)
        save_path = profile.get("save_path")
        if add_category and not save_path:
            save_path = self.config.category_save_paths.get(add_category.lower())

        profile_tags = profile.get("tags")
        base_tags = profile_tags or self.config.qbit_tags
        state = self.bot_state()
        if state.get("freeleech"):
            base_tags = merge_tag_strings(base_tags, self.config.freeleech_tags)
            add_category = self.config.freeleech_category or add_category

        tags = combined_tags(base_tags, add_category, self.config.category_as_tag)
        ratio_limit = None
        raw_ratio = profile.get("ratio")
        if raw_ratio:
            ratio_limit = float(raw_ratio)
        elif add_category:
            ratio_limit = self.config.per_category_ratio_targets.get(add_category.lower())
        return add_category, save_path, tags, ratio_limit

    def add_magnet(self, chat_id: int, magnet: str, category: str | None) -> None:
        category = normalize_category(category)
        previous_hashes = self.qbit.torrent_hashes()
        add_category, save_path, tags, ratio_limit = self.resolve_add_options(category)
        self.qbit.add_url(
            magnet,
            category=add_category,
            save_path=save_path,
            tags=tags,
            ratio_limit=ratio_limit,
        )
        new_torrents = self.qbit.wait_for_new_torrents(previous_hashes)
        self.announce_added_torrents(chat_id, "magnet link", new_torrents, category)

    def submit_magnet(
        self,
        chat_id: int,
        user_id: int | None,
        magnet: str,
        category: str | None,
    ) -> None:
        if self.role_for_user(user_id) in {"owner", "admin"}:
            self.add_magnet(chat_id, magnet, category)
            return
        if not self.can_submit_with_approval(user_id):
            raise BotError("Admin access required.")
        self.queue_approval(
            requester_id=user_id or 0,
            chat_id=chat_id,
            kind="magnet",
            value=magnet,
            category=category,
            file_name=None,
            source_path=None,
        )
        self.telegram.reply(chat_id, "Submitted for owner approval.")

    def queue_approval(
        self,
        *,
        requester_id: int,
        chat_id: int,
        kind: str,
        value: str,
        category: str | None,
        file_name: str | None,
        source_path: Path | None,
    ) -> None:
        pending_id = short_id(f"{requester_id}:{time.time()}:{kind}:{value}:{file_name}")
        pending_path = None
        if source_path:
            pending_dir = self.config.state_dir / "pending"
            pending_dir.mkdir(parents=True, exist_ok=True)
            pending_path = pending_dir / f"{pending_id}.torrent"
            shutil.copyfile(source_path, pending_path)

        pending = PendingApproval(
            pending_id=pending_id,
            requester_id=requester_id,
            chat_id=chat_id,
            kind=kind,
            value=value,
            category=category,
            file_name=file_name,
            file_path=pending_path,
            created_at=time.time(),
        )
        with self.pending_lock:
            self.pending_approvals[pending_id] = pending

        label = file_name or truncate_text(value, 80)
        text = (
            "<b>APPROVAL REQUEST</b>\n"
            "<pre>"
            f"FROM {requester_id}\n"
            f"TYPE {kind}\n"
            f"CAT  {category or '-'}\n"
            f"ITEM {escape_html(label)}"
            "</pre>"
        )
        for owner_id in self.config.owner_user_ids:
            self.telegram.reply(
                owner_id,
                text,
                parse_mode="HTML",
                reply_markup=approval_keyboard(pending_id),
            )

    def handle_approval_callback(self, data: str) -> None:
        _, decision, pending_id = data.split(":", 2)
        with self.pending_lock:
            pending = self.pending_approvals.pop(pending_id, None)
        if not pending:
            raise BotError("Approval expired or already handled.")

        if time.time() - pending.created_at > self.config.approval_timeout_seconds:
            raise BotError("Approval expired.")

        if decision == "no":
            self.telegram.reply(pending.chat_id, "Your torrent request was rejected.")
            if pending.file_path and pending.file_path.exists():
                pending.file_path.unlink()
            return

        if pending.kind == "magnet":
            self.add_magnet(pending.chat_id, pending.value, pending.category)
            return

        if pending.kind == "file" and pending.file_path:
            previous_hashes = self.qbit.torrent_hashes()
            add_category, save_path, tags, ratio_limit = self.resolve_add_options(pending.category)
            self.qbit.add_file(
                pending.file_path,
                pending.file_name or pending.file_path.name,
                category=add_category,
                save_path=save_path,
                tags=tags,
                ratio_limit=ratio_limit,
            )
            new_torrents = self.qbit.wait_for_new_torrents(previous_hashes)
            self.announce_added_torrents(
                pending.chat_id,
                pending.file_name or pending.file_path.name,
                new_torrents,
                pending.category,
            )
            pending.file_path.unlink(missing_ok=True)

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
        for torrent in torrents:
            torrent_hash = torrent.get("hash")
            name = torrent.get("name") or label
            footer = (
                f"category {category or '-'} | refresh "
                f"{format_eta(self.config.progress_update_interval_seconds)}"
            )
            title = f"ADDED // {category or 'default queue'}"
            text = render_torrent_card(torrent, title=title, footer=footer)
            keyboard = torrent_keyboard(torrent_hash) if torrent_hash else None
            sent_message = self.telegram.reply(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            message_id = sent_message.get("message_id") if sent_message else None
            if torrent_hash:
                self.remember_recent(
                    "recent_added",
                    {
                        "hash": torrent_hash,
                        "name": name,
                        "category": category,
                        "at": now,
                    },
                )
                with self.tracked_lock:
                    self.tracked_torrents[torrent_hash] = TrackedTorrent(
                        torrent_hash=torrent_hash,
                        chat_id=chat_id,
                        name=name,
                        added_at=now,
                        last_sent_at=now,
                        message_id=message_id,
                        last_text=text,
                    )

    def handle_status(self, chat_id: int) -> None:
        torrents = self.qbit.torrents_info(sort="name")
        live_until = None
        if self.config.status_live_enabled:
            live_until = time.time() + self.config.status_live_duration_seconds
        text = render_status_dashboard(torrents, config=self.config, live_until=live_until)
        sent_message = self.telegram.reply(chat_id, text, parse_mode="HTML")
        if not sent_message or not self.config.status_live_enabled:
            return

        message_id = sent_message.get("message_id")
        if not message_id:
            return

        panel = LiveStatusPanel(
            chat_id=chat_id,
            message_id=message_id,
            created_at=time.time(),
            last_sent_at=time.time(),
            last_text=text,
        )
        with self.live_status_lock:
            self.live_status_panels[(chat_id, message_id)] = panel

    def status_live_loop(self) -> None:
        while not self.stop_event.wait(5):
            now = time.time()
            with self.live_status_lock:
                panels = list(self.live_status_panels.values())

            due_panels = [
                panel
                for panel in panels
                if now - panel.last_sent_at >= self.config.status_refresh_seconds
            ]
            if not due_panels:
                continue

            try:
                torrents = self.qbit.torrents_info(sort="name")
            except Exception:
                LOGGER.exception("Could not refresh live status panels")
                continue

            for panel in due_panels:
                live_until = panel.created_at + self.config.status_live_duration_seconds
                if now >= live_until:
                    self.finish_live_status_panel(panel)
                    continue

                text = render_status_dashboard(torrents, config=self.config, live_until=live_until)
                if text == panel.last_text:
                    panel.last_sent_at = now
                    continue

                edited = self.telegram.edit_message(
                    panel.chat_id,
                    panel.message_id,
                    text,
                    parse_mode="HTML",
                )
                if edited:
                    panel.last_sent_at = now
                    panel.last_text = text
                else:
                    self.remove_live_status_panel(panel)

    def finish_live_status_panel(self, panel: LiveStatusPanel) -> None:
        footer = "<pre>LIVE WINDOW ENDED. Send /status for a fresh panel.</pre>"
        if len(panel.last_text) + len(footer) + 2 > 3900:
            final_text = panel.last_text
        else:
            final_text = f"{panel.last_text}\n\n{footer}"
        if final_text != panel.last_text:
            self.telegram.edit_message(
                panel.chat_id,
                panel.message_id,
                final_text,
                parse_mode="HTML",
            )
        self.remove_live_status_panel(panel)

    def remove_live_status_panel(self, panel: LiveStatusPanel) -> None:
        with self.live_status_lock:
            self.live_status_panels.pop((panel.chat_id, panel.message_id), None)

    def handle_search(self, chat_id: int, query: str) -> None:
        query = query.strip()
        if not query:
            raise BotError("Usage: /search torrent name")
        torrents = self.qbit.torrents_info(sort="name")
        matches = self.find_matches(query, torrents)
        if not matches:
            raise BotError(f"No torrents matched: {query}")
        for torrent in matches:
            torrent_hash = torrent.get("hash")
            self.telegram.reply(
                chat_id,
                render_torrent_card(torrent, title="SEARCH // match"),
                parse_mode="HTML",
                reply_markup=torrent_keyboard(torrent_hash) if torrent_hash else None,
            )

    def handle_stats(self, chat_id: int) -> None:
        torrents = self.qbit.torrents_info()
        transfer = self.qbit.transfer_info()
        self.telegram.reply(
            chat_id,
            render_stats_panel(torrents, transfer, config=self.config),
            parse_mode="HTML",
        )

    def handle_top(self, chat_id: int) -> None:
        torrents = self.qbit.torrents_info()
        self.telegram.reply(
            chat_id,
            render_top_panel(torrents, self.config.status_limit),
            parse_mode="HTML",
        )

    def handle_queue(self, chat_id: int) -> None:
        state = self.bot_state()
        items = state.get("recent_added", [])[: self.config.status_limit]
        if not items:
            self.telegram.reply(chat_id, "<b>QUEUE</b>\n<pre>No recent additions.</pre>", parse_mode="HTML")
            return
        text = "<b>QUEUE // recent additions</b>\n<pre>" + escape_html(
            "\n".join(
                f"{format_eta(time.time() - item.get('at', time.time()))} ago  "
                f"{truncate_text(item.get('name') or item.get('hash'), 70)}"
                for item in items
            )
        ) + "</pre>"
        self.telegram.reply(chat_id, text, parse_mode="HTML")

    def handle_recent(self, chat_id: int) -> None:
        state = self.bot_state()
        completed = state.get("recent_completed", [])[: self.config.status_limit]
        if not completed:
            self.telegram.reply(
                chat_id,
                "<b>RECENT</b>\n<pre>No completions recorded.</pre>",
                parse_mode="HTML",
            )
            return
        text = "<b>RECENT // completed</b>\n<pre>" + escape_html(
            "\n".join(
                f"{format_eta(time.time() - item.get('at', time.time()))} ago  "
                f"{truncate_text(item.get('name') or item.get('hash'), 70)}"
                for item in completed
            )
        ) + "</pre>"
        self.telegram.reply(chat_id, text, parse_mode="HTML")

    def handle_disk(self, chat_id: int) -> None:
        self.telegram.reply(
            chat_id,
            "<b>DISK // WATCH PATH</b>\n<pre>"
            + render_disk_panel(self.config.disk_watch_path)
            + "</pre>",
            parse_mode="HTML",
        )

    def handle_health(self, chat_id: int) -> None:
        telegram_ok = bool(self.telegram.call("getMe"))
        self.telegram.reply(
            chat_id,
            render_health_panel(self.config, self.qbit, telegram_ok),
            parse_mode="HTML",
        )

    def handle_logs(self, chat_id: int) -> None:
        lines = list(LOG_BUFFER)[-60:]
        if not lines:
            lines = ["No bot logs buffered."]
        text = "<b>BOT LOGS</b>\n<pre>" + escape_html("\n".join(lines)[-3500:]) + "</pre>"
        self.telegram.reply(chat_id, text, parse_mode="HTML")

    def handle_config(self, chat_id: int) -> None:
        values = {
            "TG_BOT_TOKEN": mask_secret(self.config.telegram_token),
            "QBIT_URL": self.config.qbit_url,
            "QBIT_USER": self.config.qbit_user,
            "QBIT_PASS": mask_secret(self.config.qbit_pass),
            "OWNERS": sorted(self.config.owner_user_ids),
            "ADMINS": sorted(self.config.admin_user_ids),
            "VIEWERS": sorted(self.config.viewer_user_ids),
            "APPROVAL_ENABLED": self.config.approval_enabled,
            "FREELEECH": self.bot_state().get("freeleech", False),
            "RSS_FEEDS": [feed["name"] for feed in self.config.rss_feeds],
        }
        self.telegram.reply(
            chat_id,
            "<b>SANITIZED CONFIG</b>\n<pre>"
            + escape_html(json.dumps(values, indent=2))
            + "</pre>",
            parse_mode="HTML",
        )

    def handle_freeleech(self, chat_id: int, args: str) -> None:
        value = args.strip().lower()
        state = self.bot_state()
        if value in {"on", "true", "1"}:
            state["freeleech"] = True
        elif value in {"off", "false", "0"}:
            state["freeleech"] = False
        elif value:
            raise BotError("Usage: /freeleech on|off")
        self.save_bot_state(state)
        status = "ON" if state.get("freeleech") else "OFF"
        self.telegram.reply(chat_id, f"<b>FREELEECH MODE // {status}</b>", parse_mode="HTML")

    def handle_pref(self, chat_id: int, args: str) -> None:
        parts = args.split()
        if not parts:
            raise BotError("Usage: /pref alt|dl|ul|queue ...")
        command = parts[0].lower()
        if command == "alt":
            self.qbit.toggle_alt_speed()
            self.telegram.reply(chat_id, "<b>PREF // toggled alt speed</b>", parse_mode="HTML")
        elif command in {"dl", "download"} and len(parts) == 2:
            self.qbit.set_download_limit(parse_size(parts[1]))
            self.telegram.reply(chat_id, "<b>PREF // download limit updated</b>", parse_mode="HTML")
        elif command in {"ul", "upload"} and len(parts) == 2:
            self.qbit.set_upload_limit(parse_size(parts[1]))
            self.telegram.reply(chat_id, "<b>PREF // upload limit updated</b>", parse_mode="HTML")
        elif command == "queue" and len(parts) == 2:
            enabled = parts[1].lower() in {"on", "true", "1"}
            self.qbit.set_preferences({"queueing_enabled": enabled})
            self.telegram.reply(chat_id, f"<b>PREF // queueing {parts[1]}</b>", parse_mode="HTML")
        else:
            raise BotError("Usage: /pref alt | /pref dl 5MiB | /pref ul 1MiB | /pref queue on")

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
        names = "\n".join(
            f"- {truncate_text(torrent.get('name') or torrent['hash'], 72)}" for torrent in matches
        )
        text = (
            f"<b>{escape_html(action.upper())} // {len(matches)} torrent(s)</b>\n"
            f"<pre>{escape_html(names)}</pre>"
        )
        self.telegram.reply(chat_id, text, parse_mode="HTML")

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
                    self.update_tracked_torrent_message(
                        item,
                        (
                            "<b>TRACKING STOPPED</b>\n"
                            f"<pre>{escape_html(item.name)} is no longer in qBittorrent.</pre>"
                        ),
                        now,
                    )
                    self.untrack_torrent(item.torrent_hash)
                    continue

                torrent = torrents[0]
                if float(torrent.get("progress") or 0) >= 1:
                    text = render_torrent_card(
                        torrent,
                        title="COMPLETE // ready",
                        footer="progress tracking ended",
                    )
                    self.update_tracked_torrent_message(item, text, now, final=True)
                    self.remember_recent(
                        "recent_completed",
                        {
                            "hash": item.torrent_hash,
                            "name": torrent.get("name") or item.name,
                            "at": now,
                        },
                    )
                    self.apply_torrent_action(
                        self.config.completion_action,
                        [item.torrent_hash],
                        category=self.config.completion_action_category,
                    )
                    self.untrack_torrent(item.torrent_hash)
                    continue

                text = render_torrent_card(
                    torrent,
                    title="SYNCING // live progress",
                    footer=(
                        "next refresh "
                        f"{format_eta(self.config.progress_update_interval_seconds)}"
                    ),
                )
                self.update_tracked_torrent_message(item, text, now)

    def update_tracked_torrent_message(
        self,
        item: TrackedTorrent,
        text: str,
        now: float,
        *,
        final: bool = False,
    ) -> None:
        if item.last_text == text and not final:
            with self.tracked_lock:
                if item.torrent_hash in self.tracked_torrents:
                    self.tracked_torrents[item.torrent_hash].last_sent_at = now
            return

        edited = None
        keyboard = torrent_keyboard(item.torrent_hash)
        if item.message_id:
            edited = self.telegram.edit_message(
                item.chat_id,
                item.message_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        if not edited:
            sent = self.telegram.reply(
                item.chat_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            item.message_id = sent.get("message_id") if sent else item.message_id

        with self.tracked_lock:
            if item.torrent_hash in self.tracked_torrents:
                self.tracked_torrents[item.torrent_hash].last_sent_at = now
                self.tracked_torrents[item.torrent_hash].last_text = text
                self.tracked_torrents[item.torrent_hash].message_id = item.message_id

    def untrack_torrent(self, torrent_hash: str) -> None:
        with self.tracked_lock:
            self.tracked_torrents.pop(torrent_hash, None)

    def apply_torrent_action(
        self,
        action: str,
        hashes: list[str],
        *,
        category: str | None = None,
    ) -> None:
        action = action.lower()
        if action in {"", "none", "notify"}:
            return
        if action == "pause":
            self.qbit.control_torrents("pause", hashes)
        elif action == "delete":
            self.qbit.delete_torrents(hashes, delete_files=False)
        elif action in {"delete_data", "delete+data"}:
            self.qbit.delete_torrents(hashes, delete_files=True)
        elif action in {"category", "move_category"}:
            if not category:
                LOGGER.warning("Category action requested without category")
                return
            self.qbit.set_category(hashes, category)
        else:
            LOGGER.warning("Unknown torrent action configured: %s", action)

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
                        "<b>DISK WATCH // threshold crossed</b>\n"
                        "<pre>"
                        f"PATH {escape_html(path)}\n"
                        f"USED {used_percent:.1f}% {disk_bar(used_percent)}\n"
                        f"FREE {format_bytes(usage.free)} / {format_bytes(usage.total)}"
                        "</pre>"
                    ),
                    parse_mode="HTML",
                )
                alerted.add(threshold)
            elif used_percent < threshold:
                alerted.discard(threshold)

        write_json_file(self.config.disk_alerts_file, {"alerted": sorted(alerted)})

    def ratio_alert_loop(self) -> None:
        if (
            (self.config.ratio_alert_target is None or self.config.ratio_alert_target <= 0)
            and not self.config.per_category_ratio_targets
        ):
            return

        while not self.stop_event.wait(15):
            try:
                self.check_ratio_alerts()
            except Exception:
                LOGGER.exception("Ratio alert poll failed")
            self.stop_event.wait(self.config.ratio_alert_interval_seconds)

    def check_ratio_alerts(self) -> None:
        torrents = self.qbit.torrents_info()
        state = read_json_file(self.config.ratio_alerts_file, {"alerted": []})
        alerted = {str(torrent_hash) for torrent_hash in state.get("alerted", [])}
        active_hashes = {torrent["hash"] for torrent in torrents if torrent.get("hash")}

        for torrent in torrents:
            torrent_hash = torrent.get("hash")
            if not torrent_hash or torrent_hash in alerted:
                continue
            category = str(torrent.get("category") or "").lower()
            target = self.config.per_category_ratio_targets.get(
                category,
                self.config.ratio_alert_target or 0,
            )
            if target <= 0:
                continue
            ratio = float(torrent.get("ratio") or 0)
            if ratio >= target:
                self.telegram.notify_allowed_users(
                    self.config.allowed_user_ids,
                    (
                        "<b>RATIO WATCH // target reached</b>\n"
                        "<pre>"
                        f"RATIO {ratio:.2f} >= {target:.2f}\n"
                        f"{escape_html(truncate_text(torrent.get('name') or torrent_hash, 96))}"
                        "</pre>"
                    ),
                    parse_mode="HTML",
                )
                self.apply_torrent_action(
                    self.config.ratio_action,
                    [torrent_hash],
                    category=self.config.ratio_action_category,
                )
                alerted.add(torrent_hash)

        write_json_file(self.config.ratio_alerts_file, {"alerted": sorted(alerted & active_hashes)})

    def torrent_alert_loop(self) -> None:
        while not self.stop_event.wait(20):
            try:
                self.check_torrent_alerts()
            except Exception:
                LOGGER.exception("Torrent alert poll failed")
            self.stop_event.wait(60)

    def check_torrent_alerts(self) -> None:
        if not (self.config.stalled_alert_enabled or self.config.low_speed_alert_enabled):
            return

        torrents = self.qbit.torrents_info()
        now = time.time()
        state = read_json_file(self.config.torrent_alerts_file, {"alerted": {}})
        alerted: dict[str, dict[str, float]] = state.get("alerted", {})

        with self.vitals_lock:
            for torrent in torrents:
                torrent_hash = torrent.get("hash")
                if not torrent_hash:
                    continue
                progress = float(torrent.get("progress") or 0)
                downloaded = int(torrent.get("downloaded") or 0)
                dlspeed = int(torrent.get("dlspeed") or 0)
                upspeed = int(torrent.get("upspeed") or 0)
                previous = self.vitals.get(torrent_hash)
                stagnant_since = now
                low_speed_since = None
                if previous:
                    stagnant_since = previous.stagnant_since
                    if downloaded > previous.downloaded or progress > previous.progress:
                        stagnant_since = now
                    low_speed_since = previous.low_speed_since

                active = is_active_torrent(torrent)
                low_speed = active and dlspeed + upspeed < self.config.low_speed_threshold_bytes
                if low_speed:
                    low_speed_since = low_speed_since or now
                else:
                    low_speed_since = None

                self.vitals[torrent_hash] = TorrentVitals(
                    progress=progress,
                    downloaded=downloaded,
                    dlspeed=dlspeed,
                    upspeed=upspeed,
                    seen_at=now,
                    stagnant_since=stagnant_since,
                    low_speed_since=low_speed_since,
                )

                if progress >= 1:
                    continue

                alerts_for_hash = alerted.setdefault(torrent_hash, {})
                if self.config.stalled_alert_enabled:
                    stalled_for = now - stagnant_since
                    last_alert = alerts_for_hash.get("stalled", 0)
                    if (
                        stalled_for >= self.config.stalled_alert_minutes * 60
                        and now - last_alert >= self.config.torrent_alert_interval_seconds
                    ):
                        self.telegram.notify_allowed_users(
                            self.config.allowed_user_ids,
                            "<b>STALL WATCH</b>\n<pre>"
                            + escape_html(torrent_line(torrent))
                            + "</pre>",
                            parse_mode="HTML",
                        )
                        alerts_for_hash["stalled"] = now

                if self.config.low_speed_alert_enabled and low_speed_since:
                    slow_for = now - low_speed_since
                    last_alert = alerts_for_hash.get("low_speed", 0)
                    if (
                        slow_for >= self.config.low_speed_minutes * 60
                        and now - last_alert >= self.config.torrent_alert_interval_seconds
                    ):
                        self.telegram.notify_allowed_users(
                            self.config.allowed_user_ids,
                            "<b>LOW SPEED WATCH</b>\n<pre>"
                            + escape_html(torrent_line(torrent))
                            + "</pre>",
                            parse_mode="HTML",
                        )
                        alerts_for_hash["low_speed"] = now

        active_hashes = {torrent["hash"] for torrent in torrents if torrent.get("hash")}
        alerted = {key: value for key, value in alerted.items() if key in active_hashes}
        write_json_file(self.config.torrent_alerts_file, {"alerted": alerted})

    def rss_watch_loop(self) -> None:
        while not self.stop_event.wait(10):
            try:
                self.check_rss_feeds()
            except Exception:
                LOGGER.exception("RSS watcher failed")
            self.stop_event.wait(self.config.rss_interval_seconds)

    def check_rss_feeds(self) -> None:
        seen_state = read_json_file(self.config.rss_state_file, {"seen": []})
        seen = set(seen_state.get("seen", []))
        changed = False

        for feed in self.config.rss_feeds:
            response = requests.get(feed["url"], timeout=30)
            response.raise_for_status()
            for item in parse_rss_items(response.text):
                marker = short_id(f"{feed['name']}:{item['title']}:{item['link']}")
                if marker in seen:
                    continue
                include = feed.get("include") or ""
                if include and include.lower() not in item["title"].lower():
                    continue
                category = normalize_category(feed.get("category") or None)
                previous_hashes = self.qbit.torrent_hashes()
                add_category, save_path, tags, ratio_limit = self.resolve_add_options(category)
                self.qbit.add_url(
                    item["link"],
                    category=add_category,
                    save_path=save_path,
                    tags=tags,
                    ratio_limit=ratio_limit,
                )
                new_torrents = self.qbit.wait_for_new_torrents(previous_hashes)
                self.announce_added_torrents(
                    next(iter(self.config.owner_user_ids)),
                    item["title"],
                    new_torrents,
                    category,
                )
                seen.add(marker)
                changed = True

        if changed:
            write_json_file(self.config.rss_state_file, {"seen": sorted(seen)[-1000:]})


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
