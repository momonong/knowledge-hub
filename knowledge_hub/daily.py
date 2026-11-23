"""Publish a dated set of reading cards atomically, after public collection."""
from datetime import datetime, timezone, timedelta
import json
import logging
import sqlite3
from time import time

from .sources import now_iso

TAIPEI = timezone(timedelta(hours=8), 'Asia/Taipei')
PICK_COUNT = 5
_BRIEF_ATTEMPTS = 2
_BRIEF_LEASE_SECONDS = 120


def attached_versions(conn):
    """SQL subquery for curator-provided originals, including on a public item."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_documents'").fetchone()
    return 'SELECT version_id FROM library_documents' if exists else 'SELECT NULL WHERE 0'


def taipei_now():
    return datetime.now(TAIPEI)


def _daily_cutoff(stamp):
    return stamp.replace(hour=7, minute=0, second=0, microsecond=0)


def _taipei_timestamp(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(TAIPEI).isoformat(timespec='seconds')


def run_hosted_refresh(refresh, stop, *, now=taipei_now):
    """Wake at the next Taipei 07:00, and recover a missed run after startup."""
    delay = 1
    settled_day = None
    while not stop.wait(delay):
        stamp = now().astimezone(TAIPEI)
        cutoff = _daily_cutoff(stamp)
        if stamp >= cutoff:
            if settled_day != stamp.date():
                try:
                    result = refresh()
                    # After a restart an interrupted run can retain its lease.
                    # Poll until its receipt settles or storage permits recovery.
                    if not result or result.get('state') != 'running':
                        settled_day = stamp.date()
                except Exception:
                    logging.getLogger(__name__).error('Daily refresh failed; see the local refresh status')
            cutoff += timedelta(days=1)
        delay = min(600, (cutoff - stamp).total_seconds())


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_refresh (
          day TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT NOT NULL,
          finished_at TEXT, error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS daily_cards (
          day TEXT NOT NULL, position INTEGER NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
          version_id INTEGER NOT NULL REFERENCES item_versions(id), card TEXT NOT NULL,
          PRIMARY KEY(day, position), UNIQUE(day, item_id));
        CREATE TABLE IF NOT EXISTS source_failures (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, attempted_at TEXT NOT NULL, error TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_choices (
          day TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
          version_id INTEGER NOT NULL REFERENCES item_versions(id), choice TEXT NOT NULL,
          previous_status TEXT NOT NULL, PRIMARY KEY(day,item_id,version_id));
        CREATE TABLE IF NOT EXISTS daily_status_owner (
          item_id INTEGER PRIMARY KEY REFERENCES items(id), day TEXT NOT NULL, version_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_brief_upgrades (
          day TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
          card_version_id INTEGER NOT NULL REFERENCES item_versions(id),
          version_id INTEGER NOT NULL REFERENCES item_versions(id),
          format_version INTEGER NOT NULL, mode TEXT NOT NULL,
          state TEXT NOT NULL, attempts INTEGER NOT NULL, started REAL NOT NULL,
          payload TEXT,
          PRIMARY KEY(day,item_id,card_version_id,version_id,format_version,mode));
        CREATE TRIGGER IF NOT EXISTS daily_release_status_ownership AFTER UPDATE OF status ON items
          BEGIN DELETE FROM daily_status_owner WHERE item_id=NEW.id; END;
    """)
    if 'format_version' not in {row[1] for row in conn.execute('PRAGMA table_info(daily_refresh)')}:
        conn.execute('ALTER TABLE daily_refresh ADD COLUMN format_version INTEGER NOT NULL DEFAULT 0')


class DailyRefresh:
    def __init__(self, store):
        self.store = store

    def day(self):
        return taipei_now().date().isoformat()

    def status(self):
        with self.store._connect() as conn:
            row = conn.execute('SELECT * FROM daily_refresh WHERE day=?', (self.day(),)).fetchone()
        return dict(row) if row else None

    def due(self):
        return self.status() is None

    def report(self):
        """Build a read-only view of the schedule and the currently visible briefing."""
        stamp = taipei_now()
        day = stamp.date().isoformat()
        cutoff = _daily_cutoff(stamp)
        if stamp >= cutoff:
            cutoff += timedelta(days=1)
        with self.store._connect() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT * FROM daily_refresh WHERE day=?', (day,)).fetchone()
            latest = conn.execute('SELECT MAX(day) FROM daily_cards WHERE day<=?', (day,)).fetchone()[0]
            picks = self._read_selection(conn, latest) if latest else []
            sources = self.store._read_sources(conn)
            failures = self._read_source_failures(conn)
        today = dict(row) if row else None
        if today:
            for field in ('started_at', 'finished_at'):
                today[field + '_taipei'] = _taipei_timestamp(today[field])
        for source in sources.values():
            for field in ('last_fetched_at', 'last_success_at'):
                source[field + '_taipei'] = _taipei_timestamp(source[field])
        for failure in failures:
            failure['attempted_at_taipei'] = _taipei_timestamp(failure['attempted_at'])
        return {'timezone': TAIPEI.tzname(stamp), 'local_date': day,
                'next_scheduled_at': cutoff.isoformat(timespec='seconds'), 'today': today,
                'selection_day': latest, 'cards': [self._card_receipt(pick) for pick in picks[:PICK_COUNT]],
                'sources': sources, 'recent_failures': failures}

    @staticmethod
    def _card_receipt(pick):
        item, version = pick['item'], pick['version']
        brief = pick.get('brief') or {}
        state = brief.get('state', 'unavailable')
        mode = {'degraded': 'abstract_fallback', 'ready': 'model' if brief.get('mode') == 'model' else 'local'}
        return {'item_id': item['id'], 'title': version.get('title') or item['title'],
                'version_id': str(version['id']), 'version_label': version['label'], 'scope': version['scope'],
                'brief': {'state': state, 'mode': mode.get(state, 'unavailable'),
                          **{field: brief.get(field) for field in ('model', 'ttft_ms', 'total_ms')}}}

    def selection(self, day, *, resolved=True):
        with self.store._connect() as conn:
            return self._read_selection(conn,day,resolved=resolved)

    @staticmethod
    def _read_selection(conn, day, *, resolved=True):
        attachment_ids = attached_versions(conn)
        # The original card version remains the key for choices and
        # bookmarks. A successful upgrade changes only what is displayed.
        overlay = f"""LEFT JOIN daily_brief_upgrades u ON u.rowid = (
                                SELECT b.rowid FROM daily_brief_upgrades b JOIN item_versions v ON v.id=b.version_id
                                WHERE b.day=c.day AND b.item_id=c.item_id AND b.card_version_id=c.version_id
                                  AND b.state='ready' AND b.version_id NOT IN ({attachment_ids})
                                ORDER BY CASE v.scope WHEN 'full' THEN 2 WHEN 'partial' THEN 1 ELSE 0 END DESC,
                                  (b.mode='model') DESC,b.format_version DESC,v.id DESC LIMIT 1)""" if resolved else ''
        rows = conn.execute(f"""SELECT c.card,i.status,
                                 CASE WHEN d.choice='later' AND i.status<>'later' THEN '' ELSE COALESCE(d.choice,'') END AS choice,
                                 {'u.payload AS upgrade' if resolved else 'NULL AS upgrade'}
                               FROM daily_cards c JOIN items i ON i.id=c.item_id
                               LEFT JOIN daily_choices d USING(day,item_id,version_id)
                               {overlay}
                               WHERE c.day=? AND i.source_name<>'library'
                                 AND c.version_id NOT IN ({attachment_ids})
                               ORDER BY c.position""", (day,))
        result = []
        for row in rows:
            pick = json.loads(row['card'])
            if row['upgrade']:
                pick.update(json.loads(row['upgrade']))
            pick['item']['status'] = row['status']
            pick['choice'] = row['choice']
            result.append(pick)
        return result

    def _health(self, conn, day):
        picks = self._read_selection(conn,day)
        if len(picks) != PICK_COUNT:
            return 'partial', f'合格候選 {len(picks)} 篇，目標 {PICK_COUNT} 篇。'
        pending_sources = conn.execute(
            "SELECT 1 FROM sources WHERE state NOT IN ('updated','unchanged') LIMIT 1").fetchone()
        if pending_sources or any((pick.get('brief') or {}).get('state') != 'ready' for pick in picks):
            return 'partial', ''
        return 'completed', ''

    def reconcile_health(self, day):
        """Reevaluate today's settled report after brief repair, preserving its timestamps."""
        with self.store._connect() as conn:
            today = day == self.day()
            if today:
                conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM daily_refresh WHERE day=?',(day,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            if today and day == self.day() and not row['error'] and row['state'] in ('partial','completed'):
                state, _ = self._health(conn,day)
                if state != row['state']:
                    conn.execute('UPDATE daily_refresh SET state=? WHERE day=?',(state,day))
                    result['state'] = state
            return result

    def choose(self, day, item_id, version_id, choice):
        if choice not in ('skip','later','undo'):
            raise ValueError('無效的每日選文操作。')
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            attachments = attached_versions(conn)
            card = conn.execute(f'''SELECT i.status,c.version_id FROM daily_cards c JOIN items i ON i.id=c.item_id
                                    WHERE c.day=? AND c.item_id=? AND i.source_name<>'library'
                                      AND c.version_id NOT IN ({attachments}) AND
                                    (c.version_id=? OR EXISTS (
                                        SELECT 1 FROM daily_brief_upgrades b
                                        WHERE b.day=c.day AND b.item_id=c.item_id AND b.card_version_id=c.version_id
                                          AND b.version_id=? AND b.state='ready'
                                          AND b.version_id NOT IN ({attachments})))''',
                                (day,item_id,version_id,version_id)).fetchone()
            if card is None:
                raise ValueError('這一天的精選沒有這個文章版本。')
            key = (day, item_id, card['version_id'])
            old = conn.execute('SELECT * FROM daily_choices WHERE day=? AND item_id=? AND version_id=?', key).fetchone()
            if old and old['choice'] == choice and (choice != 'later' or card['status'] == 'later'):
                return
            owns = conn.execute('SELECT 1 FROM daily_status_owner WHERE day=? AND item_id=? AND version_id=?',key).fetchone()
            if old and old['choice'] == 'later' and owns:
                conn.execute('UPDATE items SET status=?,updated_at=? WHERE id=?',
                             (old['previous_status'], now_iso(), item_id))
            if choice == 'undo':
                conn.execute('DELETE FROM daily_choices WHERE day=? AND item_id=? AND version_id=?', key)
                return
            conn.execute('''INSERT INTO daily_choices VALUES (?,?,?,?,?)
                            ON CONFLICT(day,item_id,version_id) DO UPDATE SET choice=excluded.choice,
                            previous_status=excluded.previous_status''', (*key, choice, card['status']))
            if choice == 'later':
                conn.execute("UPDATE items SET status='later',updated_at=? WHERE id=?", (now_iso(), item_id))
                conn.execute('INSERT INTO daily_status_owner(item_id,day,version_id) VALUES (?,?,?)', (item_id,day,version_id))

    def latest_day(self):
        with self.store._connect() as conn:
            return conn.execute('SELECT MAX(day) FROM daily_cards WHERE day<=?', (self.day(),)).fetchone()[0]

    def recent_keys(self, day, *, include_today=False):
        with self.store._connect() as conn:
            attachments = attached_versions(conn)
            return {row[0] for row in conn.execute(f'''SELECT i.key FROM daily_cards c JOIN items i ON i.id=c.item_id
                                  WHERE c.day >= date(?, '-30 days') AND
                                    (c.day < ? OR (? AND c.day=? AND i.source_name<>'library'
                                      AND c.version_id NOT IN ({attachments})))''', (day,day,include_today,day))}

    def source_failures(self):
        with self.store._connect() as conn:
            return self._read_source_failures(conn)

    @staticmethod
    def _read_source_failures(conn):
        return [dict(r) for r in conn.execute('SELECT name, attempted_at, error FROM source_failures ORDER BY id DESC LIMIT 50')]

    def _brief_targets(self, day, item_id):
        with self.store._connect() as conn:
            attachments = attached_versions(conn)
            return conn.execute(f"""SELECT c.item_id,c.version_id AS card_version_id,v.id AS target_id
                FROM daily_cards c JOIN items i ON i.id=c.item_id
                JOIN item_versions original ON original.id=c.version_id JOIN item_versions v
                  ON v.item_id=c.item_id AND v.label=original.label
                WHERE c.day=? AND (? IS NULL OR c.item_id=?) AND i.source_name<>'library'
                  AND c.version_id NOT IN ({attachments}) AND v.id NOT IN ({attachments})
                  AND (v.id=original.id OR (i.key LIKE 'arxiv:%' AND
                    (v.scope='full' OR (v.scope='partial' AND original.scope<>'full'))))
                ORDER BY c.position,CASE v.scope WHEN 'full' THEN 2 WHEN 'partial' THEN 1 ELSE 0 END DESC,v.id DESC
                """,(day,item_id,item_id)).fetchall()

    def _claim_brief(self, key, manual):
        clock = time()
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT state,attempts,started FROM daily_brief_upgrades
                WHERE (day,item_id,card_version_id,version_id,format_version,mode)=(?,?,?,?,?,?)''', key).fetchone()
            if row:
                if row['state']=='ready' or (row['state']=='running' and clock-row['started']<_BRIEF_LEASE_SECONDS):
                    return None
                if not manual and row['attempts'] >= _BRIEF_ATTEMPTS:
                    return None
            attempt = row['attempts'] + 1 if row else 1
            conn.execute('''INSERT INTO daily_brief_upgrades VALUES (?,?,?,?,?,?,'running',?,?,NULL)
                ON CONFLICT(day,item_id,card_version_id,version_id,format_version,mode) DO UPDATE
                  SET state='running',attempts=excluded.attempts,started=excluded.started''', (*key,attempt,clock))
        return attempt

    def upgrade_briefs(self, day, resolve, generate, *, format_version, mode, item_id=None, manual=False, freshly_built=()):
        """Refresh fixed-source briefs without replacing a published card on failure."""
        visible = {pick['item']['id']:pick for pick in self.selection(day)}
        handled = set()
        for target in self._brief_targets(day,item_id):
            card_id = target['item_id']
            if card_id in handled or card_id not in visible:
                continue
            version_id = str(target['target_id'])
            current = visible[card_id]
            if (card_id, version_id) in freshly_built:
                handled.add(card_id)
                continue
            same_version = str(current['version']['id']) == version_id
            brief = current.get('brief') or {}
            model_ready = brief.get('mode') == 'model'
            if (same_version and brief.get('state') == 'ready' and
                    (mode == 'local' and model_ready or
                     current.get('brief_format') == format_version and (mode == 'local' or model_ready))):
                handled.add(card_id)
                continue
            edition = resolve(card_id,version_id)
            if not edition:
                continue
            handled.add(card_id)
            key = (day,card_id,target['card_version_id'],version_id,format_version,mode)
            attempt = self._claim_brief(key,manual)
            if attempt is None:
                continue
            public_item, version = edition
            candidate = {'item':public_item,'version':version,
                         'category':current.get('category',''),'spectrum_reason':current.get('spectrum_reason','')}
            try:
                result = generate(candidate)
                payload = None
                if result['state'] == 'ready':
                    if current['version'].get('published_at'):
                        version = {**version,'published_at':current['version']['published_at']}
                    version = {name:version[name] for name in ('id','label','scope','published_at','source_url')}
                    payload = json.dumps({'version':version,'brief':result,'brief_format':format_version}, ensure_ascii=False)
            except Exception as error:
                # A generator failure may contain model settings or raw source.
                # Release the reservation and leave the previously visible card.
                logging.getLogger(__name__).error('Daily brief upgrade failed (%s)', type(error).__name__)
                payload = None
            with self.store._connect() as conn:
                conn.execute('''UPDATE daily_brief_upgrades SET state=?,payload=?
                    WHERE (day,item_id,card_version_id,version_id,format_version,mode)=(?,?,?,?,?,?)
                      AND attempts=? AND state='running' ''',
                             ('ready' if payload else 'failed',payload,*key,attempt))

    def run(self, sync, *, force=False, day=None):
        day = day or self.day()
        started = now_iso()
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM daily_refresh WHERE day=?", (day,)).fetchone()
            if row:
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
                active = row['state'] == 'running' and row['started_at'] > cutoff
                if active or (row['state'] != 'running' and row['format_version'] == 1 and not force):
                    return dict(row)
            conn.execute("""INSERT INTO daily_refresh(day,state,started_at,format_version) VALUES (?,'running',?,1)
                         ON CONFLICT(day) DO UPDATE SET state='running', started_at=excluded.started_at,
                         finished_at=NULL, error='', format_version=1""", (day, started))
        cards = None
        state, error = 'completed', ''
        try:
            cards = sync()
        except (OSError, ValueError, sqlite3.Error) as failure:
            state, error = 'failed', f'公開來源或快取更新失敗（{type(failure).__name__}）。'
        except Exception:
            self._finish(day, started, 'failed', '背景更新中斷，可從首頁重試。', None)
            raise
        return self._finish(day, started, state, error, cards)

    def _finish(self, day, started, state, error, cards):
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            updated = conn.execute("""UPDATE daily_refresh SET state=?, finished_at=?, error=?
                                     WHERE day=? AND state='running' AND started_at=?""",
                                   (state, now_iso(), error, day, started)).rowcount
            if updated:
                conn.execute(f'''DELETE FROM daily_cards WHERE day=? AND
                            (version_id IN ({attached_versions(conn)}) OR
                             item_id IN (SELECT id FROM items WHERE source_name='library'))''', (day,))
            if updated and cards:
                # A partial day may fill vacant slots after a source recovers.
                # Existing cards, bookmarks, and choices keep their identities.
                existing = conn.execute('SELECT item_id,position FROM daily_cards WHERE day=?',(day,)).fetchall()
                used = {entry['item_id'] for entry in existing}
                position = max((entry['position'] for entry in existing),default=0)
                attachments = attached_versions(conn)
                for pick in cards:
                    item_id, version_id = pick['item']['id'], pick['version']['id']
                    if len(used) >= PICK_COUNT:
                        break
                    if item_id in used:
                        continue
                    if not conn.execute(f'''SELECT 1 FROM item_versions v JOIN items i ON i.id=v.item_id
                        WHERE v.id=? AND i.id=? AND i.source_name<>'library'
                          AND v.id NOT IN ({attachments})''',(version_id,item_id)).fetchone():
                        continue
                    data = {**pick,'version':{k:v for k,v in pick['version'].items() if k != 'blocks'}}
                    position += 1
                    conn.execute('INSERT INTO daily_cards VALUES (?,?,?,?,?)',
                                 (day,position,item_id,version_id,json.dumps(data,ensure_ascii=False)))
                    used.add(item_id)
            if updated and state == 'completed':
                state, error = self._health(conn,day)
                conn.execute('UPDATE daily_refresh SET state=?,error=? WHERE day=?',(state,error,day))
            row = conn.execute('SELECT * FROM daily_refresh WHERE day=?', (day,)).fetchone()
            return dict(row)
