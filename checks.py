"""File and website adapters. Test checks are observed elsewhere, never executed here."""

from __future__ import annotations

import hashlib
import errno
import http.client
import ipaddress
import json
import os
import socket
import ssl
import stat
import time
import queue
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .schema import FILE_READ_CAP, WEBSITE_DEADLINE_SECONDS, sha256_bytes

Fetch = Callable[[str, float], dict[str, Any]]


def check_file(check: dict[str, Any], workspace_root: str) -> dict[str, Any]:
    rel = check["path"]
    try:
        data = _read_regular(workspace_root, rel)
    except _FileRefused as exc:
        return {"state": exc.state, "reason": str(exc), "evidence": {"relative_path": rel}, "provenance": "file-read"}
    except FileNotFoundError:
        return {"state": "failed", "reason": "missing file", "evidence": {"relative_path": rel}, "provenance": "file-read"}
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return {"state": "failed", "reason": "symlink or non-directory path rejected", "evidence": {"relative_path": rel}, "provenance": "file-read"}
        return _unverified(f"read failed: {type(exc).__name__}", rel)
    digest = sha256_bytes(data)
    evidence = {"relative_path": rel, "size": len(data), "sha256": digest}
    if "json_keys" in check:
        return _json_keys(data, check["json_keys"], evidence)
    needle = check["contains"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "state": "failed",
            "reason": "file is not valid UTF-8 text",
            "evidence": evidence,
            "provenance": "file-read",
        }
    if needle in text:
        return {"state": "passed", "reason": "required text present", "evidence": evidence, "provenance": "file-read"}
    return {"state": "failed", "reason": "required text absent", "evidence": evidence, "provenance": "file-read"}


class _FileRefused(ValueError):
    def __init__(self, message: str, state: str = "failed") -> None:
        super().__init__(message)
        self.state = state


def _read_regular(workspace_root: str, rel: str) -> bytes:
    """Descriptor-relative traversal: no symlink race, FIFO wait, or unbounded read."""
    parts = rel.split("/")
    if not Path(workspace_root).is_absolute() or any(p in {"", ".", ".."} for p in parts) or "\\" in rel:
        raise _FileRefused("path traversal rejected")
    descriptors = []
    try:
        directory = os.open(workspace_root, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _FileRefused("rejected non-regular file")
        if info.st_size > FILE_READ_CAP:
            raise _FileRefused("file exceeds 1 MiB read cap", "unverified")
        blocks, count = [], 0
        while count <= FILE_READ_CAP:
            block = os.read(descriptor, min(65536, FILE_READ_CAP + 1 - count))
            if not block:
                break
            blocks.append(block)
            count += len(block)
        if count > FILE_READ_CAP:
            raise _FileRefused("file exceeds 1 MiB read cap", "unverified")
        return b"".join(blocks)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _json_keys(data: bytes, keys: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "state": "failed",
            "reason": "file is not parseable JSON",
            "evidence": evidence,
            "provenance": "file-read",
        }
    except (RecursionError, ValueError, OverflowError, MemoryError) as exc:
        # Bounded bytes can still exceed parser depth/integer/resource limits.
        # These are checker limitations, not evidence of malformed JSON.
        return {
            "state": "unverified",
            "reason": f"JSON parser limit: {type(exc).__name__}",
            "evidence": evidence,
            "provenance": "file-read",
        }
    if not isinstance(parsed, dict):
        return {
            "state": "failed",
            "reason": "JSON value is not an object",
            "evidence": evidence,
            "provenance": "file-read",
        }
    missing = [key for key in keys if key not in parsed]
    if missing:
        return {
            "state": "failed",
            "reason": "missing JSON keys",
            "evidence": {**evidence, "missing_keys": missing},
            "provenance": "file-read",
        }
    return {
        "state": "passed",
        "reason": "required JSON keys present",
        "evidence": evidence,
        "provenance": "file-read",
    }


def _unverified(reason: str, rel: str, size: int | None = None) -> dict[str, Any]:
    evidence: dict[str, Any] = {"relative_path": rel}
    if size is not None:
        evidence["size"] = size
    return {"state": "unverified", "reason": reason, "evidence": evidence, "provenance": "file-read"}


def check_website(
    check: dict[str, Any],
    *,
    allow_loopback_http: bool,
    fetch: Fetch | None = None,
) -> dict[str, Any]:
    url = check["url"]
    expected = check["expected_status"]
    needle = check["contains"]
    refusal = url_refusal(url, allow_loopback_http=allow_loopback_http)
    sanitized = sanitize_url(url)
    if refusal:
        return {
            "state": "unverified",
            "reason": refusal,
            "evidence": {"url": sanitized, "content_expectation": needle},
            "provenance": "website-get",
        }
    getter = fetch or (lambda target, deadline: fetch_url(target, deadline, allow_loopback_http=allow_loopback_http))
    started = _utc()
    try:
        result = getter(url, WEBSITE_DEADLINE_SECONDS)
    except Exception as exc:
        return {
            "state": "unverified",
            "reason": f"request error: {type(exc).__name__}",
            "evidence": {"url": sanitized, "content_expectation": needle, "started_at": started, "checked_at": _utc()},
            "provenance": "website-get",
        }
    return _judge_http(result, expected, needle, sanitized, started)


def _judge_http(result: dict[str, Any], expected: int, needle: str, sanitized: str, started: str) -> dict[str, Any]:
    evidence = {
        "url": sanitized,
        "content_expectation": needle,
        "started_at": started,
        "checked_at": _utc(),
    }
    if result.get("error"):
        evidence["error"] = str(result["error"])[:240]
        return {"state": "unverified", "reason": str(result["error"]), "evidence": evidence, "provenance": "website-get"}
    status = result.get("status")
    size = result.get("size")
    digest = result.get("sha256")
    if isinstance(status, int):
        evidence["status"] = status
    if isinstance(size, int):
        evidence["size"] = size
    if isinstance(digest, str):
        evidence["sha256"] = digest
    if result.get("oversized"):
        return {"state": "unverified", "reason": "response exceeds 1 MiB cap", "evidence": evidence, "provenance": "website-get"}
    if not isinstance(status, int):
        return {"state": "unverified", "reason": "no HTTP status", "evidence": evidence, "provenance": "website-get"}
    if status != expected:
        return {
            "state": "failed",
            "reason": f"status {status} != expected {expected}",
            "evidence": evidence,
            "provenance": "website-get",
        }
    body = result.get("body")
    if not isinstance(body, (bytes, bytearray)):
        return {"state": "unverified", "reason": "response body unavailable", "evidence": evidence, "provenance": "website-get"}
    text = bytes(body).decode("utf-8", errors="replace")
    if needle in text:
        return {"state": "passed", "reason": "status and content matched", "evidence": evidence, "provenance": "website-get"}
    return {"state": "failed", "reason": "status matched but required content absent", "evidence": evidence, "provenance": "website-get"}


def sanitize_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        host = f"[{host}]" if ":" in host else host
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "<unparseable-url>"
    path = parts.path or "/"
    return f"{parts.scheme}://{host}{port}{path}"


def url_refusal(url: str, *, allow_loopback_http: bool) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "unparseable url"
    if any(c in url for c in "\r\n\t\\"):
        return "unsupported url characters"
    if parts.scheme not in {"http", "https"}:
        return "unsupported scheme"
    if parts.username or parts.password:
        return "url userinfo rejected"
    if parts.query:
        return "query string rejected"
    host = parts.hostname
    if not host:
        return "missing host"
    if not _host_shape_ok(host):
        return "unsupported host"
    if port == 0:
        return "invalid port"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if parts.scheme != "https" and not (allow_loopback_http and host == "localhost"):
            return "public targets must use HTTPS"
    else:
        return _address_refusal(literal, scheme=parts.scheme, allow_loopback_http=allow_loopback_http)
    return None


def _host_shape_ok(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(host) and all(part and len(part) <= 63 for part in host.split(".")) and " " not in host


def _address_refusal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, scheme: str, allow_loopback_http: bool) -> str | None:
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "disallowed destination"
    if getattr(ip, "is_site_local", False):
        return "private destination"
    text = str(ip)
    if text in {"169.254.169.254", "fd00:ec2::254"} or ip.is_link_local:
        return "link-local or metadata destination"
    if ip.is_loopback:
        if allow_loopback_http and scheme == "http":
            return None
        return "loopback requires explicit local-demo opt-in over HTTP"
    if ip.is_private:
        return "private destination"
    if scheme != "https":
        return "public targets must use HTTPS"
    return None


def fetch_url(url: str, deadline: float, *, allow_loopback_http: bool) -> dict[str, Any]:
    """GET one URL with redirects disabled, a hard deadline, and a pinned resolved address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    end_at = time.monotonic() + deadline
    infos = _resolve_addresses(host, port, deadline)
    if not infos:
        return {"error": "DNS timeout or resolution failed"}
    for info in infos:
        if _address_refusal(ipaddress.ip_address(info[4][0]), scheme=parts.scheme, allow_loopback_http=allow_loopback_http):
            return {"error": "resolved address refused"}
    ip = str(infos[0][4][0])
    parsed_ip = ipaddress.ip_address(ip)
    if _address_refusal(parsed_ip, scheme=parts.scheme, allow_loopback_http=allow_loopback_http):
        return {"error": "resolved address refused"}
    path = parts.path or "/"
    remaining = end_at - time.monotonic()
    if remaining <= 0:
        return {"error": "timeout"}
    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        context = ssl.create_default_context()
        conn = _PinnedHTTPSConnection(host, port, ip, timeout=remaining, context=context)
    else:
        conn = _PinnedHTTPConnection(host, port, ip, timeout=remaining)
    conn.response_class = _FramedHTTPResponse
    conn._end_at = end_at
    def abort_at_deadline():
        sock = getattr(conn, "_deadline_socket", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
    watchdog = threading.Timer(max(0.001, end_at - time.monotonic()), abort_at_deadline)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.request("GET", path, headers={
            "Host": parts.netloc,
            "User-Agent": "hermes-receipts/0.1.0",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        conn._deadline_socket.settimeout(max(0.001, end_at - time.monotonic()))
        response = conn.getresponse()
        status = response.status
        encoding = (response.getheader("Content-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            return {"status": status, "error": "compressed response refused"}
        lengths = response.headers.get_all("Content-Length", [])
        transfers = response.headers.get_all("Transfer-Encoding", [])
        if transfers and (lengths or len(transfers) != 1 or transfers[0].strip().lower() != "chunked"):
            return {"status": status, "error": "ambiguous or unsupported response framing"}
        if lengths:
            declared = lengths[0].strip()
            if len(lengths) != 1 or not declared.isascii() or not declared.isdecimal() or len(declared) > 16:
                return {"status": status, "error": "invalid content length"}
            if int(declared) > FILE_READ_CAP:
                return {"status": status, "oversized": True, "size": int(declared)}
        chunks = []
        total = 0
        while True:
            if time.monotonic() >= end_at:
                return {"error": "timeout", "status": status}
            if response.isclosed():
                break
            conn._deadline_socket.settimeout(max(0.001, end_at - time.monotonic()))
            block = response.read1(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > FILE_READ_CAP:
                return {"status": status, "oversized": True, "size": total}
            chunks.append(block)
        # read1() accepts early EOF for declared-length responses. A watchdog
        # shutdown can also look like ordinary close-delimited EOF. Neither is
        # a complete observation merely because its prefix matched the text.
        if time.monotonic() >= end_at:
            return {"error": "timeout", "status": status}
        if response.length not in (None, 0):
            return {"error": "incomplete response body", "status": status}
        body = b"".join(chunks)
        return {"status": status, "body": body, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    except socket.timeout:
        return {"error": "timeout"}
    except http.client.HTTPException:
        return {"error": "incomplete or invalid HTTP framing"}
    except OSError as exc:
        if time.monotonic() >= end_at:
            return {"error": "timeout"}
        return {"error": f"network error: {type(exc).__name__}"}
    finally:
        watchdog.cancel()
        conn.close()


class _FramedHTTPResponse(http.client.HTTPResponse):
    """Strict chunk framing; stdlib's normal client tolerates EOF in trailers.

    This is a per-connection response subclass, not a global patch. Body reads
    still use the standard bounded read1 path and the connection watchdog.
    """

    def _read_next_chunk_size(self):
        line = self.fp.readline(8193)
        size = line.split(b";", 1)[0].removesuffix(b"\r\n")
        if (len(line) > 8192 or not line.endswith(b"\r\n") or not size
                or len(size) > 16 or any(c not in b"0123456789abcdefABCDEF" for c in size)):
            raise http.client.IncompleteRead(b"")
        return int(size, 16)

    def _get_chunk_left(self):
        chunk_left = self.chunk_left
        if not chunk_left:
            if chunk_left is not None and self._safe_read(2) != b"\r\n":
                raise http.client.IncompleteRead(b"")
            chunk_left = self._read_next_chunk_size()
            if chunk_left == 0:
                self._read_and_discard_trailer()
                self._close_conn()
                chunk_left = None
            self.chunk_left = chunk_left
        return chunk_left

    def _read_and_discard_trailer(self):
        total = 0
        while True:
            line = self.fp.readline(8193)
            total += len(line)
            if not line.endswith(b"\r\n") or len(line) > 8192 or total > 65536:
                raise http.client.IncompleteRead(b"")
            if line == b"\r\n":
                return
            if b":" not in line:
                raise http.client.IncompleteRead(b"")


def _resolve_addresses(host: str, port: int, timeout: float):
    """Bound caller DNS waiting; a slow OS resolver cannot stall the checker."""
    results = queue.Queue(maxsize=1)
    def resolve():
        try:
            results.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            results.put([])
    threading.Thread(target=resolve, daemon=True, name="receipt-dns").start()
    try:
        return results.get(timeout=max(0.001, timeout))
    except queue.Empty:
        return []


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self._deadline_socket = self.sock


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip
        self._pinned_context = context

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        raw.settimeout(max(0.001, self._end_at - time.monotonic()))
        self.sock = self._pinned_context.wrap_socket(raw, server_hostname=self.host)
        self._deadline_socket = self.sock


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
