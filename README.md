# macOS Screen Time Agent for Home Assistant

This project ships a hardened macOS LaunchAgent that tracks a child’s Mac usage, reports it to Home Assistant over MQTT, and enforces the unified “allowed / not allowed” state that Home Assistant publishes.  
It does **not** rely on Apple Screen Time APIs or privileged entitlements and is designed to run inside the child’s standard user session.

## Key capabilities

1. **Accurate local tracking** – counts “minutes today” only while the child session is unlocked and not idle.
2. **MQTT telemetry** – publishes retained `minutes_today`, live `active` flag, and heartbeat status every ≤60 s.
3. **Central enforcement** – subscribes to retained `screen/<child>/allowed` topic and locks or logs out the session within seconds when HA denies access (budget exceeded, bedtime, school schedule, etc.).
4. **Fail-safe** – configurable grace period; defaults to “fail closed” when MQTT is offline or config is invalid.
5. **Hardening** – code, config, and LaunchAgent live under root-controlled paths (`/Library/Application Support/ha-screen-agent`, `/Library/LaunchAgents`).

---

## Repository layout

```
.
├── screentime_enforcer.py            # Main agent (installed to /Library/Application Support/ha-screen-agent/agent.py)
├── config/agent.config.sample.json   # Sample root-controlled config
├── scripts/install_service.sh        # Parent-facing installer (run with sudo)
├── requirements.txt                  # Python deps (PyObjC, MQTT, etc.)
└── homeassistant/                    # Example HA snippets & docs (see below)
```

---

## MQTT topic contract

Assuming `child_id = kiddo` and `device_id = mac-mini`:

| Direction | Topic | Payload |
|-----------|-------|---------|
| Agent → HA (retained) | `screen/kiddo/mac/mac-mini/minutes_today` | Integer minutes counted today |
| Agent → HA | `screen/kiddo/mac/mac-mini/active` | `0` or `1` (child actively using Mac) |
| Agent → HA | `screen/kiddo/mac/mac-mini/status` | JSON heartbeat (`status`, `version`, `minutes_today`, `allowed`, timestamp, etc.) |
| HA → Agent (retained) | `screen/kiddo/allowed` | `0/1`, `on/off`, or `true/false` |

Home Assistant owns the unified allow logic: `effective_allowed = override OR (budget_ok AND !bedtime AND !school_time)`. The macOS agent never interprets budget/schedule directly—it simply honors the retained `screen/<child>/allowed` value.

---

## Configuration reference (`/Library/Application Support/ha-screen-agent/config.json`)

| Field | Required | Description |
|-------|----------|-------------|
| `child_id` | ✅ | Identifier used across HA topics. |
| `device_id` | ➖ | Optional per-Mac identifier (defaults to sanitized hostname). |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`, `mqtt_tls` | ✅ | MQTT connectivity. TLS optional. |
| `topic_prefix` | ✅ | Must begin with `screen/<child_id>`. |
| `sample_interval_seconds` | ➖ | 5–60 (default 15). |
| `idle_timeout_seconds` | ➖ | Minutes stop counting if idle > timeout (default 120 s). |
| `enforcement_mode` | ➖ | `lock` (default) or `logout`. |
| `fail_mode` | ➖ | `safe` (fail closed) or `open`. |
| `offline_grace_period_seconds` | ➖ | Grace before fail-safe kicks in (default 180 s). |
| `allowed_users` | ➖ | Array of macOS short names allowed to run the agent. Prevents the service from starting in other sessions. |
| `state_path` | ➖ | Local JSON cache of today’s minutes. Defaults to child’s `~/Library/Application Support/ha-screen-agent/state.json`. |
| `log_file`, `err_log_file` | ➖ | Defaults `/tmp/ha_screen_agent.{out,err}.log`. |
| `debug_mqtt` | ➖ | Set true to enable verbose MQTT client debug logging. |
| `track_active_app` | ➖ | Set true to publish the frontmost app name to MQTT (discovery sensor enabled). |

Edit this file as an admin. Keep it root-owned and readable by the child account (e.g., root:<child_group>, mode 0640) so the LaunchAgent can load it.

---

## Requirements

- macOS device with a **parent admin** account and a **child non-admin** account.
- Home Assistant with MQTT discovery enabled and a Mosquitto broker.
- MQTT credentials for the child’s namespace (host/port/username/password, TLS if used).
- Internet access to install Apple Command Line Tools (one-time).

## Installation (parent admin account)

1. **Install Apple Command Line Tools** (once):
   ```bash
   xcode-select --install
   ```
2. **Download the agent**:
   ```bash
   git clone https://github.com/your-org/mac-screentime-enforcer.git
   cd mac-screentime-enforcer
   ```
3. **Install as root** (first run creates config, later runs upgrade):
   ```bash
   sudo ./scripts/install_service.sh
   ```
4. **Edit config as root** at `/Library/Application Support/ha-screen-agent/config.json`:
   - Set `child_id`, `device_id`, MQTT host/credentials, `allowed_users` (child short name), and optional `track_active_app`.
5. **Re-run installer as root** to apply config/upgrade:
   ```bash
   sudo ./scripts/install_service.sh
   ```
6. **Log in as the child** (standard user) and verify the agent is running:
   ```bash
   log show --predicate 'process == "python3"' --last 5m | grep ha-screen-agent
   ```
7. **Home Assistant**: discovery will create the Mac device and entities. Add/confirm your HA automations (budget, schedule, reset) to drive the `allowed` topic.

Optional: **MQTT ACLs per child** (recommended)
- Restricts the agent to its own topics/discovery payloads so the child cannot alter other data.
- See the ACL example below; repeat per child with their username/topic prefix.

Logs live in `/tmp/ha_screen_agent.{out,err}.log`. Usage state persists under the child’s Library folder so counters survive restarts but reset automatically at local midnight.

MQTT discovery: with HA MQTT discovery enabled, the agent creates a device (`<child> mac`) with entities: sensor minutes, binary sensor active, switch allowed, number daily budget (HA-managed), switch parent override (HA-managed), and (optional) frontmost app sensor when `track_active_app=true`.

Example Mosquitto ACL (replace `kiddo` + device namespace):

```
user kiddo
# Allow publishing telemetry/status for this child/device (minutes, active, status)
topic write screen/kiddo/mac/#
# Allow reading the retained allowed flag only
topic read  screen/kiddo/allowed
# Allow MQTT discovery configs
topic write homeassistant/+/+/config
# Allow reading current budget (HA-managed number state)
topic read  homeassistant/+/+/state

# Deny everything else explicitly
pattern write $
pattern read  $
```

---

## Home Assistant integration checklist

1. **MQTT broker** – reachable from the Mac with retained messages enabled, discovery enabled, and ACLs that restrict each child to their namespace.
2. **Entities** – discovery creates minutes/active/allowed plus HA-managed budget and parent override entities automatically.
3. **Automations**:
   - Midnight reset (set `allowed=1`, zero counters in HA if desired).
   - Budget enforcement (when total minutes exceeds helper, publish retained `allowed=0`).
   - Schedule enforcement (when `schedule.<child>_bedtime` or `schedule.<child>_school_time` turns on, publish retained `allowed=0` within 5 s).
4. **Dashboard**:
   - Gauge or statistic card for “Total minutes today”.
   - Device breakdown (Switch / Android / Mac).
   - Parent override toggle and schedule indicators for auditability.

---

## Security & hardening notes

- Install and own all files as `root:wheel`; child account must remain non-admin.
- The LaunchAgent lives in `/Library/LaunchAgents` (not user-writable) and is loaded into the child’s GUI session via `launchctl bootstrap gui/<uid> …`.
- Default behavior is **fail-safe**: if MQTT disconnects longer than the configured grace period, the Mac is locked until connectivity returns.
- Optional `allowed_users` list ensures the agent simply exits when run in unexpected sessions (e.g., parent admin login).
- Config should be root-owned and readable by the child account (e.g., root:<child_group> 0640) so the LaunchAgent can start; still enforce broker ACLs per child/topic.

---

## Troubleshooting

| Symptom | Checks |
|---------|--------|
| Agent does not start | `launchctl print gui/<uid> com.ha.screen-agent`; inspect `/tmp/ha_screen_agent.err.log`. |
| Minutes not updating in HA | Confirm MQTT topics via `mosquitto_sub` and ensure broker ACL allows publishing. |
| Mac never unlocks after MQTT outage | Verify grace period (`offline_grace_period_seconds`), network reachability, and that retained `allowed=1` exists. |
| Child can still use Mac when blocked | Ensure HA publishes retained `allowed=0`, LaunchAgent is running, and enforcement mode is set to `lock` or `logout` as desired. |

---

## License

MIT — see `LICENSE` (or add one if distributing publicly).
