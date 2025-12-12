#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Install the Home Assistant macOS Screen Time agent.

Usage:
  sudo ./scripts/install_service.sh [--config /path/to/config.json]

If --config is omitted, config/agent.config.sample.json is copied to the
root-controlled config location the first time you run this script.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must be run as root (use sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_CONFIG_SRC="$PROJECT_DIR/config/agent.config.sample.json"
CONFIG_SRC="$DEFAULT_CONFIG_SRC"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_SRC="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ ! -f "$CONFIG_SRC" ]]; then
    echo "Config source '$CONFIG_SRC' does not exist." >&2
    exit 1
fi

CONFIG_SRC="$(cd "$(dirname "$CONFIG_SRC")" && pwd)/$(basename "$CONFIG_SRC")"

AGENT_DIR="/Library/Application Support/ha-screen-agent"
AGENT_PATH="$AGENT_DIR/agent.py"
CONFIG_PATH="$AGENT_DIR/config.json"
VENV_PATH="$AGENT_DIR/venv"
PLIST_LABEL="com.ha.screen-agent"
PLIST_PATH="/Library/LaunchAgents/${PLIST_LABEL}.plist"
PYTHON_BIN="/usr/bin/python3"

mkdir -p "$AGENT_DIR"
install -o root -g wheel -m 0755 "$PROJECT_DIR/screentime_enforcer.py" "$AGENT_PATH"

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Creating config at $CONFIG_PATH"
    install -o root -g wheel -m 0644 "$CONFIG_SRC" "$CONFIG_PATH"
else
    echo "Existing config preserved at $CONFIG_PATH"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python 3 not found at $PYTHON_BIN" >&2
    exit 1
fi

if [[ ! -d "$VENV_PATH" ]]; then
    echo "Creating virtual environment under $VENV_PATH"
    "$PYTHON_BIN" -m venv "$VENV_PATH"
fi

"$VENV_PATH/bin/pip" install --upgrade pip wheel >/tmp/ha-screen-agent-pip.log
"$VENV_PATH/bin/pip" install -r "$PROJECT_DIR/requirements.txt" >/tmp/ha-screen-agent-install.log

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_PATH}/bin/python3</string>
        <string>${AGENT_PATH}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/ha_screen_agent.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ha_screen_agent.err.log</string>
    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
    </array>
</dict>
</plist>
EOF

chmod 0644 "$PLIST_PATH"
chown root:wheel "$PLIST_PATH"

CHILD_USER="$(
    CONFIG_PATH="$CONFIG_PATH" "$PYTHON_BIN" - <<'PY'
import json, os
path = os.environ.get("CONFIG_PATH")
if not path:
    raise SystemExit(0)
try:
    with open(path, "r") as fp:
        data = json.load(fp)
except Exception:
    raise SystemExit(0)
users = data.get("allowed_users") or []
for candidate in users:
    candidate = (candidate or "").strip()
    if candidate:
        print(candidate)
        break
PY
)"

CONFIG_GROUP="wheel"
CONFIG_MODE="0644"
if [[ -n "$CHILD_USER" && "$(id -un "$CHILD_USER" 2>/dev/null)" == "$CHILD_USER" ]]; then
    CHILD_GROUP="$(id -gn "$CHILD_USER" 2>/dev/null || true)"
    if [[ -n "$CHILD_GROUP" ]]; then
        CONFIG_GROUP="$CHILD_GROUP"
        CONFIG_MODE="0640"
    fi
fi

chown root:"$CONFIG_GROUP" "$CONFIG_PATH"
chmod "$CONFIG_MODE" "$CONFIG_PATH"
echo "Config permissions set to $CONFIG_MODE (group: $CONFIG_GROUP)."

if [[ -n "$CHILD_USER" ]]; then
    if id "$CHILD_USER" >/dev/null 2>&1; then
        CHILD_UID="$(id -u "$CHILD_USER")"
        echo "Bootstrapping LaunchAgent for GUI session user '${CHILD_USER}' (uid ${CHILD_UID})."
        launchctl bootout "gui/${CHILD_UID}" "$PLIST_PATH" >/dev/null 2>&1 || true
        if launchctl bootstrap "gui/${CHILD_UID}" "$PLIST_PATH"; then
            echo "LaunchAgent loaded for ${CHILD_USER}."
        else
            echo "Failed to bootstrap LaunchAgent for ${CHILD_USER}. Log in as that user and run:" >&2
            echo "  launchctl bootstrap gui/${CHILD_UID} $PLIST_PATH" >&2
        fi
    else
        echo "Warning: allowed_users entry '${CHILD_USER}' is not a local user. LaunchAgent not bootstrapped." >&2
    fi
else
    echo "No 'allowed_users' configured. LaunchAgent installed but not bootstrapped."
    echo "Log in as the child user and run: launchctl bootstrap gui/\$(id -u) $PLIST_PATH"
fi

cat <<EOF
------------------------------------------------------------
Agent installed to: $AGENT_PATH
Config location    : $CONFIG_PATH
LaunchAgent        : $PLIST_PATH

Next steps:
  1. Edit ${CONFIG_PATH} (as admin) to set child_id, device_id, MQTT credentials, etc.
  2. Ensure the MQTT broker topics and Home Assistant automations follow the README.
  3. Log into the child account and verify the agent is running:
       log show --predicate 'process == "python3"' --last 5m | grep ha-screen-agent
  4. Publish to screen/<child>/allowed to confirm enforcement.
------------------------------------------------------------
EOF
