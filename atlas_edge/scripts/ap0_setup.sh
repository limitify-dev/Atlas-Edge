#!/usr/bin/env bash
# Brings up the Atlas-Edge admin hotspot (ap0) on top of the station radio
# (wlan0 — a USB adapter; this Pi's onboard WiFi chip doesn't attach at
# boot). Run at boot by systemd/atlas-ap0-setup.service, and re-run by the
# watchdog (ap0_watchdog.sh) if the hotspot ever drops.
#
# The USB adapter supports concurrent station + AP mode on one radio (see
# `iw list` — look for a valid interface combination with #{managed} <= 2
# and #{AP} <= 1), so ap0 rides on wlan0's existing channel rather than
# needing a second physical radio.
#
# Idempotent — safe to re-run without tearing down an already-working setup.
set -euo pipefail

# Load ATLAS_EDGE_* overrides the same way the rest of this project does.
# systemd's EnvironmentFile= already injects these for the unit; source
# directly too so the script also works run by hand.
ENV_FILE="${ATLAS_EDGE_ENV_FILE:-/home/limitify/Developer/Atlas-Edge/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

WIFI_IFACE="${ATLAS_EDGE_WIFI_IFACE:-wlan0}"
HOTSPOT_IFACE="${ATLAS_EDGE_HOTSPOT_IFACE:-ap0}"
HOTSPOT_CONN="${ATLAS_EDGE_HOTSPOT_CONN_NAME:-Atlas-Edge-Admin}"
ASSOC_TIMEOUT_SECONDS="${ATLAS_EDGE_WIFI_ASSOC_TIMEOUT_SECONDS:-60}"

log() {
  logger -t atlas-ap0-setup -- "$1"
  echo "[atlas-ap0-setup] $1"
}

fail() {
  log "ERROR: $1"
  exit 1
}

# ── 1. Wait for the station interface to associate ─────────────────────
# ap0 rides on wlan0's radio/channel, so there's nothing to bind it to
# until wlan0 has actually joined a network.
log "Waiting up to ${ASSOC_TIMEOUT_SECONDS}s for ${WIFI_IFACE} to associate…"
waited=0
until iw dev "$WIFI_IFACE" link 2>/dev/null | grep -q "Connected to"; do
  if [ "$waited" -ge "$ASSOC_TIMEOUT_SECONDS" ]; then
    fail "${WIFI_IFACE} did not associate with any network within ${ASSOC_TIMEOUT_SECONDS}s — cannot bind ${HOTSPOT_IFACE} to it. Check the USB adapter is seated and the school network is in range."
  fi
  sleep 2
  waited=$((waited + 2))
done
log "${WIFI_IFACE} is associated."

# ── 2. Create the AP interface, idempotently ────────────────────────────
if iw dev "$HOTSPOT_IFACE" info >/dev/null 2>&1; then
  log "${HOTSPOT_IFACE} already exists — skipping creation."
else
  log "Creating ${HOTSPOT_IFACE} on ${WIFI_IFACE}…"
  iw dev "$WIFI_IFACE" interface add "$HOTSPOT_IFACE" type __ap \
    || fail "Failed to create ${HOTSPOT_IFACE} on ${WIFI_IFACE} — does this radio support concurrent AP+station mode? (check: iw list)"
fi

# ── 3. Make sure NetworkManager is managing it ──────────────────────────
# Deliberately no manual `ip link set up` here: NetworkManager auto-manages
# newly-created wifi netdevs the moment they appear (regardless of the
# `managed yes` call below), and will reset a bare __ap interface back to
# `managed` type before we ever reach it. Racing NM with a manual link-up
# just fails every time. `nmcli connection up` in step 5 brings the
# interface up itself as part of activating the AP profile.
nmcli device set "$HOTSPOT_IFACE" managed yes \
  || fail "Failed to tell NetworkManager to manage ${HOTSPOT_IFACE}."

# A freshly-added virtual interface can take NetworkManager a moment to
# register — give it a few seconds rather than racing it.
waited=0
until nmcli -t -f DEVICE device status 2>/dev/null | grep -qx "$HOTSPOT_IFACE"; do
  if [ "$waited" -ge 15 ]; then
    fail "NetworkManager never picked up ${HOTSPOT_IFACE} as a managed device."
  fi
  sleep 1
  waited=$((waited + 1))
done
log "NetworkManager is managing ${HOTSPOT_IFACE}."

# ── 4. Bind the admin hotspot profile to ap0 and bring it up ───────────
# The profile may have been created against a different/nonexistent
# interface name — always (re)bind it to the real one before activating,
# so this is also correct on a freshly re-imaged Pi.
nmcli connection modify "$HOTSPOT_CONN" connection.interface-name "$HOTSPOT_IFACE" \
  || fail "Failed to bind connection profile '${HOTSPOT_CONN}' to ${HOTSPOT_IFACE} — does that connection profile exist? (nmcli connection show)"

nmcli connection up "$HOTSPOT_CONN" ifname "$HOTSPOT_IFACE" \
  || fail "Failed to activate '${HOTSPOT_CONN}' on ${HOTSPOT_IFACE}."

log "'${HOTSPOT_CONN}' is up on ${HOTSPOT_IFACE}. Admin hotspot ready."
