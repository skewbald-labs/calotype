"""A per-capture forward proxy: Chromium's only route to the network.

Each capture gets its own proxy listener on 127.0.0.1 and its browser
context is pointed at it. For every connection Chromium opens (page loads,
redirects, images, scripts, fetch/XHR, WebSockets, iframes, popups) the proxy:

- takes the host and port from the request (CONNECT for TLS and WebSockets
  over TLS, an absolute URI for plain HTTP),
- resolves the host itself and refuses it if any address is private,
  loopback, link-local, metadata or otherwise not public,
- connects to the exact address it checked, so a DNS answer cannot change
  between the check and the connection (DNS rebinding),
- counts the bytes coming back and cuts the session off at a byte cap.

Plain HTTP requests are forwarded with ``Connection: close`` so every request
on a connection goes to the host that was checked for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .ssrf import AddressPolicy, BlockedTarget, normalize_host, parse_ip_literal

log = logging.getLogger("calotype.egress")

MAX_HEADER_BYTES = 32 * 1024
CONNECT_TIMEOUT_S = 10.0
_HOP_BY_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer", "upgrade"}


@dataclass
class EgressSession:
    policy: AddressPolicy
    max_bytes: int
    bytes_in: int = 0
    exceeded: bool = False
    blocked_hosts: list[str] = field(default_factory=list)
    requests: int = 0
    server: asyncio.base_events.Server | None = None
    port: int = 0
    exceeded_event: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: set[asyncio.Task] = field(default_factory=set)
    _writers: set[asyncio.StreamWriter] = field(default_factory=set)

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> "EgressSession":
        self.server = await asyncio.start_server(self._on_client, host="127.0.0.1", port=0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
        for w in list(self._writers):
            with contextlib.suppress(Exception):
                w.close()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 2)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        try:
            peer = writer.get_extra_info("peername")
            if not peer or peer[0] not in ("127.0.0.1", "::1"):
                return
            await self._handle(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            log.exception("egress proxy error")
        finally:
            self._writers.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        if len(head) > MAX_HEADER_BYTES:
            await _respond(writer, 431, "Request Header Fields Too Large")
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            await _respond(writer, 400, "Bad Request")
            return
        headers = [ln for ln in lines[1:] if ln]
        self.requests += 1

        if self.exceeded:
            await _respond(writer, 403, "Forbidden")
            return

        if method.upper() == "CONNECT":
            host, port = _split_host_port(target)
            if host is None:
                await _respond(writer, 400, "Bad Request")
                return
            upstream = await self._open_checked(host, port)
            if upstream is None:
                await _respond(writer, 403, "Forbidden")
                return
            up_reader, up_writer = upstream
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer)
            return

        # Plain HTTP: the request line carries an absolute URI.
        try:
            parts = urlsplit(target)
            port = parts.port or 80
        except ValueError:
            await _respond(writer, 400, "Bad Request")
            return
        if parts.scheme.lower() != "http" or not parts.hostname:
            await _respond(writer, 400, "Bad Request")
            return
        upstream = await self._open_checked(parts.hostname, port)
        if upstream is None:
            await _respond(writer, 403, "Forbidden", "X-Calotype-Blocked: 1\r\n")
            return
        up_reader, up_writer = upstream
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        out = [f"{method} {path} {version}"]
        for h in headers:
            name = h.split(":", 1)[0].strip().lower()
            if name in _HOP_BY_HOP:
                continue
            out.append(h)
        out.append("Connection: close")
        up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
        await up_writer.drain()
        # Rewrite the response head so the browser closes this connection after
        # one response and never reuses it for a request to another host.
        try:
            resp_head = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT_S * 3)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            up_writer.close()
            return
        resp_lines = resp_head.decode("latin-1").split("\r\n")
        kept = [resp_lines[0]] + [
            ln for ln in resp_lines[1:] if ln and ln.split(":", 1)[0].strip().lower() not in ("connection", "keep-alive", "proxy-connection")
        ]
        kept += ["Connection: close", "Proxy-Connection: close"]
        new_head = ("\r\n".join(kept) + "\r\n\r\n").encode("latin-1")
        self.bytes_in += len(new_head)
        writer.write(new_head)
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _open_checked(self, host: str, port: int):
        try:
            host = normalize_host(host)
            if parse_ip_literal(host) is not None:
                host = str(parse_ip_literal(host))
            ips = await self.policy.resolve_checked(host, port)
        except BlockedTarget as e:
            self.blocked_hosts.append(host)
            log.info("egress refused", extra={"fields": {"host": host, "port": port, "reason": e.code}})
            return None
        last_err: Exception | None = None
        for ip in ips:
            try:
                return await asyncio.wait_for(asyncio.open_connection(str(ip), port), CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError) as e:
                last_err = e
        log.debug("egress connect failed: %s", type(last_err).__name__ if last_err else "none")
        return None

    async def _pipe(self, c_reader, c_writer, u_reader, u_writer) -> None:
        async def upstream_to_client():
            while True:
                chunk = await u_reader.read(65536)
                if not chunk:
                    break
                self.bytes_in += len(chunk)
                if self.bytes_in > self.max_bytes:
                    # Reset both sides so the browser sees an error, not a
                    # short response it may keep waiting on.
                    self.exceeded = True
                    self.exceeded_event.set()
                    for w in (c_writer, u_writer):
                        with contextlib.suppress(Exception):
                            w.transport.abort()
                    break
                c_writer.write(chunk)
                await c_writer.drain()

        async def client_to_upstream():
            while True:
                chunk = await c_reader.read(65536)
                if not chunk:
                    break
                u_writer.write(chunk)
                await u_writer.drain()

        self._writers.add(u_writer)
        down = asyncio.create_task(upstream_to_client())
        up = asyncio.create_task(client_to_upstream())
        try:
            await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (down, up):
                t.cancel()
            await asyncio.gather(down, up, return_exceptions=True)
            self._writers.discard(u_writer)
            with contextlib.suppress(Exception):
                u_writer.close()


def _split_host_port(target: str) -> tuple[str | None, int]:
    target = target.strip()
    if target.startswith("["):
        end = target.find("]")
        if end == -1:
            return None, 0
        host, rest = target[1:end], target[end + 1 :]
        port_s = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port_s = target.rpartition(":")
        if not host:
            return None, 0
    try:
        port = int(port_s)
    except ValueError:
        return None, 0
    return host, port


async def _respond(writer: asyncio.StreamWriter, status: int, reason: str, extra: str = "") -> None:
    writer.write(f"HTTP/1.1 {status} {reason}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
    with contextlib.suppress(Exception):
        await writer.drain()
