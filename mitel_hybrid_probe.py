#!/usr/bin/env python3
import selectors
import socket
import ssl
import time


HOST = "0.0.0.0"
RAW_PORTS = [6800, 6802]
TLS_PORTS = [6801]
CERT = "/tmp/mitel_tls_probe.crt"
KEY = "/tmp/mitel_tls_probe.key"


def ts():
    return time.strftime("%H:%M:%S")


def close(sel, conn):
    try:
        sel.unregister(conn)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls_ctx.minimum_version = ssl.TLSVersion.TLSv1
tls_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
tls_ctx.load_cert_chain(CERT, KEY)
tls_ctx.set_ciphers("AES256-GCM-SHA384:AES256-SHA256:AES256-SHA:AES128-SHA:@SECLEVEL=1")

sel = selectors.DefaultSelector()

for port in RAW_PORTS + TLS_PORTS:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.listen(16)
    sock.setblocking(False)
    mode = "tls" if port in TLS_PORTS else "raw"
    sel.register(sock, selectors.EVENT_READ, {"kind": "listen", "port": port, "mode": mode})
    print(f"{ts()} listening {mode} on {port}", flush=True)

deadline = time.time() + 240
while time.time() < deadline:
    for key, _ in sel.select(timeout=1):
        meta = key.data
        if meta["kind"] == "listen":
            conn, addr = key.fileobj.accept()
            conn.setblocking(False)
            port = meta["port"]
            mode = meta["mode"]
            if mode == "tls":
                try:
                    conn = tls_ctx.wrap_socket(conn, server_side=True, do_handshake_on_connect=False)
                except Exception as exc:
                    print(f"{ts()} tls-wrap-error port={port} from={addr} {exc!r}", flush=True)
                    close(sel, conn)
                    continue
                sel.register(conn, selectors.EVENT_READ | selectors.EVENT_WRITE, {
                    "kind": "tls-handshake",
                    "port": port,
                    "addr": addr,
                })
            else:
                sel.register(conn, selectors.EVENT_READ, {
                    "kind": "raw-data",
                    "port": port,
                    "addr": addr,
                })
            print(f"{ts()} accept {mode} port={port} from={addr}", flush=True)
            continue

        conn = key.fileobj
        port = meta["port"]
        addr = meta["addr"]
        try:
            if meta["kind"] == "tls-handshake":
                conn.do_handshake()
                meta["kind"] = "tls-data"
                sel.modify(conn, selectors.EVENT_READ, meta)
                print(f"{ts()} tls-ok port={port} from={addr} cipher={conn.cipher()}", flush=True)
                continue

            data = conn.recv(4096)
            if not data:
                print(f"{ts()} close port={port} from={addr}", flush=True)
                close(sel, conn)
                continue

            print(
                f"{ts()} {meta['kind']} port={port} from={addr} bytes={len(data)} "
                f"hex={data.hex()} ascii={data!r}",
                flush=True,
            )
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass
        except Exception as exc:
            print(f"{ts()} error port={port} from={addr} {exc!r}", flush=True)
            close(sel, conn)

print(f"{ts()} done", flush=True)
