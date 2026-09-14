#!/usr/bin/env bash
# Periodic health check for the Atlas-Edge admin hotspot (ap0) — run every
# few minutes by systemd/atlas-ap0-watchdog.timer. Recovers from ap0
# disappearing (e.g. the USB WiFi adapter reset and dropped its virtual
# interfaces) or the hotspot connection dropping, without needing a reboot.
set -uo pipefail   # not -e: this script's whole job is to react to failures

ENV_FILE="${ATLAS_EDGE_ENV_FILE:-/home/limitify/Developer/Atlas-Edge/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HOTSPOT_IFACE="${ATLAS_EDGE_HOTSPOT_IFACE:-ap0}"
HOTSPOT_CONN="${ATLAS_EDGE_HOTSPOT_CONN_NAME:-Atlas-Edge-Admin}"
SETUP_SCRIPT="${ATLAS_EDGE_AP0_SETUP_SCRIPT:-/home/limitify/Developer/Atlas-Edge/atlas_edge/scripts/ap0_setup.sh}"

log() {
  logger -t atlas-ap0-watchdog -- "$1"
  echo "[atlas-ap0-watchdog] $1"
}

iface_ok() {
  iw dev "$HOTSPOT_IFACE" info >/dev/null 2>&1
}

conn_active() {
  nmcli -t -f NAME,STATE connection show --active 2>/dev/null \
    | grep -Fxq "${HOTSPOT_CONN}:activated"
}

if iface_ok && conn_active; then
  log "OK — ${HOTSPOT_IFACE} exists and '${HOTSPOT_CONN}' is activated."
  exit 0
fi

log "Unhealthy — iface_ok=$(iface_ok && echo yes || echo no), conn_active=$(conn_active && echo yes || echo no). Attempting recovery…"

# Cheap fix first: the interface itself may still be fine, just the
# connection dropped (e.g. NetworkManager restarted, or a client conflict).
if iface_ok; then
  log "Trying 'nmcli connection up ${HOTSPOT_CONN}' first…"
  if nmcli connection up "$HOTSPOT_CONN" ifname "$HOTSPOT_IFACE" >/dev/null 2>&1; then
    log "Recovered via nmcli connection up."
    exit 0
  fi
  log "Direct nmcli connection up failed."
fi

# Fall back to the full setup — handles ap0 having vanished entirely, and
# re-waits for wlan0 association if that's also gone.
log "Falling back to full setup script: ${SETUP_SCRIPT}"
if "$SETUP_SCRIPT"; then
  log "Recovered via full setup script."
  exit 0
fi

log "ERROR: recovery failed — both direct reconnect and full setup were unsuccessful. Manual intervention needed."
exit 1
