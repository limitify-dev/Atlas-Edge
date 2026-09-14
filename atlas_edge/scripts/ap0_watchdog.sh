#!/usr/bin/env bash
# Periodic health check for the Atlas-Edge admin hotspot (ap0) — run every
# few minutes by systemd/atlas-ap0-watchdog.timer. Recovers from ap0
# disappearing (e.g. the USB WiFi adapter reset and dropped its virtual
# interfaces), hostapd/dnsmasq dying, or wlan0 having associated on a
# different channel than ap0 is currently broadcasting on (ap0_setup.sh
# falls back to a fixed channel when wlan0 isn't connected yet — once it
# does connect, this is what realigns the hotspot to match, since they
# share one radio and must be on the same channel).
set -uo pipefail   # not -e: this script's whole job is to react to failures

ENV_FILE="${ATLAS_EDGE_ENV_FILE:-/home/limitify/Developer/Atlas-Edge/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

WIFI_IFACE="${ATLAS_EDGE_WIFI_IFACE:-wlan0}"
HOTSPOT_IFACE="${ATLAS_EDGE_HOTSPOT_IFACE:-ap0}"
HOSTAPD_CONF="${ATLAS_EDGE_HOTSPOT_HOSTAPD_CONF:-/etc/hostapd/atlas-ap0.conf}"
SETUP_SCRIPT="${ATLAS_EDGE_AP0_SETUP_SCRIPT:-/home/limitify/Developer/Atlas-Edge/atlas_edge/scripts/ap0_setup.sh}"

log() {
  logger -t atlas-ap0-watchdog -- "$1"
  echo "[atlas-ap0-watchdog] $1"
}

iface_ok() {
  iw dev "$HOTSPOT_IFACE" info >/dev/null 2>&1
}

hostapd_ok() {
  systemctl is-active --quiet atlas-ap0-hostapd.service
}

dnsmasq_ok() {
  systemctl is-active --quiet atlas-ap0-dnsmasq.service
}

# True when wlan0 isn't associated (nothing to compare against, so don't
# flap) OR its current channel matches what ap0 is actually running.
channel_ok() {
  if ! iw dev "$WIFI_IFACE" link 2>/dev/null | grep -q "Connected to"; then
    return 0
  fi
  local wlan_channel hostapd_channel
  wlan_channel=$(iw dev "$WIFI_IFACE" info 2>/dev/null | awk '/channel/ {print $2; exit}')
  hostapd_channel=$(awk -F= '/^channel=/ {print $2; exit}' "$HOSTAPD_CONF" 2>/dev/null)
  [ -n "$wlan_channel" ] && [ "$wlan_channel" = "$hostapd_channel" ]
}

if iface_ok && hostapd_ok && dnsmasq_ok && channel_ok; then
  log "OK — ${HOTSPOT_IFACE} exists, hostapd and dnsmasq are active, channel matches ${WIFI_IFACE}."
  exit 0
fi

log "Unhealthy — iface_ok=$(iface_ok && echo yes || echo no), hostapd_ok=$(hostapd_ok && echo yes || echo no), dnsmasq_ok=$(dnsmasq_ok && echo yes || echo no), channel_ok=$(channel_ok && echo yes || echo no). Attempting recovery…"

# Cheap fix first: only when the channel is already right — a service
# restart alone can't fix a channel mismatch, only a full setup rewrites
# hostapd's config, so a wrong channel always falls through below.
if iface_ok && channel_ok; then
  log "Trying to restart atlas-ap0-hostapd.service / atlas-ap0-dnsmasq.service first…"
  if systemctl restart atlas-ap0-hostapd.service atlas-ap0-dnsmasq.service >/dev/null 2>&1 \
     && hostapd_ok && dnsmasq_ok; then
    log "Recovered via service restart."
    exit 0
  fi
  log "Direct service restart failed."
fi

# Fall back to the full setup — handles ap0 having vanished entirely,
# re-waits for wlan0 association if that's also gone, and realigns the
# channel to match wlan0's current one.
log "Falling back to full setup script: ${SETUP_SCRIPT}"
if "$SETUP_SCRIPT"; then
  log "Recovered via full setup script."
  exit 0
fi

log "ERROR: recovery failed — both service restart and full setup were unsuccessful. Manual intervention needed."
exit 1
