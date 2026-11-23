"""Private reading drafts and version-bound resumption. Never a model input."""
import json
import re

from . import hive_reader, lessons
from .sources import now_iso
from .study import StudyError

FIELDS = {'tab': 20, 'anchor': 200, 'question_draft': 2000,
          'reflection': 8000, 'open_questions': 8000}
TABS = {'lesson', 'original', 'notes'}


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS reading_sessions (
          item_id INTEGER NOT NULL REFERENCES items(id),
          version_id INTEGER NOT NULL REFERENCES item_versions(id),
          revision INTEGER NOT NULL DEFAULT 0,
          tab TEXT NOT NULL DEFAULT 'lesson', anchor TEXT NOT NULL DEFAULT '',
          question_draft TEXT NOT NULL DEFAULT '', reflection TEXT NOT NULL DEFAULT '',
          open_questions TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
          PRIMARY KEY(item_id,version_id));
        CREATE TABLE IF NOT EXISTS version_visuals (
          version_id INTEGER PRIMARY KEY REFERENCES item_versions(id), manifest TEXT NOT NULL);
    ''')


def row_dict(row):
    return {**dict(row), 'version_id': str(row['version_id'])}


class ReadingState:
    def __init__(self, store):
        self.store = store

    def _version(self, item_id, version_id):
        try:
            item_id, version_id = lessons.identifier(item_id), lessons.identifier(version_id)
        except ValueError as exc:
            raise StudyError(400, str(exc)) from None
        item = self.store.get_item(item_id)
        version = next((v for v in item['versions'] if str(v['id']) == str(version_id)), None) if item else None
        if not version:
            raise StudyError(404, '找不到這篇文章的指定版本。')
        return item_id, version_id, {**version, 'item_key':item['key']}

    def get(self, item_id, version_id):
        item_id, version_id, _ = self._version(item_id, version_id)
        with self.store._connect() as conn:
            row = conn.execute('SELECT * FROM reading_sessions WHERE item_id=? AND version_id=?',
                               (item_id, version_id)).fetchone()
        return row_dict(row) if row else {
            'item_id': item_id, 'version_id': str(version_id), 'revision': 0,
            'tab': 'lesson', 'anchor': '', 'question_draft': '', 'reflection': '',
            'open_questions': '', 'updated_at': ''}

    def save(self, item_id, version_id, revision, fields):
        item_id, version_id, version = self._version(item_id, version_id)
        try:
            if isinstance(revision, bool) or not re.fullmatch(r'\d{1,12}', str(revision)):
                raise ValueError()
            revision = int(revision)
            if type(fields) is not dict or not fields or set(fields) - FIELDS.keys():
                raise ValueError()
            if any(not isinstance(value, str) or len(value) > FIELDS[key]
                   for key, value in fields.items()):
                raise ValueError()
            if 'tab' in fields and fields['tab'] not in TABS:
                raise ValueError()
            anchor = fields.get('anchor', '')
            saved = re.fullmatch(r'(source-turn|progress|l4|capture)-([1-9]\d{0,17})', anchor)
            visual = anchor.removeprefix('kh-visual-') if anchor.startswith('kh-visual-') else ''
            valid_anchors = {b['anchor'] for b in version['blocks']}
            if anchor and not saved and not visual and anchor not in valid_anchors and not hive_reader.valid_bookmark(anchor, version) and not re.fullmatch(
                    r'(?:lesson-section-\d{1,2}|study-\d{1,18}|study-save|reader-start|backgrounds|conversation|layer-guide)', anchor):
                raise ValueError()
        except (ValueError, TypeError):
            raise StudyError(400, '閱讀草稿格式、長度或位置無效；尚未保存。') from None
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if saved:
                table = {'source-turn':'source_turns', 'progress':'study_progress',
                         'l4':'followup_turns', 'capture':'capture_records'}[saved[1]]
                if not conn.execute(f'SELECT 1 FROM {table} WHERE id=? AND item_id=? AND version_id=?',
                                    (saved[2],item_id,version_id)).fetchone():
                    raise StudyError(400, '找不到這個版本的閱讀紀錄。')
            if visual and not hive_reader.valid_bookmark(anchor, version):
                manifest = conn.execute('SELECT manifest FROM version_visuals WHERE version_id=?', (version_id,)).fetchone()
                if not manifest or not any(visual in (p.get('anchor'), p.get('pdf_origin',{}).get('anchor'))
                                           for p in json.loads(manifest[0]).get('panels', [])):
                    raise StudyError(400, '找不到這個版本的圖表。')
            existing = conn.execute('SELECT * FROM reading_sessions WHERE item_id=? AND version_id=?',
                                    (item_id, version_id)).fetchone()
            if (existing['revision'] if existing else 0) != revision:
                raise StudyError(409, '另一個分頁已有更新；這份輸入仍保留在畫面，請複製後重新載入。')
            if existing is None:
                conn.execute('INSERT INTO reading_sessions(item_id,version_id,updated_at) VALUES (?,?,?)',
                             (item_id, version_id, now_iso()))
            # Names are a fixed allowlist; user input appears only in parameters.
            assignments = ','.join(key + '=?' for key in fields)
            conn.execute(f'UPDATE reading_sessions SET {assignments},revision=revision+1,updated_at=? '
                         'WHERE item_id=? AND version_id=?',
                         (*fields.values(), now_iso(), item_id, version_id))
            return row_dict(conn.execute('SELECT * FROM reading_sessions WHERE item_id=? AND version_id=?',
                                        (item_id, version_id)).fetchone())

    def recent(self, limit=5):
        with self.store._connect() as conn:
            return [row_dict(row) for row in conn.execute('''
                SELECT r.*,t.title FROM reading_sessions r JOIN version_titles t ON t.version_id=r.version_id
                ORDER BY r.updated_at DESC,r.rowid DESC LIMIT ?''', (limit,))]

    def continuations(self, page=1, page_size=20):
        """Page through fixed versions, preferring their current private draft.

        Receipts and source answers survive a browser closing before autosave.
        Page the lightweight activity index before fetching private draft text.
        """
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 20:
            raise ValueError('Invalid continuation page')
        query = '''WITH continuations AS (
            SELECT item_id,version_id,MAX(stamp) AS updated_at,MAX(progress_id) AS progress_id,
                   MAX(study_id) AS study_id,MAX(turn_id) AS turn_id,MAX(followup_id) AS followup_id
            FROM (
                SELECT item_id,version_id,updated_at AS stamp,NULL AS progress_id,NULL AS study_id,NULL AS turn_id,NULL AS followup_id
                  FROM reading_sessions
                UNION ALL SELECT item_id,version_id,created_at,id,NULL,NULL,NULL FROM study_progress
                UNION ALL SELECT item_id,version_id,updated_at,NULL,id,NULL,NULL FROM study_records
                UNION ALL SELECT item_id,version_id,created_at,NULL,NULL,id,NULL FROM source_turns
                UNION ALL SELECT item_id,version_id,COALESCE(finished_at,created_at),NULL,NULL,NULL,id FROM followup_turns
            ) GROUP BY item_id,version_id
        ) '''
        with self.store._connect() as conn:
            # Count and page use one snapshot while another tab may save a draft.
            conn.execute('BEGIN')
            total = conn.execute(query + 'SELECT count(*) FROM continuations').fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = [row_dict(row) for row in conn.execute(query + ''', page AS (
                SELECT * FROM continuations ORDER BY updated_at DESC,version_id DESC,item_id DESC LIMIT ? OFFSET ?
            )
            SELECT c.item_id,c.version_id,c.updated_at,t.title,v.label,v.scope,
                   COALESCE(NULLIF(r.anchor,''),'l4-' || f.id,'progress-' || p.id,
                       CASE WHEN l.id IS NOT NULL AND (s.id IS NULL OR l.created_at >= s.updated_at)
                            THEN 'source-turn-' || l.id END,'study-' || s.id) AS anchor,
                   COALESCE(r.question_draft,'') AS question_draft,
                   COALESCE(r.reflection,p.understanding,'') AS reflection,
                   COALESCE(r.open_questions,p.questions,'') AS open_questions,
                   s.id AS study_id,s.state AS study_state,l.id AS turn_id,l.prompt AS turn_prompt,
                   f.id AS followup_id,f.question AS followup_question
            FROM page c JOIN version_titles t ON t.version_id=c.version_id JOIN item_versions v ON v.id=c.version_id
                LEFT JOIN reading_sessions r ON r.item_id=c.item_id AND r.version_id=c.version_id
                LEFT JOIN study_progress p ON p.id=c.progress_id
                LEFT JOIN study_records s ON s.id=c.study_id
                LEFT JOIN source_turns l ON l.id=c.turn_id
                LEFT JOIN followup_turns f ON f.id=c.followup_id
            ORDER BY c.updated_at DESC,c.version_id DESC,c.item_id DESC''',
                (page_size, (page - 1) * page_size))]
        return {'rows': rows, 'page': page, 'pages': pages, 'total': total}

    def acknowledge_question(self, item_id, version_id, prompt):
        # A concurrent tab may already have started its next question. Only the
        # exact submitted text can be cleared; its immutable receipt retains it.
        with self.store._connect() as conn:
            conn.execute('UPDATE reading_sessions SET question_draft=\'\',revision=revision+1,updated_at=? '
                         'WHERE item_id=? AND version_id=? AND question_draft=?',
                         (now_iso(),item_id,version_id,prompt))

    def suggest_summary(self, item_id, version_id):
        """An extractive, editable teaching recap, never a mastery assessment."""
        from .study import StudyService
        rows = [r for r in StudyService(self.store).records(item_id)
                if r['version_id'] == str(version_id) and r['state'] == 'completed']
        lesson = next((r for r in reversed(rows) if r['mode'] == 'lesson'), None)
        if not lesson:
            return {'record_id': None, 'understanding': '', 'questions': ''}
        parts = []
        for section in lesson['answer']['sections']:
            if section['role'] in ('problem', 'mechanism', 'comparison'):
                parts.append(section['title'] + '\n' + section['text'])
        # Whole sections only, no half sentence silently labelled a conclusion.
        while parts and len('\n\n'.join(parts)) > 7600:
            parts.pop()
        return {'record_id': lesson['id'], 'understanding': '\n\n'.join(parts),
                'questions': lesson['answer']['limitations'][:4000]}
