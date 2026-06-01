#!/usr/bin/env python3
import argparse
import ipaddress
import signal
import socket
import sys
import time

from scapy.all import BOOTP, DHCP, Ether, IP, UDP, conf, get_if_hwaddr, sniff


MITEL_MAC_DEFAULT = "08:00:0f:69:43:5b"


def mac_bytes(mac: str) -> bytes:
    return bytes(int(part, 16) for part in mac.split(":"))


def ip_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


def u32(value: int) -> bytes:
    return int(value).to_bytes(4, "big")


def normalize_mac(mac: str) -> str:
    return ":".join(part.zfill(2).lower() for part in mac.split(":"))


class MitelDhcpResponder:
    def __init__(self, args):
        self.iface = args.iface
        self.target_mac = normalize_mac(args.target_mac)
        self.server_ip = args.server_ip
        self.packet_src_ip = args.packet_src_ip or args.server_ip
        self.client_ip = args.client_ip
        self.router = args.router
        self.dns = args.dns
        self.subnet = args.subnet
        self.lease = args.lease
        self.tftp = args.tftp
        self.call_server = args.call_server
        self.omit_call_server = args.omit_call_server
        self.omit_ip_call_options = args.omit_ip_call_options
        self.sip_server_option = args.sip_server_option
        self.cfg_uri = args.cfg_uri
        self.fast_burst = args.fast_burst
        self.preemptive_ack_burst = args.preemptive_ack_burst
        self.no_vlan = args.no_vlan
        self.server_mac = get_if_hwaddr(self.iface)
        self.running = True
        self.l2sock = conf.L2socket(iface=self.iface)

        mitel_parts = [
            "id:ipphone.mitel.com",
            f"sw_tftp={self.tftp}",
        ]
        if self.cfg_uri:
            mitel_parts.append(f"cfg_uri={self.cfg_uri}")
        if not self.omit_call_server:
            mitel_parts.append(f"call_srv={self.call_server}")
        if not self.no_vlan:
            mitel_parts.append("vlan=1")
        mitel_parts.extend(["l2p=6", "dscp=46"])
        self.mitel_option = (";".join(mitel_parts) + ";").encode("ascii")

    def log(self, message: str):
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    def dhcp_message_type(self, packet):
        if DHCP not in packet:
            return None
        for opt in packet[DHCP].options:
            if isinstance(opt, tuple) and opt[0] == "message-type":
                return opt[1]
        return None

    def requested_ip(self, packet):
        for opt in packet[DHCP].options:
            if isinstance(opt, tuple) and opt[0] == "requested_addr":
                return opt[1]
        return None

    def base_reply(self, packet, message_type):
        bootp = packet[BOOTP]
        options = [
            ("message-type", message_type),
            ("server_id", self.server_ip),
            ("lease_time", self.lease),
            ("renewal_time", self.lease // 2),
            ("rebinding_time", int(self.lease * 0.875)),
            ("subnet_mask", self.subnet),
            ("router", self.router),
            ("name_server", self.dns),
            ("NTP_server", self.router),
        ]
        if not self.omit_ip_call_options:
            options.extend([
                (128, ip_bytes(self.tftp)),
                (129, ip_bytes(self.call_server)),
                (130, b"MITEL IP PHONE"),
                (131, ip_bytes(self.call_server)),
            ])
        if self.sip_server_option:
            options.append((120, b"\x01" + ip_bytes(self.server_ip)))
        options.extend([
            ("tftp_server_name", self.tftp),
            ("vendor_specific", self.mitel_option),
            ("vendor_specific_information", self.mitel_option),
            ("tftp_server_ip_address", self.tftp),
        ])

        if not self.no_vlan:
            options.extend([
                (132, u32(1)),
                (133, u32(6)),
                (134, u32(46)),
            ])

        options.append("end")

        # siaddr must match server_id; Mitel ignores ACK when siaddr points at TFTP only.
        return (
            Ether(src=self.server_mac, dst=self.target_mac if self.fast_burst else "ff:ff:ff:ff:ff:ff")
            / IP(src=self.packet_src_ip, dst="255.255.255.255")
            / UDP(sport=67, dport=68)
            / BOOTP(
                op=2,
                yiaddr=self.client_ip,
                siaddr=self.server_ip,
                chaddr=mac_bytes(self.target_mac) + b"\x00" * 10,
                xid=bootp.xid,
                flags=bootp.flags | 0x8000,
                secs=bootp.secs,
                sname=self.server_ip.encode("ascii"),
            )
            / DHCP(options=options)
        )

    def send_burst(self, packet, count=3):
        raw_packet = bytes(packet)
        for _ in range(count):
            self.l2sock.send(raw_packet)

    def handle(self, packet):
        if Ether not in packet or BOOTP not in packet or DHCP not in packet:
            return
        src = normalize_mac(packet[Ether].src)
        if src != self.target_mac:
            return

        message_type = self.dhcp_message_type(packet)
        requested = self.requested_ip(packet)
        self.log(
            f"phone DHCP {message_type!r} xid=0x{packet[BOOTP].xid:08x} "
            f"requested={requested or '-'} secs={packet[BOOTP].secs}"
        )

        if message_type in (1, "discover"):
            reply = self.base_reply(packet, "offer")
            self.send_burst(reply, 12 if self.fast_burst else 3)
            if self.preemptive_ack_burst:
                ack = self.base_reply(packet, "ack")
                self.send_burst(ack, self.preemptive_ack_burst)
            self.log(
                f"sent OFFER {self.client_ip} server={self.server_ip} "
                f"tftp={self.tftp} call_srv={'omitted' if self.omit_call_server else self.call_server} "
                f"opt125={self.mitel_option.decode()}"
            )
        elif message_type in (3, "request"):
            if requested and requested not in (self.client_ip, self.server_ip):
                self.log(f"request was for {requested}; ACKing our lease anyway")
            reply = self.base_reply(packet, "ack")
            self.send_burst(reply, 40 if self.fast_burst else 3)
            self.log(f"sent ACK {self.client_ip}")

    def run(self):
        self.log(
            f"starting iface={self.iface} server_mac={self.server_mac} "
            f"target={self.target_mac} offer={self.client_ip}"
        )
        self.log("power-cycle or reboot the Mitel now if it is not already retrying DHCP")
        sniff(
            iface=self.iface,
            filter="udp and (port 67 or port 68)",
            prn=self.handle,
            store=False,
            stop_filter=lambda _: not self.running,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default="en0")
    parser.add_argument("--target-mac", default=MITEL_MAC_DEFAULT)
    parser.add_argument("--server-ip", default="192.168.4.30")
    parser.add_argument("--packet-src-ip")
    parser.add_argument("--client-ip", default="192.168.4.240")
    parser.add_argument("--router", default="192.168.4.1")
    parser.add_argument("--dns", default="192.168.4.1")
    parser.add_argument("--subnet", default="255.255.252.0")
    parser.add_argument("--lease", type=int, default=86400)
    parser.add_argument("--tftp", default="192.168.4.30")
    parser.add_argument("--call-server", default="192.168.4.30")
    parser.add_argument("--omit-call-server", action="store_true")
    parser.add_argument("--omit-ip-call-options", action="store_true")
    parser.add_argument("--sip-server-option", action="store_true")
    parser.add_argument("--cfg-uri")
    parser.add_argument("--fast-burst", action="store_true")
    parser.add_argument("--preemptive-ack-burst", type=int, default=0)
    parser.add_argument("--no-vlan", action="store_true", default=True)
    args = parser.parse_args()

    for value in [args.server_ip, args.packet_src_ip or args.server_ip, args.client_ip, args.router, args.dns, args.subnet, args.tftp, args.call_server]:
        ipaddress.ip_address(value)

    responder = MitelDhcpResponder(args)

    def stop(_signum, _frame):
        responder.running = False
        responder.log("stopping")
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    responder.run()


if __name__ == "__main__":
    main()
