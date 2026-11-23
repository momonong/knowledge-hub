"""Regression tests for security boundaries, publisher IDs, arXiv version ordering, and CSRF/Origin."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

from knowledge_hub.__main__ import BIND_HOST, build_parser
from knowledge_hub.sources import (
    ARXIV_QUERY_URL,
    PYTHON_314_DOC_URL,
    PYTORCH_RELEASE_API,
    parse_arxiv_feed,
    parse_python_whatsnew_html,
    validate_fetch_url,
    validate_manual_url,
)
from knowledge_hub.web import create_app


def _arxiv_feed_xml(version: int) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2611.00001v{version}</id>
    <title>Fixture paper v{version}</title>
    <published>2026-11-01T09:00:00Z</published>
    <updated>2026-11-{version:02d}T09:00:00Z</updated>
    <summary>Fixture abstract version {version}.</summary>
    <author><name>Fixture Author</name></author>
    <link rel="alternate" href="http://arxiv.org/abs/2611.00001v{version}"/>
  </entry>
</feed>""".encode("utf-8")


class SecurityAndEdgeRegressionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "worker-hub.sqlite3"
        self.payloads: dict[str, bytes] = {}
        self.requested: list[str] = []
        self.app = create_app(self.db, fetch=self._fetch)

    def _fetch(self, url: str) -> bytes:
        self.requested.append(url)
        if url in self.payloads:
            return self.payloads[url]
        raise OSError(f"Unconfigured URL: {url}")

    def _request(
        self,
        path: str,
        fields: dict | None = None,
        *,
        origin: str = "http://127.0.0.1:8765",
        host: str = "127.0.0.1:8765",
    ):
        env = {}
        setup_testing_defaults(env)
        parsed = urlsplit(path)
        env.update(
            PATH_INFO=parsed.path,
            QUERY_STRING=parsed.query,
            HTTP_HOST=host,
            SERVER_NAME="127.0.0.1",
            SERVER_PORT="8765",
        )
        if fields is not None:
            body = urlencode(fields).encode("utf-8")
            env.update(
                REQUEST_METHOD="POST",
                HTTP_ORIGIN=origin,
                CONTENT_TYPE="application/x-www-form-urlencoded",
                CONTENT_LENGTH=str(len(body)),
            )
            env["wsgi.input"] = io.BytesIO(body)
        status: list[int] = []

        def start_response(value, headers, exc_info=None):
            status.append(int(value.split()[0]))

        resp = self.app(env, start_response)
        try:
            return status[0], b"".join(resp).decode("utf-8")
        finally:
            if hasattr(resp, "close"):
                resp.close()

    def _csrf(self) -> str:
        code, body = self._request("/")
        self.assertEqual(code, 200)
        marker = 'name="csrf" value="'
        idx = body.index(marker) + len(marker)
        return body[idx : body.index('"', idx)]

    def test_initial_unattempted_state_and_xml_dtd_rejection(self):
        code, raw = self._request("/export.json")
        self.assertEqual(code, 200)
        exported = json.loads(raw)
        self.assertEqual(exported["sources"]["pytorch"]["state"], "unattempted")
        self.assertEqual(exported["sources"]["arxiv"]["state"], "unattempted")

        malicious_xml = b"""<?xml version="1.0"?>
<!DOCTYPE feed [<!ENTITY xxe "secret">]>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""
        with self.assertRaises(ValueError):
            parse_arxiv_feed(malicious_xml)

    def test_url_allowlist_and_note_anchor_validation(self):
        for bad_url in (
            "http://docs.python.org/3.14/whatsnew/3.14.html",
            "https://arxiv.org/html/2611.00001",
            "https://arxiv.org/html/2611.00001v1?foo=1",
            "https://arxiv.org/html/2611.00001v1#S1",
            "https://user:pass@arxiv.org/html/2611.00001v1",
        ):
            with self.assertRaises(ValueError):
                validate_manual_url(bad_url)
            with self.assertRaises(ValueError):
                validate_fetch_url(bad_url)

        self.payloads[PYTORCH_RELEASE_API] = json.dumps(
            {
                "tag_name": "v2.16.0",
                "name": "Empty Body Release",
                "author": {"login": "bot"},
                "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.16.0",
                "published_at": "2026-11-10T00:00:00Z",
                "body": "<script>evil()</script>",
            }
        ).encode("utf-8")

        csrf = self._csrf()
        code, _ = self._request("/sync", {"csrf": csrf})
        self.assertEqual(code, 303)

        _, raw_export = self._request("/export.json")
        exp = json.loads(raw_export)
        pt_item = exp["items"][0]
        ver_id = pt_item["versions"][0]["id"]

        _, item_html = self._request(f"/items/{pt_item['id']}")
        self.assertIn('data-reading-scope="unavailable"', item_html)

        code_bad_anchor, _ = self._request(
            f"/items/{pt_item['id']}/notes",
            {"csrf": csrf, "version_id": ver_id, "anchor": "nonexistent", "text": "note"},
        )
        self.assertEqual(code_bad_anchor, 400)

        code_bad_ver, _ = self._request(
            f"/items/{pt_item['id']}/relations",
            {
                "csrf": csrf,
                "version_id": "9999",
                "anchor": "unavailable",
                "concept": "測試",
                "kind": "explains",
            },
        )
        self.assertEqual(code_bad_ver, 400)

    def test_local_anchor_vs_publisher_html_id(self):
        no_id_html = b"<main><h1>Reference</h1><p>No publisher ID.</p></main>"
        parsed = parse_python_whatsnew_html(no_id_html, PYTHON_314_DOC_URL)
        self.assertEqual(len(parsed["blocks"]), 1)
        self.assertEqual(parsed["blocks"][0]["anchor"], "p1")
        self.assertEqual(parsed["blocks"][0]["source_url"], PYTHON_314_DOC_URL)

        # Invalid publisher ID with spaces must not be slugified into a fake publisher fragment
        bad_id_html = b'<main><h1>Reference</h1><p id="not a valid publisher id!">Paragraph text.</p></main>'
        parsed_bad = parse_python_whatsnew_html(bad_id_html, PYTHON_314_DOC_URL)
        self.assertEqual(parsed_bad["blocks"][0]["anchor"], "p1")
        self.assertEqual(parsed_bad["blocks"][0]["source_url"], PYTHON_314_DOC_URL)

        # End-to-end check with note bound to local anchor p1
        self.payloads[PYTHON_314_DOC_URL] = no_id_html
        csrf = self._csrf()
        code, _ = self._request("/add", {"csrf": csrf, "url": PYTHON_314_DOC_URL})
        self.assertEqual(code, 303)

        _, raw_export = self._request("/export.json")
        item = json.loads(raw_export)["items"][0]
        ver = item["versions"][0]
        self.assertEqual(ver["blocks"][0]["anchor"], "p1")
        self.assertEqual(ver["blocks"][0]["source_url"], PYTHON_314_DOC_URL)

        code_note, _ = self._request(
            f"/items/{item['id']}/notes",
            {"csrf": csrf, "version_id": ver["id"], "anchor": "p1", "text": "無出版者ID段落筆記"},
        )
        self.assertEqual(code_note, 303)
        _, page_html = self._request(f"/items/{item['id']}")
        self.assertIn("無出版者ID段落筆記", page_html)
        self.assertNotIn(f"{PYTHON_314_DOC_URL}#p1", page_html)

    def test_arxiv_v9_v10_version_selection_and_fetch_target(self):
        csrf = self._csrf()
        # 1. Sync v9 abstract
        self.payloads[ARXIV_QUERY_URL] = _arxiv_feed_xml(9)
        self.assertEqual(self._request("/sync", {"csrf": csrf})[0], 303)

        # 2. Sync v10 abstract
        self.payloads[ARXIV_QUERY_URL] = _arxiv_feed_xml(10)
        self.assertEqual(self._request("/sync", {"csrf": csrf})[0], 303)

        # 3. Later add v9 full text
        v9_html_url = "https://arxiv.org/html/2611.00001v9"
        self.payloads[v9_html_url] = b"""<!doctype html><html><body>
        <article class="ltx_document"><h1 class="ltx_title_document">Fixture paper v9</h1>
        <section id="S1"><div id="S1.p1"><p>Full text of v9.</p></div></section>
        </article></body></html>"""
        self.assertEqual(
            self._request("/add", {"csrf": csrf, "url": v9_html_url})[0], 303
        )

        _, raw_export = self._request("/export.json")
        arxiv_item = next(
            it for it in json.loads(raw_export)["items"] if it["key"] == "arxiv:2611.00001"
        )
        item_path = f"/items/{arxiv_item['id']}"

        # Default view must still point to newer v10 (currently abstract)
        _, default_page = self._request(item_path)
        self.assertIn("Fixture abstract version 10.", default_page)
        self.assertIn('data-reading-scope="abstract"', default_page)

        # Explicitly opening v9 must work and prioritize v9's full snapshot over abstract
        _, v9_page = self._request(f"{item_path}?version=v9")
        self.assertIn("Full text of v9.", v9_page)
        self.assertIn('data-reading-scope="full"', v9_page)

        # Clicking "取得全文" must target v10, not v9
        v10_html_url = "https://arxiv.org/html/2611.00001v10"
        self.payloads[v10_html_url] = b"""<!doctype html><html><body>
        <article class="ltx_document"><h1 class="ltx_title_document">Fixture paper v10</h1>
        <section id="S1"><div id="S1.p1"><p>Full text of v10.</p></div></section>
        </article></body></html>"""
        self.assertEqual(
            self._request(f"{item_path}/fetch", {"csrf": csrf})[0], 303
        )
        self.assertEqual(self.requested[-1], v10_html_url)

        _, updated_default_page = self._request(item_path)
        self.assertIn("Full text of v10.", updated_default_page)
        self.assertIn('data-reading-scope="full"', updated_default_page)

    def test_origin_port_equality_and_non_ascii_csrf_token(self):
        csrf = self._csrf()
        # Mismatched port on Origin must return 403 even with valid CSRF token
        code_port_mismatch, _ = self._request(
            "/sync",
            {"csrf": csrf},
            host="127.0.0.1:8765",
            origin="http://127.0.0.1:8766",
        )
        self.assertEqual(code_port_mismatch, 403)

        # Mismatched scheme on Origin must return 403
        code_scheme_mismatch, _ = self._request(
            "/sync",
            {"csrf": csrf},
            host="127.0.0.1:8765",
            origin="https://127.0.0.1:8765",
        )
        self.assertEqual(code_scheme_mismatch, 403)

        # Non-ASCII CSRF token "無效" with matching Origin must return 403 without raising TypeError
        code_non_ascii_csrf, _ = self._request(
            "/sync",
            {"csrf": "無效"},
            host="127.0.0.1:8765",
            origin="http://127.0.0.1:8765",
        )
        self.assertEqual(code_non_ascii_csrf, 403)

    def test_cli_parser_and_bind_host(self):
        self.assertEqual(BIND_HOST, "127.0.0.1")
        parser = build_parser()
        args = parser.parse_args(["--db", "/tmp/test.sqlite3", "--port", "8765"])
        self.assertEqual(args.db, "/tmp/test.sqlite3")
        self.assertEqual(args.port, 8765)
