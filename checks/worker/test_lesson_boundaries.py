"""Additional packet boundaries; executed only by the host."""

import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub import lessons
from knowledge_hub.sources import compute_content_hash
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app


class LessonBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'packets.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.entry = dict(key='python:synthetic', source_name='python', title='固定舊標題',
                          label='3.14', scope='partial',
                          source_url='https://docs.python.org/3.14/whatsnew/3.14.html',
                          blocks=[dict(anchor='p1', text='Synthetic source evidence.')])
        self.item_id, self.version_id = self.insert(self.entry)
        self.app = create_app(self.db)

    def insert(self, entry):
        entry = copy.deepcopy(entry)
        entry['content_hash'] = compute_content_hash(entry['title'], entry['scope'],
                                                      entry['label'], entry['blocks'])
        item_id, version_id, _ = self.store.upsert_parsed_entry(entry)
        return item_id, version_id

    def create(self, **changes):
        fields = dict(item_id=self.item_id, version_id=self.version_id, title='合成教材',
                      learning_goal='驗證實際邊界', blueprint_md='# 原始稿\r\n\r\n保留空白  \n',
                      origin='manual')
        fields.update(changes)
        return self.store.create_lesson(**fields)

    def post(self, path, fields):
        env = {}
        setup_testing_defaults(env)
        body = urlencode(dict(csrf=self.store.get_csrf_token(), **fields)).encode()
        env.update(PATH_INFO=path, HTTP_HOST='127.0.0.1:8765', REQUEST_METHOD='POST',
                   HTTP_ORIGIN='http://127.0.0.1:8765', CONTENT_LENGTH=str(len(body)),
                   CONTENT_TYPE='application/x-www-form-urlencoded')
        env['wsgi.input'] = io.BytesIO(body)
        status = []
        result = self.app(env, lambda value, headers: status.append(int(value.split()[0])))
        b''.join(result)
        return status[0]

    def test_maximum_encoded_unicode_is_preserved_only_on_lesson_routes(self):
        # Four-byte code points exercise the largest percent-encoding expansion.
        blueprint = '𠮷' * 16000
        fields = dict(version_id=self.version_id, title='題' * 200,
                      learning_goal='目' * 2000, blueprint_md=blueprint, origin='agent_assisted')
        self.assertGreater(len(urlencode(fields).encode()), 65536)
        self.assertEqual(self.post(f'/items/{self.item_id}/lessons', fields), 303)
        lesson = self.store.get_lessons()[0]
        self.assertEqual(lesson['blueprint_md'], blueprint)
        notes = '𠮷' * 10000
        self.assertEqual(self.post(f'/lessons/{lesson["id"]}/result',
                                  dict(notebook_url='', review_notes=notes)), 303)
        self.assertEqual(self.store.get_lesson(lesson['id'])['review_notes'], notes)
        self.assertEqual(self.post('/interests', dict(terms='字' * 10000)), 400)
        self.assertEqual(self.post(f'/items/{self.item_id}/lessons',
                                  dict(fields, blueprint_md=blueprint + '字')), 400)
        self.assertEqual(len(self.store.get_lessons()), 1)

    def test_old_version_title_when_first_draft_created_after_article_update(self):
        newer = copy.deepcopy(self.entry)
        newer['title'] = '更新後標題'
        newer['blocks'][0]['text'] = 'New evidence.'
        self.insert(newer)
        lesson = self.store.get_lesson(self.create())
        self.assertEqual(lesson['source']['title'], '固定舊標題')
        self.assertEqual(lesson['source']['scope'], 'partial')
        self.assertEqual(lesson['source']['blocks'][0]['source_url'], self.entry['source_url'])
        self.assertNotIn('#p1', lessons.source_markdown(lesson['source']).decode())
        before = lessons.export_zip(lesson)
        self.store.update_lesson_result(lesson['id'], '', '核對筆記不應改變 ZIP')
        self.assertEqual(lessons.export_zip(self.store.get_lesson(lesson['id'])), before)
        with self.store._connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('UPDATE lessons SET blueprint_md=? WHERE id=?', ('改寫', lesson['id']))

    def test_foreign_parent_and_invalid_identifiers_leave_no_new_draft(self):
        parent = self.create()
        other = copy.deepcopy(self.entry)
        other['blocks'][0]['text'] = 'Different version.'
        _, version_id = self.insert(other)
        with self.assertRaises(ValueError):
            self.create(version_id=version_id, parent_id=str(parent))
        for value in ('v1', '0', '-1', '9' * 100, '9223372036854775808'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.create(version_id=value)
            with self.subTest(parent=value), self.assertRaises(ValueError):
                self.create(parent_id=value)
        self.assertEqual(len(self.store.get_lessons()), 1)

    def test_unavailable_blank_and_invalid_fields_never_insert(self):
        for scope, text in [('unavailable', 'Placeholder'), ('full', ' \n\t')]:
            entry = copy.deepcopy(self.entry)
            entry['scope'] = scope
            entry['blocks'][0]['text'] = text
            _, version_id = self.insert(entry)
            with self.assertRaises(ValueError):
                self.create(version_id=version_id)
        for changes in (dict(title=' '), dict(title='題' * 201),
                        dict(learning_goal='目' * 2001), dict(blueprint_md='\n '),
                        dict(origin='ai_generated')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(**changes)
        self.assertEqual(self.store.get_lessons(), [])

    def test_result_url_rejects_normalization_tricks_without_mutating_result(self):
        lesson_id = self.create()
        url = 'https://notebooklm.google.com/notebook/01234567-89ab-cdef-0123-456789abcdef/'
        self.store.update_lesson_result(lesson_id, url, '原核對筆記')
        for bad in (url + '?', url + '#', url.replace('.com/', '.com:443/'),
                    url.replace('/notebook/', '/notebook/%30'), '\n' + url,
                    url.replace('https://', 'https://user@'), url.replace('https:', 'http:')):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                self.store.update_lesson_result(lesson_id, bad, '不可寫入')
        with self.assertRaises(ValueError):
            self.store.update_lesson_result(lesson_id, '', '字' * 10001)
        saved = self.store.get_lesson(lesson_id)
        self.assertEqual((saved['notebook_url'], saved['review_notes']), (url, '原核對筆記'))
