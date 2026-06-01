#!/usr/bin/env python3
import selectors
import socket
import ssl
import time


HOST = "0.0.0.0"
PORTS = [6800, 6801, 6802]
CERT = "/tmp/mitel_tls_probe.crt"
KEY = "/tmp/mitel_tls_probe.key"


def ts():
    return time.strftime("%H:%M:%S")


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.minimum_version = ssl.TLSVersion.TLSv1
ctx.maximum_version = ssl.TLSVersion.TLSv1_2
ctx.load_cert_chain(CERT, KEY)
ctx.set_ciphers("AES256-GCM-SHA384:AES256-SHA256:AES256-SHA:AES128-SHA:@SECLEVEL=1")

sel = selectors.DefaultSelector()

for port in PORTS:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.listen(16)
    sock.setblocking(False)
    sel.register(sock, selectors.EVENT_READ, {"kind": "listen", "port": port})
    print(f"{ts()} listening tls on {port}", flush=True)

deadline = time.time() + 240
while time.time() < deadline:
    for key, mask in sel.select(timeout=1):
        meta = key.data
        if meta["kind"] == "listen":
            conn, addr = key.fileobj.accept()
            conn.setblocking(False)
            port = meta["port"]
            try:
                tls_conn = ctx.wrap_socket(conn, server_side=True, do_handshake_on_connect=False)
            except Exception as exc:
                print(f"{ts()} wrap-error port={port} from={addr} {exc!r}", flush=True)
                conn.close()
                continue
            sel.register(
                tls_conn,
                selectors.EVENT_READ | selectors.EVENT_WRITE,
                {"kind": "handshake", "port": port, "addr": addr},
            )
            print(f"{ts()} accept port={port} from={addr}", flush=True)
            continue

        conn = key.fileobj
        port = meta["port"]
        addr = meta["addr"]
        try:
            if meta["kind"] == "handshake":
                conn.do_handshake()
                meta["kind"] = "data"
                sel.modify(conn, selectors.EVENT_READ, meta)
                print(f"{ts()} tls-ok port={port} from={addr} cipher={conn.cipher()}", flush=True)
            else:
                data = conn.recv(4096)
                if not data:
                    print(f"{ts()} close port={port} from={addr}", flush=True)
                    sel.unregister(conn)
                    conn.close()
                else:
                    print(
                        f"{ts()} data port={port} from={addr} bytes={len(data)} "
                        f"hex={data.hex()} ascii={data!r}",
                        flush=True,
                    )
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass
        except Exception as exc:
            print(f"{ts()} error port={port} from={addr} {exc!r}", flush=True)
            try:
                sel.unregister(conn)
            except Exception:
                pass
            conn.close()

print(f"{ts()} done", flush=True)
