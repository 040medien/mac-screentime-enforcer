# macOS Screen Time Agent for Home Assistant

Hardened macOS LaunchAgent that tracks a child’s Mac usage, reports it to Home Assistant over MQTT, and enforces the retained “allowed” flag from HA. Runs entirely in the child’s user session—no Apple Screen Time APIs or special entitlements.

## What you get

- **Accurate local tracking**: counts minutes only when the child session is unlocked and not idle.
- **Enforcement**: locks or logs out within seconds when HA publishes `allowed=0`.
- **MQTT discovery & telemetry**: retained minutes, live active flag, heartbeat status, optional active app sensor.
- **Fail-safe defaults**: configurable grace window; fails closed when MQTT is down or config is bad.

## Requirements

- macOS with a **parent admin** account and a **child non-admin** account.
- Home Assistant with MQTT discovery enabled and an MQTT broker (e.g., Mosquitto).
- MQTT credentials scoped to the child’s namespace; internet to install Apple Command Line Tools once.

## Install (parent admin account)

1. Install Command Line Tools: `xcode-select --install`
2. Clone: `git clone https://github.com/your-org/mac-screentime-enforcer.git && cd mac-screentime-enforcer`
3. Install as root: `sudo ./scripts/install_service.sh`
   - Prompts for child name, device ID, MQTT host/creds, managed users (mac_user=child_name pairs), optional active-app sensor if no config exists. Default managed user is the child name (set it to the child’s macOS short name if different).
   - Reuse an existing config via `--config /path/to/config.json`.
4. Update later: edit `/Library/Application Support/ha-screen-agent/config.json` as root, rerun the installer.
5. Log in as the child and verify: `log show --predicate 'process == "python3"' --last 5m | grep ha-screen-agent`
6. Home Assistant: with MQTT discovery on, a device named `<child> mac` appears under **Settings → Devices & Services → Integrations → MQTT** with minutes, active, allowed switch, budget number, parent override switch, optional active-app sensor. Add the automations below to drive `allowed`.

### macOS prompts & permissions

- **Background item notice** on the child’s first login (expected for the LaunchAgent).
- **Accessibility approval (admin required)** for `python3` at `/Library/Application Support/ha-screen-agent/agent.py` so it can lock/log out and, if enabled, read the frontmost app. Approve under **Settings → Privacy & Security → Accessibility**, then log out/in.

## Home Assistant integration

- **MQTT topics (child_id=kiddo, device_id=mac-mini)**  
  - Agent → HA (retained): `screen/kiddo/mac/mac-mini/minutes_today` (integer minutes)  
  - Agent → HA (retained): `screen/kiddo/mac/mac-mini/active` (`0/1`)  
  - Agent → HA: `screen/kiddo/mac/mac-mini/status` (JSON heartbeat)  
  - HA → Agent (retained): `screen/kiddo/allowed` (`0/1`, `on/off`, `true/false`)
- **Discovery entities**: minutes sensor, active binary sensor, allowed switch, daily budget number (HA-managed), parent override switch (HA-managed), optional active app sensor.
- **Daily reset**: the agent resets its local minutes at midnight while running. If it is offline at midnight, wrap the minutes sensor in a HA `utility_meter` with a daily cycle to keep a strict per-day view.

### Example automations (MQTT discovery)

Update IDs to match your discovered entities (`base_id = child_id + "_" + device_id + "_mac"`):

```yaml
automation:
  - alias: "Kiddo Mac - Block when budget exceeded"
    trigger:
      - platform: numeric_state
        entity_id: sensor.kiddo_macbookpro_mac_minutes
        above: number.kiddo_macbookpro_mac_daily_budget_minutes
    condition:
      - condition: state
        entity_id: switch.kiddo_macbookpro_mac_parent_override
        state: "off"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.kiddo_macbookpro_mac_allowed

  - alias: "Kiddo Mac - Allow when back under budget"
    trigger:
      - platform: numeric_state
        entity_id: sensor.kiddo_macbookpro_mac_minutes
        below: number.kiddo_macbookpro_mac_daily_budget_minutes
    condition:
      - condition: state
        entity_id: switch.kiddo_macbookpro_mac_parent_override
        state: "off"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.kiddo_macbookpro_mac_allowed

  - alias: "Kiddo Mac - Reset each morning"
    trigger:
      - platform: time
        at: "03:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.kiddo_macbookpro_mac_allowed
      - service: switch.turn_off
        target:
          entity_id: switch.kiddo_macbookpro_mac_parent_override
      # Minutes reset locally at midnight; add more reset tasks here if needed
```

## Configuration
Configuration lives in `/Library/Application Support/ha-screen-agent/config.json`

| Field | Required | Notes |
|-------|----------|-------|
| `managed_users` | ✅ | List of mappings (one per macOS account to manage). Each entry: `mac_user_account`, `child_name` (letters/numbers/hyphen/underscore), optional `topic_prefix` (must start with `screen/<child_name>`), optional `device_id`. The agent only runs when the current macOS user matches an entry and uses that child name for topics/discovery. |
| `device_id` | ➖ | Defaults to sanitized hostname if not set in the entry. |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`, `mqtt_tls` | ✅ | MQTT connectivity (TLS optional). |
| `sample_interval_seconds` | ➖ | 5–60, default 15. |
| `blocked_check_seconds` | ➖ | Polling interval (seconds) while blocked; 0.5–10, default 1.0 to re-lock quickly if the child reauthenticates. |
| `idle_timeout_seconds` | ➖ | Idle threshold in seconds, default 120. |
| `enforcement_mode` | ➖ | `lock` (default) or `logout`. |
| `logout_method` | ➖ | How to force logout when `enforcement_mode=logout`: `osascript` (default, shows a prompt) or `kill_loginwindow` (kills the loginwindow process to bypass prompts). |
| `fail_mode` | ➖ | `safe` (fail closed) or `open`. |
| `offline_grace_period_seconds` | ➖ | Default 0. |
| `state_path` | ➖ | Defaults to `~/Library/Application Support/ha-screen-agent/state.json`. |
| `log_file`, `err_log_file` | ➖ | Defaults `/tmp/ha_screen_agent.{out,err}.log`. |
| `debug_mqtt` | ➖ | Set true for verbose client logging. |
| `track_active_app` | ➖ | Publish frontmost app name to MQTT. |

Edit as admin; keep it root-owned and readable by the child account (e.g., root:<child_group> 0640). The installer prompts for basics when no config exists.

Example for two kids on one Mac:

```json
{
  "mqtt_host": "mqtt.local",
  "managed_users": [
    { "mac_user_account": "kid1", "child_name": "alice" },
    { "mac_user_account": "kid2", "child_name": "bob", "topic_prefix": "screen/bobmac" }
  ]
}
```

## MQTT ACL example (per child)

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

## Repository layout

```
.
├── screentime_enforcer.py            # Main agent (installed to /Library/Application Support/ha-screen-agent/agent.py)
├── config/agent.config.sample.json   # Sample root-controlled config
├── scripts/install_service.sh        # Parent-facing installer (run with sudo)
├── requirements.txt                  # Python deps (PyObjC, MQTT, etc.)
└── homeassistant/                    # Example HA snippets & docs
```

## Security & hardening

- Install and own all files as `root:wheel`; child account stays non-admin.
- LaunchAgent lives in `/Library/LaunchAgents` and is bootstrapped into the child’s GUI session.
- Default behavior is **fail-safe**: when MQTT is down beyond the grace window, the Mac locks until connectivity returns.
- `managed_users` controls which macOS accounts the agent will run under; broker ACLs should still enforce per-child topics.

## Troubleshooting

| Symptom | Checks |
|---------|--------|
| Agent does not start | `launchctl print gui/<uid> com.ha.screen-agent`; inspect `/tmp/ha_screen_agent.err.log`. |
| Minutes not updating in HA | Confirm MQTT topics via `mosquitto_sub` and broker ACLs allow publishing. |
| Mac never unlocks after MQTT outage | Verify `offline_grace_period_seconds`, network reachability, and retained `allowed=1`. |
| Child can still use Mac when blocked | Ensure HA publishes retained `allowed=0`, LaunchAgent is running, and enforcement mode is set correctly. |

### Harder lockouts (when logout prompts appear)

macOS shows a cancelable confirmation dialog when users are logged out, so a determined child can dodge `enforcement_mode=logout`. To make the block harder to bypass:

- Prefer `enforcement_mode=lock` (default). The agent immediately locks the session instead of attempting logout.
- Require a password to unlock after sleep/screensaver: **System Settings → Privacy & Security → Require password after sleep or screen saver begins** → set to *Immediately*.
- Give the child account its own password (even a simple PIN) so the lock screen cannot be dismissed without supervision.
- Disable automatic login and fast user switching so the lock screen is always shown.
- Shorten `idle_timeout_seconds` and keep `sample_interval_seconds` small (e.g., 5–10 seconds) to reduce any window where they can act before the lock triggers.

These steps keep the session locked instead of relying on logout, eliminating the cancelable prompt.

## License

GPL-3.0 — see `LICENSE`.
