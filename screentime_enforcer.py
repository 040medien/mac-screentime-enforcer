#!/usr/bin/env python3
"""
Home Assistant macOS Screen Time Agent
======================================

A lightweight, locally running agent that:
1. Tracks whether the child session is actively used.
2. Publishes usage statistics and heartbeat data to Home Assistant via MQTT.
3. Subscribes to a retained `screen/<child>/allowed` topic and enforces locks/logouts
   when Home Assistant denies access (budget exceeded, schedule blocks, etc.).

The script is designed to be installed under `/Library/Application Support/ha-screen-agent`
and launched for the child user via `/Library/LaunchAgents`. See README for details.
"""

from __future__ import annotations

import json
import logging
import os
import locale
import platform
import re
import signal
import subprocess
import sys
import time
import plistlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
from Quartz.CoreGraphics import (  # type: ignore
    CGEventSourceSecondsSinceLastEventType,
    CGSessionCopyCurrentDictionary,
    kCGAnyInputEventType,
    kCGEventSourceStateHIDSystemState,
)

VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = "/Library/Application Support/ha-screen-agent/config.json"
DEFAULT_STATE_PATH = (
    Path.home() / "Library" / "Application Support" / "ha-screen-agent" / "state.json"
)
DEFAULT_LOG_PATH = "/tmp/ha_screen_agent.out.log"
DEFAULT_ERR_LOG_PATH = "/tmp/ha_screen_agent.err.log"


SUPPORTED_LANG_PHRASES = {
    "en": {
        "title": "Screen Time",
        "final_voice": "You have used all your screen time {child_id}.",
        "warn5_body": "5 minutes of screen time remain.",
        "warn5_voice": "You have five minutes of screen time left.",
        "warn1_body": "1 minute of screen time remains.",
        "warn1_voice": "You have one minute of screen time left.",
        "login_remaining_body": "You have {minutes} minutes of screen time left today.",
        "login_remaining_voice": "You have {minutes} minutes of screen time left today.",
        "login_out_body": "Screen time is out for today.",
        "login_out_voice": "Your screen time is used up for today.",
    },
    "de": {
        "title": "Bildschirmzeit",
        "final_voice": "Du hast deine Bildschirmzeit aufgebraucht {child_id}.",
        "warn5_body": "Noch 5 Minuten Bildschirmzeit übrig.",
        "warn5_voice": "Du hast noch fünf Minuten Bildschirmzeit.",
        "warn1_body": "Noch 1 Minute Bildschirmzeit übrig.",
        "warn1_voice": "Du hast noch eine Minute Bildschirmzeit.",
        "login_remaining_body": "Du hast noch {minutes} Minuten Bildschirmzeit heute.",
        "login_remaining_voice": "Du hast noch {minutes} Minuten Bildschirmzeit heute.",
        "login_out_body": "Für heute ist keine Bildschirmzeit mehr übrig.",
        "login_out_voice": "Deine Bildschirmzeit für heute ist aufgebraucht.",
    },
    "fr": {
        "title": "Temps d'écran",
        "final_voice": "Tu as utilisé tout ton temps d'écran {child_id}.",
        "warn5_body": "Il reste 5 minutes de temps d'écran.",
        "warn5_voice": "Il te reste cinq minutes de temps d'écran.",
        "warn1_body": "Il reste 1 minute de temps d'écran.",
        "warn1_voice": "Il te reste une minute de temps d'écran.",
        "login_remaining_body": "Il te reste {minutes} minutes de temps d'écran aujourd'hui.",
        "login_remaining_voice": "Il te reste {minutes} minutes de temps d'écran aujourd'hui.",
        "login_out_body": "Plus de temps d'écran pour aujourd'hui.",
        "login_out_voice": "Ton temps d'écran est terminé pour aujourd'hui.",
    },
    "es": {
        "title": "Tiempo de pantalla",
        "final_voice": "Has usado todo tu tiempo de pantalla {child_id}.",
        "warn5_body": "Quedan 5 minutos de tiempo de pantalla.",
        "warn5_voice": "Te quedan cinco minutos de tiempo de pantalla.",
        "warn1_body": "Queda 1 minuto de tiempo de pantalla.",
        "warn1_voice": "Te queda un minuto de tiempo de pantalla.",
        "login_remaining_body": "Te quedan {minutes} minutos de tiempo de pantalla hoy.",
        "login_remaining_voice": "Te quedan {minutes} minutos de tiempo de pantalla hoy.",
        "login_out_body": "No queda tiempo de pantalla para hoy.",
        "login_out_voice": "Tu tiempo de pantalla para hoy se ha terminado.",
    },
    "it": {
        "title": "Tempo schermo",
        "final_voice": "Hai usato tutto il tempo schermo {child_id}.",
        "warn5_body": "Restano 5 minuti di tempo schermo.",
        "warn5_voice": "Ti restano cinque minuti di tempo schermo.",
        "warn1_body": "Resta 1 minuto di tempo schermo.",
        "warn1_voice": "Ti resta un minuto di tempo schermo.",
        "login_remaining_body": "Hai ancora {minutes} minuti di tempo schermo oggi.",
        "login_remaining_voice": "Hai ancora {minutes} minuti di tempo schermo oggi.",
        "login_out_body": "Nessun tempo schermo rimasto per oggi.",
        "login_out_voice": "Hai terminato il tempo schermo per oggi.",
    },
    "nl": {
        "title": "Schermtijd",
        "final_voice": "Je hebt al je schermtijd gebruikt {child_id}.",
        "warn5_body": "Nog 5 minuten schermtijd over.",
        "warn5_voice": "Je hebt nog vijf minuten schermtijd.",
        "warn1_body": "Nog 1 minuut schermtijd over.",
        "warn1_voice": "Je hebt nog één minuut schermtijd.",
        "login_remaining_body": "Je hebt nog {minutes} minuten schermtijd vandaag.",
        "login_remaining_voice": "Je hebt nog {minutes} minuten schermtijd vandaag.",
        "login_out_body": "Geen schermtijd meer over voor vandaag.",
        "login_out_voice": "Je schermtijd voor vandaag is op.",
    },
    "pt": {
        "title": "Tempo de tela",
        "final_voice": "Você usou todo o seu tempo de tela {child_id}.",
        "warn5_body": "Restam 5 minutos de tempo de tela.",
        "warn5_voice": "Você tem cinco minutos de tempo de tela restantes.",
        "warn1_body": "Resta 1 minuto de tempo de tela.",
        "warn1_voice": "Você tem um minuto de tempo de tela restante.",
        "login_remaining_body": "Você tem {minutes} minutos de tempo de tela hoje.",
        "login_remaining_voice": "Você tem {minutes} minutos de tempo de tela hoje.",
        "login_out_body": "Sem tempo de tela restante para hoje.",
        "login_out_voice": "Seu tempo de tela de hoje acabou.",
    },
    "ja": {
        "title": "スクリーンタイム",
        "final_voice": "{child_id} のスクリーンタイムを使い切りました。",
        "warn5_body": "スクリーンタイムはあと5分です。",
        "warn5_voice": "スクリーンタイムはあと5分です。",
        "warn1_body": "スクリーンタイムはあと1分です。",
        "warn1_voice": "スクリーンタイムはあと1分です。",
        "login_remaining_body": "今日はスクリーンタイムがあと{minutes}分残っています。",
        "login_remaining_voice": "今日はスクリーンタイムがあと{minutes}分残っています。",
        "login_out_body": "今日はスクリーンタイムがもうありません。",
        "login_out_voice": "今日のスクリーンタイムは終わりました。",
    },
    "zh": {
        "title": "屏幕使用时间",
        "final_voice": "你已用完所有屏幕时间 {child_id}。",
        "warn5_body": "屏幕时间还剩 5 分钟。",
        "warn5_voice": "屏幕时间还剩五分钟。",
        "warn1_body": "屏幕时间还剩 1 分钟。",
        "warn1_voice": "屏幕时间还剩一分钟。",
        "login_remaining_body": "今天还剩 {minutes} 分钟的屏幕时间。",
        "login_remaining_voice": "今天还剩 {minutes} 分钟的屏幕时间。",
        "login_out_body": "今天的屏幕时间已用完。",
        "login_out_voice": "今天的屏幕时间已经用完了。",
    },
}




def _normalize_lang(value: str) -> str:
    if not value:
        return "en"
    value = value.split(',')[0]
    for sep in ('-','_'):
        if sep in value:
            value = value.split(sep)[0]
            break
    return value.lower() or "en"


def _detect_language() -> str:
    candidates = []
    for key in ("LANGUAGE", "LANG", "APPLELANGUAGE"):
        val = os.environ.get(key)
        if val:
            candidates.append(val)
    try:
        plist_path = Path.home() / "Library" / "Preferences" / ".GlobalPreferences.plist"
        if plist_path.exists():
            with plist_path.open("rb") as handle:
                prefs = plistlib.load(handle)
            apple_langs = prefs.get("AppleLanguages") or []
            candidates.extend(apple_langs)
    except Exception:
        pass
    try:
        loc = locale.getdefaultlocale()[0]
        if loc:
            candidates.append(loc)
    except Exception:
        pass
    for cand in candidates:
        code = _normalize_lang(cand)
        if code in SUPPORTED_LANG_PHRASES:
            return code
    return "en"


def _sanitize_device_id(value: str) -> str:
    sanitized = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.lower())
    return sanitized or "mac"


def _validate_topic_segment(value: str, field: str) -> str:
    if not value:
        raise ValueError(f"Config `{field}` is required.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(
            f"Config `{field}` must use only letters, numbers, hyphens, or underscores (got {value!r})."
        )
    return value


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _as_bool(payload: str) -> Optional[bool]:
    normalized = payload.strip().lower()
    if normalized in {"1", "true", "on", "yes"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    return None


@dataclass
class AgentConfig:
    child_id: str
    device_id: str
    mqtt_host: str
    topic_prefix: str
    mqtt_port: int = 1883
    mqtt_username: Optional[str] = None
    mqtt_password: Optional[str] = None
    mqtt_tls: bool = False
    sample_interval_seconds: int = 15
    blocked_check_seconds: float = 1.0
    idle_timeout_seconds: int = 120
    enforcement_mode: str = "lock"  # lock | logout
    logout_method: str = "osascript"  # osascript | kill_loginwindow
    fail_mode: str = "safe"  # safe | open
    offline_grace_period_seconds: int = 0
    rapid_relogin_shutdown_enabled: bool = True
    rapid_relogin_window_seconds: int = 60
    rapid_relogin_max_attempts: int = 4
    rapid_relogin_warn_attempt: int = 3
    rapid_relogin_warn_voice: bool = True
    state_path: Path = DEFAULT_STATE_PATH
    log_file: str = DEFAULT_LOG_PATH
    err_log_file: str = DEFAULT_ERR_LOG_PATH
    debug_mqtt: bool = False
    track_active_app: bool = False
    managed_user: Optional[str] = None

    @classmethod
    def load(cls, path: Path, session_user: Optional[str] = None) -> Optional["AgentConfig"]:
        session_user = session_user or os.environ.get("USER") or os.path.basename(Path.home())
        if not path.exists():
            raise FileNotFoundError(
                f"Required config file missing at {path}. "
                "Run the installer script to create it."
            )
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        managed_user: Optional[str] = None

        managed_users_raw = data.get("managed_users")
        if not managed_users_raw:
            raise ValueError("Config `managed_users` is required (list of objects).")
        if not isinstance(managed_users_raw, list):
            raise ValueError("`managed_users` must be a list of mappings.")

        selected = None
        for entry in managed_users_raw:
            if not isinstance(entry, dict):
                raise ValueError("Each entry in `managed_users` must be an object.")
            mac_user = (entry.get("mac_user_account") or "").strip()
            child_name = (entry.get("child_name") or "").strip()
            entry_prefix = (entry.get("topic_prefix") or "").strip()
            entry_device = (entry.get("device_id") or "").strip()
            if not mac_user or not child_name:
                raise ValueError("`managed_users` entries require mac_user_account and child_name.")
            if session_user and mac_user == session_user:
                selected = {
                    "mac_user_account": mac_user,
                    "child_name": child_name,
                    "topic_prefix": entry_prefix,
                    "device_id": entry_device,
                }
                break

        if selected is None:
            return None

        managed_user = selected["mac_user_account"]
        child_id = _validate_topic_segment(selected["child_name"], "child_name")
        topic_prefix = selected["topic_prefix"] or f"screen/{child_id}"
        if not topic_prefix.startswith(f"screen/{child_id}"):
            raise ValueError(
                "Config `topic_prefix` must start with `screen/<child_id>` "
                f"(expected prefix `screen/{child_id}`)."
            )
        device_id_raw = selected["device_id"] or data.get("device_id")

        mqtt_host = data.get("mqtt_host", "").strip()
        if not mqtt_host:
            raise ValueError("Config `mqtt_host` is required.")

        device_id = device_id_raw
        if not device_id:
            device_id = _sanitize_device_id(platform.node() or "mac")

        mqtt_port = int(data.get("mqtt_port", 1883))
        if not (1 <= mqtt_port <= 65535):
            raise ValueError("Config `mqtt_port` must be between 1 and 65535.")

        sample_interval = int(data.get("sample_interval_seconds", 15))
        if not (5 <= sample_interval <= 60):
            raise ValueError("`sample_interval_seconds` must be between 5 and 60.")

        idle_timeout = int(data.get("idle_timeout_seconds", 120))
        if idle_timeout < sample_interval:
            raise ValueError("`idle_timeout_seconds` must be >= sample interval.")

        blocked_check_seconds = float(data.get("blocked_check_seconds", 1.0))
        if blocked_check_seconds < 0.5 or blocked_check_seconds > 10:
            raise ValueError("`blocked_check_seconds` must be between 0.5 and 10 seconds.")

        enforcement_mode = data.get("enforcement_mode", "lock").lower()
        if enforcement_mode not in {"lock", "logout"}:
            raise ValueError("`enforcement_mode` must be lock or logout.")

        logout_method = data.get("logout_method", "osascript").lower()
        if logout_method not in {"osascript", "kill_loginwindow"}:
            raise ValueError("`logout_method` must be osascript or kill_loginwindow.")

        fail_mode = data.get("fail_mode", "safe").lower()
        if fail_mode not in {"safe", "open"}:
            raise ValueError("`fail_mode` must be safe or open.")

        grace = int(data.get("offline_grace_period_seconds", 0))
        if grace < 0 or grace > 900:
            raise ValueError("`offline_grace_period_seconds` must be between 0 and 900.")

        rapid_relogin_shutdown_enabled = bool(data.get("rapid_relogin_shutdown_enabled", True))
        rapid_relogin_window_seconds = int(data.get("rapid_relogin_window_seconds", 60))
        if rapid_relogin_window_seconds < 5 or rapid_relogin_window_seconds > 300:
            raise ValueError("`rapid_relogin_window_seconds` must be between 5 and 300.")
        rapid_relogin_max_attempts = int(data.get("rapid_relogin_max_attempts", 4))
        if rapid_relogin_max_attempts < 2 or rapid_relogin_max_attempts > 10:
            raise ValueError("`rapid_relogin_max_attempts` must be between 2 and 10.")
        rapid_relogin_warn_attempt = int(data.get("rapid_relogin_warn_attempt", 3))
        if rapid_relogin_warn_attempt < 1 or rapid_relogin_warn_attempt >= rapid_relogin_max_attempts:
            raise ValueError("`rapid_relogin_warn_attempt` must be at least 1 and less than `rapid_relogin_max_attempts`.")
        rapid_relogin_warn_voice = bool(data.get("rapid_relogin_warn_voice", True))

        state_path = Path(
            data.get("state_path", str(DEFAULT_STATE_PATH))
        ).expanduser()

        log_file = data.get("log_file", DEFAULT_LOG_PATH)
        err_file = data.get("err_log_file", DEFAULT_ERR_LOG_PATH)
        debug_mqtt = bool(data.get("debug_mqtt", False))
        track_active_app = bool(data.get("track_active_app", False))

        return cls(
            child_id=child_id,
            device_id=_sanitize_device_id(device_id),
            mqtt_host=mqtt_host,
            topic_prefix=topic_prefix.rstrip("/"),
            mqtt_port=mqtt_port,
            mqtt_username=data.get("mqtt_username"),
            mqtt_password=data.get("mqtt_password"),
            mqtt_tls=bool(data.get("mqtt_tls", False)),
            sample_interval_seconds=sample_interval,
            idle_timeout_seconds=idle_timeout,
            blocked_check_seconds=blocked_check_seconds,
            enforcement_mode=enforcement_mode,
            logout_method=logout_method,
            fail_mode=fail_mode,
            offline_grace_period_seconds=grace,
            rapid_relogin_shutdown_enabled=rapid_relogin_shutdown_enabled,
            rapid_relogin_window_seconds=rapid_relogin_window_seconds,
            rapid_relogin_max_attempts=rapid_relogin_max_attempts,
            rapid_relogin_warn_attempt=rapid_relogin_warn_attempt,
            rapid_relogin_warn_voice=rapid_relogin_warn_voice,
            state_path=state_path,
            log_file=log_file,
            err_log_file=err_file,
            debug_mqtt=debug_mqtt,
            track_active_app=track_active_app,
            managed_user=managed_user,
        )

    @property
    def minutes_topic(self) -> str:
        return f"{self.topic_prefix}/mac/{self.device_id}/minutes_today"

    @property
    def active_topic(self) -> str:
        return f"{self.topic_prefix}/mac/{self.device_id}/active"

    @property
    def status_topic(self) -> str:
        return f"{self.topic_prefix}/mac/{self.device_id}/status"

    @property
    def availability_topic(self) -> str:
        return f"{self.topic_prefix}/mac/{self.device_id}/availability"

    @property
    def allow_topic(self) -> str:
        return f"{self.topic_prefix}/allowed"

    @property
    def discovery_base_id(self) -> str:
        return f"{self.child_id}_{self.device_id}_mac"

    @property
    def budget_state_topic(self) -> str:
        return f"homeassistant/{self.discovery_base_id}/daily_budget/state"

    @property
    def active_app_topic(self) -> str:
        return f"{self.topic_prefix}/mac/{self.device_id}/active_app"

    @property
    def override_state_topic(self) -> str:
        return f"homeassistant/{self.discovery_base_id}/override/state"


class UsageState:
    def __init__(self, path: Path):
        self.path = path
        self._data = {
            "date": _now_local().date().isoformat(),
            "seconds_today": 0.0,
            "rapid_relogin_attempts": [],
        }
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            attempts = data.get("rapid_relogin_attempts")
            if not isinstance(attempts, list):
                attempts = []
            sanitized_attempts = []
            for attempt in attempts:
                try:
                    sanitized_attempts.append(float(attempt))
                except Exception:
                    continue
            if data.get("date") == _now_local().date().isoformat():
                self._data = {
                    "date": data.get("date"),
                    "seconds_today": float(data.get("seconds_today", 0.0)),
                    "rapid_relogin_attempts": sanitized_attempts,
                }
        except FileNotFoundError:
            pass
        except Exception:
            logging.getLogger("ha-screen-agent").warning(
                "Failed to read state file, starting fresh.", exc_info=True
            )

    def add_seconds(self, seconds: float) -> None:
        self._data["seconds_today"] = float(self._data.get("seconds_today", 0.0)) + max(
            0.0, seconds
        )

    def minutes_today(self) -> int:
        return int(self._data.get("seconds_today", 0.0) // 60)

    def prune_rapid_relogin_attempts(self, window_seconds: int, now_monotonic: Optional[float] = None) -> None:
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        attempts = self._data.get("rapid_relogin_attempts", [])
        self._data["rapid_relogin_attempts"] = [
            float(attempt) for attempt in attempts if now_monotonic - float(attempt) <= window_seconds
        ]

    def rapid_relogin_attempt_count(self, window_seconds: int, now_monotonic: Optional[float] = None) -> int:
        self.prune_rapid_relogin_attempts(window_seconds=window_seconds, now_monotonic=now_monotonic)
        return len(self._data.get("rapid_relogin_attempts", []))

    def add_rapid_relogin_attempt(self, now_monotonic: Optional[float] = None) -> None:
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        attempts = list(self._data.get("rapid_relogin_attempts", []))
        attempts.append(float(now_monotonic))
        self._data["rapid_relogin_attempts"] = attempts

    def clear_rapid_relogin_attempts(self) -> None:
        self._data["rapid_relogin_attempts"] = []

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(self._data, handle)
            tmp_path.replace(self.path)
        except Exception:
            logging.getLogger("ha-screen-agent").error(
                "Unable to persist usage state.", exc_info=True
            )

    def ensure_today(self) -> None:
        today = _now_local().date().isoformat()
        if self._data.get("date") != today:
            self._data = {
                "date": today,
                "seconds_today": 0.0,
                "rapid_relogin_attempts": [],
            }
            self.save()


class ScreenTimeAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.state = UsageState(self.config.state_path)
        self.logger = logging.getLogger("ha-screen-agent")

        self._mqtt_client = self._build_mqtt_client()
        self._mqtt_connected = False
        self._offline_since: Optional[float] = None
        self._allowed: Optional[bool] = None
        self._last_allowed_payload: Optional[str] = None
        self._last_minutes_published: Optional[int] = None
        self._last_status_publish = 0.0
        self._last_state_save = 0.0
        self._last_tick = time.monotonic()
        self._running = True
        self._ignored_retained_block = False
        self._language = _detect_language()
        self._budget_minutes: Optional[float] = None
        self._warned_5 = False
        self._warned_1 = False
        self._last_active_app: Optional[str] = None
        self._discovery_published = False
        self._login_announced = False

    def _phrase(self, key: str) -> str:
        lang = self._language if self._language in SUPPORTED_LANG_PHRASES else "en"
        try:
            return SUPPORTED_LANG_PHRASES[lang][key]
        except Exception:
            return SUPPORTED_LANG_PHRASES["en"].get(key, "")

    # ------------------------------------------------------------------ MQTT --

    @staticmethod
    def _mqtt_rc_reason(rc: int) -> str:
        rc_map = {
            0: "success",
            1: "incorrect protocol version",
            2: "invalid client identifier",
            3: "server unavailable",
            4: "bad username or password",
            5: "not authorized",
        }
        return rc_map.get(rc, "unknown")

    def _build_mqtt_client(self) -> mqtt.Client:
        suffix = self.config.managed_user or self.config.child_id
        client_id = f"ha-screen-agent-{self.config.device_id}-{suffix}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        # Ensure HA receives an inactive state if the agent dies unexpectedly (non-retained).
        client.will_set(self.config.active_topic, payload="0", qos=1, retain=False)
        if self.config.mqtt_username:
            client.username_pw_set(
                self.config.mqtt_username, password=self.config.mqtt_password or None
            )
        if self.config.mqtt_tls:
            client.tls_set()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        if self.config.debug_mqtt:
            mqtt_logger = logging.getLogger("ha-screen-agent.mqtt")
            client.enable_logger(mqtt_logger)
            client.on_log = lambda c, u, level, buf: mqtt_logger.debug("paho log %s: %s", level, buf)
        return client

    def _discovery_device(self) -> dict:
        dev_id = f"{self.config.child_id}_{self.config.device_id}_mac"
        return {
            "identifiers": [dev_id],
            "name": f"{self.config.child_id} mac",
            "manufacturer": "Screen Time Agent",
            "model": "macOS agent",
            "sw_version": VERSION,
        }

    def _publish_discovery(self) -> None:
        if self._discovery_published:
            return
        try:
            device = self._discovery_device()
            base_id = f"{self.config.child_id}_{self.config.device_id}_mac"
            availability = {
                "availability_topic": self.config.availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
            }
            disc = [
                (
                    "sensor",
                    f"{base_id}_minutes",
                    {
                        "name": f"{self.config.child_id} Mac Minutes",
                        "unique_id": f"{base_id}_minutes",
                        "state_topic": self.config.minutes_topic,
                        "device_class": "duration",
                        "state_class": "total_increasing",
                        "unit_of_measurement": "min",
                        "icon": "mdi:timer-outline",
                        "device": device,
                        **availability,
                    },
                ),
                (
                    "binary_sensor",
                    f"{base_id}_active",
                    {
                        "name": f"{self.config.child_id} Mac Active",
                        "unique_id": f"{base_id}_active",
                        "state_topic": self.config.active_topic,
                        "payload_on": "1",
                        "payload_off": "0",
                        "device_class": "running",
                        "icon": "mdi:laptop",
                        "device": device,
                        **availability,
                    },
                ),
            ]
            if self.config.track_active_app:
                disc.append(
                    (
                        "sensor",
                        f"{base_id}_active_app",
                        {
                            "name": f"{self.config.child_id} Mac Active App",
                            "unique_id": f"{base_id}_active_app",
                            "state_topic": self.config.active_app_topic,
                            "icon": "mdi:laptop",
                            "device": device,
                            **availability,
                        },
                    )
                )
            extra = [
                (
                    "switch",
                    f"{base_id}_allowed",
                    {
                        "name": f"{self.config.child_id} Mac Allowed",
                        "unique_id": f"{base_id}_allowed",
                        "state_topic": self.config.allow_topic,
                        "command_topic": self.config.allow_topic,
                        "payload_on": "1",
                        "payload_off": "0",
                        "icon": "mdi:shield-check",
                        "device": device,
                        **availability,
                    },
                ),
                (
                    "number",
                    f"{base_id}_daily_budget_minutes",
                    {
                        "name": f"{self.config.child_id} Mac Daily Budget (min)",
                        "unique_id": f"{base_id}_daily_budget_minutes",
                        "state_topic": f"homeassistant/{base_id}/daily_budget/state",
                        "command_topic": f"homeassistant/{base_id}/daily_budget/state",
                        "min": 0,
                        "max": 240,
                        "step": 5,
                        "mode": "box",
                        "unit_of_measurement": "min",
                        "icon": "mdi:timer-sand",
                        "device": device,
                        **availability,
                    },
                ),
                (
                    "switch",
                    f"{base_id}_parent_override",
                    {
                        "name": f"{self.config.child_id} Mac Parent Override",
                        "unique_id": f"{base_id}_parent_override",
                        "state_topic": self.config.override_state_topic,
                        "command_topic": self.config.override_state_topic,
                        "payload_on": "ON",
                        "payload_off": "OFF",
                        "icon": "mdi:shield-star",
                        "device": device,
                        **availability,
                    },
                ),
            ]
            disc.extend(extra)
            for domain, obj_id, payload in disc:
                topic = f"homeassistant/{domain}/{obj_id}/config"
                self._mqtt_client.publish(topic, json.dumps(payload), retain=True, qos=1)
            # Ensure the override switch has a defined initial state for automations.
            self._mqtt_client.publish(
                self.config.override_state_topic, payload="OFF", retain=True, qos=1
            )
            self._discovery_published = True
        except Exception:
            self.logger.warning("Failed to publish MQTT discovery topics.", exc_info=True)

    def start(self) -> None:
        self.logger.info("Starting HA Screen Agent v%s", VERSION)
        self._connect_mqtt()
        self._main_loop()

    def _connect_mqtt(self) -> None:
        self.logger.info(
            "Connecting to MQTT %s:%s",
            self.config.mqtt_host,
            self.config.mqtt_port,
        )
        try:
            self._mqtt_client.connect_async(
                self.config.mqtt_host, self.config.mqtt_port, keepalive=60
            )
        except Exception as exc:
            self.logger.error(
                "Failed to start MQTT connection to %s:%s: %s",
                self.config.mqtt_host,
                self.config.mqtt_port,
                exc,
            )
            return
        self._mqtt_client.loop_start()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Dict[str, Any],
        reason_code: mqtt.ReasonCode,
        properties: Optional[mqtt.Properties] = None,
    ):
        rc = getattr(reason_code, "value", reason_code)
        try:
            rc = int(rc)
        except Exception:
            self.logger.warning("Unexpected reason_code type on connect: %r", reason_code)
            rc = -1
        if rc == 0:
            self.logger.info("Connected to MQTT broker (rc=0: success).")
            self._mqtt_connected = True
            self._offline_since = None
            self._ignored_retained_block = False
            self._language = _detect_language()
            client.subscribe(self.config.allow_topic)
            client.subscribe(self.config.budget_state_topic)
            if self.config.track_active_app:
                self._last_active_app = None
            # Request retained allowed value ASAP
            client.publish(self.config.availability_topic, payload="online", retain=True, qos=1)
            client.publish(
                self.config.status_topic,
                json.dumps({"event": "online", "version": VERSION}),
                qos=1,
                retain=False,
            )
            self._publish_discovery()
        else:
            self.logger.error("MQTT connection failed (rc=%s: %s)", rc, self._mqtt_rc_reason(rc))

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: mqtt.ReasonCode,
        properties: Optional[mqtt.Properties] = None,
    ):
        rc_int = getattr(reason_code, "value", reason_code)
        try:
            rc_int = int(rc_int)
        except Exception:
            self.logger.warning("Unexpected reason_code type on disconnect: %r", reason_code)
            rc_int = reason_code
        self._mqtt_connected = False
        if rc_int != 0:
            self.logger.warning("Unexpected MQTT disconnect (rc=%s: %s)", rc_int, self._mqtt_rc_reason(rc_int))
        self._offline_since = time.monotonic()

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage):
        payload = (message.payload or b"").decode("utf-8", errors="ignore")

        if message.topic == self.config.budget_state_topic:
            try:
                budget_val = float(payload)
            except Exception:
                self.logger.warning("Invalid budget payload '%s' on %s", payload, message.topic)
                return
            self._budget_minutes = max(0.0, budget_val)
            self._warned_5 = False
            self._warned_1 = False
            self._maybe_announce_initial_remaining()
            return

        allowed = _as_bool(payload)
        if allowed is None:
            self.logger.warning(
                "Received invalid allowed payload '%s' on %s",
                payload,
                message.topic,
            )
            return

        if (
            self.config.fail_mode == "open"
            and allowed is False
            and getattr(message, "retain", False)
            and not self._ignored_retained_block
        ):
            self.logger.info("Ignoring retained allowed=0 on connect (fail_mode=open).")
            self._ignored_retained_block = True
            return

        previous = self._allowed
        self._allowed = allowed
        self._last_allowed_payload = payload
        self.logger.info("Allowed state updated to %s", allowed)


        if not allowed and (previous is None or previous != allowed):
            self._enforce_block()

    # ----------------------------------------------------------- MAIN LOOP --

    def _main_loop(self) -> None:
        try:
            while self._running:
                loop_start = time.monotonic()
                elapsed = loop_start - self._last_tick
                self._last_tick = loop_start

                self.state.ensure_today()
                loop_now = time.monotonic()
                self.state.prune_rapid_relogin_attempts(
                    self.config.rapid_relogin_window_seconds,
                    now_monotonic=loop_now,
                )
                allowed_now = self._current_allowed_state()
                blocked = not allowed_now
                session_locked = self._is_session_locked()
                active = False if blocked else self._is_active_session(session_locked=session_locked)
                if active:
                    self.state.add_seconds(elapsed)

                minutes_now = self.state.minutes_today()
                self._check_budget_warnings(minutes_today=minutes_now)
                self._maybe_announce_initial_remaining()
                active_app = (
                    self._frontmost_app_name()
                    if self.config.track_active_app and active
                    else None
                )

                self._handle_rapid_relogin_protection(
                    blocked=blocked,
                    session_locked=session_locked,
                    now_monotonic=loop_now,
                )
                self._maybe_save_state()
                self._publish_metrics_if_needed(
                    active_now=active,
                    active_app=active_app,
                    force=blocked or not self._mqtt_connected,
                )
                if blocked:
                    self._enforce_block(active_now=active)
                else:
                    self._enforce_if_required(active_now=active)

                sleep_time = (
                    float(self.config.blocked_check_seconds)
                    if blocked
                    else max(1.0, float(self.config.sample_interval_seconds))
                )
                time.sleep(sleep_time)
        except KeyboardInterrupt:
            self.logger.info("Stopping agent (SIGINT).")
        finally:
            self._shutdown()

    # ------------------------------------------------------ STATE & METRICS --

    def _maybe_save_state(self) -> None:
        now = time.monotonic()
        if now - self._last_state_save >= 30:
            self.state.save()
            self._last_state_save = now

    def _check_budget_warnings(self, minutes_today: int) -> None:
        if self._budget_minutes is None:
            return
        remaining = self._budget_minutes - minutes_today
        if remaining <= 1 and not self._warned_1:
            self._notify_remaining(1, voice_only=True)
            self._warned_1 = True
            self._warned_5 = True
        elif remaining <= 5 and not self._warned_5:
            self._notify_remaining(5, voice_only=False)
            self._warned_5 = True
        if remaining > 5:
            self._warned_5 = False
        if remaining > 1:
            self._warned_1 = False

    def _rapid_relogin_attempt_count(self, now_monotonic: Optional[float] = None) -> int:
        return self.state.rapid_relogin_attempt_count(
            self.config.rapid_relogin_window_seconds,
            now_monotonic=now_monotonic,
        )

    def _handle_rapid_relogin_protection(
        self, blocked: bool, session_locked: bool, now_monotonic: float
    ) -> None:
        if not self.config.rapid_relogin_shutdown_enabled:
            self._last_session_locked = session_locked
            self._blocked_unlock_counted = False if (not blocked or session_locked) else self._blocked_unlock_counted
            return

        if not blocked:
            if self._rapid_relogin_attempt_count(now_monotonic) > 0:
                self.logger.info("Clearing rapid relogin streak after access restored.")
            self.state.clear_rapid_relogin_attempts()
            self._rapid_relogin_warned_count = 0
            self._blocked_unlock_counted = False
            self._last_session_locked = session_locked
            return

        if session_locked:
            self._blocked_unlock_counted = False
        elif self._last_session_locked and not self._blocked_unlock_counted:
            self.state.add_rapid_relogin_attempt(now_monotonic)
            attempt_count = self._rapid_relogin_attempt_count(now_monotonic)
            self._blocked_unlock_counted = True
            self.logger.warning(
                "Rapid relogin attempt detected while blocked: %s/%s within %ss.",
                attempt_count,
                self.config.rapid_relogin_max_attempts,
                self.config.rapid_relogin_window_seconds,
            )
            if (
                self.config.rapid_relogin_warn_voice
                and attempt_count >= self.config.rapid_relogin_warn_attempt
                and self._rapid_relogin_warned_count < self.config.rapid_relogin_warn_attempt
            ):
                self._notify_rapid_relogin_warning(attempt_count)
                self._rapid_relogin_warned_count = attempt_count
            if attempt_count >= self.config.rapid_relogin_max_attempts:
                self._shutdown_computer()

        self._last_session_locked = session_locked

    def _publish_metrics_if_needed(
        self, active_now: bool, active_app: Optional[str] = None, force: bool = False
    ) -> None:
        minutes = self.state.minutes_today()
        now = time.monotonic()
        heartbeat_due = now - self._last_status_publish >= 55
        try:
            publish_client = self._mqtt_client
        except Exception:
            return

        if force or self._last_minutes_published != minutes:
            publish_client.publish(
                self.config.minutes_topic, payload=str(minutes), retain=True, qos=1
            )
            self._last_minutes_published = minutes
        active_flag = "1" if active_now else "0"
        publish_client.publish(self.config.active_topic, payload=active_flag, retain=False, qos=0)
        if self.config.track_active_app:
            app_payload = active_app if (active_now and active_app) else ""
            if force or app_payload != (self._last_active_app or ""):
                publish_client.publish(
                    self.config.active_app_topic,
                    payload=app_payload,
                    retain=True,
                    qos=1,
                )
            self._last_active_app = active_app if (active_now and active_app) else None
        if heartbeat_due or force:
            status_payload = {
                "status": "online" if self._mqtt_connected else "degraded",
                "version": VERSION,
                "child_id": self.config.child_id,
                "device_id": self.config.device_id,
                "allowed": self._current_allowed_state(),
                "minutes_today": minutes,
                "last_allowed_payload": self._last_allowed_payload,
                "rapid_relogin_attempts": self._rapid_relogin_attempt_count(now),
                "active_app": self._last_active_app if self.config.track_active_app else None,
                "timestamp": _now_local().isoformat(),
            }
            publish_client.publish(
                self.config.status_topic,
                payload=json.dumps(status_payload),
                retain=False,
                qos=0,
            )
            self._last_status_publish = now

    # ----------------------------------------------------------- ENFORCEMENT --

    def _current_allowed_state(self) -> bool:
        if self._allowed is not None:
            return self._allowed
        if self.config.fail_mode == "open":
            return True
        if self._offline_since is None:
            return False
        elapsed = time.monotonic() - self._offline_since
        return elapsed < self.config.offline_grace_period_seconds

    def _enforce_if_required(self, active_now: bool) -> None:
        allowed = self._current_allowed_state()
        if allowed:
            return
        self._enforce_block(active_now=active_now)

    def _enforce_block(self, active_now: bool = False) -> None:
        if self.config.enforcement_mode == "logout":
            self._logout_session()
        else:
            if not self._is_session_locked() or active_now:
                self._lock_screen()

    # ----------------------------------------------------------- SENSING --

    def _is_session_locked(self) -> bool:
        session = CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("CGSSessionScreenIsLocked", 0))

    def _is_active_session(self, session_locked: Optional[bool] = None) -> bool:
        try:
            idle_seconds = CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState, kCGAnyInputEventType
            )
        except Exception:
            self.logger.exception("Unable to read idle timer; assuming active to keep accounting.")
            return True

        if idle_seconds is None:
            return True
        if idle_seconds > self.config.idle_timeout_seconds:
            return False
        if session_locked is None:
            session_locked = self._is_session_locked()
        if session_locked:
            return False
        return True

    def _frontmost_app_name(self) -> Optional[str]:
        script = 'tell application "System Events" to get name of first application process whose frontmost is true'
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=True,
                capture_output=True,
                text=True,
            )
            name = (result.stdout or "").strip()
            return name or None
        except Exception:
            self.logger.debug("Unable to read frontmost app.", exc_info=True)
            return None

    def _maybe_announce_initial_remaining(self) -> None:
        if self._login_announced or self._budget_minutes is None:
            return
        try:
            remaining = int(max(0.0, self._budget_minutes - self.state.minutes_today()))
            title = self._phrase("title") or "Screen Time"
            if remaining > 0:
                body = self._phrase("login_remaining_body").format(minutes=remaining)
                voice = self._phrase("login_remaining_voice").format(minutes=remaining)
            else:
                body = self._phrase("login_out_body")
                voice = self._phrase("login_out_voice")
            script = (
                f'display notification "{_escape_applescript_string(body)}" '
                f'with title "{_escape_applescript_string(title)}"'
            )
            subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._speak(voice)
        except Exception:
            self.logger.debug("Failed to announce remaining time.", exc_info=True)
        finally:
            self._login_announced = True

    def _notify_rapid_relogin_warning(self, attempt_count: int) -> None:
        remaining_attempts = max(0, self.config.rapid_relogin_max_attempts - attempt_count)
        if remaining_attempts <= 0:
            return
        message = (
            f"Warning. One more login attempt will shut down this computer."
            if remaining_attempts == 1
            else f"Warning. {remaining_attempts} more login attempts will shut down this computer."
        )
        self._speak(message)

    def _notify_remaining(self, minutes: int, voice_only: bool = False) -> None:
        voice_key = "warn5_voice" if minutes >= 5 else "warn1_voice"
        body_key = "warn5_body" if minutes >= 5 else "warn1_body"
        if not voice_only:
            msg = self._phrase(body_key)
            title = self._phrase("title")
            script = (
                f'display notification "{_escape_applescript_string(msg)}" '
                f'with title "{_escape_applescript_string(title)}"'
            )
            try:
                subprocess.run(
                    ["/usr/bin/osascript", "-e", script],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                self.logger.warning("Failed to show remaining-time notification.", exc_info=True)
        self._speak(self._phrase(voice_key))

    def _speak(self, text: str) -> None:
        try:
            subprocess.run(
                ["/usr/bin/say", text],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self.logger.warning("Failed to play voice alert.", exc_info=True)

    # ----------------------------------------------------------- ACTIONS --

    def _lock_screen(self) -> None:
        self.logger.info("Locking screen (allowed=0).")
        script = 'tell application "System Events" to key code 12 using {control down, command down}'
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except subprocess.CalledProcessError as exc:
            self.logger.warning("osascript lock failed (%s); trying CGSession -suspend", exc)

        try:
            subprocess.run(
                [
                    "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession",
                    "-suspend",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            self.logger.error("Failed to lock screen via CGSession: %s", exc)

    def _logout_session(self) -> None:
        self.logger.warning(
            "Logging out session (allowed=0) via %s", self.config.logout_method
        )
        if self.config.logout_method == "kill_loginwindow":
            user = self.config.managed_user or os.environ.get("USER") or ""
            try:
                subprocess.run(
                    ["/usr/bin/killall", "-u", user, "loginwindow"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except subprocess.CalledProcessError as exc:
                self.logger.error(
                    "Force logout via killall loginwindow failed: %s", exc
                )

        script = 'tell application "System Events" to log out'
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            self.logger.error("Failed to log out via osascript: %s", exc)

    # ---------------------------------------------------------- SHUTDOWN --

    def _shutdown_computer(self) -> None:
        self.logger.critical(
            "Rapid relogin threshold reached (%s attempts in %ss). Initiating shutdown.",
            self.config.rapid_relogin_max_attempts,
            self.config.rapid_relogin_window_seconds,
        )
        try:
            self._publish_offline_state()
        except Exception:
            self.logger.debug("Failed to publish offline state before shutdown.", exc_info=True)
        try:
            subprocess.run(
                ["/sbin/shutdown", "-h", "now"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            self.logger.critical("Failed to shut down computer: %s", exc)
            self._enforce_block(active_now=True)

    def _publish_offline_state(self) -> None:
        try:
            payload = {
                "status": "offline",
                "version": VERSION,
                "child_id": self.config.child_id,
                "device_id": self.config.device_id,
                "timestamp": _now_local().isoformat(),
            }
            client = self._mqtt_client
            client.publish(self.config.active_topic, payload="0", retain=False, qos=0)
            client.publish(self.config.availability_topic, payload="offline", retain=True, qos=1)
            if self.config.track_active_app:
                client.publish(self.config.active_app_topic, payload="", retain=True, qos=1)
            client.publish(
                self.config.status_topic,
                payload=json.dumps(payload),
                retain=True,
                qos=1,
            )
        except Exception:
            self.logger.debug("Unable to publish offline state during shutdown.", exc_info=True)

    def _shutdown(self) -> None:
        self._running = False
        self.state.save()
        try:
            self._publish_offline_state()
        except Exception:
            self.logger.debug("Failed to publish offline state on shutdown.", exc_info=True)
        try:
            self._mqtt_client.disconnect()
            self._mqtt_client.loop_stop()
        except Exception:
            self.logger.warning("Error while shutting down MQTT.", exc_info=True)


def _setup_logging(cfg: AgentConfig) -> None:
    log_path = Path(cfg.log_file).expanduser()
    err_path = Path(cfg.err_log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    err_path.parent.mkdir(parents=True, exist_ok=True)

    handler_file = logging.FileHandler(log_path)
    handler_file.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    handler_err = logging.FileHandler(err_path)
    handler_err.setLevel(logging.ERROR)
    handler_err.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler_file, handler_err, logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    current_user = os.environ.get("USER") or os.path.basename(Path.home())
    config_path = Path(
        os.environ.get("HA_SCREEN_AGENT_CONFIG", DEFAULT_CONFIG_PATH)
    ).expanduser()
    try:
        cfg = AgentConfig.load(config_path, session_user=current_user)
        if cfg is None:
            print(
                f"Current user '{current_user}' not listed in managed_users; exiting quietly.",
                file=sys.stderr,
            )
            sys.exit(0)
    except Exception as exc:  # pragma: no cover - startup validation
        print(f"Failed to load config: {exc}", file=sys.stderr)
        sys.exit(2)

    _setup_logging(cfg)
    agent = ScreenTimeAgent(cfg)

    def handle_signal(signum, frame):
        agent.logger.info("Received signal %s, shutting down.", signum)
        agent._running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    agent.start()


if __name__ == "__main__":
    main()
