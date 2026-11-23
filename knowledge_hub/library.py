"""Additive source catalogue, immutable local originals and lexical search.

No fetching or model calls are performed here. Curator records are never reading
versions; only explicitly attached extracted text enters item_versions.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.parse import urlsplit
import zipfile

from .store import KnowledgeStore
from .sources import compute_content_hash, now_iso

MAX_BLOB = 20 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
MIMES = {'application/pdf', 'text/html', 'text/plain'}


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(',', ':'))
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError('Invalid JSON content') from error


def text(value, name, limit=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(f'Invalid {name}')
    return value


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('Invalid identifier / anchor')
    return value


def url(value):
    text(value, 'HTTPS URL', 4096)
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or not p.hostname or p.username is not None
                or p.password is not None or p.port not in (None, 443)
                or any(c.isspace() or ord(c) < 32 for c in value)
                or '\\' in value or any(c in value for c in '<>"')):
            raise ValueError('Invalid HTTPS URL')
    except ValueError as error:
        raise ValueError('Invalid HTTPS URL') from error
    return value


def bounded_json(value):
    if len(encoded(value).encode('utf-8')) > MAX_JSON:
        raise ValueError('JSON input exceeds 16 MiB')
    def walk(v, depth=0, key=''):
        if depth > 24:
            raise ValueError('JSON nesting exceeds limit')
        if isinstance(v, dict):
            if len(v) > 200:
                raise ValueError('Too many fields')
            for k, child in v.items():
                if not isinstance(k, str):
                    raise ValueError('Invalid JSON key')
                walk(child, depth + 1, k)
        elif isinstance(v, list):
            if len(v) > 10000:
                raise ValueError('Too many entries')
            for child in v:
                walk(child, depth + 1, key)
        elif isinstance(v, str):
            if len(v.encode('utf-8')) > MAX_TEXT or '\x00' in v:
                raise ValueError('Invalid / oversized text')
            if v and ('url' in key.lower() and isinstance(v, str)):
                url(v)
        elif v is not None and not isinstance(v, (int, float, bool)):
            raise ValueError('Invalid JSON value')
    walk(value)


@contextmanager
def directory(path: Path, create=False):
    """Walk absolute components with directory FDs; never follow symlinks."""
    if '..' in Path(path).parts:
        raise ValueError('Parent traversal is not allowed')
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_at(fd, name, limit):
    rawfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(rawfd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError('Invalid file type or size')
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError('File exceeds size limit or is empty')
    return raw


class KnowledgeLibrary:
    def __init__(self, store: KnowledgeStore, objects_dir: Path | None = None):
        self.store = store
        self.objects_dir = Path(objects_dir) if objects_dir is not None else store.db_path.parent / 'library' / 'objects'
        if '..' in self.objects_dir.parts:
            raise ValueError('Parent traversal is not allowed in objects_dir')
        self.objects_dir = Path(os.path.abspath(self.objects_dir))
        # Inspect existing ancestors even before the first document is attached.
        candidate = Path(os.path.abspath(self.objects_dir))
        while not candidate.exists() and not candidate.is_symlink():
            candidate = candidate.parent
        with directory(candidate):
            pass
        with store._connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS library_records (
                    category TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(category,id));
                CREATE TABLE IF NOT EXISTS library_concept_sources (
                    concept_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    PRIMARY KEY(concept_id,source_id));
                CREATE TABLE IF NOT EXISTS library_documents (
                    source_id TEXT NOT NULL, blob_sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
                    version_id INTEGER NOT NULL REFERENCES item_versions(id),
                    mapping_json TEXT NOT NULL, acquired_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,blob_sha256));
                CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
                    source_id UNINDEXED, kind UNINDEXED, item_id UNINDEXED,
                    version_id UNINDEXED, anchor UNINDEXED, title, body);
            ''')

    @staticmethod
    def _records(c, category):
        return [json.loads(r[0]) for r in c.execute(
            'SELECT payload FROM library_records WHERE category=? ORDER BY id', (category,))]

    @staticmethod
    def _validate_seed(catalog, concept_map, starter):
        for role, obj, named_version in (
                ('catalog', catalog, 'knowledge-hub-research-seed-v1'),
                ('concept_map', concept_map, 'curated-concepts-v1'),
                ('starter', starter, None)):
            if not isinstance(obj, dict):
                raise ValueError('Seed must contain JSON objects')
            bounded_json(obj)
            if 'schema_version' in obj:
                version = obj['schema_version']
                # Accept only the existing v1 formats for this document's role.
                # Exact types exclude True, 1.0 and other int-like JSON values.
                if not ((type(version) is int and version == 1) or
                        (type(version) is str and named_version is not None and version == named_version)):
                    raise ValueError(f'Unsupported {role} schema_version')
        def indexed(obj, field):
            values = obj.get(field)
            if not isinstance(values, list):
                raise ValueError(f'Missing list: {field}')
            result = {}
            for entry in values:
                if not isinstance(entry, dict):
                    raise ValueError(f'Invalid {field} entry')
                key = identifier(entry.get('id'))
                text(entry.get('title'), 'title', 2000)
                if key in result:
                    raise ValueError(f'Duplicate {field} id: {key}')
                result[key] = entry
            return result
        domains = indexed(catalog, 'domains')
        sources = indexed(catalog, 'sources')
        concepts = indexed(concept_map, 'concepts')
        def references(values, allowed, label):
            if not isinstance(values, list) or len(set(map(str, values))) != len(values):
                raise ValueError(f'Invalid or duplicate {label}')
            if any(not isinstance(v, str) or v not in allowed for v in values):
                raise ValueError(f'Unknown {label}')
        for source in sources.values():
            url(source.get('primary_url'))
            if not isinstance(source.get('primary_domain'), str) or source['primary_domain'] not in domains:
                raise ValueError('Unknown primary domain')
            references(source.get('domains'), domains, 'source domains')
            if source['primary_domain'] not in source['domains']:
                raise ValueError('Primary domain missing from domains')
        for concept in concepts.values():
            if not isinstance(concept.get('domain'), str) or concept['domain'] not in domains:
                raise ValueError('Unknown concept domain')
            references(concept.get('supporting_source_ids'), sources, 'supporting sources')
        relations = concept_map.get('relations')
        entries = starter.get('items')
        if not isinstance(relations, list) or not isinstance(entries, list):
            raise ValueError('Missing relations or starter items')
        relation_rows = {}
        for relation in relations:
            if not isinstance(relation, dict):
                raise ValueError('Invalid relation')
            if any(not isinstance(relation.get(k), str) or relation[k] not in concepts for k in ('source', 'target')):
                raise ValueError('Unknown relation endpoint')
            if (relation.get('type') != 'helpful_background_for'
                    or relation.get('assertion_status') != 'curator_proposal'):
                raise ValueError('Relation must remain a curator_proposal')
            references(relation.get('evidence_for_underlying_concepts'), sources, 'relation evidence')
            key = encoded([relation['source'], relation['target'], relation['type']])
            if key in relation_rows:
                raise ValueError('Duplicate relation')
            relation_rows[key] = relation
        starter_rows, orders = {}, set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('source_id'), str) or entry['source_id'] not in sources:
                raise ValueError('Unknown starter source')
            order = entry.get('order')
            if type(order) is not int or order < 1 or order in orders or entry['source_id'] in starter_rows:
                raise ValueError('Invalid / duplicate starter order or source')
            orders.add(order)
            starter_rows[entry['source_id']] = entry
        rows = [('domain', domains), ('source', sources), ('concept', concepts),
                ('relation', relation_rows), ('starter', starter_rows)]
        # Preserve provenance and all top-level descriptive fields as well.
        for name, obj, excluded in [('catalog', catalog, {'domains', 'sources'}),
                                    ('concept_map', concept_map, {'concepts', 'relations'}),
                                    ('starter', starter, {'items'})]:
            rows.append(('metadata', {name: {k: v for k, v in obj.items() if k not in excluded}}))
        return rows

    def import_seed(self, catalog: dict, concept_map: dict, starter: dict) -> dict:
        rows = self._validate_seed(catalog, concept_map, starter)
        with self.store._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for category, entries in rows:
                for key, entry in entries.items():
                    payload = encoded(entry)
                    old = c.execute('SELECT payload FROM library_records WHERE category=? AND id=?',
                                    (category, key)).fetchone()
                    if old and old[0] != payload:
                        raise ValueError(f'Conflicting immutable {category}: {key}')
                    c.execute('INSERT OR IGNORE INTO library_records VALUES (?,?,?)', (category, key, payload))
            for concept in concept_map['concepts']:
                for sid in concept['supporting_source_ids']:
                    c.execute('INSERT OR IGNORE INTO library_concept_sources VALUES (?,?)', (concept['id'], sid))
            self._index_cards(c)
        return self.stats()

    def _index_cards(self, c):
        c.execute("DELETE FROM library_fts WHERE kind='source_card'")
        concepts = {x['id']: x for x in self._records(c, 'concept')}
        for source in self._records(c, 'source'):
            linked = [concepts[r[0]] for r in c.execute(
                'SELECT concept_id FROM library_concept_sources WHERE source_id=?', (source['id'],))]
            body = '\n'.join([str(source.get(k, '')) for k in
                              ('summary_zh', 'why_fit_zh', 'topics', 'authors_or_org')] +
                             [x['title'] + '\n' + str(x.get('summary_zh') or '') for x in linked])
            c.execute('INSERT INTO library_fts VALUES (?,?,?,?,?,?,?)',
                      (source['id'], 'source_card', None, None, '', source['title'], body))

    def domains(self):
        with self.store._connect() as c:
            return self._records(c, 'domain')

    def sources_for_item(self, item_id):
        with self.store._connect() as c:
            return [dict(r) for r in c.execute("""SELECT DISTINCT d.source_id,
                json_extract(r.payload,'$.title') AS title FROM library_documents d
                JOIN library_records r ON r.category='source' AND r.id=d.source_id
                WHERE d.item_id=? ORDER BY d.source_id""", (item_id,))]

    def stats(self) -> dict:
        with self.store._connect() as c:
            result = {key: c.execute('SELECT count(*) FROM library_records WHERE category=?', (cat,)).fetchone()[0]
                      for key, cat in [('sources', 'source'), ('concepts', 'concept'), ('relations', 'relation')]}
            result['documents'] = c.execute('SELECT count(*) FROM library_documents').fetchone()[0]
            return result

    def list_sources(self, query='', domain='', starter_only=False) -> list[dict]:
        if not isinstance(query, str) or not isinstance(domain, str):
            raise ValueError('Query and domain must be strings')
        hits = {x['source_id'] for x in self.search(query)} if query.strip() else None
        with self.store._connect() as c:
            starters = {x['source_id']: x for x in self._records(c, 'starter')}
            records = self._records(c, 'source')
            for r in records:
                r['documents'] = self._documents(c, r['id'])
                r['starter'] = starters.get(r['id'])
        records = [r for r in records if (hits is None or r['id'] in hits)
                   and (not domain or domain in r['domains']) and (not starter_only or r['starter'])]
        return sorted(records, key=lambda r: (r['starter']['order'] if r['starter'] else 100000, r['title'].casefold()))

    def _documents(self, c, sid):
        result = []
        for row in c.execute('SELECT * FROM library_documents WHERE source_id=? ORDER BY version_id', (sid,)):
            d = dict(row)
            d.update(json.loads(d.pop('mapping_json')))
            result.append(d)
        return result

    def get_source(self, source_id) -> dict | None:
        with self.store._connect() as c:
            row = c.execute("SELECT payload FROM library_records WHERE category='source' AND id=?", (source_id,)).fetchone()
            if not row:
                return None
            source = json.loads(row[0])
            source['documents'] = self._documents(c, source_id)
            concepts = self._records(c, 'concept')
            mine = {x['id'] for x in concepts if source_id in x['supporting_source_ids']}
            source['concepts'] = [x for x in concepts if x['id'] in mine]
            relevant = {x: ('同概念：' + next(z['title'] for z in concepts if z['id'] == x)) for x in mine}
            for rel in self._records(c, 'relation'):
                if rel['source'] in mine:
                    relevant[rel['target']] = '一跳建議背景（此來源為前置）：' + str(rel.get('note_zh') or '')
                if rel['target'] in mine:
                    relevant[rel['source']] = '一跳建議背景（可先讀此背景）：' + str(rel.get('note_zh') or '')
            related = {}
            titles = {s['id']: s['title'] for s in self._records(c, 'source')}
            for concept in concepts:
                if concept['id'] in relevant:
                    for sid in concept['supporting_source_ids']:
                        if sid != source_id:
                            related[sid] = {'source_id': sid, 'title': titles[sid],
                                            'reason': relevant[concept['id']], 'assertion_status': 'curator_proposal'}
            source['related'] = sorted(related.values(), key=lambda x: x['source_id'])
            starter = c.execute("SELECT payload FROM library_records WHERE category='starter' AND id=?", (source_id,)).fetchone()
            source['starter'] = json.loads(starter[0]) if starter else None
            return source

    def validate_document(self, source_id, raw, *, media_type, source_url, version_label,
                          blocks, scope='partial', extraction_note='', rights_note=''):
        identifier(source_id)
        if self.get_source(source_id) is None:
            raise ValueError('Unknown document source')
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BLOB:
            raise ValueError('Original must be 1..20 MiB bytes')
        if not isinstance(media_type, str) or media_type not in MIMES or (media_type == 'application/pdf' and not raw.startswith(b'%PDF-')):
            raise ValueError('Invalid media type or PDF magic')
        url(source_url)
        text(version_label, 'version label', 200)
        if not isinstance(scope, str) or scope not in {'partial', 'abstract', 'full'}:
            raise ValueError('Invalid extraction scope')
        if not isinstance(blocks, list) or not blocks or len(blocks) > 10000:
            raise ValueError('Nonempty bounded blocks required')
        anchors, total = set(), 0
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {'anchor', 'text', 'source_url'}:
                raise ValueError('Blocks require anchor, text, source_url')
            anchor = identifier(block['anchor'])
            if anchor in anchors:
                raise ValueError('Duplicate block anchor')
            anchors.add(anchor)
            text(block['text'], 'block text', MAX_TEXT)
            total += len(block['text'].encode('utf-8'))
            url(block['source_url'])
        if total > MAX_TEXT:
            raise ValueError('Extracted text exceeds 2 MiB')
        for note in (extraction_note, rights_note):
            if not isinstance(note, str) or len(note) > 10000 or '\x00' in note:
                raise ValueError('Invalid document note')
        mapping = dict(media_type=media_type, source_url=source_url, version_label=version_label,
                       blocks=blocks, scope=scope, extraction_note=extraction_note, rights_note=rights_note)
        digest = hashlib.sha256(raw).hexdigest()
        with self.store._connect() as c:
            old = c.execute('SELECT mapping_json FROM library_documents WHERE source_id=? AND blob_sha256=?',
                            (source_id, digest)).fetchone()
            if old and old[0] != encoded(mapping):
                raise ValueError('Same original has conflicting text/version mapping')
            mime = c.execute('SELECT media_type FROM library_documents WHERE blob_sha256=? LIMIT 1', (digest,)).fetchone()
            if mime and mime[0] != media_type:
                raise ValueError('Original media type conflict')
        return digest, mapping

    def _write_blob(self, digest, raw):
        with directory(self.objects_dir / digest[:2], create=True) as fd:
            try:
                present = read_at(fd, digest, MAX_BLOB)
            except FileNotFoundError:
                present = None
            if present is not None:
                if hashlib.sha256(present).hexdigest() != digest:
                    raise ValueError('Original hash mismatch')
                return
            name = '.tmp-' + os.urandom(16).hex()
            tempfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(tempfd, 'wb') as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                # link is an atomic no-clobber publication of the complete temporary file.
                try:
                    os.link(name, digest, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    if hashlib.sha256(read_at(fd, digest, MAX_BLOB)).hexdigest() != digest:
                        raise ValueError('Original hash mismatch')
                os.fsync(fd)
            finally:
                os.unlink(name, dir_fd=fd)

    @staticmethod
    def _item_key(source):
        candidates = [source.get('arxiv_id'), source.get('arxiv'), source.get('primary_url')]
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            match = re.fullmatch(r'(?:https://(?:export\.)?arxiv\.org/(?:abs|pdf|html)/)?((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7}))(?:v\d+)?(?:\.pdf)?', candidate)
            if match:
                return 'arxiv:' + match[1]
        return 'library:' + source['id']

    def add_document(self, source_id, raw: bytes, *, media_type, source_url,
                     version_label, blocks, scope='partial', extraction_note='',
                     rights_note='') -> dict:
        digest, mapping = self.validate_document(source_id, raw, media_type=media_type,
            source_url=source_url, version_label=version_label, blocks=blocks, scope=scope,
            extraction_note=extraction_note, rights_note=rights_note)
        source = self.get_source(source_id)
        with self.store._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT mapping_json FROM library_documents WHERE source_id=? AND blob_sha256=?', (source_id, digest)).fetchone()
            if old:
                if old[0] != encoded(mapping):
                    raise ValueError('Original mapping conflict')
                self.read_blob(digest)
                return next(d for d in self._documents(c, source_id) if d['blob_sha256'] == digest)
            mime = c.execute('SELECT media_type FROM library_documents WHERE blob_sha256=? LIMIT 1', (digest,)).fetchone()
            if mime and mime[0] != mapping['media_type']:
                raise ValueError('Original media type conflict')
            key, ts = self._item_key(source), now_iso()
            content_hash = compute_content_hash(source['title'], mapping['scope'], mapping['version_label'], mapping['blocks'])
            item = c.execute('SELECT id FROM items WHERE key=?', (key,)).fetchone()
            item_id = item[0] if item else None
            version = c.execute('SELECT * FROM item_versions WHERE item_id=? AND content_hash=?', (item_id, content_hash)).fetchone() if item else None
            if version and (json.loads(version['blocks_json']) != mapping['blocks']
                            or version['source_url'] != mapping['source_url']):
                raise ValueError('Existing text version has conflicting source mapping')
            self._write_blob(digest, raw)
            if item is None:
                item_id = c.execute('INSERT INTO items(key,source_name,title,created_at,updated_at) VALUES (?,?,?,?,?)',
                                    (key, 'library', source['title'], ts, ts)).lastrowid
            if version:
                version_id = version['id']
            else:
                version_id = c.execute('''INSERT INTO item_versions(item_id,label,scope,source_url,author,published_at,fetched_at,content_hash,blocks_json)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (item_id, mapping['version_label'], mapping['scope'], mapping['source_url'],
                    str(source.get('authors_or_org', '')), str(source.get('year', '')), ts, content_hash, json.dumps(mapping['blocks'], ensure_ascii=False))).lastrowid
                c.execute('INSERT INTO version_titles(version_id,title) VALUES (?,?)', (version_id, source['title']))
            current = c.execute('SELECT fulltext_status FROM items WHERE id=?', (item_id,)).fetchone()[0]
            status = 'ready' if mapping['scope'] == 'full' else ('partial' if mapping['scope'] == 'partial' else 'not_requested')
            if current != 'ready' and (current != 'partial' or status == 'ready'):
                c.execute('UPDATE items SET fulltext_status=?,updated_at=? WHERE id=?', (status, ts, item_id))
            c.execute('INSERT INTO library_documents VALUES (?,?,?,?,?,?,?)',
                      (source_id, digest, mapping['media_type'], item_id, version_id, encoded(mapping), ts))
            # Several originals can share one text version. Index it once per source.
            exists = c.execute("SELECT 1 FROM library_fts WHERE source_id=? AND version_id=? AND kind='document'", (source_id, version_id)).fetchone()
            if not exists:
                for b in mapping['blocks']:
                    c.execute('INSERT INTO library_fts VALUES (?,?,?,?,?,?,?)',
                              (source_id, 'document', item_id, version_id, b['anchor'], source['title'], b['text']))
            return next(d for d in self._documents(c, source_id) if d['blob_sha256'] == digest)

    def _read_digest(self, digest):
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise ValueError('Invalid SHA256')
        with directory(self.objects_dir / digest[:2]) as fd:
            raw = read_at(fd, digest, MAX_BLOB)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Original hash mismatch')
        return raw

    def read_blob(self, sha256) -> tuple[bytes, str]:
        if not isinstance(sha256, str) or not SHA.fullmatch(sha256):
            raise ValueError('Invalid SHA256')
        with self.store._connect() as c:
            row = c.execute('SELECT media_type FROM library_documents WHERE blob_sha256=? LIMIT 1', (sha256,)).fetchone()
        if row is None:
            raise ValueError('Unregistered original')
        return self._read_digest(sha256), row[0]

    def search(self, query) -> list[dict]:
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError('Search exceeds 2000 characters')
        query = query.strip()
        if not query:
            return []
        tokens = re.findall(r'\w+', query, re.UNICODE)
        expression = ' OR '.join('"' + token.replace('"', '""') + '"' for token in tokens)
        with self.store._connect() as c:
            ranked = list(c.execute('SELECT rowid,* FROM library_fts WHERE library_fts MATCH ? ORDER BY bm25(library_fts),rowid', (expression,))) if expression else []
            # Literal substring retains Traditional Chinese matching; LIKE wildcards are escaped.
            literal = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            extra = c.execute("SELECT rowid,* FROM library_fts WHERE title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' ORDER BY rowid", (literal, literal))
            seen, results = set(), []
            for row in [*ranked, *extra]:
                if row['rowid'] in seen:
                    continue
                seen.add(row['rowid'])
                body = row['body']
                pos = body.casefold().find(query.casefold())
                start = max(0, pos - 60)
                hit = {'source_id': row['source_id'], 'title': row['title'], 'kind': row['kind'],
                       'snippet': body[start:start + 240]}
                if row['kind'] == 'document':
                    hit.update(item_id=row['item_id'], version_id=row['version_id'], anchor=row['anchor'])
                results.append(hit)
            return results

    def export_metadata(self):
        with self.store._connect() as c:
            result = {name: self._records(c, category) for name, category in
                      [('domains', 'domain'), ('sources', 'source'), ('concepts', 'concept'),
                       ('relations', 'relation'), ('starter', 'starter')]}
            result['schema_version'] = 1
            result['starter'].sort(key=lambda entry: entry['order'])
            result['metadata'] = {r['id']: json.loads(r['payload']) for r in c.execute(
                "SELECT id,payload FROM library_records WHERE category='metadata' ORDER BY id")}
            result['documents'] = [d for source in result['sources'] for d in self._documents(c, source['id'])]
            return result

    def backup(self, destination: Path) -> dict:
        destination = Path(destination)
        if '..' in destination.parts:
            raise ValueError('Parent traversal is not allowed in backup destination')
        destination = Path(os.path.abspath(destination))
        with directory(self.store.db_path.parent):
            pass
        # Build from an independent SQLite snapshot; objects are immutable and verified
        # against the document registrations in that snapshot, not the live connection.
        with directory(destination.parent) as output_fd:
            if os.path.lexists(destination):
                raise FileExistsError(destination)
            # Temporary SQLite stays beside the database, never in the repository by default.
            with tempfile.TemporaryDirectory(prefix='.library-backup-', dir=self.store.db_path.parent) as tmp:
                snapshot = Path(tmp) / 'knowledge.sqlite3'
                with self.store._connect() as src:
                    target = sqlite3.connect(snapshot)
                    try:
                        src.backup(target)
                        blobs = target.execute('SELECT DISTINCT blob_sha256 FROM library_documents ORDER BY blob_sha256').fetchall()
                    finally:
                        target.close()
                name = '.backup-' + os.urandom(16).hex()
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=output_fd)
                try:
                    manifest = {'schema_version': 1, 'created_at': now_iso(), 'contains_private_data': True, 'files': []}
                    with os.fdopen(fd, 'w+b') as stream:
                        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                            dbraw = snapshot.read_bytes()
                            archive.writestr('knowledge.sqlite3', dbraw)
                            manifest['files'].append({'path': 'knowledge.sqlite3', 'sha256': hashlib.sha256(dbraw).hexdigest()})
                            for (digest,) in blobs:
                                raw = self._read_digest(digest)
                                path = f'objects/{digest[:2]}/{digest}'
                                archive.writestr(path, raw)
                                manifest['files'].append({'path': path, 'sha256': digest})
                            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(name, destination.name, src_dir_fd=output_fd, dst_dir_fd=output_fd, follow_symlinks=False)
                    os.fsync(output_fd)
                finally:
                    os.unlink(name, dir_fd=output_fd)
        return {'output': str(destination), 'documents': len(blobs), 'manifest': manifest}


def load_json(path):
    with directory(path.parent) as fd:
        raw = read_at(fd, path.name, MAX_JSON)
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f'Duplicate JSON key: {key}')
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        bounded_json(value)
        return value
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Invalid JSON: {path.name}') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    imp = commands.add_parser('import')
    for flag in ('catalog', 'concepts', 'starter'):
        imp.add_argument('--' + flag, type=Path, required=True)
    attach = commands.add_parser('attach')
    attach.add_argument('--manifest', type=Path, required=True)
    back = commands.add_parser('backup')
    back.add_argument('--output', type=Path, required=True)
    commands.add_parser('stats')
    args = parser.parse_args(argv)
    try:
        # Read/bound all external inputs before creating a database or processing files.
        if args.command == 'import':
            seed = [load_json(getattr(args, k)) for k in ('catalog', 'concepts', 'starter')]
            KnowledgeLibrary._validate_seed(*seed)
        elif args.command == 'attach':
            manifest = load_json(args.manifest)
            if not isinstance(manifest, dict) or set(manifest) != {'documents'} or not isinstance(manifest['documents'], list) or len(manifest['documents']) > 12:
                raise ValueError('Manifest requires at most 12 documents')
            prepared, total = [], 0
            for entry in manifest['documents']:
                if not isinstance(entry, dict) or not {'source_id', 'blob_path', 'media_type', 'source_url', 'version_label', 'blocks'} <= set(entry) or set(entry) - {'source_id', 'blob_path', 'media_type', 'source_url', 'version_label', 'blocks', 'scope', 'extraction_note', 'rights_note'}:
                    raise ValueError('Invalid manifest document fields')
                relative = entry['blob_path']
                if not isinstance(relative, str) or not relative or '\\' in relative:
                    raise ValueError('Invalid relative blob_path')
                path = Path(relative)
                if path.is_absolute() or '..' in path.parts or not path.name:
                    raise ValueError('blob_path must stay inside manifest directory')
                with directory(args.manifest.parent / path.parent) as fd:
                    raw = read_at(fd, path.name, MAX_BLOB)
                total += len(raw)
                if total > 100 * 1024 * 1024:
                    raise ValueError('Manifest originals exceed 100 MiB')
                prepared.append((entry['source_id'], raw, {k: v for k, v in entry.items() if k not in {'source_id', 'blob_path'}}))
        lib = KnowledgeLibrary(KnowledgeStore(args.db))
        if args.command == 'import':
            result = lib.import_seed(*seed)
        elif args.command == 'attach':
            seen, media, versions = {}, {}, {}
            for sid, raw, fields in prepared:
                digest, mapping = lib.validate_document(sid, raw, **fields)
                if (sid, digest) in seen and seen[sid, digest] != encoded(mapping):
                    raise ValueError('Conflicting duplicate manifest document')
                seen[sid, digest] = encoded(mapping)
                if digest in media and media[digest] != mapping['media_type']:
                    raise ValueError('Conflicting manifest original media type')
                media[digest] = mapping['media_type']
                source = lib.get_source(sid)
                key = lib._item_key(source)
                content_hash = compute_content_hash(source['title'], mapping['scope'], mapping['version_label'], mapping['blocks'])
                vkey = (key, content_hash)
                vmapping = encoded([mapping['blocks'], mapping['source_url']])
                if vkey in versions and versions[vkey] != vmapping:
                    raise ValueError('Conflicting manifest text source mapping')
                versions[vkey] = vmapping
                with lib.store._connect() as c:
                    old = c.execute('''SELECT v.blocks_json,v.source_url FROM item_versions v
                        JOIN items i ON i.id=v.item_id WHERE i.key=? AND v.content_hash=?''',
                        (key, content_hash)).fetchone()
                    if old and encoded([json.loads(old[0]), old[1]]) != vmapping:
                        raise ValueError('Existing text version has conflicting source mapping')
                # Missing objects are normal for new documents; any existing file
                # (including a symlink or damaged file) must pass safe hash reading.
                object_path = lib.objects_dir / digest[:2] / digest
                if os.path.lexists(object_path):
                    lib._read_digest(digest)
                ancestor = object_path.parent
                while not ancestor.exists() and not ancestor.is_symlink():
                    ancestor = ancestor.parent
                with directory(ancestor):
                    pass
            result = {'documents': [lib.add_document(sid, raw, **fields) for sid, raw, fields in prepared]}
        elif args.command == 'backup':
            result = lib.backup(args.output)
        else:
            result = lib.stats()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError, UnicodeError) as error:
        parser.exit(1, f'library: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
