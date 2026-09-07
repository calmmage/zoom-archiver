"""Zoom S2S collector. Explicit writes, byte verification, no automatic trash.

The Typer app can also be mounted by an embedding application.
Raw API objects stay local in sqlite; only fixed diagnostics leave this module.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import unicodedata
import shutil
import socket
import sqlite3
import importlib
from dataclasses import dataclass, field
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

import httpx
import typer
@dataclass
class RunResult:
    ok: bool
    new_items: int | None
    auth_state: str
    reason: str = ''
    payload: dict = field(default_factory=dict)


def parse_window(value, *, now=None):
    if value is None or not value.strip():
        return None
    clock = now or datetime.now(timezone.utc)
    raw = value.strip()
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if len(raw) > 1 and raw[-1] in units and raw[:-1].isdigit():
        return clock - timedelta(**{units[raw[-1]]: int(raw[:-1])})
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


KEY_NAMES = ('ZOOM_ACCOUNT_ID', 'ZOOM_CLIENT_ID', 'ZOOM_CLIENT_SECRET')
SETUP_HELP = 'see README: Server-to-Server OAuth setup'
FREE_RESERVE = 50_000_000_000
app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False, help='Inventory, archive and verify Zoom cloud recordings; trash is manual only.')


class CollectorError(RuntimeError):
    """Fixed, secret-free diagnostic safe for job reports."""


class AuthError(CollectorError):
    def __init__(self, state='unknown'):
        self.state = state
        super().__init__(f'auth_state={state}; {SETUP_HELP}')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_keys(lookup=None):
    """Resolve environment credentials or an explicit module:function hook.

    Hook output is captured; exceptions become fixed unknown-auth diagnostics.
    Configuring a hook makes it authoritative (no silent environment fallback).
    """
    try:
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            if lookup is None:
                hook = os.environ.get('ZOOM_ARCHIVER_KEY_RESOLVER')
                if hook:
                    module, name = hook.split(':', 1)
                    lookup = getattr(importlib.import_module(module), name)
                else:
                    lookup = os.environ.get
            values = [lookup(name) for name in KEY_NAMES]
    except Exception:
        raise AuthError('unknown') from None
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise AuthError('missing')
    return tuple(values)


# Zoom silently moves `from` forward when a listing span reaches its "one month"
# clamp (from=2026-02-10&to=2026-03-11 is echoed back as from=2026-02-11), so
# adjacent 30-day windows can skip a whole day. Keep spans short and overlapping
# by one day; inventory() dedupes meetings by uuid.
WINDOW_DAYS = 14


def windows(since='90d', *, now=None):
    clock = now or datetime.now(timezone.utc)
    cutoff = parse_window(since, now=clock)
    if cutoff is None or cutoff > clock:
        raise CollectorError('invalid since window')
    end = clock.date()
    while end >= cutoff.date():
        start = max(cutoff.date(), end - timedelta(days=WINDOW_DAYS - 1))
        yield start.isoformat(), end.isoformat()
        if start <= cutoff.date():
            break
        end = start


def meeting_key(value):
    encoded = quote(str(value), safe='')
    return quote(encoded, safe='') if str(value).startswith('/') or '//' in str(value) else encoded


class ZoomClient:
    def __init__(self, credentials, *, http=None, sleep=time.sleep, user_id='me'):
        self.credentials = credentials
        self.http = http or httpx.Client(timeout=60, follow_redirects=False)
        self.sleep = sleep
        self.user_id = user_id
        self._token = None
        self._expires = 0

    def token(self):
        if self._token and time.monotonic() < self._expires:
            return self._token
        account, client, secret = self.credentials
        response = self.http.post('https://zoom.us/oauth/token', params={'grant_type': 'account_credentials', 'account_id': account}, auth=(client, secret))
        if response.status_code != 200:
            raise AuthError('expired' if response.status_code in (400, 401) else 'unknown')
        payload = response.json()
        if not payload.get('access_token'):
            raise AuthError('unknown')
        self._token = payload['access_token']
        self._expires = time.monotonic() + max(0, int(payload.get('expires_in', 3600)) - 60)
        return self._token

    def request(self, method, path, **kwargs):
        for attempt in range(5):
            try:
                response = self.http.request(method, 'https://api.zoom.us/v2' + path, headers={'Authorization': 'Bearer ' + self.token()}, **kwargs)
            except httpx.TransportError:
                if attempt == 4:
                    raise CollectorError('Zoom transport failed after 5 attempts') from None
            else:
                if response.status_code == 401:
                    self._token = None
                    if attempt == 4:
                        raise AuthError('expired')
                elif response.status_code != 429 and response.status_code < 500:
                    if response.status_code >= 400:
                        raise CollectorError(f'Zoom API HTTP {response.status_code}')
                    return response
            if method == 'DELETE':  # An uncertain mutation is never blindly retried.
                raise CollectorError('trash outcome unknown; inspect Zoom before retrying')
            self.sleep(min(60, 2 ** (attempt + 1)))
        raise CollectorError('Zoom API unavailable after 5 attempts')

    def inventory(self, since='90d', *, now=None, limit=None):
        seen = {}
        for start, end in windows(since, now=now):
            page, tokens = '', set()
            while True:
                response = self.request('GET', '/users/' + quote(self.user_id, safe='') + '/recordings', params={'from': start, 'to': end, 'page_size': 300, 'next_page_token': page})
                data = response.json()
                echoed = (str(data.get('from') or start), str(data.get('to') or end))
                if echoed != (start, end):
                    raise CollectorError(f'Zoom clamped the listing window: asked {start}..{end}, served {echoed[0]}..{echoed[1]}')
                for meeting in data.get('meetings', []):
                    seen[str(meeting['uuid'])] = meeting
                page = data.get('next_page_token', '')
                if not page:
                    break
                if page in tokens:
                    raise CollectorError('Zoom repeated a pagination token')
                tokens.add(page)
        rows = sorted(seen.values(), key=lambda m: m['start_time'], reverse=True)
        return rows if limit is None else rows[:limit]

    def recording_files(self, uuid):
        return self.request('GET', f'/meetings/{meeting_key(uuid)}/recordings').json()

    @contextlib.contextmanager
    def stream(self, url, offset=0):
        headers = {'Authorization': 'Bearer ' + self.token(), 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        # Download URLs are received from Zoom, never from free-form CLI input.
        # Bearer credentials are sent only to Zoom hosts; signed CDN redirects
        # can be followed over HTTPS without forwarding the OAuth token.
        for _ in range(8):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise CollectorError('unsafe download URL')
            host = parsed.hostname.lower()
            trusted = any(host == d or host.endswith('.' + d) for d in ('zoom.us', 'zoom.com', 'zoomgov.com'))
            if not trusted:
                headers.pop('Authorization', None)
            with self.http.stream('GET', url, headers=headers, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = str(response.url.join(response.headers['location']))
                    continue
                yield response
                return
        raise CollectorError('too many download redirects')


@contextlib.contextmanager
def file_lock(path: Path, *, stale_s=30, timeout=30):
    """O_EXCL manifest protocol. A live local PID is never reclaimed by age.

    Foreign-host locks require manual inspection: a remote download may be alive.
    """
    deadline = time.monotonic() + timeout
    owner = {'pid': os.getpid(), 'host': socket.gethostname(), 'created_at': now_iso()}
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(owner, handle)
            inode = path.stat().st_ino
            break
        except FileExistsError:
            try:
                stat = path.stat()
                if time.time() - stat.st_mtime > stale_s:
                    old = json.loads(path.read_text())
                    if old.get('host') == socket.gethostname():
                        try:
                            os.kill(int(old['pid']), 0)
                        except ProcessLookupError:
                            if path.stat().st_ino == stat.st_ino:
                                path.unlink()  # stale marker only
                            continue
            except FileNotFoundError:
                continue
            except (ValueError, KeyError, PermissionError):
                pass
            if time.monotonic() >= deadline:
                raise CollectorError('lock busy; inspect owner before retry')
            time.sleep(0.1)
    try:
        yield
    finally:
        if path.exists() and path.stat().st_ino == inode:
            path.unlink()  # owned marker only


def merge_preserving(old, new):
    if isinstance(old, dict) and isinstance(new, dict):
        result = dict(old)
        for key, value in new.items():
            result[key] = merge_preserving(result[key], value) if key in result else value
        return result
    if isinstance(old, list) and isinstance(new, list):
        return old + [v for v in new if v not in old]
    return new


def update_manifest(path, *, meeting=None, file=None, **updates):
    path = Path(path)
    if any(p.is_symlink() for p in (path, path.with_name(path.name + '.lock'), path.with_name(path.name + '.part'), *path.parents)):
        raise CollectorError('symlink in manifest path')
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_name(path.name + '.lock')):
        data = json.loads(path.read_text()) if path.exists() else {'meeting': {}, 'files': [], 'transcripts': {}, 'mail': {}, 'safe_to_trash': False, 'notes': []}
        # Keep previous snapshots of changed entries, including unknown fields.
        if meeting:
            current = data.setdefault('meeting', {})
            merged = merge_preserving(current, meeting)
            if current and merged != current:
                data.setdefault('meeting_history', []).append(current)
            data['meeting'] = merged
        if file:
            entries = data.setdefault('files', [])
            entry = next((f for f in entries if f.get('zoom_file_id') == file['zoom_file_id']), None)
            if entry is None:
                entries.append(file)
            else:
                merged = merge_preserving(entry, file)
                if merged != entry:
                    data.setdefault('file_history', []).append(dict(entry))
                    entry.update(merged)
        for key, value in updates.items():
            data[key] = merge_preserving(data[key], value) if key in data else value
        temporary = path.with_name(path.name + '.part')
        with temporary.open('w') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    return data


def clean_name(value):
    value = str(value)
    if not value or value in ('.', '..') or '/' in value or '\\' in value or any(ord(c) < 32 for c in value):
        raise CollectorError('unsafe source filename')
    return value


def meeting_directory(meeting):
    stamp = datetime.fromisoformat(meeting['start_time'].replace('Z', '+00:00'))
    slug = re.sub(r'[^\w-]+', '-', meeting.get('topic', ''), flags=re.UNICODE).strip('-')[:70] or 'untitled'
    # macOS SMB stores names in NFD; an NFC path can stop resolving after the first write.
    slug = unicodedata.normalize('NFD', slug)
    mid = clean_name(meeting['id'])
    return Path(f'{stamp.year:04d}') / f'{stamp:%Y-%m-%d}_{stamp:%H%M}_{slug}_{mid}'


def original_name(file):
    if file.get('file_name'):
        return clean_name(file['file_name'])
    ext = file.get('file_extension') or {'TRANSCRIPT': 'vtt', 'CC': 'vtt', 'CHAT': 'txt', 'TIMELINE': 'json', 'SUMMARY': 'json'}.get(file.get('file_type'), file.get('file_type', 'bin').lower())
    return clean_name(f"{file.get('recording_type', 'recording')}_{file['id']}.{ext.lower()}")


def publish_exclusive(partial: Path, target: Path, *, expected_size: int, expected_sha256: str):
    """Publish without replacing any destination; retain the verified partial.

    SMB does not necessarily support hard links or no-replacement rename flags.
    O_EXCL atomically claims the final name, including against a racing symlink.
    The copy is visible before completion: consumers must require the verified
    ledger/manifest state. A crash or failed read-back retains both artifacts.
    """
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        with partial.open('rb') as source:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    if (target.stat().st_size != expected_size or sha256(target) != expected_sha256
            or partial.stat().st_size != expected_size or sha256(partial) != expected_sha256):
        raise CollectorError('exclusive publication read-back mismatch; both artifacts retained')


class Archive:
    def __init__(self, root: Path, *, write=False, transcript_marker_dir=None):
        self.nas = Path(root).expanduser().resolve()
        self.transcript_marker_dir = Path(transcript_marker_dir).expanduser() if transcript_marker_dir is not None else None
        if not self.nas.is_dir():
            raise CollectorError('archive root unavailable; refusing to create root directory')
        self.root = self.nas
        self.write = write
        self.checked(self.root)
        database = self.checked(self.root / '_state/inventory.sqlite')
        if write:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(database)
        elif database.is_file():
            self.db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        else:
            self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        if write or not database.exists():
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meetings(uuid TEXT PRIMARY KEY,id TEXT,topic TEXT,start_time TEXT,duration REAL,total_size INTEGER,relative_dir TEXT UNIQUE,raw_json TEXT,seen_at TEXT);
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,meeting_uuid TEXT,file_type TEXT,file_size INTEGER,download_url TEXT,recording_start TEXT,recording_end TEXT,status TEXT,raw_json TEXT,seen_at TEXT);
            CREATE TABLE IF NOT EXISTS downloads(file_id TEXT PRIMARY KEY,path TEXT UNIQUE,status TEXT,bytes INTEGER,sha256 TEXT,downloaded_at TEXT,verified_at TEXT);
            ''')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def checked(self, path):
        # Reject symlinks at every existing store component, including DB/manifests.
        path = Path(path)
        try:
            parts = path.relative_to(self.nas).parts
        except ValueError:
            raise CollectorError('path outside archive store') from None
        current = self.nas
        for part in parts:
            if part in ('.', '..'):
                raise CollectorError('unsafe store path')
            current = current / part
            if current.is_symlink():
                raise CollectorError('symlink in archive store')
        if not path.is_relative_to(self.root):
            raise CollectorError('path outside Zoom store')
        return path

    def require_write(self):
        if not self.write:
            raise CollectorError('archive writes require --download and no --dry-run')

    def upsert(self, meetings):
        self.require_write()
        stamp = now_iso()
        with self.db:
            for m in meetings:
                uuid = str(m['uuid'])
                previous = self.db.execute('SELECT relative_dir FROM meetings WHERE uuid=?', (uuid,)).fetchone()
                directory = previous['relative_dir'] if previous else str(meeting_directory(m))
                collision = self.db.execute('SELECT uuid FROM meetings WHERE relative_dir=?', (directory,)).fetchone()
                if collision and collision['uuid'] != uuid:
                    directory += '_' + hashlib.sha256(uuid.encode()).hexdigest()[:12]
                self.db.execute('INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET id=excluded.id,topic=excluded.topic,start_time=excluded.start_time,duration=excluded.duration,total_size=excluded.total_size,raw_json=excluded.raw_json,seen_at=excluded.seen_at', (uuid,str(m['id']),m.get('topic',''),m['start_time'],m.get('duration'),m.get('total_size'),directory,json.dumps(m,ensure_ascii=False),stamp))
                seen = set()
                for f in m.get('recording_files', []):
                    fid = str(f['id']); seen.add(fid)
                    old = self.db.execute('SELECT meeting_uuid FROM files WHERE id=?', (fid,)).fetchone()
                    if old and old['meeting_uuid'] != uuid:
                        raise CollectorError('file ID changed meeting; refusing ambiguous inventory')
                    self.db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET file_type=excluded.file_type,file_size=excluded.file_size,download_url=excluded.download_url,recording_start=excluded.recording_start,recording_end=excluded.recording_end,status=excluded.status,raw_json=excluded.raw_json,seen_at=excluded.seen_at', (fid,uuid,f.get('file_type'),f.get('file_size'),f.get('download_url'),f.get('recording_start'),f.get('recording_end'),f.get('status'),json.dumps(f,ensure_ascii=False),stamp))
                    self.db.execute('INSERT OR IGNORE INTO downloads(file_id,status) VALUES (?,?)', (fid,'queued'))
                # Only a returned, complete meeting list can prove individual absence.
                for old in self.db.execute('SELECT id FROM files WHERE meeting_uuid=?', (uuid,)).fetchall():
                    if old['id'] not in seen:
                        self.db.execute("UPDATE files SET status='missing-on-zoom' WHERE id=?", (old['id'],))
                        self.db.execute("UPDATE downloads SET status='missing-on-zoom' WHERE file_id=? AND status!='verified'", (old['id'],))
        for m in meetings:
            for row in self.rows(uuid=str(m['uuid'])):
                self.manifest(row)

    def rows(self, *, uuid=None):
        sql = '''SELECT f.*,f.status AS zoom_status,d.status AS download_status,d.path,d.bytes,d.sha256,d.downloaded_at,d.verified_at,m.relative_dir,m.raw_json AS meeting_json FROM files f JOIN meetings m ON m.uuid=f.meeting_uuid JOIN downloads d ON d.file_id=f.id'''
        return self.db.execute(sql + (' WHERE m.uuid=?' if uuid else '') + ' ORDER BY m.start_time DESC,f.id', (uuid,) if uuid else ()).fetchall()

    def target(self, row):
        if row['path']:
            return self.checked(self.root / row['path'])
        name = original_name(json.loads(row['raw_json']))
        if name in ('manifest.json', 'transcripts', 'clips') or name.endswith(('.part', '.lock')):
            raise CollectorError('source name conflicts with archive metadata')
        directory = self.checked(self.root / row['relative_dir'])
        # Multiple original names remain distinct via a per-file subdirectory.
        peers = self.rows(uuid=row['meeting_uuid'])
        if sum(original_name(json.loads(p['raw_json'])) == name for p in peers) > 1:
            directory /= clean_name(row['id'])
        return self.checked(directory / name)

    def manifest(self, row):
        if not self.write:
            return
        m = json.loads(row['meeting_json'])
        target = self.target(row)
        manifest = self.checked(self.root / row['relative_dir'] / 'manifest.json')
        self.checked(manifest.with_name('manifest.json.lock'))
        self.checked(manifest.with_name('manifest.json.part'))
        update_manifest(manifest, meeting={'uuid': m['uuid'], 'id': m['id'], 'topic': m.get('topic',''), 'start_time':m['start_time'], 'duration_min':m.get('duration'), 'host_email':m.get('host_email'), 'source':'zoom-cloud'}, file={'name':str(target.relative_to(manifest.parent)), 'file_type':row['file_type'], 'file_size':row['file_size'], 'sha256':row['sha256'], 'status':row['download_status'], 'zoom_status':row['zoom_status'], 'downloaded_at':row['downloaded_at'], 'verified_at':row['verified_at'], 'zoom_file_id':row['id'], 'download_route':'zoom-s2s-api'}, safe_to_trash=False)

    def status(self, fid, status, **values):
        self.require_write()
        if set(values) - {'path','bytes','sha256','downloaded_at','verified_at'}:
            raise ValueError('unknown download field')
        with self.db:
            assignments = ','.join(f'{k}=?' for k in values)
            self.db.execute('UPDATE downloads SET status=?' + (','+assignments if assignments else '') + ' WHERE file_id=?', (status,*values.values(),fid))
        row = next(r for r in self.rows() if r['id'] == fid)
        self.manifest(row)
        return status

    def download_one(self, row, client, *, disk_usage=shutil.disk_usage):
        self.require_write()
        target = self.target(row)
        if row['download_status'] == 'verified':
            return self.verify_one(row)
        if row['zoom_status'] == 'missing-on-zoom':
            return self.status(row['id'], 'missing-on-zoom')
        if row['zoom_status'] != 'completed':
            return self.status(row['id'], 'queued')
        expected = row['file_size']
        if not isinstance(expected, int) or expected < 0:
            return self.status(row['id'], 'mismatch(missing-expected-size)')
        if target.exists():
            if row['sha256'] and row['download_status'] == 'downloading':
                return self.verify_one(row)  # Recover a crash after promotion.
            return self.status(row['id'], 'mismatch(existing-final-preserved)')
        if disk_usage(self.nas).free < FREE_RESERVE + 2 * expected:
            raise CollectorError('archive free space below 50 GB reserve plus partial and final sizes')
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = self.checked(target.with_name(target.name + '.part'))
        lock = self.checked(target.with_name(target.name + '.lock'))
        with file_lock(lock):
            if target.exists():
                return self.status(row['id'], 'mismatch(existing-final-preserved)')
            self.status(row['id'], 'downloading', path=str(target.relative_to(self.root)))
            partial.touch(exist_ok=True)
            if partial.stat().st_size == expected and row['sha256'] and sha256(partial) == row['sha256']:
                failure = self._publish(row, partial, target, expected, row['sha256'], disk_usage)
                return failure or self.verify_one(row)
            for attempt in range(5):
                offset = partial.stat().st_size
                try:
                    with client.stream(row['download_url'], offset) as response:
                        if response.status_code in (404, 410):
                            return self.status(row['id'], 'missing-on-zoom')
                        if response.status_code == 401:
                            client._token = None
                            raise httpx.TransportError('refresh token')
                        if response.status_code == 429 or response.status_code >= 500:
                            raise httpx.TransportError('transient HTTP')
                        if response.status_code not in (200,206):
                            raise CollectorError(f'download HTTP {response.status_code}')
                        mismatch = None
                        if offset or response.status_code == 206:
                            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range',''))
                            if response.status_code != 206 or not match or tuple(map(int,match.groups())) != (offset, expected-1, expected):
                                mismatch = 'range'
                        length = response.headers.get('content-length')
                        if length is not None and (not length.isdigit() or int(length) != expected-offset):
                            mismatch = 'content-length'
                        if response.headers.get('content-encoding','identity') != 'identity':
                            mismatch = 'encoded-response'
                        if mismatch:
                            return self.status(row['id'], f'mismatch({mismatch})')
                        digest = hashlib.sha256()
                        with partial.open('rb') as prior:
                            for chunk in iter(lambda:prior.read(1024*1024), b''):
                                digest.update(chunk)
                        with partial.open('ab') as output:
                            count = offset
                            for chunk in response.iter_raw():
                                output.write(chunk); digest.update(chunk)
                                count += len(chunk)
                                if count > expected:
                                    output.flush(); os.fsync(output.fileno())
                                    return self.status(row['id'], 'mismatch(size)', bytes=count)
                            output.flush(); os.fsync(output.fileno())
                        if partial.stat().st_size != expected:
                            return self.status(row['id'], 'mismatch(size)', bytes=partial.stat().st_size)
                        if target.exists():
                            return self.status(row['id'], 'mismatch(existing-final-preserved)')
                        # Save the digest before exclusive publication; retain the partial.
                        self.status(row['id'], 'downloading', bytes=expected, sha256=digest.hexdigest(), downloaded_at=now_iso())
                        failure = self._publish(row, partial, target, expected, digest.hexdigest(), disk_usage)
                        if failure:
                            return failure
                        fresh = next(r for r in self.rows() if r['id'] == row['id'])
                        return self.verify_one(fresh)
                except httpx.TransportError:
                    if attempt < 4:
                        client.sleep(min(60, 2 ** (attempt+1)))
                        continue
                    return self.status(row['id'], 'mismatch(transport)', bytes=partial.stat().st_size)
                except CollectorError:
                    self.status(row['id'], 'mismatch(download-refused)')
                    raise

    def _publish(self, row, partial, target, expected, digest, disk_usage):
        try:
            # The partial is now allocated. Reserve enough for the final copy.
            if disk_usage(self.nas).free < FREE_RESERVE + expected:
                raise CollectorError('archive free space below reserve for exclusive publication')
            publish_exclusive(partial, target, expected_size=expected, expected_sha256=digest)
        except FileExistsError:
            return self.status(row['id'], 'mismatch(existing-final-preserved)')
        except (OSError, CollectorError):
            return self.status(row['id'], 'mismatch(publication-failed-partial-retained)')
        return None

    def verify_one(self, row):
        target = self.target(row)
        if not target.is_file():
            status = 'mismatch(missing-on-disk)'
        elif target.stat().st_size != row['file_size']:
            status = 'mismatch(size)'
        elif not row['sha256'] or sha256(target) != row['sha256']:
            status = 'mismatch(sha256)'
        else:
            status = 'verified'
        if self.write:
            return self.status(row['id'],status, **({'verified_at':now_iso()} if status=='verified' else {}))
        return status

    def safe_meetings(self):
        safe = []
        for m in self.db.execute('SELECT * FROM meetings ORDER BY start_time DESC'):
            rows = self.rows(uuid=m['uuid'])
            if not rows or any(r['download_status'] != 'verified' or self.verify_one(r) != 'verified' for r in rows):
                continue
            directory = self.checked(self.root / m['relative_dir'])
            transcripts = self.checked(directory / 'transcripts')
            if not self._completed_transcript(directory, transcripts):
                continue
            safe.append(dict(m))
        return safe

    def _completed_transcript(self, directory, transcripts):
        """Override in an adapter to validate a completed source-bound pair.

        The public default only checks an explicitly configured marker folder.
        """
        return self._markers_clear()

    def _markers_clear(self):
        folder = self.transcript_marker_dir
        if folder is None:
            return True
        if not folder.is_dir() or folder.is_symlink():
            return False
        try:
            return not any(p.is_symlink() or p.name.endswith(('.wanted', '.lock'))
                           for p in folder.rglob('*'))
        except OSError:
            return False

    def safe_to_trash(self):
        safe = self.safe_meetings()
        lines = ['# Safe to trash candidates — ' + datetime.now().strftime('%d %b %Y'), '', 'Manual review only. Nothing was deleted.', '', '| UUID | Meeting ID | Topic | Date | Bytes | Evidence |','|---|---|---|---|---|---|']
        for m in safe:
            if self.write:
                manifest = self.checked(self.root / m['relative_dir'] / 'manifest.json')
                update_manifest(manifest, safe_to_trash=True)
            topic = m['topic'].replace('|','\\|').replace('\n',' ')
            size = sum(r['file_size'] for r in self.rows(uuid=m['uuid']))
            date = datetime.fromisoformat(m['start_time'].replace('Z','+00:00')).strftime('%d %b %Y')
            lines.append(f"| {m['uuid']} | {m['id']} | {topic} | {date} | {size} | All files verified again; configured eligibility gate passed |")
        text = '\n'.join(lines)+'\n'
        if self.write:
            target = self.checked(self.root / '_state/safe-to-trash.md')
            with file_lock(self.checked(target.with_name(target.name+'.lock'))):
                target.write_text(text)
        return text

    def trash(self, client, ids, *, yes=False, dry_run=False):
        if not yes:
            raise CollectorError('trash refuses without --yes')
        # A fresh local verify and fresh remote file list are mandatory, even if
        # a previous safe-to-trash report exists. UUID selects one recurrence.
        if not ids:
            raise CollectorError('trash requires at least one ID')
        safe = self.safe_meetings()
        selected = []
        for identifier in ids:
            matches = [m for m in safe if identifier in (m['uuid'],m['id'])]
            if len(matches) != 1:
                raise CollectorError('trash ID unsafe, absent, or ambiguous; use the exact safe UUID')
            m = matches[0]
            remote = client.recording_files(m['uuid'])
            local = {r['id']:r for r in self.rows(uuid=m['uuid'])}
            remote_files = remote.get('recording_files', [])
            if not remote_files or {str(f['id']) for f in remote_files} != set(local) or any(f.get('status')!='completed' or f.get('file_size')!=local[str(f['id'])]['file_size'] for f in remote_files):
                raise CollectorError('Zoom files changed; inventory and download again before trash')
            selected.append(m)
        for m in selected:
            if not self._markers_clear():
                raise CollectorError('transcript marker directory busy or unavailable')
            if not dry_run:
                response = client.request('DELETE', f"/meetings/{meeting_key(m['uuid'])}/recordings", params={'action':'trash'})
                if response.status_code != 204:
                    raise CollectorError('trash acknowledgement unexpected; inspect Zoom before retrying')
        return len(selected)


def execute(command, *, since='90d', limit=5, dry_run=False, download=False, root=None, user_id=None, ids='', yes=False, client=None, credentials=None, transcript_marker_dir=None, archive_factory=None, key_loader=None, result_factory=None, client_factory=None):
    """Importable entry point with per-call dependency injection for adapters.

    Factories default at call time so module monkeypatches continue to work.
    archive_factory receives the direct root and write flag; a caller can bind
    additional policy in a subclass without duplicating download implementation.
    """
    archive_factory = archive_factory or Archive
    key_loader = key_loader or load_keys
    result_factory = result_factory or RunResult
    client_factory = client_factory or ZoomClient
    archive_options = {'transcript_marker_dir': transcript_marker_dir} if transcript_marker_dir is not None else {}
    owned = client is None
    try:
        if limit < 0:
            raise CollectorError('limit must be non-negative')
        if command == 'trash' and not yes:
            raise CollectorError('trash refuses without --yes')
        if client is None and command in ('inventory', 'download', 'trash'):
            keys = credentials if credentials is not None else key_loader()
            user = (user_id or os.environ.get('ZOOM_USER_ID', 'me')).strip()
            if command in ('inventory', 'download') and user in ('me', ''):
                raise CollectorError('S2S requires --user-id or ZOOM_USER_ID (Zoom user ID or email); see README')
            client = client_factory(keys, user_id=user)
        writes = download and not dry_run
        if command == 'inventory':
            meetings = client.inventory(since, limit=limit or None)
            if writes:
                with archive_factory(resolve_root(root), write=True, **archive_options) as archive:
                    archive.upsert(meetings)
            lines = [f"{m['id']} | {datetime.fromisoformat(m['start_time'].replace('Z','+00:00')):%d %b %Y} | {m.get('total_size',0)} bytes | {m.get('topic','')}" for m in meetings]
            return result_factory(True,len(meetings),'ok',payload={'lines':lines,'persisted':writes})
        with archive_factory(resolve_root(root), write=writes, **archive_options) as archive:
            if command == 'download':
                if limit == 0:
                    raise CollectorError('download requires a positive meeting limit')
                meetings = client.inventory(since, limit=limit)
                if not writes:
                    return result_factory(True,0,'ok', 'preview; pass --download to write archive', {'meetings':len(meetings)})
                archive.upsert(meetings)
                statuses = []
                for m in meetings:
                    for row in archive.rows(uuid=str(m['uuid'])):
                        statuses.append(archive.download_one(row,client))
                ok = all(s=='verified' for s in statuses)
                return result_factory(ok,statuses.count('verified'),'ok', '' if ok else 'download incomplete; partials preserved', {'statuses':statuses})
            if command == 'verify':
                statuses = [archive.verify_one(r) for r in archive.rows()]
                ok = bool(statuses) and all(s=='verified' for s in statuses)
                return result_factory(ok,statuses.count('verified'),'ok', '' if ok else 'archive verification incomplete', {'statuses':statuses})
            if command == 'safe-to-trash':
                return result_factory(True,0,'ok',payload={'lines':[archive.safe_to_trash()]})
            if command == 'trash':
                count = archive.trash(client,[i.strip() for i in ids.split(',') if i.strip()],yes=yes,dry_run=dry_run)
                return result_factory(True,count,'ok', 'trash preview' if dry_run else 'explicit trash completed')
            raise CollectorError('unknown command')
    except AuthError as exc:
        return result_factory(False,None,exc.state,str(exc))
    except Exception as exc:
        # Network exception reprs can contain signed URLs; never echo them.
        return result_factory(False,None,'unknown', str(exc) if isinstance(exc,CollectorError) else 'collector failed; inspect local state')
    finally:
        if owned and client is not None:
            client.http.close()


def resolve_root(root=None):
    value = root or os.environ.get('ZOOM_ARCHIVER_ROOT')
    if not value:
        raise CollectorError('archive root required; pass --root or ZOOM_ARCHIVER_ROOT')
    return Path(value).expanduser()


def print_result(result):
    for line in result.payload.get('lines', []):
        typer.echo(line)
    typer.echo(f'ok={str(result.ok).lower()} auth_state={result.auth_state} {result.reason}')
    if result.payload.get('statuses'):
        typer.echo(json.dumps(result.payload['statuses']))
    raise typer.Exit(0 if result.ok else 1)


def _cli(command, since, limit, dry_run, download, root, user_id, ids='', yes=False, transcript_marker_dir=None):
    print_result(execute(command, since=since, limit=limit, dry_run=dry_run,
                         download=download, root=root, user_id=user_id, ids=ids,
                         yes=yes, transcript_marker_dir=transcript_marker_dir))


def _register(command):
    def run(since: str='90d', limit: int=(0 if command=='inventory' else 5), dry_run: bool=False, download: bool=False, root: Path=typer.Option(None), user_id: str=typer.Option(None), transcript_marker_dir: Path=typer.Option(None)):
        _cli(command, since, limit, dry_run, download, root, user_id, transcript_marker_dir=transcript_marker_dir)
    app.command(command)(run)


for _verb in ('inventory', 'download', 'verify', 'safe-to-trash'):
    _register(_verb)


@app.command('trash')
def trash_command(ids: str=typer.Option(...), yes: bool=False, dry_run: bool=False, download: bool=False, root: Path=typer.Option(None), user_id: str=typer.Option(None), since: str='90d', limit: int=5, transcript_marker_dir: Path=typer.Option(None)):
    _cli('trash', since, limit, dry_run, download, root, user_id, ids, yes, transcript_marker_dir)


@app.command('mirror')
def mirror_command(to: Path=typer.Option(...), root: Path=typer.Option(None)):
    """Copy manifest-verified files to another existing root and recheck hashes."""
    from .mirror import mirror
    try:
        result = mirror(resolve_root(root), to)
    except Exception:
        typer.echo('mirror failed; inspect local state')
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, sort_keys=True))
    raise typer.Exit(1 if result['bad'] or result['unverified'] else 0)
