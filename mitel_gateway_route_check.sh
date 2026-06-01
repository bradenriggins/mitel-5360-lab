#!/bin/zsh
set -u

LOG=/tmp/mitel-gateway-route-check.log
IFACE=en0
HOME_SSID='DavTower - Unit 1106'
PASS='332492574'
SSID='83DCD2-2.4'

exec >"$LOG" 2>&1

echo "start=$(date)"
echo "before_network=$(networksetup -getairportnetwork "$IFACE" 2>&1)"
echo "before_ip=$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
networksetup -setairportnetwork "$IFACE" "$SSID" "$PASS"
echo "join_rc=$?"
sleep 15
echo "after_network=$(networksetup -getairportnetwork "$IFACE" 2>&1)"
echo "after_ip=$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
echo "ifconfig="
ifconfig "$IFACE"
echo "route_192_168_0_1="
route -n get 192.168.0.1 2>&1 || true
echo "ping_gateway="
ping -c 2 -W 1000 192.168.0.1 || true
echo "arp="
arp -an | grep '192\.168\.0\.' || true
networksetup -setairportpower "$IFACE" off
sleep 3
networksetup -setairportpower "$IFACE" on
sleep 6
networksetup -setairportnetwork "$IFACE" "$HOME_SSID" || true
sleep 10
echo "restored_network=$(networksetup -getairportnetwork "$IFACE" 2>&1)"
echo "restored_ip=$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
echo "end=$(date)"
