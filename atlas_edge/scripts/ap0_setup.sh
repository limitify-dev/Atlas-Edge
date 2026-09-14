#!/usr/bin/env bash
# Brings up the Atlas-Edge admin hotspot (ap0) on top of the station radio
# (wlan0 — a USB adapter; this Pi's onboard WiFi chip doesn't attach at
# boot). Run at boot by systemd/atlas-ap0-setup.service, and re-run by the
# watchdog (ap0_watchdog.sh) if the hotspot ever drops.
#
# The USB adapter (rtl8xxxu) supports concurrent station + AP mode on one
# radio — confirmed working with hostapd directly. NetworkManager's own
# "Hotspot" activation (nmcli/wpa_supplicant) does NOT work here: it runs a
# pre-beacon scan on the AP interface that this driver can't service while
# wlan0 is actively associated, so it just hangs and times out. So this
# script bypasses NM's hotspot machinery entirely — it marks ap0 permanently
# unmanaged by NM and drives hostapd + dnsmasq directly, which is also the
# standard, more robust way this is normally done on Linux.
#
# Also critical: this driver gives ap0 the *same* MAC address as wlan0 by
# default, and NetworkManager resets it back to that shared address any time
# it touches the interface. A duplicate MAC on the same radio makes the
# kernel refuse to bring the interface up at all ("Name not unique on
# network"). So this script always (re)assigns ap0 a distinct
# locally-administered MAC derived from wlan0's, every run.
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
ASSOC_TIMEOUT_SECONDS="${ATLAS_EDGE_WIFI_ASSOC_TIMEOUT_SECONDS:-60}"

HOTSPOT_SSID="${ATLAS_EDGE_HOTSPOT_SSID:?ATLAS_EDGE_HOTSPOT_SSID must be set}"
HOTSPOT_PASSWORD="${ATLAS_EDGE_HOTSPOT_PASSWORD:?ATLAS_EDGE_HOTSPOT_PASSWORD must be set}"
HOTSPOT_IP="${ATLAS_EDGE_HOTSPOT_IP:-10.42.0.1}"
HOTSPOT_PREFIX="${ATLAS_EDGE_HOTSPOT_PREFIX:-24}"
HOTSPOT_DHCP_RANGE_START="${ATLAS_EDGE_HOTSPOT_DHCP_RANGE_START:-10.42.0.10}"
HOTSPOT_DHCP_RANGE_END="${ATLAS_EDGE_HOTSPOT_DHCP_RANGE_END:-10.42.0.200}"

HOSTAPD_CONF="${ATLAS_EDGE_HOTSPOT_HOSTAPD_CONF:-/etc/hostapd/atlas-ap0.conf}"
DNSMASQ_CONF="${ATLAS_EDGE_HOTSPOT_DNSMASQ_CONF:-/etc/dnsmasq-atlas-ap0.conf}"

log() { logger -t atlas-ap0-setup -- "$1"; echo "[atlas-ap0-setup] $1"; }
fail() { log "ERROR: $1"; exit 1; }

if [ "${#HOTSPOT_PASSWORD}" -lt 8 ]; then
  fail "ATLAS_EDGE_HOTSPOT_PASSWORD must be at least 8 characters (WPA2 requirement)."
fi

# ── 1. Wait for the station interface to associate ─────────────────────
# ap0 rides on wlan0's radio/channel, so there's nothing to bind it to
# until wlan0 has actually joined a network, and we need its channel.
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

WIFI_CHANNEL=$(iw dev "$WIFI_IFACE" info | awk '/channel/ {print $2; exit}')
[ -n "$WIFI_CHANNEL" ] || fail "Could not determine ${WIFI_IFACE}'s current channel (iw dev ${WIFI_IFACE} info)."
log "${WIFI_IFACE} is on channel ${WIFI_CHANNEL} — ${HOTSPOT_IFACE} must share it (same radio)."

# ── 2. Create the AP interface, idempotently ────────────────────────────
if iw dev "$HOTSPOT_IFACE" info >/dev/null 2>&1; then
  log "${HOTSPOT_IFACE} already exists — skipping creation."
else
  log "Creating ${HOTSPOT_IFACE} on ${WIFI_IFACE}…"
  iw dev "$WIFI_IFACE" interface add "$HOTSPOT_IFACE" type __ap \
    || fail "Failed to create ${HOTSPOT_IFACE} on ${WIFI_IFACE} — does this radio support concurrent AP+station mode? (check: iw list)"
fi

# ── 3. Give it a distinct MAC ────────────────────────────────────────────
# Must be down to change its address. This driver hands out wlan0's own MAC
# by default, and NM resets it back to that on every release — a duplicate
# MAC on the same radio makes `ip link set up` fail outright.
ip link set "$HOTSPOT_IFACE" down 2>/dev/null || true
base_mac=$(cat "/sys/class/net/${WIFI_IFACE}/address")
first_octet=${base_mac%%:*}
rest_of_mac=${base_mac#*:}
locally_administered=$(printf '%02x' $(( 0x$first_octet | 0x02 )))
HOTSPOT_MAC="${locally_administered}:${rest_of_mac}"
ip link set dev "$HOTSPOT_IFACE" address "$HOTSPOT_MAC" \
  || fail "Failed to set a distinct MAC (${HOTSPOT_MAC}) on ${HOTSPOT_IFACE}."
log "${HOTSPOT_IFACE} MAC set to ${HOTSPOT_MAC} (distinct from ${WIFI_IFACE}'s ${base_mac})."

# ── 4. Tell NetworkManager to leave it alone, permanently ──────────────
# NM's own hotspot activation doesn't work on this driver (see header), and
# if NM manages this interface at all it resets the MAC set above on
# release. hostapd/dnsmasq own ap0 completely from here on.
nmcli device set "$HOTSPOT_IFACE" managed no 2>/dev/null || true

# ── 5. Bring it up and assign it a static address ───────────────────────
ip link set "$HOTSPOT_IFACE" up || fail "Failed to bring ${HOTSPOT_IFACE} up (ip link set)."
ip addr flush dev "$HOTSPOT_IFACE"
ip addr add "${HOTSPOT_IP}/${HOTSPOT_PREFIX}" dev "$HOTSPOT_IFACE"
log "${HOTSPOT_IFACE} is up at ${HOTSPOT_IP}/${HOTSPOT_PREFIX}."

# ── 6. Write hostapd + dnsmasq config and (re)start them ───────────────
mkdir -p "$(dirname "$HOSTAPD_CONF")"
cat > "$HOSTAPD_CONF" <<EOF
interface=${HOTSPOT_IFACE}
driver=nl80211
ssid=${HOTSPOT_SSID}
hw_mode=g
channel=${WIFI_CHANNEL}
wpa=2
wpa_passphrase=${HOTSPOT_PASSWORD}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ctrl_interface=/var/run/hostapd
EOF
chmod 600 "$HOSTAPD_CONF"

cat > "$DNSMASQ_CONF" <<EOF
interface=${HOTSPOT_IFACE}
bind-interfaces
except-interface=lo
dhcp-range=${HOTSPOT_DHCP_RANGE_START},${HOTSPOT_DHCP_RANGE_END},12h
dhcp-option=3,${HOTSPOT_IP}
dhcp-option=6,${HOTSPOT_IP}
EOF

log "Restarting atlas-ap0-hostapd.service and atlas-ap0-dnsmasq.service…"
systemctl restart atlas-ap0-hostapd.service \
  || fail "atlas-ap0-hostapd.service failed to start — check: journalctl -u atlas-ap0-hostapd.service"
systemctl restart atlas-ap0-dnsmasq.service \
  || fail "atlas-ap0-dnsmasq.service failed to start — check: journalctl -u atlas-ap0-dnsmasq.service"

log "Admin hotspot '${HOTSPOT_SSID}' ready on ${HOTSPOT_IFACE} (${HOTSPOT_IP})."
