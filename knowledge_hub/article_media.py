"""Stream the two article videos after the caller has authorized the request."""

import os
import re
import stat
from pathlib import Path


_FILENAMES = frozenset({"original.mp4", "guided.mp4"})
_CHUNK_SIZE = 64 * 1024
_PRIVATE_HEADERS = [
    ("Cache-Control", "no-store"),
    ("Vary", "Cookie"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
]
_BYTE_RANGE = re.compile(r"([0-9]*)-([0-9]*)")


class _FileBody:
    def __init__(self, media, size):
        self.media = media
        self.remaining = size

    def __iter__(self):
        return self

    def __next__(self):
        if not self.remaining:
            self.close()
            raise StopIteration
        try:
            chunk = self.media.read(min(self.remaining, _CHUNK_SIZE))
        except BaseException:
            self.close()
            raise
        if not chunk:
            self.close()
            raise OSError("Article video ended before the advertised length")
        self.remaining -= len(chunk)
        return chunk

    def close(self):
        self.remaining = 0
        self.media.close()


def _open_readonly(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)


def _bounded_int(digits, size):
    # Larger values have the same effect as size for each range calculation.
    digits = digits.lstrip("0")
    if len(digits) > len(str(size)):
        return size
    return min(int(digits or "0"), size)


def _byte_interval(spec, size):
    match = _BYTE_RANGE.fullmatch(spec)
    if match is None or size == 0:
        return None
    first, last = match.groups()
    if first:
        start = _bounded_int(first, size)
        stop = min(_bounded_int(last, size) + 1, size) if last else size
        return (start, stop) if start < stop else None
    suffix_length = _bounded_int(last, size)
    return (size - suffix_length, size) if suffix_length else None


def _empty(start_response, status, headers=()):
    start_response(status, [("Content-Type", "text/plain; charset=utf-8"),
                            ("Content-Length", "0"), *_PRIVATE_HEADERS, *headers])
    return ()


def serve(environ, start_response, media_root, filename):
    """Serve a fixed filename from the operator-owned media directory (POSIX)."""
    if filename not in _FILENAMES:
        return _empty(start_response, "404 Not Found")
    method = environ.get("REQUEST_METHOD", "GET")
    if method not in ("GET", "HEAD"):
        return _empty(start_response, "405 Method Not Allowed", [("Allow", "GET, HEAD")])
    try:
        media = open(Path(media_root) / filename, "rb", buffering=0, opener=_open_readonly)
    except OSError:
        return _empty(start_response, "404 Not Found")

    stream = None
    try:
        info = os.fstat(media.fileno())
        if not stat.S_ISREG(info.st_mode):
            return _empty(start_response, "404 Not Found")

        size = info.st_size
        status = "200 OK"
        start, stop = 0, size
        headers = [("Content-Type", "video/mp4"), ("Accept-Ranges", "bytes"), *_PRIVATE_HEADERS]
        range_unit, separator, spec = environ.get("HTTP_RANGE", "").strip().partition("=")
        if (method == "GET" and "HTTP_IF_RANGE" not in environ
                and range_unit.lower() == "bytes"):
            interval = _byte_interval(spec, size) if separator else None
            if interval is None:
                start_response("416 Range Not Satisfiable", headers + [
                    ("Content-Range", f"bytes */{size}"), ("Content-Length", "0"),
                ])
                return ()
            start, stop = interval
            status = "206 Partial Content"
            headers.append(("Content-Range", f"bytes {start}-{stop - 1}/{size}"))

        length = stop - start
        if method == "GET" and length:
            media.seek(start)
        start_response(status, headers + [("Content-Length", str(length))])
        if method == "GET" and length:
            stream = _FileBody(media, length)
            return stream
        return ()
    finally:
        if stream is None:
            media.close()
