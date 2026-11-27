"""Bounded OpenAI-compatible SSE transport for validated JSON records on POSIX."""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
from ipaddress import IPv6Address, ip_address
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
from time import monotonic
from typing import Iterator
from urllib.parse import urlsplit

from .broker import read_token


_MAX_REQUEST = 128 * 1024
_MAX_RESPONSE = 1024 * 1024
_MAX_SSE_EVENT = 64 * 1024
_MAX_OUTPUT = 128 * 1024
_MAX_LINE = 32 * 1024
_MAX_TOKENS = 2048
FOLLOWUP_KINDS = ('evidence', 'limits', 'inference')
_JSON_WHITESPACE = b' \t\r\n'
_CHUNK = 8192
_SSE_BREAK = re.compile(rb'\r\n|[\r\n]')
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_PATH_PART = re.compile(r"[A-Za-z0-9._~-]+")
_WORKER_SCRIPT = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from knowledge_hub.model_stream import _worker; _worker(float(sys.argv[2]))"
)
_ERROR_MESSAGES = {
    "invalid_config": "串流模型設定或憑證無效。",
    "invalid_request": "串流模型輸入或期限設定超出限制。",
    "provider_unavailable": "模型供應端拒絕請求或暫時無法連線。",
    "invalid_response": "模型回應的串流格式無效，或內容超出限制。",
    "incomplete_output": "模型在完成可用輸出前結束串流。",
    "first_token_timeout": "模型未在首行期限內產生可交付的內容。",
    "total_timeout": "模型未在總期限內完成串流。",
}


class ModelStreamError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    token_file: str | os.PathLike[str] | None = field(default=None, repr=False)
    reasoning_effort: str | None = None
    structured: bool = False

    def __post_init__(self):
        try:
            url = _parse_base_url(self.base_url)
            if not _MODEL.fullmatch(self.model):
                raise ValueError
            if self.reasoning_effort not in (None, "none", "low", "medium", "high") or type(self.structured) is not bool:
                raise ValueError
            if self.token_file is not None:
                path = os.fspath(self.token_file)
                if not isinstance(path, str) or not 1 <= len(path) <= 4096 or "\x00" in path:
                    raise ValueError
                object.__setattr__(self, "token_file", path)
        except (TypeError, ValueError):
            raise ModelStreamError("invalid_config") from None
        object.__setattr__(self, "base_url", url.geturl().rstrip("/"))


def _parse_base_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(
        not 33 <= ord(ch) <= 126 for ch in value
    ):
        raise ValueError
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (parsed.scheme not in ("http", "https") or not host or "@" in parsed.netloc
            or "?" in value or "#" in value or "%" in parsed.netloc or "\\" in value
            or parsed.netloc.endswith(":") or parsed.port == 0):
        raise ValueError
    try:
        literal = ip_address(host)
    except ValueError:
        literal = None
    if literal is None and not _HOST.fullmatch(host):
        raise ValueError
    if parsed.scheme == "http":
        if literal is None or not literal.is_loopback:
            raise ValueError
        if isinstance(literal, IPv6Address) and literal.ipv4_mapped is not None:
            raise ValueError
    parts = parsed.path.removesuffix("/").split("/")[1:]
    if not parts or parts[-1] != "v1" or any(
        not _PATH_PART.fullmatch(part) or part in (".", "..") for part in parts
    ):
        raise ValueError
    return parsed


def _request_payload(config, messages, response_schema=None, stream_sections=False) -> bytes:
    if not isinstance(config, ModelConfig):
        raise ModelStreamError("invalid_config")
    if type(stream_sections) is not bool or stream_sections and response_schema is None:
        raise ModelStreamError("invalid_request")
    if response_schema is not None:
        if not config.structured:
            raise ModelStreamError("invalid_config")
        if not isinstance(response_schema, dict) or response_schema.get("type") != "object":
            raise ModelStreamError("invalid_request")
    if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= 32:
        raise ModelStreamError("invalid_request")
    for message in messages:
        if (not isinstance(message, dict) or message.keys() != {"role", "content"}
                or message["role"] not in ("system", "developer", "user", "assistant")
                or not isinstance(message["content"], str) or len(message["content"]) > _MAX_REQUEST):
            raise ModelStreamError("invalid_request")
    try:
        raw = json.dumps({
            "base_url": config.base_url,
            "model": config.model,
            "token_file": config.token_file,
            "reasoning_effort": config.reasoning_effort,
            "messages": messages,
            "response_schema": response_schema,
            "stream_sections": stream_sections,
        }, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise ModelStreamError("invalid_request") from None
    if len(raw) > _MAX_REQUEST:
        raise ModelStreamError("invalid_request")
    return raw


def stream_model(
    config: ModelConfig, messages: list[dict], *, first_timeout: float = 15, total_timeout: float = 45,
    timing: dict | None = None, response_schema: dict | None = None, stream_sections: bool = False,
    first_content_deadline: bool = False,
) -> Iterator[str]:
    """Yield JSON records, optionally decoding a structured document or L4 sections."""

    started = monotonic()
    if first_content_deadline and (response_schema is None or stream_sections):
        raise ModelStreamError("invalid_request")
    if any(type(value) not in (int, float) or not 0 < value <= limit
           for value, limit in ((first_timeout, 15), (total_timeout, 45))):
        raise ModelStreamError("invalid_request")
    if os.name != "posix":
        raise ModelStreamError("invalid_config")
    first_deadline = started + first_timeout
    deadline = started + total_timeout
    request = _request_payload(config, messages, response_schema, stream_sections)
    project = os.fspath(Path(__file__).resolve().parents[1])
    try:
        worker = subprocess.Popen(
            [sys.executable, "-IB", "-c", _WORKER_SCRIPT, project, str(deadline)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except (OSError, subprocess.SubprocessError):
        raise ModelStreamError("provider_unavailable") from None
    try:
        yield from _read_worker(worker, request, first_deadline, deadline, timing, started, first_content_deadline)
    finally:
        # A socket or resolver in the worker can still be blocked here.
        worker.kill()
        worker.wait()
        worker.stdin.close()
        worker.stdout.close()
        if timing is not None:
            timing['total_ms'] = round((monotonic() - started) * 1000, 2)


def _read_worker(worker, request, first_deadline, deadline, timing, started, first_content_deadline=False):
    pending = bytearray()
    input_bytes = memoryview(request)
    first_received = False

    def remaining():
        now = monotonic()
        if not first_received and first_deadline < deadline and now >= first_deadline:
            raise ModelStreamError("first_token_timeout")
        if now >= deadline:
            raise ModelStreamError("total_timeout")
        return (deadline if first_received else min(first_deadline, deadline)) - now

    with selectors.DefaultSelector() as poll:
        os.set_blocking(worker.stdin.fileno(), False)
        os.set_blocking(worker.stdout.fileno(), False)
        poll.register(worker.stdin, selectors.EVENT_WRITE)
        poll.register(worker.stdout, selectors.EVENT_READ)
        while True:
            for ready, _ in poll.select(remaining()):
                remaining()
                if ready.fileobj is worker.stdin:
                    try:
                        length = os.write(ready.fd, input_bytes[:_CHUNK])
                        input_bytes = input_bytes[length:]
                    except BrokenPipeError:
                        input_bytes = input_bytes[:0]
                    except BlockingIOError:
                        pass
                    if not input_bytes:
                        poll.unregister(worker.stdin)
                        worker.stdin.close()
                    continue
                try:
                    chunk = os.read(ready.fd, _CHUNK)
                except BlockingIOError:
                    continue
                if not chunk:
                    if worker.poll() == -signal.SIGALRM:
                        raise ModelStreamError("total_timeout")
                    remaining()
                    raise ModelStreamError("provider_unavailable")
                pending.extend(chunk)
                while (end := pending.find(b"\n")) >= 0:
                    remaining()
                    frame = bytes(pending[:end])
                    del pending[:end + 1]
                    if frame == b"D":
                        return
                    if frame == b"T":
                        if first_content_deadline:
                            first_received = True
                        if timing is not None:
                            timing.setdefault('ttft_ms', round((monotonic() - started) * 1000, 2))
                        continue
                    if frame.startswith(b"E"):
                        code = frame[1:].decode("ascii")
                        raise ModelStreamError(code if code in _ERROR_MESSAGES else "invalid_response")
                    if not frame.startswith(b"L") or len(frame) > _MAX_LINE + 1:
                        raise ModelStreamError("invalid_response")
                    first_received = True
                    if timing is not None:
                        timing.setdefault('first_line_ms', round((monotonic() - started) * 1000, 2))
                    yield frame[1:].decode("utf-8")
                if len(pending) > _MAX_LINE + 1:
                    raise ModelStreamError("invalid_response")


def _worker(deadline: float) -> None:
    # The default action of SIGALRM exits even if Python's consumer suspended its
    # generator or the worker is in libc DNS, TLS, a socket read, or a pipe write.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.setitimer(signal.ITIMER_REAL, max(deadline - monotonic(), 0.000001))

    def frame(prefix: bytes, text: str = ""):
        sys.stdout.buffer.write(prefix + text.encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()

    try:
        raw = sys.stdin.buffer.read(_MAX_REQUEST + 1)
        if len(raw) > _MAX_REQUEST:
            raise ModelStreamError("invalid_request")
        data = json.loads(raw)
        schema = data["response_schema"]
        config = ModelConfig(data["base_url"], data["model"], data["token_file"], data["reasoning_effort"],
                             structured=schema is not None)
        for line in _fetch_lines(config, data["messages"], lambda: frame(b"T"), schema, data["stream_sections"]):
            frame(b"L", line)
        frame(b"D")
    except ModelStreamError as error:
        frame(b"E", error.code)
    except (OSError, http.client.HTTPException):
        frame(b"E", "provider_unavailable")


def _fetch_lines(config: ModelConfig, messages, first_content, response_schema, stream_sections):
    try:
        token = read_token(config.token_file) if config.token_file is not None else None
    except ValueError:
        raise ModelStreamError("invalid_config") from None
    url = urlsplit(config.base_url)
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=None)
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    payload = {
        "messages": messages,
        "model": config.model,
        "stream": True,
        "max_tokens": _MAX_TOKENS,
    }
    if config.reasoning_effort is not None:
        payload['reasoning_effort'] = config.reasoning_effort
    if response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "knowledge_hub", "strict": True, "schema": response_schema},
        }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        connection.request("POST", url.path + "/chat/completions", body=body, headers=headers)
        with connection.getresponse() as response:
            if response.status != 200:
                raise ModelStreamError("provider_unavailable")
            if response.getheader("Content-Type", "").partition(";")[0].strip().lower() != "text/event-stream":
                raise ModelStreamError("invalid_response")
            try:
                yield from _completion_lines(response, first_content, structured=response_schema is not None,
                                             stream_sections=stream_sections)
            except (http.client.IncompleteRead, ConnectionResetError):
                raise ModelStreamError("incomplete_output") from None
    finally:
        connection.close()


def _sse_lines(response):
    pending = bytearray()
    total = 0
    skip_lf = False
    while chunk := response.read1(_CHUNK):
        total += len(chunk)
        if total > _MAX_RESPONSE:
            raise ModelStreamError("invalid_response")
        start = 1 if skip_lf and chunk.startswith(b"\n") else 0
        skip_lf = False
        for match in _SSE_BREAK.finditer(chunk, start):
            pending.extend(chunk[start:match.start()])
            if len(pending) > _MAX_SSE_EVENT:
                raise ModelStreamError("invalid_response")
            yield bytes(pending)
            pending.clear()
            start = match.end()
            skip_lf = match.end() == len(chunk) and match[0] == b"\r"
        pending.extend(chunk[start:])
        if len(pending) > _MAX_SSE_EVENT:
            raise ModelStreamError("invalid_response")


def _sse_events(response):
    data = []
    size = 0
    event_type = b""
    for index, line in enumerate(_sse_lines(response)):
        if index == 0:
            line = line.removeprefix(b"\xef\xbb\xbf")
        if not line:
            if data and event_type not in (b"ping", b"heartbeat"):
                event = b"\n".join(data)
                if event.strip():
                    yield event
            data.clear()
            size = 0
            event_type = b""
            continue
        key, _, value = line.partition(b":")
        value = value.removeprefix(b" ")
        if key == b"data":
            size += len(value) + 1
            if size > _MAX_SSE_EVENT:
                raise ModelStreamError("invalid_response")
            data.append(value)
        elif key == b"event":
            event_type = value


def _reject_constant(_):
    raise ValueError("Non-JSON number")


def _model_message(event: bytes) -> tuple[str, bool]:
    if event.strip() == b"[DONE]":
        return "", True
    try:
        value = json.loads(event.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError
        if "error" in value:
            raise ModelStreamError("provider_unavailable")
        choices = value["choices"]
        if choices == []:  # Optional usage chunk
            return "", False
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError
        index = choice.get("index", 0)
        if type(index) is not int or index != 0:
            raise ValueError
        finish = choice.get("finish_reason")
        if finish not in (None, "stop"):
            raise ModelStreamError("incomplete_output")
        delta = choice.get("delta")
        if delta is None:
            delta = {}
        if not isinstance(delta, dict):
            raise ValueError
        content = delta.get("content")
        if delta.get("refusal") or delta.get("tool_calls"):
            raise ModelStreamError("incomplete_output")
        if content is not None and not isinstance(content, str):
            raise ValueError
        return content or "", finish == "stop"
    except (ValueError, KeyError, RecursionError):
        raise ModelStreamError("invalid_response") from None


class _JSONRecords:
    """Accept a sequence of JSON objects with optional presentation whitespace."""
    def __init__(self):
        self.partial = bytearray()
        self.fence = ''
        self.emitted = False

    def line(self, line, *, final=False):
        line = line.strip()
        if not line:
            return None
        if line.startswith(b"```"):
            if self.partial:
                raise ModelStreamError("invalid_response")
            if self.fence == 'open' and self.emitted and line == b"```":
                self.fence = 'closed'
            elif not self.fence and not self.emitted and line.lower() in (b"```", b"```json", b"```jsonl"):
                self.fence = 'open'
            else:
                raise ModelStreamError("invalid_response")
            return None
        if self.fence == 'closed' or not self.partial and not line.startswith(b'{'):
            raise ModelStreamError("invalid_response")
        self.partial.extend(line)
        if len(self.partial) > _MAX_LINE:
            raise ModelStreamError("invalid_response")
        # The provider may pretty-print one complete object across several
        # lines. Keep the raw string (including duplicate keys) for callers.
        if not line.endswith(b'}') and not final:
            self.partial.extend(b'\n')
            return None
        raw = self.partial.decode('utf-8')
        try:
            value = json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            if not final and exc.pos == len(raw):
                self.partial.extend(b'\n')
                return None
            raise ModelStreamError("incomplete_output" if final else "invalid_response") from None
        except (RecursionError, ValueError):
            raise ModelStreamError("invalid_response") from None
        if not isinstance(value, dict):
            raise ModelStreamError("invalid_response")
        self.emitted = True
        self.partial.clear()
        return raw.replace('\n', ' ')


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return value


def _decode_structured(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ModelStreamError("invalid_response") from None


def _structured_line(value):
    if not isinstance(value, dict):
        raise ModelStreamError("invalid_response")
    try:
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(line.encode("utf-8"))
    except (ValueError, RecursionError):
        raise ModelStreamError("invalid_response") from None
    if size > _MAX_LINE:
        raise ModelStreamError("invalid_response")
    return line


class _StructuredSections:
    """Parse the one-key L4 envelope as it arrives, never yielding unfinished objects."""
    _MARKERS = {
        'object': (ord('{'), 'key'),
        'colon': (ord(':'), 'array'),
        'array': (ord('['), 'section'),
        'comma': (ord(','), 'section'),
        'array_end': (ord(']'), 'object_end'),
        'object_end': (ord('}'), 'done'),
    }

    def __init__(self):
        self.state = 'object'
        self.partial = bytearray()
        self.depth = 0
        self.in_string = self.escaped = False
        self.count = 0

    def _fragment_byte(self, byte):
        self.partial.append(byte)
        if len(self.partial) > _MAX_LINE:
            raise ModelStreamError("invalid_response")
        if self.in_string:
            if self.escaped:
                self.escaped = False
            elif byte == ord('\\'):
                self.escaped = True
            elif byte == ord('"'):
                self.in_string = False
        elif byte == ord('"'):
            self.in_string = True
        elif byte == ord('{'):
            self.depth += 1
        elif byte == ord('}'):
            self.depth -= 1
        if self.depth or self.in_string:
            return None

        value = _decode_structured(bytes(self.partial))
        self.partial.clear()
        if self.state == 'key':
            if value != 'sections':
                raise ModelStreamError("invalid_response")
            self.state = 'colon'
            return None
        if (not isinstance(value, dict) or set(value) != {'kind', 'text', 'ids'}
                or value['kind'] != FOLLOWUP_KINDS[self.count] or not isinstance(value['text'], str)
                or not isinstance(value['ids'], list) or any(not isinstance(key, str) for key in value['ids'])):
            raise ModelStreamError("invalid_response")
        self.count += 1
        self.state = 'array_end' if self.count == len(FOLLOWUP_KINDS) else 'comma'
        return _structured_line(value)

    def feed(self, chunk):
        for byte in chunk:
            if self.partial:
                if line := self._fragment_byte(byte):
                    yield line
            elif byte not in _JSON_WHITESPACE:
                if self.state == 'key' and byte == ord('"'):
                    self.partial.append(byte)
                    self.in_string = True
                elif self.state == 'section' and byte == ord('{'):
                    self.partial.append(byte)
                    self.depth = 1
                elif self.state in self._MARKERS and byte == self._MARKERS[self.state][0]:
                    self.state = self._MARKERS[self.state][1]
                else:
                    raise ModelStreamError("invalid_response")

    def finish(self):
        if self.state != 'done':
            raise ModelStreamError("incomplete_output")


def _completion_lines(response, first_content, *, structured=False, stream_sections=False):
    pending = bytearray()
    total = 0
    sections = _StructuredSections() if stream_sections else None
    records = _JSONRecords() if not structured else None
    for event in _sse_events(response):
        content, finished = _model_message(event)
        if content and content.strip() and first_content is not None:
            first_content()
            first_content = None
        try:
            chunk = content.encode("utf-8")
        except UnicodeError:
            raise ModelStreamError("invalid_response") from None
        total += len(chunk)
        if total > _MAX_OUTPUT:
            raise ModelStreamError("invalid_response")
        if sections is not None:
            yield from sections.feed(chunk)
            if finished:
                sections.finish()
                return
            continue
        pending.extend(chunk)
        if structured:
            if len(pending) > _MAX_LINE:
                raise ModelStreamError("invalid_response")
            if finished:
                if not pending.strip():
                    raise ModelStreamError("incomplete_output")
                yield _structured_line(_decode_structured(bytes(pending)))
                return
            continue
        while (end := pending.find(b"\n")) >= 0:
            line = bytes(pending[:end]).removesuffix(b"\r")
            del pending[:end + 1]
            if len(line) > _MAX_LINE:
                raise ModelStreamError("invalid_response")
            if record := records.line(line):
                yield record
        if len(pending) > _MAX_LINE:
            raise ModelStreamError("invalid_response")
        if finished:
            if record := records.line(bytes(pending), final=True):
                yield record
            if not records.emitted or records.partial or records.fence == 'open':
                raise ModelStreamError("incomplete_output")
            return
    raise ModelStreamError("incomplete_output")
