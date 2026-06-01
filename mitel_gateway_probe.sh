#!/bin/zsh
set -u

LOG=/tmp/mitel-gateway-probe.log
IFACE=en1
HOME_SSID='DavTower - Unit 1106'
PASS='332492574'
SSIDS=('83DCD2-2.4' '83DCD2-5')

exec >"$LOG" 2>&1

echo "probe_start=$(date)"
echo "host=$(hostname)"
echo "user=$(whoami)"
echo "before_airport_power=$(networksetup -getairportpower "$IFACE" 2>&1)"
echo "before_network=$(networksetup -getairportnetwork "$IFACE" 2>&1)"
echo "before_ifconfig="
ifconfig "$IFACE"

scan_subnet() {
  local ip="$1"
  local prefix="${ip%.*}"
  echo "scan_subnet=${prefix}.0/24"
  for i in {1..254}; do
    (ping -c 1 -W 500 "$prefix.$i" >/dev/null 2>&1 && echo "$prefix.$i") &
  done
  wait
  echo "arp_after_scan="
  arp -an | grep "$prefix\\." | sort || true
  echo "port_probe="
  for i in {1..254}; do
    local host="$prefix.$i"
    ping -c 1 -W 250 "$host" >/dev/null 2>&1 || continue
    printf '%s ' "$host"
    nc -G 1 -z "$host" 80 >/dev/null 2>&1 && printf '80 '
    nc -G 1 -z "$host" 443 >/dev/null 2>&1 && printf '443 '
    nc -G 1 -z "$host" 23 >/dev/null 2>&1 && printf '23 '
    nc -G 1 -z "$host" 5060 >/dev/null 2>&1 && printf '5060 '
    echo
  done
  echo "http_headers="
  for i in {1..254}; do
    local host="$prefix.$i"
    nc -G 1 -z "$host" 80 >/dev/null 2>&1 || continue
    echo "http://$host/"
    curl -m 2 -sSI "http://$host/" | sed -n '1,8p' || true
  done
}

for ssid in "${SSIDS[@]}"; do
  echo "attempt_ssid=$ssid"
  networksetup -setairportnetwork "$IFACE" "$ssid" "$PASS"
  sleep 18
  echo "after_network=$(networksetup -getairportnetwork "$IFACE" 2>&1)"
  echo "after_ifconfig="
  ifconfig "$IFACE"
  echo "route_default="
  route -n get default 2>/dev/null | awk '/interface:|gateway:/{print}' || true
  ip="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
  echo "ip=$ip"
  if [[ -n "$ip" ]]; then
    scan_subnet "$ip"
  fi
done

echo "restore_home_start=$(date)"
networksetup -setairportpower "$IFACE" off
sleep 4
networksetup -setairportpower "$IFACE" on
sleep 8
networksetup -setairportnetwork "$IFACE" "$HOME_SSID" || true
sleep 12
echo "restore_network=$(networksetup -getairportnetwork "$IFACE" 2>&1)"
echo "restore_ifconfig="
ifconfig "$IFACE"
echo "probe_end=$(date)"
