"""Find recent project topics from local Git or public repositories."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess

from .discovery import themes
from .sources import PUBLIC_PROJECTS_URL

MAX_PROJECTS = 20
ACTIVE_DAYS = 30
_NAME = re.compile(r'[A-Za-z0-9_.-]{1,100}')
_CACHE_KEY = 'discovery_public_projects'


def _git(repo, *args):
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(repo), *args],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=2, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return b''


def collect(root, now=None):
    """Scan only immediate repository directories. Never read chat or notes."""
    now = now or datetime.now(timezone.utc)
    try:
        directories = sorted(Path(root).iterdir())[:100]
    except OSError:
        return []
    found = []
    for repo in directories:
        if repo.is_symlink() or not repo.is_dir() or not (repo/'.git').exists():
            continue
        stamp = _git(repo,'log','-1','--format=%ct','HEAD').strip()
        if not stamp.isdigit() or len(stamp)>11:
            continue
        activity = datetime.fromtimestamp(int(stamp),timezone.utc)
        if not _active(activity,now):
            continue
        # Keep only controlled topic labels. The README itself is discarded.
        size = _git(repo,'cat-file','-s','HEAD:README.md').strip()
        if size.isdigit() and int(size)<=262144:
            text = _git(repo,'show','HEAD:README.md')[:6000].decode('utf-8',errors='replace')
        else:
            text = ''
        topic_ids = sorted(themes(repo.name.replace('-',' ')+' '+text))
        if topic_ids:
            found.append({'name':repo.name[:100], 'topics':topic_ids, 'active_at':activity.isoformat()})
    found.sort(key=lambda row:row['active_at'],reverse=True)
    return found[:MAX_PROJECTS]


@lru_cache(maxsize=4)
def _local(root, day):
    return collect(root)


def _active(date, now):
    return now-timedelta(days=ACTIVE_DAYS) <= date <= now+timedelta(hours=12)


def refresh_public_projects(store, fetch):
    """Use a fixed anonymous list and cache only public name, date and labels."""
    try:
        repos = json.loads(fetch(PUBLIC_PROJECTS_URL))
        if not isinstance(repos,list) or len(repos)>30:
            raise ValueError('Unexpected public project list')
    except (OSError, ValueError):
        return
    now = datetime.now(timezone.utc)
    projects = []
    for repo in repos:
        if (not isinstance(repo,dict) or repo.get('private') is not False
                or repo.get('archived') is not False or repo.get('fork') is not False):
            continue
        name = repo.get('name')
        if (not isinstance(name,str) or not _NAME.fullmatch(name)
                or repo.get('full_name') != f'momonong/{name}'):
            continue
        try:
            activity = datetime.fromisoformat(repo['pushed_at'])
            if not activity.tzinfo or not _active(activity,now):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        description = repo.get('description')
        tags = repo.get('topics')
        text = ' '.join([name.replace('-',' '),
                         description[:1000] if isinstance(description,str) else '',
                         ' '.join(tag[:80] for tag in tags[:20] if isinstance(tag,str)) if isinstance(tags,list) else ''])
        topics = sorted(themes(text))
        if topics:
            projects.append({'name':name,'topics':topics,'active_at':activity.isoformat()})
    with store._connect() as conn:
        conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                     (_CACHE_KEY,json.dumps(projects[:MAX_PROJECTS])))


def current(store, project_root=None, *, public_projects=False):
    if project_root is not None:
        day = datetime.now(timezone(timedelta(hours=8))).date()
        return _local(Path(project_root),day), 'local'
    if public_projects:
        with store._connect() as conn:
            row = conn.execute('SELECT value FROM meta WHERE key=?',(_CACHE_KEY,)).fetchone()
        if row:
            now = datetime.now(timezone.utc)
            return [p for p in json.loads(row[0]) if _active(datetime.fromisoformat(p['active_at']),now)], 'public'
    return [], 'unavailable'
