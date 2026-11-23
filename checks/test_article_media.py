import os
import tempfile
import unittest
from pathlib import Path

from knowledge_hub import article_media


class ArticleMedia(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.original = self.root / "original.mp4"
        self.original.write_bytes(b"0123456789")

    def request(self, method="GET", filename="original.mp4", **headers):
        environ = {"REQUEST_METHOD": method, **headers}
        response = []

        def start_response(status, pairs):
            response.extend((int(status.split()[0]), dict(pairs)))

        body = article_media.serve(environ, start_response, self.root, filename)
        return (*response, body)

    def read(self, *args, **kwargs):
        status, headers, body = self.request(*args, **kwargs)
        try:
            return status, headers, b"".join(body)
        finally:
            if hasattr(body, "close"):
                body.close()

    def test_mp4_headers_and_body(self):
        status, headers, payload = self.read()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["Content-Length"], "10")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(payload, b"0123456789")

    def test_head_reports_full_length_without_body(self):
        status, headers, payload = self.read("HEAD", HTTP_RANGE="bytes=0-1")
        self.assertEqual((status, headers["Content-Length"], payload), (200, "10", b""))
        self.assertNotIn("Content-Range", headers)

    def test_supports_browser_probe_suffix_and_open_ended_ranges(self):
        for range_value, content_range, payload in (
            ("bytes=0-1", "bytes 0-1/10", b"01"),
            ("bytes=3-", "bytes 3-9/10", b"3456789"),
            ("bytes=-3", "bytes 7-9/10", b"789"),
            ("bytes=8-999", "bytes 8-9/10", b"89"),
            ("bytes=-999", "bytes 0-9/10", b"0123456789"),
        ):
            with self.subTest(range_value=range_value):
                status, headers, body = self.read(HTTP_RANGE=range_value)
                self.assertEqual(status, 206)
                self.assertEqual(headers["Content-Range"], content_range)
                self.assertEqual(headers["Content-Length"], str(len(payload)))
                self.assertEqual(body, payload)

    def test_invalid_or_unsatisfiable_byte_range_returns_total_size(self):
        for range_value in ("bytes=10-", "bytes=4-2", "bytes=-0", "bytes=-",
                            "bytes=0-1,3-4", "bytes=wrong"):
            with self.subTest(range_value=range_value):
                status, headers, payload = self.read(HTTP_RANGE=range_value)
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], "bytes */10")
                self.assertEqual(headers["Content-Length"], "0")
                self.assertEqual(payload, b"")

    def test_ignores_unsupported_range_unit_and_unverifiable_if_range(self):
        for headers in (
            {"HTTP_RANGE": "items=0-1"},
            {"HTTP_RANGE": "bytes=0-1", "HTTP_IF_RANGE": '"stale"'},
        ):
            with self.subTest(headers=headers):
                status, headers, payload = self.read(**headers)
                self.assertEqual((status, headers["Content-Length"], payload),
                                 (200, "10", b"0123456789"))
                self.assertNotIn("Content-Range", headers)

    def test_empty_file_has_no_satisfiable_range(self):
        self.original.write_bytes(b"")
        status, headers, payload = self.read(HTTP_RANGE="bytes=0-1")
        self.assertEqual((status, headers["Content-Range"], payload), (416, "bytes */0", b""))

    def test_allows_only_the_two_video_names(self):
        (self.root / "guided.mp4").write_bytes(b"lesson")
        (self.root / "other.mp4").write_bytes(b"private")
        self.assertEqual(self.read(filename="guided.mp4")[2], b"lesson")
        for filename in ("other.mp4", "./original.mp4", "../original.mp4", str(self.original)):
            with self.subTest(filename=filename):
                self.assertEqual(self.read(filename=filename)[0], 404)

    def test_rejects_symlink_and_other_nonregular_files(self):
        guided = self.root / "guided.mp4"
        guided.symlink_to(self.original)
        self.assertEqual(self.read(filename="guided.mp4")[0], 404)
        guided.unlink()
        guided.mkdir()
        self.assertEqual(self.read(filename="guided.mp4")[0], 404)
        guided.rmdir()
        os.mkfifo(guided)
        self.assertEqual(self.read(filename="guided.mp4")[0], 404)

    def test_rejects_other_methods(self):
        status, headers, payload = self.read("POST")
        self.assertEqual((status, headers["Allow"], payload), (405, "GET, HEAD", b""))

    def test_streams_multiple_chunks_and_handles_early_close(self):
        self.original.write_bytes(b"v" * 262_144)
        status, headers, body = self.request()
        self.assertEqual((status, headers["Content-Length"]), (200, "262144"))
        try:
            first_chunk = next(iter(body))
            self.assertGreater(len(first_chunk), 0)
            self.assertLess(len(first_chunk), 262_144)
            self.assertEqual(first_chunk, b"v" * len(first_chunk))
        finally:
            body.close()
        self.assertEqual(list(body), [])
