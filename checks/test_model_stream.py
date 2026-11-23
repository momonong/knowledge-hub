import json
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from knowledge_hub import model_stream


FIXTURE_SCHEMA = {'type': 'object', 'properties': {'sections': {'type': 'array'}},
                  'required': ['sections'], 'additionalProperties': False}
FIXTURE_SECTIONS = [
    {'kind': 'evidence', 'text': '原文提到「段落」以及一個測試用的 } 字元。', 'ids': ['S1']},
    {'kind': 'limits', 'text': '原文未交代', 'ids': []},
    {'kind': 'inference', 'text': '原文未交代', 'ids': []},
]


def delta(content=None, *, finish=None, role=None):
    fields = {} if content is None else {"content": content}
    if role:
        fields["role"] = role
    return sse({"choices": [{"index": 0, "delta": fields, "finish_reason": finish}]})


def sse(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


def header(handler, content_type="text/event-stream; charset=utf-8", *, chunked=False):
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    if chunked:
        handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()


def write(handler, value, *, chunked=False):
    if chunked:
        handler.wfile.write(f"{len(value):x}\r\n".encode() + value + b"\r\n")
    else:
        handler.wfile.write(value)
    handler.wfile.flush()


class StreamTests(unittest.TestCase):
    def test_structured_abstention_has_no_ids_and_only_the_checked_missing_text(self):
        from knowledge_hub.followup import _response_schema
        items = _response_schema(['S1','S2'])['properties']['sections']['items']
        missing, supported = (choice['properties'] for choice in items['anyOf'])
        self.assertEqual(missing['ids']['maxItems'], 0)
        self.assertEqual(missing['text']['enum'], ['原文未交代'])
        self.assertEqual(supported['ids']['minItems'], 1)
        self.assertEqual(supported['ids']['items']['enum'], ['S1','S2'])
        self.assertEqual(_response_schema([])['properties']['sections']['items']['properties'], missing)

    def test_config_rejects_insecure_or_ambiguous_model_destinations(self):
        for url in (
            "http://example.invalid/v1",
            "http://127.1/v1",
            "https://user:secret@example.invalid/v1",
            "https://example.invalid/v1?secret=an-example",
            "https://example.invalid/v1#fragment",
            "https://example.invalid/v1/../v1",
            "https://example.invalid/v1\r\nX-Injection:test",
            "http://[::ffff:127.0.0.1]/v1",
        ):
            with self.subTest(url=url), self.assertRaises(model_stream.ModelStreamError) as failure:
                model_stream.ModelConfig(url, "fixture")
            self.assertEqual(failure.exception.code, "invalid_config")

    def test_structured_flags_are_independent_and_environment_can_be_overridden(self):
        from knowledge_hub.__main__ import build_parser

        with patch.dict('os.environ', {'KH_PRIMARY_STRUCTURED': 'true', 'KH_FALLBACK_STRUCTURED': '0'}):
            parser = build_parser()
            config = parser.parse_args(['--db', '/tmp/fixture.db'])
            self.assertIs(config.primary_structured, True)
            self.assertIs(config.fallback_structured, False)
            overridden = parser.parse_args(['--db', '/tmp/fixture.db', '--no-primary-structured', '--fallback-structured'])
            self.assertIs(overridden.primary_structured, False)
            self.assertIs(overridden.fallback_structured, True)

    def test_structured_parser_yields_first_section_without_reading_rest(self):
        first = json.dumps(FIXTURE_SECTIONS[0], ensure_ascii=True, indent=2)
        rest = ','.join(json.dumps(row, ensure_ascii=False) for row in FIXTURE_SECTIONS[1:])
        permitted = False

        def chunks():
            yield delta(r'{ "\u0073ec')
            yield delta('tions" : [ ' + first[:23])
            yield delta(first[23:])
            self.assertTrue(permitted, 'Later upstream frames were read before yielding the first section')
            yield delta(',' + rest + '] }')
            yield delta(finish='stop')

        source = iter(chunks())
        class Response:
            def read1(self, _size):
                return next(source, b'')

        rows = model_stream._completion_lines(Response(), None, structured=True, stream_sections=True)
        self.assertEqual(json.loads(next(rows)), FIXTURE_SECTIONS[0])
        permitted = True
        self.assertEqual([json.loads(row) for row in rows], FIXTURE_SECTIONS[1:])

    def test_schema_requires_the_route_capability(self):
        config = model_stream.ModelConfig("http://127.0.0.1:9/v1", "fixture")
        with self.assertRaises(model_stream.ModelStreamError) as failure:
            list(model_stream.stream_model(config, [{"role": "user", "content": "fixture"}],
                                           response_schema=FIXTURE_SCHEMA))
        self.assertEqual(failure.exception.code, "invalid_config")

    def test_deadline_covers_a_blocked_dns_lookup(self):
        config = model_stream.ModelConfig("http://127.0.0.1:9/v1", "fixture")
        block_dns = (
            "import socket, time; "
            "socket.getaddrinfo = lambda *a, **kw: time.sleep(60); "
        )
        started = time.monotonic()
        with patch.object(model_stream, "_WORKER_SCRIPT", block_dns + model_stream._WORKER_SCRIPT):
            with self.assertRaises(model_stream.ModelStreamError) as failure:
                list(model_stream.stream_model(config, [{"role": "user", "content": "fixture"}],
                                               first_timeout=0.4, total_timeout=1.3))
        self.assertEqual(failure.exception.code, "first_token_timeout")
        self.assertLess(time.monotonic() - started, 1.15)

    def test_payload_is_bounded_before_provider_request(self):
        config = model_stream.ModelConfig("http://127.0.0.1:9/v1", "fixture")
        with self.assertRaises(model_stream.ModelStreamError) as failure:
            list(model_stream.stream_model(config, [{"role": "user", "content": "a" * 300_000}]))
        self.assertEqual(failure.exception.code, "invalid_request")


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.action = lambda handler: None
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((self.path, self.headers.get("Authorization"), json.loads(raw)))
                try:
                    owner.action(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("Loopback TCP is disabled in this sandbox")
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.messages = [{"role": "user", "content": "只使用公開原文"}]

    def stream(self, **options):
        return model_stream.stream_model(
            model_stream.ModelConfig(self.base_url, "fixture", options.pop("token_file", None)),
            self.messages, **options,
        )

    def structured_stream(self, schema=FIXTURE_SCHEMA, *, sections=True, **options):
        config = model_stream.ModelConfig(self.base_url, 'fixture', structured=True)
        return model_stream.stream_model(config, self.messages, response_schema=schema, stream_sections=sections, **options)

    def test_structured_first_section_escapes_and_arrives_before_rest_of_object(self):
        finish = Event()
        self.addCleanup(finish.set)
        first = json.dumps(FIXTURE_SECTIONS[0], ensure_ascii=True, indent=2)
        rest = ','.join(json.dumps(row, ensure_ascii=False) for row in FIXTURE_SECTIONS[1:])

        def response(handler):
            header(handler)
            # Key and Unicode escapes are allowed to span deltas, just like ordinary values.
            write(handler, delta(r'{ "\u0073ec') + delta('tions" : [ ' + first[:23]) + delta(first[23:]))
            if finish.wait(3):
                write(handler, delta(',' + rest + '] }') + delta(finish='stop'))

        self.action = response
        rows = self.structured_stream(first_timeout=1.2, total_timeout=4)
        self.addCleanup(rows.close)
        self.assertEqual(json.loads(next(rows)), FIXTURE_SECTIONS[0])
        finish.set()
        self.assertEqual([json.loads(row) for row in rows], FIXTURE_SECTIONS[1:])
        spec = self.requests[0][2]['response_format']
        self.assertEqual(spec['type'], 'json_schema')
        self.assertIs(spec['json_schema']['strict'], True)
        self.assertEqual(spec['json_schema']['schema'], FIXTURE_SCHEMA)

    def test_structured_envelope_rejects_invalid_order_shape_count_and_trailing_garbage(self):
        first, second, third = (json.dumps(row) for row in FIXTURE_SECTIONS)
        valid = '{"sections":[' + ','.join((first, second, third)) + ']}'
        malformed = (
            ('{"extra":0,"sections":[' + first + ']}', 0),
            ('{"sections":[' + second + ']}', 0),
            ('{"sections":[{"kind":"evidence","kind":"evidence","text":"原文未交代","ids":[]}]}', 0),
            ('{"sections":[{"kind":"evidence","text","原文未交代","ids":[]}]}', 0),
            ('{"sections":[{"kind":"evidence","text":"\\ud800","ids":[]}]}', 0),
            ('{"sections":[' + first + ']}', 1),
            ('{"sections":[' + ','.join((first, second, third, first)) + ']}', 3),
            (valid + ' {}', 3),
            (valid[:-1] + ',"extra":false}', 3),
            (valid[:-2] + ',]}', 3),
            (valid[:-1], 3),
        )
        for raw, count in malformed:
            with self.subTest(raw=raw):
                def response(handler):
                    header(handler)
                    write(handler, delta(raw) + delta(finish='stop'))
                self.action = response
                rows = self.structured_stream()
                received = []
                with self.assertRaises(model_stream.ModelStreamError) as failure:
                    for row in rows:
                        received.append(json.loads(row))
                self.assertEqual(received, FIXTURE_SECTIONS[:count])
                self.assertIn(failure.exception.code, ('invalid_response', 'incomplete_output'))

    def test_structured_single_object_waits_for_end_and_rejects_a_second_object(self):
        schema = {'type': 'object', 'properties': {'key': {'type': 'string'}},
                  'required': ['key'], 'additionalProperties': False}
        for suffix in ('', ' {}'):
            with self.subTest(suffix=suffix):
                def response(handler):
                    header(handler)
                    write(handler, delta('{\n "key" : "value"\n}' + suffix) + delta(finish='stop'))
                self.action = response
                rows = self.structured_stream(schema, sections=False)
                if suffix:
                    with self.assertRaises(model_stream.ModelStreamError):
                        next(rows)
                else:
                    self.assertEqual([json.loads(row) for row in rows], [{'key': 'value'}])

    def test_structured_capability_does_not_change_other_callers_without_schema(self):
        def response(handler):
            header(handler)
            write(handler, delta('{"legacy":true}\n') + delta(finish='stop'))
        self.action = response
        rows = self.structured_stream(schema=None, sections=False)
        self.assertEqual([json.loads(row) for row in rows], [{'legacy': True}])
        self.assertNotIn('response_format', self.requests[0][2])

    def test_structured_aliases_are_still_checked_by_the_public_l4_flow(self):
        from knowledge_hub.followup import Followups
        from knowledge_hub.sources import parse_arxiv_html
        from knowledge_hub.store import KnowledgeStore

        with TemporaryDirectory() as tmp:
            store = KnowledgeStore(Path(tmp) / 'study.db')
            entry = parse_arxiv_html(
                b'<article><h1>Reading fixture</h1><p id="p1">The system keeps each event with its exact original version.</p></article>',
                'https://arxiv.org/html/2611.01234v1',
            )
            item_id, version_id, _ = store.upsert_parsed_entry(entry)
            item = store.get_item(item_id)
            version = store.select_version(item['versions'], version_id)
            config = model_stream.ModelConfig(self.base_url, 'fixture', structured=True)
            service = Followups(store, {'primary': config})
            for alias in ('S1', 'S99'):
                with self.subTest(alias=alias):
                    continuing = Event()
                    self.addCleanup(continuing.set)

                    def response(handler):
                        header(handler)
                        first = json.dumps({**FIXTURE_SECTIONS[0], 'ids': [alias]})
                        write(handler, delta('{"sections":[' + first))
                        if continuing.wait(3):
                            rest = ','.join(json.dumps(row) for row in FIXTURE_SECTIONS[1:])
                            write(handler, delta(',' + rest + ']}') + delta(finish='stop'))

                    self.action = response
                    events = service.run(item, version, 'How are events recorded?', 'p1', f'fixture-{alias}')
                    try:
                        event, _ = next(events)
                        self.assertEqual(event, 'meta')
                        event, data = next(events)
                        continuing.set()
                        if alias == 'S1':
                            self.assertEqual(event, 'section')
                            self.assertEqual(data['citations'][0]['href'], f'/items/{item_id}?version={version_id}#p1')
                            self.assertEqual([event for event, _ in events], ['section', 'section', 'done'])
                        else:
                            self.assertEqual(event, 'error')
                            self.assertEqual(data['reason'], 'invalid_citation')
                            self.assertEqual(list(events), [])
                    finally:
                        continuing.set()
                        events.close()
            request = self.requests[0][2]
            self.assertNotIn('JSONL', request['messages'][0]['content'])
            self.assertIn('sections', request['response_format']['json_schema']['schema']['properties'])

    def test_chunked_utf8_is_yielded_before_upstream_completion(self):
        finish = Event()
        self.addCleanup(finish.set)

        def response(handler):
            handler.protocol_version = "HTTP/1.1"
            header(handler, chunked=True)
            write(handler, b": heartbeat\r\n\r\n", chunked=True)
            write(handler, delta(role="assistant"), chunked=True)
            first = delta('{"text":"段落"}\n')
            split = first.index("段".encode()) + 1
            for piece in (first[:split], first[split:-1], first[-1:]):
                write(handler, piece, chunked=True)
            if finish.wait(2):
                write(handler, delta('{"text":"第二列"}'), chunked=True)
                write(handler, delta(finish="stop"), chunked=True)
                write(handler, b"data: [DONE]\r\n\r\n", chunked=True)
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()

        self.action = response
        with TemporaryDirectory() as tmp:
            secret_path = Path(tmp) / "fixture-token"
            secret_path.touch(mode=0o600)
            secret_path.write_text("synthetic-fixture-token")
            rows = self.stream(first_timeout=1.2, total_timeout=3, token_file=secret_path)
            self.addCleanup(rows.close)
            self.assertEqual(next(rows), '{"text":"段落"}')
            finish.set()
            self.assertEqual(list(rows), ['{"text":"第二列"}'])
        path, credential, request = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(credential, "Bearer synthetic-fixture-token")
        self.assertEqual(request["messages"], self.messages)
        self.assertEqual(request["model"], "fixture")
        self.assertIs(request["stream"], True)
        self.assertGreater(request["max_tokens"], 0)
        self.assertLessEqual(request["max_tokens"], 4096)

    def test_sse_done_with_cr_separators_and_multiline_data(self):
        def response(handler):
            header(handler)
            event = b'data: {"choices":\rdata: [{"delta":{"content":"{\\"ok\\":true}\\n"}}]}\r\r'
            write(handler, event)
            write(handler, b"data: [DONE]\r\r")

        self.action = response
        self.assertEqual(list(self.stream()), ['{"ok":true}'])

    def test_multiline_object_and_one_optional_fence_preserve_the_json_contract(self):
        def response(handler):
            header(handler)
            write(handler, delta('```json\n{\n "text": "a } inside a string",\n "number": 2\n}\n'))
            write(handler, delta('{"second":true}\n```'))
            write(handler, delta(finish="stop"))
        self.action = response
        self.assertEqual([json.loads(s) for s in self.stream()],
                         [{"text": "a } inside a string", "number": 2}, {"second": True}])

    def test_text_after_fenced_json_is_rejected(self):
        def response(handler):
            header(handler)
            write(handler, delta('```json\n{"ok":true}\n```\nextra'))
            write(handler, delta(finish="stop"))
        self.action = response
        with self.assertRaises(model_stream.ModelStreamError):
            list(self.stream())

    def test_first_complete_line_deadline_ignores_role_heartbeat_and_partial_model_output(self):
        def response(handler):
            header(handler)
            write(handler, delta(role="assistant"))
            write(handler, b'event: ping\ndata: {"type":"ping"}\n\n')
            write(handler, b"data: \n\n")
            write(handler, delta('{"text":"'))
            for _ in range(80):
                write(handler, b": heartbeat\n\n")
                time.sleep(0.03)

        self.action = response
        start = time.monotonic()
        with self.assertRaises(model_stream.ModelStreamError) as failure:
            list(self.stream(first_timeout=0.5, total_timeout=2))
        self.assertEqual(failure.exception.code, "first_token_timeout")
        self.assertLess(time.monotonic() - start, 1.25)

    def test_times_first_content_separately_from_first_verified_protocol_line(self):
        def response(handler):
            header(handler)
            write(handler, delta(role="assistant"))
            write(handler, sse({"choices": [{"delta": {"reasoning_content": "hidden"}}]}))
            time.sleep(0.12)
            write(handler, delta('{"text":"'))
            time.sleep(0.15)
            write(handler, delta('你好"}\n'))
            write(handler, delta(finish="stop"))

        self.action = response
        timing = {}
        self.assertEqual(list(self.stream(timing=timing)), ['{"text":"你好"}'])
        self.assertGreaterEqual(timing["ttft_ms"], 100)
        self.assertGreater(timing["first_line_ms"] - timing["ttft_ms"], 90)
        self.assertGreaterEqual(timing["total_ms"], timing["first_line_ms"])

    def test_total_deadline_closes_upstream_even_while_consumer_is_paused(self):
        disconnected = Event()

        def response(handler):
            header(handler)
            write(handler, delta('{"text":"visible"}\n'))
            handler.connection.settimeout(2)
            if handler.connection.recv(1) == b"":
                disconnected.set()

        self.action = response
        rows = self.stream(first_timeout=0.7, total_timeout=0.95)
        self.addCleanup(rows.close)
        self.assertEqual(next(rows), '{"text":"visible"}')
        self.assertTrue(disconnected.wait(1.5))
        with self.assertRaises(model_stream.ModelStreamError) as failure:
            next(rows)
        self.assertEqual(failure.exception.code, "total_timeout")

    def test_many_deliverable_lines_cannot_extend_the_total_deadline(self):
        def response(handler):
            header(handler)
            for index in range(60):
                write(handler, delta('{"n":' + str(index) + '}\n'))
                time.sleep(0.05)

        self.action = response
        rows = self.stream(first_timeout=0.55, total_timeout=0.85)
        self.addCleanup(rows.close)
        start = time.monotonic()
        received = []
        with self.assertRaises(model_stream.ModelStreamError) as failure:
            for row in rows:
                received.append(json.loads(row))
        self.assertEqual(failure.exception.code, "total_timeout")
        self.assertGreaterEqual(len(received), 2)
        self.assertLess(len(received), 60)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_generator_close_disconnects_upstream_before_the_deadline(self):
        disconnected = Event()

        def response(handler):
            header(handler)
            write(handler, delta('{"text":"first"}\n'))
            handler.connection.settimeout(2)
            if handler.connection.recv(1) == b"":
                disconnected.set()

        self.action = response
        rows = self.stream(first_timeout=1, total_timeout=3)
        self.addCleanup(rows.close)
        self.assertEqual(next(rows), '{"text":"first"}')
        rows.close()
        self.assertTrue(disconnected.wait(1.0))

    def test_unfinished_or_truncated_output_is_an_error_after_complete_lines(self):
        for end in (b"", delta(finish="length"), b"data: [DONE]\n\n"):
            def response(handler):
                header(handler)
                write(handler, delta('{"text":"usable"}\n{"text":') + end)

            with self.subTest(end=end):
                self.action = response
                rows = self.stream(first_timeout=1, total_timeout=2)
                self.addCleanup(rows.close)
                self.assertEqual(next(rows), '{"text":"usable"}')
                with self.assertRaises(model_stream.ModelStreamError) as failure:
                    next(rows)
                self.assertEqual(failure.exception.code, "incomplete_output")

    def test_invalid_or_oversized_model_line_does_not_escape(self):
        for invalid in ("not JSON\n", "x" * 50_000):
            def response(handler):
                header(handler)
                write(handler, delta(invalid) + delta(finish="stop"))

            with self.subTest(length=len(invalid)):
                self.action = response
                with self.assertRaises(model_stream.ModelStreamError) as failure:
                    list(self.stream())
                self.assertEqual(failure.exception.code, "invalid_response")

    def test_insecure_token_file_fails_before_connecting(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "insecure-fixture-token"
            token_path.write_text("synthetic-fixture-token")
            token_path.chmod(0o644)
            with self.assertRaises(model_stream.ModelStreamError) as failure:
                list(self.stream(token_file=token_path))
        self.assertEqual(failure.exception.code, "invalid_config")
        self.assertEqual(self.requests, [])

    def test_redirect_is_not_followed_and_upstream_error_text_is_not_exposed(self):
        def response(handler):
            handler.send_response(302)
            handler.send_header("Location", self.base_url + "/unexpected")
            handler.end_headers()
            handler.wfile.write(b"UPSTREAM_SECRET_SENTINEL")

        self.action = response
        with self.assertRaises(model_stream.ModelStreamError) as failure:
            list(self.stream())
        self.assertEqual(failure.exception.code, "provider_unavailable")
        self.assertNotIn("UPSTREAM_SECRET_SENTINEL", str(failure.exception))
        self.assertEqual([x[0] for x in self.requests], ["/v1/chat/completions"])


if __name__ == "__main__":
    unittest.main()
