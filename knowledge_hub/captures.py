"""Private, fixed-edition captures and passive recall."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from urllib.parse import quote as url_quote

from . import grounding, pdf_evidence, visuals

from .connections import reading_url
from .excerpts import sentence_spans
from .lessons import identifier
from .metric_notation import contains_displayed, plain_metric
from .qa import REQUEST_ID
from .sources import SAFE_ANCHOR_RE, now_iso


TAGS = ('#想法', '#盲點/待查', '#可試看看')
_BLIND_SPOT = TAGS[1]
_FIELDS = {
    'tag': {'anchor', 'quote', 'tag', 'comment'},
    'missing': {'turn_id'},
    'draft': {'turn_id'},
    'edit': {'id', 'turn_id', 'revision', 'text'},
    'confirm': {'id', 'turn_id', 'revision', 'text'},
}
_PUNCTUATION = re.compile(r'[。！？.!?]["\'”’」』）\]]*$')


class CaptureError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _id(value):
    try:
        return identifier(value)
    except ValueError:
        raise CaptureError(400, '保存內容缺少有效的識別值。') from None


def _text(value, limit, *, required=False, trim=True):
    if (not isinstance(value, str) or len(value) > limit or '\x00' in value
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise CaptureError(400, '保存的文字格式或長度無效。')
    if required and not value.strip():
        raise CaptureError(400, '請選取有效文字或填入內容。')
    return value.strip() if trim else value


def _command(fields):
    if type(fields) is not dict:
        raise CaptureError(400, '保存操作格式無效。')
    action, request_id = fields.get('action'), fields.get('request_id')
    if (not isinstance(action, str) or action not in _FIELDS or not isinstance(request_id, str)
            or not REQUEST_ID.fullmatch(request_id)
            or fields.keys() - (_FIELDS[action] | {'action', 'request_id'})):
        raise CaptureError(400, '保存操作或請求識別值無效。')
    if action == 'tag':
        tag = fields.get('tag')
        if tag not in TAGS:
            raise CaptureError(400, '請選擇有效的意圖標籤。')
        return action, request_id, {
            'anchor': _text(fields.get('anchor'), 200, required=True, trim=False),
            'quote': _text(fields.get('quote'), 3000, required=True, trim=False),
            'tag': tag,
            'comment': _text(fields.get('comment', ''), 1000),
        }
    if action in ('missing', 'draft'):
        return action, request_id, {'turn_id': _id(fields.get('turn_id'))}
    capture_id, turn_id = fields.get('id'), fields.get('turn_id')
    revision = fields.get('revision')
    if not any((capture_id, turn_id)) or not re.fullmatch(r'[1-9][0-9]{0,11}', str(revision)):
        raise CaptureError(400, '請指定草稿及讀取時的修訂版本。')
    return action, request_id, {
        'id': _id(capture_id) if capture_id not in (None, '') else None,
        'turn_id': _id(turn_id) if turn_id not in (None, '') else None,
        'revision': int(revision),
        'text': _text(fields.get('text'), 8000, required=True)
        if action == 'edit' or 'text' in fields else None,
    }


def _row_record(row):
    record = dict(row)
    link_anchor = record.pop('link_anchor')
    record['version_id'] = str(record['version_id'])
    if record['state'] == 'draft':
        link_anchor = f"capture-{record['id']}"
    record['href'] = reading_url(record['item_id'], record['version_id'], link_anchor)
    return _display_capture(record)


def _display_capture(record):
    if record['source'] == 'guide' and (quote := plain_metric(record['quote'])) != record['quote']:
        return {**record, 'display_quote': quote}
    return record


def _edition(conn, item_id, version_id):
    row = conn.execute('''SELECT v.item_id,v.id,v.blocks_json,v.scope,v.source_url,v.label,i.key
                           FROM item_versions v JOIN items i ON i.id=v.item_id
                           WHERE v.item_id=? AND v.id=?''', (item_id, version_id)).fetchone()
    if row is None:
        raise CaptureError(404, '找不到這篇文章的固定版本。')
    edition = dict(row)
    edition['blocks'] = {block['anchor']: block for block in json.loads(edition.pop('blocks_json'))}
    saved = conn.execute('SELECT manifest FROM version_visuals WHERE version_id=?',(version_id,)).fetchone()
    edition['visuals'] = json.loads(saved[0]) if saved else {}
    return edition


def _block_url(edition, anchor):
    return edition['blocks'][anchor].get('source_url') or edition['source_url']


def _citation_location(cite, edition):
    anchor, quote = cite.get('anchor'), cite.get('quote')
    if (not isinstance(anchor, str) or len(anchor) > 200 or not SAFE_ANCHOR_RE.fullmatch(anchor)
            or not isinstance(quote, str) or not quote):
        return None
    item_id, version_id = edition['item_id'], edition['id']
    block = edition['blocks'].get(anchor)
    direct = cite.get('source_url')
    if (block and quote in block['text'] and direct == _block_url(edition, anchor)
            and cite.get('href') == reading_url(item_id, version_id, anchor)):
        return anchor, anchor, direct
    local = f'kh-visual-{anchor}'
    if cite.get('href') != reading_url(item_id, version_id, local):
        return None
    html = visuals.paper_url(edition['key'], edition['label'])
    pdf = pdf_evidence.pdf_source(edition['key'], edition['label'])
    page = re.match(r'page-([1-9][0-9]{0,2})-(?:figure|table)-', anchor)
    if ((html and direct == f'{html}#{url_quote(anchor, safe="")}')
            or (pdf and page and direct == f'{pdf}#page={page[1]}')):
        return anchor, local, direct
    panels = grounding._panels({'key':edition['key']},edition,edition['visuals'],anchor)
    match = panels.get(anchor)
    if match:
        panel, url = match
        origin = panel.get('pdf_origin',{})
        if direct == url or (origin and direct == origin.get('page_url') and
                origin.get('anchor') in grounding._panels({'key':edition['key']},edition,edition['visuals'],origin['anchor'])):
            return anchor, local, direct
    return None


def _guide_location(conn, edition, anchor, quote):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='grounded_guides'").fetchone():
        return None
    item_id, version_id = edition['item_id'], edition['id']
    for row in conn.execute('SELECT payload FROM grounded_guides WHERE item_id=? AND version_id=?',
                            (item_id, version_id)):
        guide = json.loads(row['payload'])
        if guide.get('item_id') != item_id or str(guide.get('version_id')) != str(version_id):
            continue
        for section in guide['sections']:
            if section['missing'] or not contains_displayed(section['text'], quote):
                continue
            for cite in section['citations']:
                if cite.get('anchor') == anchor and (location := _citation_location(cite, edition)):
                    return location
    return None


def _source_location(conn, edition, anchor, citations):
    for cite in citations:
        if cite.get('anchor') == anchor and (location := _citation_location(cite, edition)):
            return location
    if anchor:
        panels = grounding._panels({'key': edition['key']}, edition, edition['visuals'],anchor)
        if anchor in panels:
            return anchor, f'kh-visual-{anchor}', panels[anchor][1]
    if anchor in edition['blocks']:
        return anchor, anchor, _block_url(edition, anchor)
    for cite in citations:
        if location := _citation_location(cite, edition):
            return location
    return '', '', edition['source_url']


def _sentence(text):
    start, end, _ = next(sentence_spans(text))
    result = text[start:end]
    return result if _PUNCTUATION.search(result) else result + '。'


def _suggest(sections):
    parts = [_sentence(sections['evidence']['text'])]
    limits = sections.get('limits')
    if limits and limits['missing']:
        parts.append('原文未交代其適用條件與限制。')
    elif limits and limits['text'].strip():
        parts.append('適用條件與限制：' + _sentence(limits['text']))
    inference = sections.get('inference')
    if inference and not inference['missing'] and inference['text'].strip():
        parts.append('推論：' + _sentence(inference['text']))
    if len(parts) == 1:
        parts.append('適用範圍仍需再核對。')
    return ' '.join(parts)


def _followup_capture(conn, action, turn, edition):
    if turn is None or turn['item_id'] != edition['item_id'] or str(turn['version_id']) != str(edition['id']):
        raise CaptureError(404, '找不到這篇文章版本的指定追問。')
    if turn['state'] != 'completed':
        raise CaptureError(409, '這筆追問尚未完成，請等到完成後再保存。')
    sections = {section['kind']: section for section in turn['sections']}
    if action == 'missing':
        if not any(s['missing'] and s['text'] == grounding.MISSING and not s['citations']
                   for s in sections.values()):
            raise CaptureError(400, '這筆追問沒有標記為原文未交代的內容。')
        citations = [cite for section in sections.values() for cite in section['citations']]
        anchor, link_anchor, source_url = _source_location(conn, edition, turn['anchor'], citations)
        return {
            'kind': 'missing', 'source': 'followup', 'anchor': anchor, 'link_anchor': link_anchor,
            'source_url': source_url, 'turn_id': turn['id'], 'tag': _BLIND_SPOT,
            'text': turn['question'], 'state': 'saved',
        }
    evidence = sections.get('evidence')
    if evidence and not evidence['missing'] and evidence['text'].strip():
        for cite in evidence['citations']:
            if location := _citation_location(cite, edition):
                anchor, link_anchor, source_url = location
                return {
                    'kind': 'understanding', 'source': 'followup', 'anchor': anchor, 'link_anchor': link_anchor,
                    'source_url': source_url, 'turn_id': turn['id'], 'quote': cite['quote'],
                    'text': plain_metric(_suggest(sections)) if turn['mode'] == 'model' else _suggest(sections),
                    'state': 'draft',
                }
    raise CaptureError(400, '這筆追問沒有可綁定到同版原文的證據。')


def _note_text(record):
    if record['kind'] == 'tag':
        return record['tag'] + '\n' + '\n\n'.join(filter(None, (record['comment'], record['quote'])))
    if record['kind'] == 'missing':
        return record['tag'] + '\n' + record['text']
    return '#我的理解\n' + record['text']


def _write_note(conn, item_id, version_id, record, stamp):
    # Keep the exact-version note and its capture in the same writer transaction.
    # KnowledgeStore.add_note opens a separate connection.
    row = conn.execute('INSERT INTO notes(item_id,version_id,anchor,text,created_at) VALUES (?,?,?,?,?)',
                       (item_id, version_id, record['link_anchor'], _note_text(record), stamp))
    return row.lastrowid


def _fold(text):
    return unicodedata.normalize('NFKC', text).casefold()


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS capture_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            version_id INTEGER NOT NULL REFERENCES item_versions(id) ON DELETE CASCADE,
            turn_id INTEGER REFERENCES followup_turns(id) ON DELETE CASCADE,
            note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
            kind TEXT NOT NULL CHECK (kind IN ('tag','missing','understanding')),
            source TEXT NOT NULL CHECK (source IN ('original','guide','followup')),
            anchor TEXT NOT NULL,
            link_anchor TEXT NOT NULL,
            source_url TEXT NOT NULL,
            quote TEXT NOT NULL DEFAULT '',
            tag TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL CHECK (state IN ('saved','draft')),
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(turn_id,kind)
        );
        CREATE INDEX IF NOT EXISTS capture_edition ON capture_records(item_id,version_id,id);
        CREATE INDEX IF NOT EXISTS capture_note ON capture_records(note_id) WHERE note_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS capture_intents (
            request_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            capture_id INTEGER NOT NULL REFERENCES capture_records(id) ON DELETE CASCADE,
            receipt TEXT NOT NULL
        );
    """)


class CaptureStore:
    def __init__(self, store, followups):
        self.store = store
        self.followups = followups

    def act(self, item, version, fields):
        """Apply a private capture intent once, returning the original durable receipt."""
        try:
            item_id, version_id = _id(item['id']), _id(version['id'])
        except (KeyError, TypeError):
            raise CaptureError(400, '保存內容缺少已選定的文章版本。') from None
        action, request_id, data = _command(fields)
        fingerprint = hashlib.sha256(json.dumps(
            (item_id, version_id, action, data), ensure_ascii=False, sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        # Reading a follow-up may expire old leases in its own connection.
        turn = self.followups.get(data['turn_id']) if action in ('missing', 'draft') else None
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT fingerprint,receipt FROM capture_intents WHERE request_id=?',
                                    (request_id,)).fetchone()
            if previous is not None:
                if previous['fingerprint'] != fingerprint:
                    raise CaptureError(409, '這個請求識別值已用於不同的保存內容。')
                return _display_capture(json.loads(previous['receipt']))
            edition = _edition(conn, item_id, version_id)
            if action in ('edit', 'confirm'):
                row = self._revise(conn, item_id, version_id, action, data)
            elif action == 'tag':
                if edition['scope'] == 'unavailable':
                    raise CaptureError(400, '這個版本沒有可綁定的原文段落。')
                anchor, quote = data['anchor'], data['quote']
                block = edition['blocks'].get(anchor)
                if block and quote in block['text']:
                    source, location = 'original', (anchor, anchor, _block_url(edition, anchor))
                else:
                    source, location = 'guide', _guide_location(conn, edition, anchor, quote)
                if not location:
                    raise CaptureError(400, '選取內容與這個版本的原文或導讀不符。')
                anchor, link_anchor, source_url = location
                row = self._insert(conn, item_id, version_id, {
                    **data, 'kind': 'tag', 'source': source, 'anchor': anchor, 'link_anchor': link_anchor,
                    'source_url': source_url, 'state': 'saved',
                })
            else:
                entry = _followup_capture(conn, action, turn, edition)
                row = conn.execute('SELECT * FROM capture_records WHERE turn_id=? AND kind=?',
                                   (entry['turn_id'], entry['kind'])).fetchone()
                if row is None:
                    row = self._insert(conn, item_id, version_id, entry)
            result = _row_record(row)
            conn.execute('INSERT INTO capture_intents VALUES (?,?,?,?)',
                         (request_id, fingerprint, result['id'], json.dumps(result, ensure_ascii=False)))
            return result

    def _insert(self, conn, item_id, version_id, values):
        stamp = now_iso()
        note_id = _write_note(conn, item_id, version_id, values, stamp) if values['state'] == 'saved' else None
        row = conn.execute("""INSERT INTO capture_records(
            item_id,version_id,turn_id,note_id,kind,source,anchor,link_anchor,source_url,quote,tag,comment,text,state,
            created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            item_id, version_id, values.get('turn_id'), note_id, values['kind'], values['source'],
            values['anchor'], values['link_anchor'], values['source_url'], values.get('quote', ''), values.get('tag', ''),
            values.get('comment', ''), values.get('text', ''), values['state'], stamp, stamp,
        ))
        return conn.execute('SELECT * FROM capture_records WHERE id=?', (row.lastrowid,)).fetchone()

    def _revise(self, conn, item_id, version_id, action, data):
        row = conn.execute("""SELECT * FROM capture_records
            WHERE item_id=? AND version_id=? AND kind='understanding'
              AND (? IS NULL OR id=?) AND (? IS NULL OR turn_id=?)""", (
            item_id, version_id, data['id'], data['id'], data['turn_id'], data['turn_id'],
        )).fetchone()
        if row is None:
            raise CaptureError(404, '找不到這篇文章版本的理解草稿。')
        if row['state'] != 'draft' or row['revision'] != data['revision']:
            raise CaptureError(409, '草稿已有其他更新，請重新讀取後再保存。')
        text = row['text'] if data['text'] is None else data['text']
        state = 'saved' if action == 'confirm' else 'draft'
        stamp = now_iso()
        note_id = (_write_note(conn, item_id, version_id, {**dict(row), 'text': text}, stamp)
                   if state == 'saved' else None)
        conn.execute("""UPDATE capture_records
            SET text=?,state=?,note_id=?,revision=revision+1,updated_at=? WHERE id=?""",
            (text, state, note_id, stamp, row['id']))
        return conn.execute('SELECT * FROM capture_records WHERE id=?', (row['id'],)).fetchone()

    def list(self, item_id, version_id):
        item_id, version_id = _id(item_id), _id(version_id)
        with self.store._connect() as conn:
            if not conn.execute('SELECT 1 FROM item_versions WHERE item_id=? AND id=?',
                                (item_id, version_id)).fetchone():
                raise CaptureError(404, '找不到這篇文章的固定版本。')
            rows = conn.execute("""SELECT * FROM capture_records WHERE item_id=? AND version_id=?
                ORDER BY updated_at DESC,id DESC""", (item_id, version_id))
            return [_row_record(row) for row in rows]

    def search(self, query):
        """Search saved titles, private notes/drafts, questions and concepts locally."""
        needle = _fold(_text(query, 240))
        if not needle:
            return []
        with self.store._connect() as conn:
            conn.create_function('capture_fold', 1, _fold, deterministic=True)
            rows = conn.execute(_SEARCH, {'query': needle, 'blind_spot': _BLIND_SPOT})
            return [{
                'title': row['title'], 'text': row['text'], 'kind': row['kind'],
                'href': reading_url(row['item_id'], row['version_id'], row['anchor']),
            } for row in rows]

    def related(self, item, version):
        """Only run when the reader explicitly opens the private recall panel."""
        from .discovery import terms
        explicit = [r['concept'] for r in item.get('relations', []) if str(r['version_id']) == str(version['id'])]
        words = sorted(terms(version.get('title') or item['title']), key=lambda word: (-len(word), word))
        results = {}
        for concept in list(dict.fromkeys([*explicit,*words]))[:5]:
            for row in self.search(concept):
                if row['kind'] != 'title':
                    results.setdefault((row['href'],row['kind'],row['text']), row)
        return list(results.values())[:12]


_SEARCH = """
WITH candidates(item_id,version_id,anchor,text,kind,stamp,id) AS (
    SELECT v.item_id,max(v.id),'',t.title,'title',max(v.fetched_at),max(v.id)
      FROM version_titles t JOIN item_versions v ON v.id=t.version_id
     GROUP BY v.item_id,t.title
    UNION ALL
    SELECT n.item_id,n.version_id,COALESCE(c.link_anchor,n.anchor),n.text,
        CASE WHEN c.tag=:blind_spot OR (
                substr(n.text,1,length(:blind_spot))=:blind_spot
                AND substr(n.text,length(:blind_spot)+1,1) IN ('',' ',char(9),char(10),char(13))
             )
             THEN 'question' WHEN c.kind='understanding' THEN 'understanding' ELSE 'note' END,
        n.created_at,n.id
      FROM notes n LEFT JOIN capture_records c ON c.note_id=n.id
    UNION ALL
    SELECT item_id,version_id,'capture-'||id,text,'draft',updated_at,id
      FROM capture_records WHERE state='draft'
    UNION ALL
    SELECT item_id,version_id,anchor,concept||' ('||kind||')','concept',created_at,id FROM relations
    UNION ALL
    SELECT item_id,version_id,'question-'||id,question,'question',created_at,id FROM questions
    UNION ALL
    SELECT item_id,version_id,'',open_questions,'question',updated_at,version_id
      FROM reading_sessions WHERE trim(open_questions)<>''
    UNION ALL
    SELECT item_id,version_id,'progress-'||id,questions,'question',created_at,id
      FROM study_progress WHERE trim(questions)<>''
    UNION ALL
    SELECT item_id,version_id,'progress-'||id,understanding,'understanding',created_at,id
      FROM study_progress WHERE trim(understanding)<>''
)
SELECT c.*,t.title FROM candidates c JOIN version_titles t ON t.version_id=c.version_id
WHERE instr(capture_fold(c.text),:query)>0
ORDER BY c.stamp DESC,c.id DESC,c.kind,c.item_id,c.version_id LIMIT 30
"""
