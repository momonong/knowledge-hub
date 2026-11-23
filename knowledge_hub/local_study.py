"""Private, version-bound conversations over cited public passages."""
import hashlib
import json
import re

from . import excerpts, lessons, qa, source_reader, study_context
from .connections import reading_url
from .sources import now_iso
from .study import StudyError


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS source_turns (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id),
            version_id INTEGER NOT NULL REFERENCES item_versions(id),
            parent_id INTEGER REFERENCES source_turns(id),
            request_id TEXT NOT NULL UNIQUE,
            prompt TEXT NOT NULL, answer_json TEXT NOT NULL, created_at TEXT NOT NULL, origin TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS source_turn_article ON source_turns(item_id,version_id,id);
    ''')
    if 'origin' not in {r[1] for r in conn.execute('PRAGMA table_info(source_turns)')}:
        conn.execute("ALTER TABLE source_turns ADD COLUMN origin TEXT NOT NULL DEFAULT ''")


def record(row):
    result = dict(row)
    result['answer'] = json.loads(result.pop('answer_json'))
    return result


def question_ref(origin, prompt):
    return hashlib.sha256((origin+'\0'+prompt).encode()).hexdigest()


_HEADING = re.compile(r'^(?:(\d+(?:\.\d+)*|[A-Z](?:\.\d+)*)\s+(.{2,140})|(?i:(Abstract|Introduction|Related Work|Conclusions?|Limitations|Methodology|Results)))$')
_SECTION_ROLES = (
    ('related', r'related work|prior work|literature review|background'),
    ('abstract', r'^abstract$'),
    ('introduction', r'introduction|motivation|problem (?:setting|statement|formulation)'),
    ('limitations', r'limitations?|future work|discussion'),
    ('conclusion', r'conclusions?|summary'),
    ('results', r'experiment|evaluation|results?|benchmarks?|ablation|analysis'),
    ('method', r'method|approach|architecture|framework|system|algorithm|design|implementation'),
)


def section_labels(blocks):
    headings, labels = {}, []
    for block in blocks:
        text = block['text'].strip()
        match = _HEADING.fullmatch(text) if len(text) < 155 and '\n' not in text else None
        if match and not re.search(r'[.?!。！？]$',text):
            level = match[1].count('.')+1 if match[1] else 1
            title = match[2] or match[3]
            headings = {k:v for k,v in headings.items() if k < level}
            parent = headings[max(headings)][0] if headings else 'other'
            role = next((role for role,pattern in _SECTION_ROLES if re.search(pattern,title,re.I)),parent)
            headings[level] = ('related' if parent=='related' else role, title)
        labels.append(headings[max(headings)] if headings else ('other',''))
    return labels


class LocalStudy:
    def __init__(self, store):
        self.store = store

    def turns(self, item_id, version_id):
        with self.store._connect() as conn:
            return [record(row) for row in conn.execute(
                'SELECT * FROM source_turns WHERE item_id=? AND version_id=? ORDER BY id', (item_id,version_id))]

    def local_context(self, sources, query=''):
        """Search more of the exact local editions than fit in a Broker message."""
        focus = study_context.words(query)
        selected = []
        with self.store._connect() as conn:
            for source in sources:
                row = conn.execute('SELECT blocks_json FROM item_versions WHERE id=? AND item_id=? AND content_hash=?',
                                   (source['version_id'], source['item_id'], source['content_hash'])).fetchone()
                if row is None:
                    continue
                blocks = study_context.readable_blocks({'blocks':json.loads(row[0])})
                labels = section_labels(blocks)
                title = study_context.words(source['title'])
                top = excerpts.select_spans(blocks,title | focus,focus,limit=26 if selected else 44)
                # Keep the opening and the original selection alongside focused
                # paragraphs. Abstracts often define the contribution across sentences.
                anchors = {e['anchor'] for e in source['excerpts']}
                indices = dict.fromkeys([*range(min(7,len(blocks))),
                    *(i for i,b in enumerate(blocks) if b['anchor'] in anchors), *(x[0] for x in top)])
                passages = []
                for i in indices:
                    block = blocks[i]
                    portions = [text for j,text,*_ in top if j == i] if len(block['text'])>8000 else [block['text']]
                    for text in portions:
                        passages.append({'anchor':block['anchor'], 'quote':text,
                            'section_role':labels[i][0], 'section_heading':labels[i][1],
                            'source_url':block.get('source_url') or source['source_url'],
                            'reading_url':reading_url(source['item_id'],source['version_id'],block['anchor'])})
                selected.append({**source,'excerpts':passages or source['excerpts']})
        return {'sources':selected}

    def related_questions(self, sources):
        """Only local reading history is joined to suggested public backgrounds."""
        items = tuple(source['item_id'] for source in sources[1:])
        if not items:
            return []
        marks = ','.join('?' for _ in items)
        with self.store._connect() as conn:
            rows = conn.execute(f'''
                SELECT q.*,t.title FROM (
                  SELECT item_id,version_id,'progress-'||id AS anchor,questions AS prompt,created_at AS stamp
                    FROM study_progress WHERE item_id IN ({marks}) AND trim(questions)<>''
                  UNION ALL
                  SELECT item_id,version_id,'study-save',open_questions,updated_at
                    FROM reading_sessions WHERE item_id IN ({marks}) AND trim(open_questions)<>''
                  UNION ALL
                  SELECT item_id,version_id,'source-turn-'||id,prompt,created_at FROM source_turns
                    WHERE item_id IN ({marks}) AND json_array_length(answer_json,'$.sections')=0
                ) q JOIN version_titles t ON t.version_id=q.version_id ORDER BY stamp DESC,version_id DESC LIMIT 32
            ''',items*3).fetchall()
        found, seen = [], set()
        for row in rows:
            prompt = next((line.strip() for line in row['prompt'].splitlines() if line.strip()), '')
            if not prompt or len(prompt)>2000 or prompt in seen:
                continue
            seen.add(prompt)
            origin = reading_url(row['item_id'],row['version_id'],row['anchor'])
            key = question_ref(origin,prompt)
            found.append({'key':key,'origin':origin,'title':row['title'],'prompt':prompt})
            if len(found) == 3:
                break
        return found

    def resume(self, item_id, version_id, request_id, key):
        if not isinstance(request_id,str) or not qa.REQUEST_ID.fullmatch(request_id) or not re.fullmatch(r'[a-f0-9]{64}',key or ''):
            raise StudyError(400,'提問識別值無效。')
        try:
            item_id,version_id=lessons.identifier(item_id),lessons.identifier(version_id)
            with self.store._connect() as conn:
                original = conn.execute('SELECT * FROM source_turns WHERE request_id=?',(request_id,)).fetchone()
            if original:
                if (original['item_id'] == item_id and original['version_id'] == version_id and original['origin']
                        and question_ref(original['origin'],original['prompt']) == key):
                    return record(original)
                raise StudyError(409,'同一提問識別值已用於另一份內容。')
            sources=study_context.public_context(self.store,item_id,version_id)['sources']
        except ValueError as exc:
            raise StudyError(400,str(exc)) from None
        prior=next((row for row in self.related_questions(sources) if row['key']==key),None)
        if not prior:
            raise StudyError(409,'上一份問題或閱讀背景已更新，請重新開啟文章核對。')
        return self.ask(item_id,version_id,request_id,prior['prompt'],origin=prior['origin'])

    @staticmethod
    def _existing(conn, request_id, identity):
        row = conn.execute('SELECT * FROM source_turns WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if tuple(row[k] for k in ('item_id','version_id','parent_id','prompt','origin')) != identity:
                raise StudyError(409,'同一提問識別值已用於另一份內容。')
            return record(row)
        return None

    def ask(self, item_id, version_id, request_id, prompt, parent_id=None, *, origin=''):
        if not isinstance(request_id,str) or not qa.REQUEST_ID.fullmatch(request_id):
            raise StudyError(400,'提問識別值無效。')
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>2000:
            raise StudyError(400,'請輸入 1 至 2000 字的問題。')
        try:
            item_id,version_id = lessons.identifier(item_id),lessons.identifier(version_id)
            parent_id = None if parent_id in (None,'') else lessons.identifier(parent_id)
        except ValueError as exc:
            raise StudyError(400,str(exc)) from None
        prompt = prompt.strip()
        identity = item_id,version_id,parent_id,prompt,origin
        parent = None
        with self.store._connect() as conn:
            prior = self._existing(conn,request_id,identity)
            if prior:
                return prior
            if not conn.execute('SELECT 1 FROM item_versions WHERE id=? AND item_id=?',(version_id,item_id)).fetchone():
                raise StudyError(404,'找不到這篇文章的指定版本。')
            if parent_id is not None:
                row = conn.execute('SELECT * FROM source_turns WHERE id=? AND item_id=? AND version_id=?',
                                   (parent_id,item_id,version_id)).fetchone()
                if not row:
                    raise StudyError(400,'上一題不屬於這篇文章的同一版本。')
                parent = record(row)
        try:
            query = source_reader.retrieval_query(prompt,parent)
            context = study_context.public_context(self.store,item_id,version_id,query)
        except ValueError as exc:
            raise StudyError(400,str(exc)) from None
        answer = source_reader.ask(self.local_context(context['sources'],query),prompt,parent)
        # Keep exactly the citations displayed when the question was asked. A
        # later source import can change lexical rankings without changing history.
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = self._existing(conn,request_id,identity)
            if prior:
                return prior
            cursor = conn.execute('INSERT INTO source_turns(item_id,version_id,parent_id,prompt,origin,request_id,answer_json,created_at) '
                                  'VALUES(?,?,?,?,?,?,?,?)',(*identity,request_id,json.dumps(answer,ensure_ascii=False),now_iso()))
            return record(conn.execute('SELECT * FROM source_turns WHERE id=?',(cursor.lastrowid,)).fetchone())
