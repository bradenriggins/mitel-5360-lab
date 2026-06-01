#!/usr/bin/env python3
import selectors
import socket
import time


HOST = "0.0.0.0"
PORTS = [6800, 6801, 6802]


sel = selectors.DefaultSelector()


for port in PORTS:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.listen(5)
    sock.setblocking(False)
    sel.register(sock, selectors.EVENT_READ, ("listen", port))
    print(f"{time.strftime('%H:%M:%S')} listening on {port}", flush=True)


deadline = time.time() + 90
while time.time() < deadline:
    for key, _ in sel.select(timeout=1):
        kind = key.data[0]
        port = key.data[1]
        if kind == "listen":
            conn, addr = key.fileobj.accept()
            conn.setblocking(False)
            sel.register(conn, selectors.EVENT_READ, ("conn", port, addr))
            print(f"{time.strftime('%H:%M:%S')} accept port={port} from={addr}", flush=True)
        else:
            _, port, addr = key.data
            data = key.fileobj.recv(4096)
            if not data:
                print(f"{time.strftime('%H:%M:%S')} close port={port} from={addr}", flush=True)
                sel.unregister(key.fileobj)
                key.fileobj.close()
            else:
                print(f"{time.strftime('%H:%M:%S')} recv port={port} from={addr} bytes={len(data)} hex={data.hex()} ascii={data!r}", flush=True)

print(f"{time.strftime('%H:%M:%S')} done", flush=True)
