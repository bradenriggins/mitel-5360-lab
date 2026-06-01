#!/usr/bin/env bash
set -euo pipefail

# ── Config — override with env vars ──────────────────────────────────────────
LOG_DIR="${MITEL_LOG_DIR:-/tmp/mitel-lab-logs}"
CAFFEINATE_LABEL="com.mitel-lab.caffeinate"
PHONE_ADMIN_PORT="${MITEL_PHONE_ADMIN_PORT:-18070}"
PHONE_ADMIN_TARGET="${PHONE_ADMIN_TARGET:-192.168.0.70:80}"
BOOTZ_SSH_TARGETS=("${MITEL_BOOTZ_SSH_TARGET:-bootz}")
BOOTZ_SSH_KEY="${MITEL_SSH_KEY:-~/.ssh/id_ecdsa}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${MITEL_PYTHON:-python3}"
LAB="$REPO_ROOT/mitel_lab.py"
DHCP="$REPO_ROOT/mitel_dhcp_responder.py"
LAB_HOST="${LAB_HOST:-192.168.4.30}"
PHONE_HOST="${PHONE_HOST:-192.168.4.33}"

BASE_DHCP_ARGS="--iface en0 --server-ip $LAB_HOST --packet-src-ip $LAB_HOST --client-ip $PHONE_HOST --router ${MITEL_GATEWAY:-192.168.4.1} --dns ${MITEL_DNS:-192.168.4.1} --tftp $LAB_HOST --call-server $LAB_HOST --lease 14400 --cfg-uri http://$LAB_HOST/ --fast-burst --preemptive-ack-burst 120"
SIP_DHCP_ARGS="$BASE_DHCP_ARGS --omit-call-server --omit-ip-call-options --sip-server-option"
MINET_DHCP_ARGS="$BASE_DHCP_ARGS"

start_phone_admin_tunnel() {
  pkill -f "[s]sh .*${PHONE_ADMIN_PORT}:${PHONE_ADMIN_TARGET}" 2>/dev/null || true
  local target
  for target in "${BOOTZ_SSH_TARGETS[@]}"; do
    if ssh -fN \
      -o BatchMode=yes \
      -o ConnectTimeout=5 \
      -o ExitOnForwardFailure=yes \
      -o StrictHostKeyChecking=accept-new \
      -i "$BOOTZ_SSH_KEY" \
      -L "0.0.0.0:${PHONE_ADMIN_PORT}:${PHONE_ADMIN_TARGET}" \
      "$target"; then
      echo "Native admin tunnel via $target"
      return 0
    fi
  done
  echo "warning: native admin tunnel unavailable" >&2
  return 1
}

start_lab() {
  local dhcp_args="$1"
  sudo mkdir -p "$LOG_DIR"
  sudo pkill -f '[m]itel_lab.py' 2>/dev/null || true
  sudo pkill -f '[m]itel_dhcp_responder.py' 2>/dev/null || true
  launchctl remove "$CAFFEINATE_LABEL" 2>/dev/null || true
  sudo bash -c "nohup '$PY' '$LAB' > '$LOG_DIR/lab.log' 2>&1 & echo \$! > '$LOG_DIR/lab.pid'"
  launchctl submit -l "$CAFFEINATE_LABEL" -- /usr/bin/caffeinate -im
  start_phone_admin_tunnel || true
  sudo bash -c "nohup '$PY' '$DHCP' $dhcp_args > '$LOG_DIR/dhcp.log' 2>&1 & echo \$! > '$LOG_DIR/dhcp.pid'"
  echo "Mitel lab started"
  echo "Dashboard: http://127.0.0.1/"
  echo "LAN: http://$LAB_HOST/"
  echo "Native admin: http://$LAB_HOST:${PHONE_ADMIN_PORT}/"
}

case "${1:-status}" in
  start)
    start_lab "$SIP_DHCP_ARGS"
    ;;
  start-minet)
    start_lab "$MINET_DHCP_ARGS"
    ;;
  stop)
    sudo pkill -f '[m]itel_lab.py' 2>/dev/null || true
    sudo pkill -f '[m]itel_dhcp_responder.py' 2>/dev/null || true
    pkill -f "[s]sh .*${PHONE_ADMIN_PORT}:${PHONE_ADMIN_TARGET}" 2>/dev/null || true
    launchctl remove "$CAFFEINATE_LABEL" 2>/dev/null || true
    echo "Mitel lab stopped"
    ;;
  status)
    pgrep -fl 'mitel_lab.py|mitel_dhcp_responder.py' || true
    pgrep -fl "[s]sh .*${PHONE_ADMIN_PORT}:${PHONE_ADMIN_TARGET}" || true
    lsof -nP -iTCP:"$PHONE_ADMIN_PORT" -sTCP:LISTEN || true
    if [[ -f "$LOG_DIR/lab.log" ]]; then
      echo "--- lab.log tail ---"
      tail -20 "$LOG_DIR/lab.log"
    fi
    if [[ -f "$LOG_DIR/dhcp.log" ]]; then
      echo "--- dhcp.log tail ---"
      tail -20 "$LOG_DIR/dhcp.log"
    fi
    ;;
  *)
    echo "usage: $0 {start|start-minet|stop|status}" >&2
    exit 2
    ;;
esac
