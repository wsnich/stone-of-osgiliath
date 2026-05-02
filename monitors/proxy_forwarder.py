"""
Local SOCKS5 proxy forwarder.

Chromium/Playwright cannot use SOCKS5 proxies that require username/password
authentication. This module starts a local SOCKS5 server on 127.0.0.1 that
accepts connections without authentication, then authenticates with the real
upstream SOCKS5 proxy on the browser's behalf.

Usage:
    port = await start_local_proxy("socks5://user:pass@host:9595")
    # Point Playwright at socks5://127.0.0.1:port  (no auth needed)
"""

import asyncio
import logging
import struct
from urllib.parse import urlparse

log = logging.getLogger("proxy_fwd")

_servers: dict[str, tuple] = {}   # upstream_url -> (server, port)


async def start_local_proxy(upstream_url: str) -> int:
    """Start (or reuse) a local forwarder for the given upstream proxy URL.
    Returns the local port number.
    """
    if upstream_url in _servers:
        server, port = _servers[upstream_url]
        if not server.is_serving():
            del _servers[upstream_url]
        else:
            return port

    p = urlparse(upstream_url)
    up_host = p.hostname
    up_port = p.port
    username = p.username or ""
    password = p.password or ""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await _relay(reader, writer, up_host, up_port, username, password)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    asyncio.create_task(server.serve_forever())
    _servers[upstream_url] = (server, port)
    log.info("Local SOCKS5 forwarder started on 127.0.0.1:%d → %s:%d", port, up_host, up_port)
    return port


async def _relay(cr: asyncio.StreamReader, cw: asyncio.StreamWriter,
                 up_host: str, up_port: int, username: str, password: str):
    """Handle one client connection: complete SOCKS5 with client (no-auth),
    connect to upstream with auth, forward the client's CONNECT request, then relay."""

    # ── 1. Greet client — offer no-auth (0x00) ──────────────────────────────
    hdr = await cr.readexactly(2)        # VER, NMETHODS
    if hdr[0] != 5:
        return
    methods = await cr.readexactly(hdr[1])
    # Accept no-auth (0x00) always — no need to challenge the local client
    cw.write(b"\x05\x00")
    await cw.drain()

    # ── 2. Read client CONNECT request ───────────────────────────────────────
    req = await cr.readexactly(4)        # VER, CMD, RSV, ATYP
    if req[0] != 5 or req[1] != 1:      # only CONNECT (0x01) supported
        cw.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
        return

    atyp = req[3]
    if atyp == 1:                        # IPv4
        addr_bytes = await cr.readexactly(4)
        import socket
        target_host = socket.inet_ntoa(addr_bytes)
    elif atyp == 3:                      # Domain
        length = (await cr.readexactly(1))[0]
        addr_bytes = await cr.readexactly(length)
        target_host = addr_bytes.decode()
    elif atyp == 4:                      # IPv6
        addr_bytes = await cr.readexactly(16)
        import socket
        target_host = socket.inet_ntop(socket.AF_INET6, addr_bytes)
    else:
        return

    port_bytes = await cr.readexactly(2)
    target_port = struct.unpack("!H", port_bytes)[0]

    # ── 3. Connect to upstream SOCKS5 with authentication ───────────────────
    ur, uw = await asyncio.open_connection(up_host, up_port)

    # Authenticate
    if username:
        uw.write(b"\x05\x01\x02")       # greeting: support user/pass auth
        await uw.drain()
        resp = await ur.readexactly(2)
        if resp != b"\x05\x02":
            cw.write(b"\x05\x01\x00\x01" + b"\x00" * 6)
            return
        auth = (b"\x01" +
                bytes([len(username)]) + username.encode() +
                bytes([len(password)]) + password.encode())
        uw.write(auth)
        await uw.drain()
        auth_resp = await ur.readexactly(2)
        if auth_resp[1] != 0:
            cw.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return
    else:
        uw.write(b"\x05\x01\x00")       # no-auth
        await uw.drain()
        await ur.readexactly(2)

    # Forward the CONNECT request to upstream
    if atyp == 3:
        fwd = (b"\x05\x01\x00\x03" +
               bytes([len(target_host)]) + target_host.encode() +
               port_bytes)
    elif atyp == 1:
        fwd = b"\x05\x01\x00\x01" + addr_bytes + port_bytes
    else:
        fwd = b"\x05\x01\x00\x04" + addr_bytes + port_bytes

    uw.write(fwd)
    await uw.drain()

    # Read upstream response
    up_resp = await ur.readexactly(4)
    if up_resp[3] == 1:
        await ur.readexactly(4 + 2)     # IPv4 + port
    elif up_resp[3] == 3:
        n = (await ur.readexactly(1))[0]
        await ur.readexactly(n + 2)
    elif up_resp[3] == 4:
        await ur.readexactly(16 + 2)

    if up_resp[1] != 0:
        # Forward failure to client
        cw.write(b"\x05" + bytes([up_resp[1]]) + b"\x00\x01" + b"\x00" * 6)
        return

    # ── 4. Tell client success ───────────────────────────────────────────────
    cw.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    await cw.drain()

    # ── 5. Relay data bidirectionally ────────────────────────────────────────
    async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
