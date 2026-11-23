import io
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.util import setup_testing_defaults


ORIGIN = "http://127.0.0.1:8765"
RELEASE_API = "https://api.github.com/repos/pytorch/pytorch/releases/latest"
DOC_URL = "https://docs.python.org/3.14/whatsnew/3.14.html"
PAPER_HTML = "https://arxiv.org/html/2611.00001v1"
RELEASE = {
    "id": 101,
    "tag_name": "v2.15.0",
    "name": "PyTorch 測試版本",
    "author": {"login": "fixture"},
    "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.15.0",
    "published_at": "2026-11-09T10:00:00Z",
    "updated_at": "2026-11-09T10:00:00Z",
    "body": "## Highlights\n\nThis release introduces a compiler backend that reduces kernel launch overhead by 12% against the previous release.\n\n<script>sourceDanger()</script>",
}
HTML = b"""<!doctype html><html><head><title>Fixture paper</title></head><body>
<nav>Site menu</nav><article class="ltx_document">
<h1 class="ltx_title_document">Fixture retrieval paper</h1>
<section id="S1"><h2>Evidence</h2><div class="ltx_para" id="S1.p1">
<p class="ltx_p">Measured retrieval evidence.</p></div></section>
<script>sourceDanger()</script></article></body></html>"""
DOC = b"""<!doctype html><html><head><title>Fixture Python changes</title></head>
<body><nav>Site menu</nav><main><h1>Fixture Python changes</h1>
<section id="free-threaded"><h2>Free threading</h2>
<p>Documented behavior for free threading.</p></section></main></body></html>"""


def feed(version):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2611.00001v{version}</id>
    <title>Fixture retrieval paper</title>
    <published>2026-11-09T09:00:00Z</published>
    <updated>2026-11-{8 + version:02d}T09:00:00Z</updated>
    <summary>Fixture abstract version {version}.</summary>
    <author><name>Fixture Author</name></author>
    <link rel="alternate" href="http://arxiv.org/abs/2611.00001v{version}"/>
  </entry>
</feed>""".encode()


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.csrf = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "csrf":
            self.csrf = attrs.get("value")


class ReadingLoopAcceptance(unittest.TestCase):
    def setUp(self):
        try:
            from knowledge_hub.web import create_app
        except ModuleNotFoundError as error:
            if error.name not in {"knowledge_hub", "knowledge_hub.web"}:
                raise
            self.fail("The reading application entry point is missing")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "hub.sqlite3"
        self.create_app = create_app
        self.version = 1
        self.failing = set()
        self.requested = []
        self.app = self.create_app(self.db, fetch=self.fetch)

    def fetch(self, url):
        self.requested.append(url)
        if url in self.failing:
            raise OSError("synthetic failure")
        if url == RELEASE_API:
            return json.dumps(RELEASE, ensure_ascii=False).encode()
        if url == PAPER_HTML:
            return HTML
        if url == PAPER_HTML.replace('/html/', '/pdf/'):
            raise OSError('Synthetic PDF unavailable')
        if url == DOC_URL:
            return DOC
        parsed = urlsplit(url)
        if parsed.netloc == 'huggingface.co' and parsed.path == '/api/daily_papers':
            return b'[]'
        if (parsed.scheme, parsed.netloc, parsed.path) == (
            "https", "export.arxiv.org", "/api/query"
        ):
            query = parse_qs(parsed.query)
            if query.get("search_query") == ["cat:cs.LG"] and query.get("max_results") == ["10"]:
                if "arxiv-query" in self.failing:
                    raise OSError("synthetic feed failure")
                return feed(self.version)
        from knowledge_hub import sources
        if url in sources.ARXIV_CATEGORY_URLS.values():
            return b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        if url in sources.RELEASE_APIS.values():
            raise OSError('No framework release for this fixture')
        if urlsplit(url).netloc == 'rss.arxiv.org':
            raise OSError('No RSS data for this fixture')
        self.fail(f"Unexpected fetch destination: {url}")

    def request(self, path, fields=None, *, origin=ORIGIN, host="127.0.0.1:8765"):
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
            body = urlencode(fields).encode()
            env.update(
                REQUEST_METHOD="POST",
                HTTP_ORIGIN=origin,
                CONTENT_TYPE="application/x-www-form-urlencoded",
                CONTENT_LENGTH=str(len(body)),
            )
            env["wsgi.input"] = io.BytesIO(body)
        status = []

        def start_response(value, headers, exc_info=None):
            status.append(int(value.split()[0]))

        response = self.app(env, start_response)
        try:
            return status, b"".join(response).decode()
        finally:
            if hasattr(response, "close"):
                response.close()

    def page(self, path):
        status, body = self.request(path)
        self.assertEqual(status, [200])
        return body

    def post(self, path, expected=303, **fields):
        csrf = Forms(self.page("/")).csrf
        self.assertTrue(csrf)
        status, body = self.request(path, {"csrf": csrf, **fields})
        self.assertEqual(status, [expected], body)

    def exported(self):
        data = json.loads(self.page("/export.json"))
        self.assertEqual(data["schema_version"], 1)
        return data

    def item(self, prefix):
        return next(item for item in self.exported()["items"] if item["key"].startswith(prefix))

    def test_sync_distinguishes_updates_unchanged_and_fetch_failure(self):
        self.post("/interests", terms="PyTorch, retrieval")
        self.post("/sync")
        first = self.exported()
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(first["sources"]["pytorch"]["state"], "updated")
        self.assertEqual(first["sources"]["arxiv"]["state"], "updated")
        self.assertIn("PyTorch 測試版本", self.page("/"))
        self.assertEqual(first["interests"], ["PyTorch", "retrieval"])

        self.post("/sync")
        second = self.exported()
        self.assertEqual(second["sources"]["arxiv"]["state"], "unchanged")
        self.assertEqual({i["id"] for i in second["items"]}, {i["id"] for i in first["items"]})
        self.assertTrue(all(len(i["versions"]) == 1 for i in second["items"]))

        self.failing.add("arxiv-query")
        self.post("/sync")
        failure = self.exported()
        self.assertEqual(failure["sources"]["arxiv"]["state"], "failed")
        self.assertEqual(failure["sources"]["pytorch"]["state"], "unchanged")
        self.assertEqual(len(failure["items"]), 2)

    def test_note_and_evidence_survive_revision_and_restart(self):
        self.post("/sync")
        item = self.item("arxiv:")
        path = f"/items/{item['id']}"
        self.assertIn('data-reading-scope="abstract"', self.page(path))

        self.post(path + "/fetch")
        full = next(v for v in self.item("arxiv:")["versions"] if v["scope"] == "full")
        block = next(b for b in full["blocks"] if "Measured retrieval evidence." in b["text"])
        self.assertIn('data-reading-scope="full"', self.page(path))
        self.assertIn(PAPER_HTML + "#S1.p1", self.page(path))
        self.assertNotIn("<script>sourceDanger()</script>", self.page(path))

        self.post(path + "/status", status="later")
        self.post(path + "/notes", version_id=full["id"], anchor=block["anchor"],
                  text="跨日找回，待比較 <script>noteDanger()</script>")
        self.post(path + "/relations", version_id=full["id"], anchor=block["anchor"],
                  concept="檢索", kind="explains")
        self.version = 2
        self.post("/sync")

        self.app = self.create_app(self.db, fetch=self.fetch)
        item = self.item("arxiv:")
        self.assertEqual(item["status"], "later")
        self.assertTrue(any(v["label"] == "v2" for v in item["versions"]))
        self.assertEqual(item["notes"][0]["version_id"], full["id"])
        self.assertEqual(item["notes"][0]["anchor"], block["anchor"])
        self.assertEqual(item["relations"][0]["origin"], "manual")
        old = self.page(path + "?" + urlencode({"version": full["id"]}))
        self.assertIn("Measured retrieval evidence.", old)
        self.assertNotIn("<script>noteDanger()</script>", old)
        results = self.page("/search?" + urlencode({"q": "跨日找回"}))
        self.assertIn("Fixture retrieval paper", results)
        self.assertRegex(results, re.escape(path) + r'(?:[?"#])')

    def test_full_text_failure_keeps_abstract_and_reports_failure(self):
        self.post("/sync")
        self.failing.add(PAPER_HTML)
        path = f"/items/{self.item('arxiv:')['id']}"
        self.post(path + "/fetch")
        self.assertIn('data-reading-scope="abstract"', self.page(path))
        self.assertEqual(self.item("arxiv:")["fulltext_status"], "failed")

    def test_manual_document_and_local_write_boundaries(self):
        self.post("/add", url=DOC_URL)
        item = self.item("python:")
        self.assertIn("Documented behavior for free threading.", self.page(f"/items/{item['id']}"))
        seen = list(self.requested)
        self.post("/add", expected=400, url="https://docs.python.org@127.0.0.1/private")
        csrf = Forms(self.page("/")).csrf
        self.assertEqual(self.request("/sync", {"csrf": csrf}, origin="https://untrusted.example")[0], [403])
        self.assertEqual(self.request("/sync", {"csrf": "bad"})[0], [403])
        self.assertIn(self.request("/", host="untrusted.example")[0], ([400], [403]))
        self.assertEqual(self.requested, seen)
