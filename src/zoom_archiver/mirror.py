"""Manifest-driven byte-checked mirroring; no pruning of either archive."""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from .cli import CollectorError, file_lock, publish_exclusive, sha256


def checked(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or not relative.parts or any(p in ('.', '..') for p in relative.parts):
        raise CollectorError('unsafe mirror path')
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise CollectorError('symlink in mirror path')
    return current


def _matches(path, size, digest):
    return path.is_file() and path.stat().st_size == size and sha256(path) == digest


def _copy(source, destination, size, digest):
    if not _matches(source, size, digest):
        raise CollectorError('mirror source mismatch')
    if destination.exists():
        if _matches(destination, size, digest):
            return False
        raise CollectorError('mirror destination mismatch; existing bytes preserved')
    # Unique scratch names preserve earlier failed attempts. The mirror lock
    # serializes cooperating writers; final publication is followed by read-back.
    partial = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.part')
    with partial.open('xb') as output, source.open('rb') as incoming:
        shutil.copyfileobj(incoming, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    if not _matches(partial, size, digest) or not _matches(source, size, digest):
        raise CollectorError('mirror copy mismatch; partial retained')
    if destination.exists() or destination.is_symlink():
        raise CollectorError('mirror destination appeared; partial retained')
    publish_exclusive(partial, destination, expected_size=size, expected_sha256=digest)
    if not _matches(destination, size, digest):
        raise CollectorError('mirror read-back mismatch')
    return True


def mirror(root: Path, to: Path):
    """Copy eligible rows and provenance; report counters without private paths.

    Both roots must exist and must be disjoint. Mismatched existing destinations
    are retained for manual inspection; callers must not concurrently mutate
    either tree outside this tool's lock protocol.
    """
    root, to = Path(root).expanduser(), Path(to).expanduser()
    for path in (root, to):
        if not path.is_dir() or path.is_symlink():
            raise CollectorError('mirror root unavailable or symlinked')
    root, to = root.resolve(), to.resolve()
    if root.is_relative_to(to) or to.is_relative_to(root):
        raise CollectorError('mirror roots must be disjoint')
    result = dict(ok=0, copied=0, skipped=0, bad=0, unverified=0)
    lock = checked(to, Path('_mirror.lock'))
    with file_lock(lock):
        manifests = sorted(root.glob('[0-9][0-9][0-9][0-9]/*/manifest.json'))
        if not manifests:
            result['unverified'] += 1
        for manifest in manifests:
            try:
                checked(root, manifest.relative_to(root))
                raw = manifest.read_bytes()
                data = json.loads(raw)
                rows = data['files']
                if not isinstance(rows, list) or not rows:
                    raise CollectorError('mirror manifest contains no files')
                relative_dir = manifest.parent.relative_to(root)
                failed = False
                for row in rows:
                    try:
                        name, digest, size = row.get('name'), row.get('sha256'), row.get('file_size')
                        if (not isinstance(name, str) or not isinstance(digest, str)
                                or not re.fullmatch('[0-9a-f]{64}', digest)
                                or row.get('status') not in ('verified', 'downloaded')
                                or not isinstance(size, int) or size < 0):
                            result['unverified'] += 1
                            failed = True
                            continue
                        relname = Path(name)
                        if ('\\' in name or relname.is_absolute() or '..' in relname.parts
                                or name in ('manifest.json', '.') or name.endswith(('.part', '.lock'))):
                            raise CollectorError('unsafe mirror filename')
                        relative = relative_dir / relname
                        source, destination = checked(root, relative), checked(to, relative)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        copied = _copy(source, destination, size, digest)
                        result['copied' if copied else 'skipped'] += 1
                        result['ok'] += 1
                    except (OSError, ValueError, TypeError, AttributeError, CollectorError):
                        result['bad'] += 1
                        failed = True
                if manifest.read_bytes() != raw:
                    raise CollectorError('source manifest changed during mirror')
                # Publish provenance only after every row passed. Preserve every
                # previous destination manifest as a retained snapshot on update.
                if not failed:
                    destination = checked(to, manifest.relative_to(root))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists() and destination.read_bytes() != raw:
                        snapshot = destination.with_name('manifest.' + uuid.uuid4().hex + '.json')
                        with snapshot.open('xb') as output:
                            output.write(destination.read_bytes())
                            output.flush()
                            os.fsync(output.fileno())
                    partial = destination.with_name('manifest.' + uuid.uuid4().hex + '.part')
                    with partial.open('xb') as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    if partial.read_bytes() != raw:
                        raise CollectorError('mirror manifest copy mismatch')
                    os.replace(partial, destination)
                    if destination.read_bytes() != raw:
                        raise CollectorError('mirror manifest read-back mismatch')
            except (OSError, ValueError, KeyError, TypeError, CollectorError):
                result['bad'] += 1
    return result
