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
import signal
import subprocess
import sys
import time
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
        "grace_body": "Screen time will end in 1 minute.",
        "grace_voice": "Screen time will end in one minute.",
        "final_voice": "You have used all your screen time {child_id}.",
        "warn5_body": "5 minutes of screen time remain.",
        "warn5_voice": "You have five minutes of screen time left.",
        "warn1_body": "1 minute of screen time remains.",
        "warn1_voice": "You have one minute of screen time left.",
    },
    "de": {
        "title": "Bildschirmzeit",
        "grace_body": "Bildschirmzeit endet in 1 Minute.",
        "grace_voice": "Die Bildschirmzeit endet in einer Minute.",
        "final_voice": "Du hast deine Bildschirmzeit aufgebraucht {child_id}.",
        "warn5_body": "Noch 5 Minuten Bildschirmzeit übrig.",
        "warn5_voice": "Du hast noch fünf Minuten Bildschirmzeit.",
        "warn1_body": "Noch 1 Minute Bildschirmzeit übrig.",
        "warn1_voice": "Du hast noch eine Minute Bildschirmzeit.",
    },
    "fr": {
        "title": "Temps d'écran",
        "grace_body": "Le temps d'écran se termine dans 1 minute.",
        "grace_voice": "Le temps d'écran se termine dans une minute.",
        "final_voice": "Tu as utilisé tout ton temps d'écran {child_id}.",
        "warn5_body": "Il reste 5 minutes de temps d'écran.",
        "warn5_voice": "Il te reste cinq minutes de temps d'écran.",
        "warn1_body": "Il reste 1 minute de temps d'écran.",
        "warn1_voice": "Il te reste une minute de temps d'écran.",
    },
    "es": {
        "title": "Tiempo de pantalla",
        "grace_body": "El tiempo de pantalla terminará en 1 minuto.",
        "grace_voice": "El tiempo de pantalla terminará en un minuto.",
        "final_voice": "Has usado todo tu tiempo de pantalla {child_id}.",
        "warn5_body": "Quedan 5 minutos de tiempo de pantalla.",
        "warn5_voice": "Te quedan cinco minutos de tiempo de pantalla.",
        "warn1_body": "Queda 1 minuto de tiempo de pantalla.",
        "warn1_voice": "Te queda un minuto de tiempo de pantalla.",
    },
    "it": {
        "title": "Tempo schermo",
        "grace_body": "Il tempo schermo finirà tra 1 minuto.",
        "grace_voice": "Il tempo schermo finirà tra un minuto.",
        "final_voice": "Hai usato tutto il tempo schermo {child_id}.",
        "warn5_body": "Restano 5 minuti di tempo schermo.",
        "warn5_voice": "Ti restano cinque minuti di tempo schermo.",
        "warn1_body": "Resta 1 minuto di tempo schermo.",
        "warn1_voice": "Ti resta un minuto di tempo schermo.",
    },
    "nl": {
        "title": "Schermtijd",
        "grace_body": "Schermtijd eindigt over 1 minuut.",
        "grace_voice": "Schermtijd eindigt over een minuut.",
        "final_voice": "Je hebt al je schermtijd gebruikt {child_id}.",
        "warn5_body": "Nog 5 minuten schermtijd over.",
        "warn5_voice": "Je hebt nog vijf minuten schermtijd.",
        "warn1_body": "Nog 1 minuut schermtijd over.",
        "warn1_voice": "Je hebt nog één minuut schermtijd.",
    },
    "pt": {
        "title": "Tempo de tela",
        "grace_body": "O tempo de tela termina em 1 minuto.",
        "grace_voice": "O tempo de tela termina em um minuto.",
        "final_voice": "Você usou todo o seu tempo de tela {child_id}.",
        "warn5_body": "Restam 5 minutos de tempo de tela.",
        "warn5_voice": "Você tem cinco minutos de tempo de tela restantes.",
        "warn1_body": "Resta 1 minuto de tempo de tela.",
        "warn1_voice": "Você tem um minuto de tempo de tela restante.",
    },
    "ja": {
        "title": "スクリーンタイム",
        "grace_body": "1分後にスクリーンタイムが終了します。",
        "grace_voice": "1分後にスクリーンタイムが終わります。",
        "final_voice": "{child_id} のスクリーンタイムを使い切りました。",
        "warn5_body": "スクリーンタイムはあと5分です。",
        "warn5_voice": "スクリーンタイムはあと5分です。",
        "warn1_body": "スクリーンタイムはあと1分です。",
        "warn1_voice": "スクリーンタイムはあと1分です。",
    },
    "zh": {
        "title": "屏幕使用时间",
        "grace_body": "屏幕时间将在1分钟后结束。",
        "grace_voice": "屏幕时间将在一分钟后结束。",
        "final_voice": "你已用完所有屏幕时间 {child_id}。",
        "warn5_body": "屏幕时间还剩 5 分钟。",
        "warn5_voice": "屏幕时间还剩五分钟。",
        "warn1_body": "屏幕时间还剩 1 分钟。",
        "warn1_voice": "屏幕时间还剩一分钟。",
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


def _now_local() -> datetime:
    return datetime.now().astimezone()


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
    idle_timeout_seconds: int = 120
    enforcement_mode: str = "lock"  # lock | logout
    fail_mode: str = "safe"  # safe | open
    offline_grace_period_seconds: int = 180
    allowed_users: Optional[List[str]] = None
    state_path: Path = DEFAULT_STATE_PATH
    log_file: str = DEFAULT_LOG_PATH
    err_log_file: str = DEFAULT_ERR_LOG_PATH
    debug_mqtt: bool = False

    @classmethod
    def load(cls, path: Path) -> "AgentConfig":
        if not path.exists():
            raise FileNotFoundError(
                f"Required config file missing at {path}. "
                "Run the installer script to create it."
            )
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        child_id = data.get("child_id", "").strip()
        if not child_id:
            raise ValueError("Config `child_id` is required.")

        topic_prefix = data.get("topic_prefix", f"screen/{child_id}")
        if not topic_prefix.startswith(f"screen/{child_id}"):
            raise ValueError(
                "Config `topic_prefix` must start with `screen/<child_id>` "
                f"(expected prefix `screen/{child_id}`)."
            )

        mqtt_host = data.get("mqtt_host", "").strip()
        if not mqtt_host:
            raise ValueError("Config `mqtt_host` is required.")

        device_id = data.get("device_id")
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

        enforcement_mode = data.get("enforcement_mode", "lock").lower()
        if enforcement_mode not in {"lock", "logout"}:
            raise ValueError("`enforcement_mode` must be lock or logout.")

        fail_mode = data.get("fail_mode", "safe").lower()
        if fail_mode not in {"safe", "open"}:
            raise ValueError("`fail_mode` must be safe or open.")

        grace = int(data.get("offline_grace_period_seconds", 180))
        if grace < 0 or grace > 900:
            raise ValueError("`offline_grace_period_seconds` must be between 0 and 900.")

        state_path = Path(
            data.get("state_path", str(DEFAULT_STATE_PATH))
        ).expanduser()

        log_file = data.get("log_file", DEFAULT_LOG_PATH)
        err_file = data.get("err_log_file", DEFAULT_ERR_LOG_PATH)
        debug_mqtt = bool(data.get("debug_mqtt", False))

        allowed_users_raw = data.get("allowed_users")
        if allowed_users_raw is not None:
            if not isinstance(allowed_users_raw, list) or not all(
                isinstance(item, str) for item in allowed_users_raw
            ):
                raise ValueError("`allowed_users` must be a list of macOS short names.")
            allowed_users = [item.strip() for item in allowed_users_raw if item.strip()]
            if not allowed_users:
                allowed_users = None
        else:
            allowed_users = None

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
            enforcement_mode=enforcement_mode,
            fail_mode=fail_mode,
            offline_grace_period_seconds=grace,
            state_path=state_path,
            log_file=log_file,
            err_log_file=err_file,
            allowed_users=allowed_users,
            debug_mqtt=debug_mqtt,
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
    def allow_topic(self) -> str:
        return f"{self.topic_prefix}/allowed"

    @property
    def discovery_base_id(self) -> str:
        return f"{self.child_id}_{self.device_id}_mac"

    @property
    def budget_state_topic(self) -> str:
        return f"homeassistant/{self.discovery_base_id}/daily_budget/state"


class UsageState:
    def __init__(self, path: Path):
        self.path = path
        self._data = {"date": _now_local().date().isoformat(), "seconds_today": 0.0}
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("date") == _now_local().date().isoformat():
                self._data = data
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
            self._data = {"date": today, "seconds_today": 0.0}
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
        self._grace_until: Optional[float] = None
        self._grace_final_warned = False
        self._discovery_published = False
        self._budget_minutes: Optional[float] = None
        self._warned_5 = False
        self._warned_1 = False

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
        client_id = f"ha-screen-agent-{self.config.device_id}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
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
            disc = [
                (
                    "sensor",
                    f"{base_id}_minutes",
                    {
                        "name": f"{self.config.child_id} Mac Minutes",
                        "unique_id": f"{base_id}_minutes",
                        "state_topic": self.config.minutes_topic,
                        "unit_of_measurement": "min",
                        "icon": "mdi:timer-outline",
                        "device": device,
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
                    },
                ),
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
                    },
                ),
                (
                    "switch",
                    f"{base_id}_parent_override",
                    {
                        "name": f"{self.config.child_id} Mac Parent Override",
                        "unique_id": f"{base_id}_parent_override",
                        "state_topic": f"homeassistant/{base_id}/override/state",
                        "command_topic": f"homeassistant/{base_id}/override/state",
                        "payload_on": "ON",
                        "payload_off": "OFF",
                        "icon": "mdi:shield-star",
                        "device": device,
                    },
                ),
            ]
            for domain, obj_id, payload in disc:
                topic = f"homeassistant/{domain}/{obj_id}/config"
                self._mqtt_client.publish(topic, json.dumps(payload), retain=True, qos=1)
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
            # Request retained allowed value ASAP
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

        self._grace_until = None
        self._grace_final_warned = False

        if previous is not None and previous != allowed and not allowed:
            self._enforce_block()

    # ----------------------------------------------------------- MAIN LOOP --

    def _main_loop(self) -> None:
        try:
            while self._running:
                loop_start = time.monotonic()
                elapsed = loop_start - self._last_tick
                self._last_tick = loop_start

                self.state.ensure_today()
                active = self._is_active_session()
                if active:
                    self.state.add_seconds(elapsed)

                minutes_now = self.state.minutes_today()
                self._check_budget_warnings(minutes_today=minutes_now)

                self._maybe_save_state()
                self._publish_metrics_if_needed(
                    active_now=active, force=not self._mqtt_connected
                )
                self._enforce_if_required(active_now=active)

                sleep_time = max(1.0, float(self.config.sample_interval_seconds))
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

    def _publish_metrics_if_needed(self, active_now: bool, force: bool = False) -> None:
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
        publish_client.publish(
            self.config.active_topic, payload=active_flag, retain=False, qos=0
        )
        if heartbeat_due or force:
            status_payload = {
                "status": "online" if self._mqtt_connected else "degraded",
                "version": VERSION,
                "child_id": self.config.child_id,
                "device_id": self.config.device_id,
                "allowed": self._current_allowed_state(),
                "minutes_today": minutes,
                "last_allowed_payload": self._last_allowed_payload,
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

    def _is_active_session(self) -> bool:
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
        if self._is_session_locked():
            return False
        return True

    def _notify_remaining(self, minutes: int, voice_only: bool = False) -> None:
        voice_key = "warn5_voice" if minutes >= 5 else "warn1_voice"
        body_key = "warn5_body" if minutes >= 5 else "warn1_body"
        if not voice_only:
            msg = self._phrase(body_key)
            title = self._phrase("title")
            script = f'display notification "{msg}" with title "{title}"'
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

    def _notify_grace_start(self) -> None:
        msg = self._phrase("grace_body")
        title = self._phrase("title")
        script = f'display notification "{msg}" with title "{title}"'
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self.logger.warning("Failed to show grace notification.", exc_info=True)
        self._speak(self._phrase("grace_voice"))

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
        self.logger.warning("Logging out session (allowed=0).")
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

    def _shutdown(self) -> None:
        self._running = False
        self.state.save()
        try:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
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
    config_path = Path(
        os.environ.get("HA_SCREEN_AGENT_CONFIG", DEFAULT_CONFIG_PATH)
    ).expanduser()
    try:
        cfg = AgentConfig.load(config_path)
    except Exception as exc:  # pragma: no cover - startup validation
        print(f"Failed to load config: {exc}", file=sys.stderr)
        sys.exit(2)

    current_user = os.environ.get("USER") or os.path.basename(Path.home())
    if cfg.allowed_users and current_user not in cfg.allowed_users:
        print(
            f"Current user '{current_user}' not in allowed_users. Exiting quietly.",
            file=sys.stderr,
        )
        sys.exit(0)

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
