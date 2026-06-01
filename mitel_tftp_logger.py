#!/usr/bin/env python3
import argparse
import os
import socket
import struct
import time


def ts():
    return time.strftime("%H:%M:%S")


def parse_rrq(data):
    if len(data) < 4 or data[:2] != b"\x00\x01":
        return None
    parts = data[2:].split(b"\x00")
    if len(parts) < 2:
        return None
    try:
        filename = parts[0].decode("ascii", "replace")
        mode = parts[1].decode("ascii", "replace")
    except Exception:
        return None
    return filename, mode


def safe_path(root, filename):
    cleaned = filename.replace("\\", "/").lstrip("/")
    path = os.path.realpath(os.path.join(root, cleaned))
    root_real = os.path.realpath(root)
    if path == root_real or path.startswith(root_real + os.sep):
        return path
    return None


def send_error(sock, addr, code, message):
    payload = struct.pack("!HH", 5, code) + message.encode("ascii", "replace") + b"\x00"
    sock.sendto(payload, addr)


def serve_file(sock, addr, path, block_size=512):
    with open(path, "rb") as f:
        block = 1
        while True:
            chunk = f.read(block_size)
            packet = struct.pack("!HH", 3, block) + chunk
            sock.sendto(packet, addr)
            sock.settimeout(4)
            try:
                ack, ack_addr = sock.recvfrom(2048)
            except socket.timeout:
                print(f"{ts()} timeout waiting ACK block={block} from={addr}", flush=True)
                return
            if ack_addr != addr or len(ack) < 4 or ack[:2] != b"\x00\x04":
                print(f"{ts()} unexpected response from={ack_addr} hex={ack.hex()}", flush=True)
                return
            ack_block = struct.unpack("!H", ack[2:4])[0]
            if ack_block != block:
                print(f"{ts()} unexpected ACK block={ack_block} wanted={block}", flush=True)
                return
            if len(chunk) < block_size:
                print(f"{ts()} served {path} to={addr}", flush=True)
                return
            block = (block + 1) % 65536


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("STATIC_FILE_DIR", "./static-files"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=69)
    args = parser.parse_args()

    os.makedirs(args.root, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    print(f"{ts()} tftp listening on {args.host}:{args.port} root={args.root}", flush=True)

    while True:
        data, addr = sock.recvfrom(4096)
        rrq = parse_rrq(data)
        if not rrq:
            print(f"{ts()} non-rrq from={addr} bytes={len(data)} hex={data.hex()}", flush=True)
            continue

        filename, mode = rrq
        path = safe_path(args.root, filename)
        exists = bool(path and os.path.isfile(path))
        print(f"{ts()} rrq from={addr} filename={filename!r} mode={mode!r} exists={exists}", flush=True)
        if exists:
            child = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            child.bind((args.host, 0))
            serve_file(child, addr, path)
            child.close()
        else:
            send_error(sock, addr, 1, "not found")


if __name__ == "__main__":
    main()
