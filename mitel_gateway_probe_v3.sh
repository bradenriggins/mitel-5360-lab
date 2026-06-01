#!/bin/zsh
set -u

LOG=/tmp/mitel-gateway-probe-v3.log
IFACE=en1
HOME_SSID='DavTower - Unit 1106'
PASS='332492574'
SSIDS=('83DCD2-2.4' '83DCD2-5')

exec >"$LOG" 2>&1

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

snapshot() {
  log "$1 network=$(networksetup -getairportnetwork "$IFACE" 2>&1)"
  log "$1 ip=$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
  log "$1 ifconfig=$(ifconfig "$IFACE" | tr '\n' ' ')"
  log "$1 route_start"
  route -n get default 2>/dev/null || true
  log "$1 route_end"
}

probe_gateway_lan() {
  local ip="$1"
  local prefix="${ip%.*}"
  log "probe_prefix=${prefix}.0/24"

  log "gateway_http_headers"
  curl -m 3 -sSI "http://${prefix}.1/" || true
  log "gateway_https_headers"
  curl -k -m 3 -sSI "https://${prefix}.1/" || true
  log "gateway_html_head"
  curl -k -m 5 -s "https://${prefix}.1/" | sed -n '1,40p' || true

  log "arp_discovery_start"
  for i in {1..254}; do
    (ping -c 1 -W 300 "$prefix.$i" >/dev/null 2>&1) &
  done
  wait
  log "arp_complete"
  arp -an | grep "$prefix\\." | grep -v incomplete | sort || true

  log "live_hosts"
  for i in {1..254}; do
    host="$prefix.$i"
    ping -c 1 -W 300 "$host" >/dev/null 2>&1 && echo "$host"
  done

  log "port_probe_complete_arp_hosts"
  arp -an | grep "$prefix\\." | grep -v incomplete | awk '{gsub(/[()]/, "", $2); print $2}' | sort -u | while read host; do
    printf '%s ' "$host"
    nc -G 1 -z "$host" 80 >/dev/null 2>&1 && printf '80 '
    nc -G 1 -z "$host" 443 >/dev/null 2>&1 && printf '443 '
    nc -G 1 -z "$host" 23 >/dev/null 2>&1 && printf '23 '
    nc -G 1 -z "$host" 5060 >/dev/null 2>&1 && printf '5060 '
    nc -G 1 -z "$host" 5061 >/dev/null 2>&1 && printf '5061 '
    echo
  done
}

log "probe_start host=$(hostname) user=$(whoami)"
snapshot before

joined=0
for ssid in "${SSIDS[@]}"; do
  log "attempt_ssid=$ssid"
  output="$(networksetup -setairportnetwork "$IFACE" "$ssid" "$PASS" 2>&1)"
  rc=$?
  log "join_rc=$rc join_output=$output"
  sleep 20
  snapshot "after_$ssid"
  ip="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
  if [[ $rc -eq 0 && -n "$ip" && "$ip" != 192.168.4.* ]]; then
    joined=1
    probe_gateway_lan "$ip"
    break
  fi
done

log "joined=$joined"
log "restore_home_start"
networksetup -setairportpower "$IFACE" off
sleep 4
networksetup -setairportpower "$IFACE" on
sleep 8
output="$(networksetup -setairportnetwork "$IFACE" "$HOME_SSID" 2>&1)"
log "restore_join_output=$output"
sleep 15
snapshot restored
log "probe_end"
