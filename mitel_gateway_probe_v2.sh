#!/bin/zsh
set -u

LOG=/tmp/mitel-gateway-probe-v2.log
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
  log "$1 route=$(route -n get default 2>/dev/null | awk '/gateway:|interface:/{gsub(/^ +/,\"\"); printf \"%s; \", $0}')"
}

probe_current_subnet() {
  local ip="$1"
  [[ -z "$ip" ]] && return
  local prefix="${ip%.*}"
  log "scan_prefix=${prefix}.0/24"

  log "common_gateway_probe"
  for host in 192.168.0.1 192.168.1.1 192.168.100.1 10.0.0.1 "$prefix.1"; do
    printf '%s ' "$host"
    ping -c 1 -W 500 "$host" >/dev/null 2>&1 && printf 'ping '
    nc -G 1 -z "$host" 80 >/dev/null 2>&1 && printf '80 '
    nc -G 1 -z "$host" 443 >/dev/null 2>&1 && printf '443 '
    echo
  done

  log "live_hosts"
  for i in {1..254}; do
    (ping -c 1 -W 500 "$prefix.$i" >/dev/null 2>&1 && echo "$prefix.$i") &
  done
  wait

  log "arp"
  arp -an | grep "$prefix\\." | sort || true

  log "ports"
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

  log "http_headers"
  for i in {1..254}; do
    local host="$prefix.$i"
    nc -G 1 -z "$host" 80 >/dev/null 2>&1 || continue
    echo "http://$host/"
    curl -m 2 -sSI "http://$host/" | sed -n '1,8p' || true
  done
}

log "probe_start host=$(hostname) user=$(whoami)"
snapshot before

for ssid in "${SSIDS[@]}"; do
  log "attempt_ssid=$ssid"
  output="$(networksetup -setairportnetwork "$IFACE" "$ssid" "$PASS" 2>&1)"
  rc=$?
  log "join_rc=$rc join_output=$output"
  sleep 20
  snapshot "after_$ssid"
  ip="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
  if [[ $rc -eq 0 && -n "$ip" && "$ip" != 192.168.4.* ]]; then
    probe_current_subnet "$ip"
  else
    log "skip_scan reason=join_failed_or_still_home_subnet"
  fi
done

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
