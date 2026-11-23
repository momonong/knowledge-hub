"""Host acceptance for version-bound lesson packets and a usable NotebookLM bridge."""
import hashlib
import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

import test_reading_loop as fixtures


class LessonWorkflowAcceptance(unittest.TestCase):
    def setUp(self):
        from knowledge_hub.sources import parse_arxiv_html
        from knowledge_hub.store import KnowledgeStore
        from knowledge_hub.web import create_app
        self.parse = parse_arxiv_html
        self.create_app = create_app
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "hub.sqlite3"
        self.store = KnowledgeStore(self.db)
        self.item_id, self.version_id, _ = self.store.upsert_parsed_entry(self.parse(fixtures.HTML, fixtures.PAPER_HTML))
        self.app = self.create_app(self.db)
        self.blueprint = "# 教學藍圖\n\n解釋機制，再走完一個例子。<script>blueprintDanger()</script>"
        self.url = "https://notebook.google.com/notebook/00000000-0000-4000-8000-000000000001"

    def request(self, path, fields=None, *, origin=fixtures.ORIGIN):
        env = {}
        setup_testing_defaults(env)
        parts = urlsplit(path)
        env.update(PATH_INFO=parts.path, QUERY_STRING=parts.query, HTTP_HOST="127.0.0.1:8765", SERVER_NAME="127.0.0.1", SERVER_PORT="8765")
        if fields is not None:
            body = urlencode(fields).encode()
            env.update(REQUEST_METHOD="POST", HTTP_ORIGIN=origin, CONTENT_TYPE="application/x-www-form-urlencoded", CONTENT_LENGTH=str(len(body)))
            env["wsgi.input"] = io.BytesIO(body)
        status, headers = [], {}
        def start(value, values):
            status.append(int(value.split()[0]))
            headers.update(values)
        reply = self.app(env, start)
        try:
            return status[0], headers, b"".join(reply)
        finally:
            if hasattr(reply, "close"):
                reply.close()

    def csrf(self):
        code, _, body = self.request("/")
        self.assertEqual(code, 200)
        return fixtures.Forms(body.decode()).csrf

    def fields(self, **changes):
        data = {"csrf": self.csrf(), "version_id": str(self.version_id), "title": "機制深讀樣稿",
                "learning_goal": "能說清楚機制與設計取捨", "blueprint_md": self.blueprint, "origin": "agent_assisted"}
        data.update(changes)
        return data

    def create(self, **changes):
        code, headers, body = self.request(f"/items/{self.item_id}/lessons", self.fields(**changes))
        self.assertEqual(code, 303, body.decode()[-300:])
        match = re.fullmatch(r"/lessons/([1-9][0-9]*)", headers.get("Location", ""))
        self.assertIsNotNone(match, headers)
        return int(match.group(1))

    def lessons(self):
        code, _, body = self.request("/export.json")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("lessons", data, "Portable export must include saved lesson packets")
        return data["lessons"]

    def exported_files(self, lesson_id):
        code, headers, raw = self.request(f"/lessons/{lesson_id}/export.zip")
        self.assertEqual(code, 200, raw[:200])
        self.assertTrue(headers.get("Content-Type", "").startswith("application/zip"))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertEqual(set(z.namelist()), {"source.md", "lesson-blueprint.md", "producer-instructions.txt", "manifest.json"})
            self.assertIsNone(z.testzip())
            return {name: z.read(name) for name in z.namelist()}

    def test_export_preserves_source_version_and_excludes_private_material(self):
        self.store.add_note(self.item_id, str(self.version_id), "S1.p1", "PRIVATE-NOTE-DO-NOT-SEND")
        lesson_id = self.create()
        before = self.exported_files(lesson_id)
        self.assertIn(b"Measured retrieval evidence.", before["source.md"])
        self.assertEqual(before["lesson-blueprint.md"].decode(), self.blueprint)
        self.assertNotIn(b"PRIVATE-NOTE-DO-NOT-SEND", b"\n".join(before.values()))
        manifest = json.loads(before["manifest.json"])
        self.assertEqual(str(manifest["version_id"]), str(self.version_id))
        self.assertEqual(manifest["source_scope"], "full")
        self.assertEqual(manifest["origin"], "agent_assisted")
        for name in ("source.md", "lesson-blueprint.md", "producer-instructions.txt"):
            self.assertEqual(manifest["files"][name], hashlib.sha256(before[name]).hexdigest())
        newer = self.parse(fixtures.HTML.replace(b"Fixture retrieval paper", b"New latest title").replace(b"Measured retrieval evidence.", b"New version evidence."), fixtures.PAPER_HTML.replace("v1", "v2"))
        self.store.upsert_parsed_entry(newer)
        self.app = self.create_app(self.db)
        self.assertEqual(before, self.exported_files(lesson_id))
        record = next(x for x in self.lessons() if x["id"] == lesson_id)
        self.assertEqual(record["source"]["title"], "Fixture retrieval paper")
        self.assertEqual(str(record["version_id"]), str(self.version_id))

    def test_new_draft_keeps_old_blueprint_and_results_survive_restart(self):
        first = self.create()
        code, _, body = self.request(f"/lessons/{first}/result", {"csrf": self.csrf(), "notebook_url": self.url, "review_notes": "待核對的真實影片。<script>reviewDanger()</script>"})
        self.assertEqual(code, 303, body.decode()[-300:])
        second = self.create(parent_id=str(first), blueprint_md="# 修訂稿\n\n補充完整反例。")
        old = self.exported_files(first)
        self.app = self.create_app(self.db)
        records = {x["id"]: x for x in self.lessons()}
        self.assertEqual(records[first]["notebook_url"], self.url)
        self.assertEqual(records[first]["blueprint_md"], self.blueprint)
        self.assertEqual(records[second]["parent_id"], first)
        self.assertEqual(records[second]["notebook_url"], "")
        self.assertEqual(old, self.exported_files(first))
        code, _, body = self.request(f"/lessons/{first}")
        self.assertEqual(code, 200)
        self.assertIn(self.url.encode(), body)
        self.assertNotIn(b"<script>blueprintDanger()", body)
        self.assertNotIn(b"<script>reviewDanger()", body)
        code, _, home = self.request("/manage")
        self.assertEqual(code, 200)
        self.assertIn(f"/lessons/{first}".encode(), home)
        self.assertIn(f"/lessons/{second}".encode(), home)

    def test_origin_csrf_version_and_notebook_destination_are_checked(self):
        fields = self.fields()
        for form, origin, expected in (({**fields, "csrf": "forged"}, fixtures.ORIGIN, 403),
                                       (fields, "https://attacker.example", 403),
                                       ({**fields, "version_id": "999999"}, fixtures.ORIGIN, 400)):
            code, _, _ = self.request(f"/items/{self.item_id}/lessons", form, origin=origin)
            self.assertEqual(code, expected)
        lesson_id = self.create()
        for url in ("javascript:alert(1)", "https://attacker.example/notebook/test",
                    self.url.replace("notebook.google.com", "notebook.google.com.attacker.example"),
                    self.url.replace("https://", "https://fixture-user:fixture-pass@"),
                    self.url + "?token=fixture"):
            with self.subTest(url=url):
                code, _, _ = self.request(f"/lessons/{lesson_id}/result", {"csrf": self.csrf(), "notebook_url": url, "review_notes": ""})
                self.assertEqual(code, 400)
        self.assertEqual(next(x for x in self.lessons() if x["id"] == lesson_id)["notebook_url"], "")

    def test_source_over_one_megabyte_is_rejected_without_truncation(self):
        from knowledge_hub.sources import compute_content_hash
        large = self.parse(fixtures.HTML, fixtures.PAPER_HTML.replace("v1", "v2"))
        large["blocks"] = [{"anchor": "p1", "text": "字" * 350000, "source_url": large["source_url"]}]
        large["content_hash"] = compute_content_hash(large["title"], large["scope"], large["label"], large["blocks"])
        _, version_id, _ = self.store.upsert_parsed_entry(large)
        code, _, _ = self.request(f"/items/{self.item_id}/lessons", self.fields(version_id=str(version_id)))
        self.assertEqual(code, 400)
        self.assertEqual(self.lessons(), [])


if __name__ == "__main__":
    unittest.main()
