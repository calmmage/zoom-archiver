"""Synthetic embedding-policy fixture; never imported by the package."""
import math
import re
import json
from datetime import datetime
from pathlib import Path
from zoom_archiver.cli import sha256, CollectorError
VERSIONS = ("v1-openai-whisper1", "v2-gemini")


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{name}: expected finite nonnegative number')
    return value

def validate_sidecar(payload, *, version=None, audio_sha256=None):
    """Return original dict unchanged or raise ValueError; never normalize raw text."""
    if not isinstance(payload, dict):
        raise ValueError('sidecar must be an object')
    required = {'version', 'engine', 'model', 'host', 'created_at', 'audio_sha256', 'duration_s', 'rtf', 'langs', 'segments', 'text'}
    if required - payload.keys():
        raise ValueError('missing fields: ' + ', '.join(sorted(required - payload.keys())))
    if payload['version'] not in VERSIONS or (version and payload['version'] != version):
        raise ValueError('version mismatch or unknown version')
    for key in ('engine', 'model', 'host', 'created_at'):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ValueError(f'{key}: expected nonempty string')
    try:
        stamp = datetime.fromisoformat(payload['created_at'].replace('Z', '+00:00'))
        if stamp.utcoffset() is None:
            raise ValueError('timezone missing')
    except (ValueError, TypeError) as exc:
        raise ValueError('created_at: expected timezone-aware ISO8601') from exc
    digest = payload['audio_sha256']
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('audio_sha256: expected SHA256 hex')
    if audio_sha256 and digest.lower() != audio_sha256.lower():
        raise ValueError('audio_sha256 mismatch')
    duration = _number(payload['duration_s'], 'duration_s')
    _number(payload['rtf'], 'rtf')
    if not isinstance(payload['text'], str):
        raise ValueError('text must be string')
    langs = payload['langs']
    if not isinstance(langs, dict):
        raise ValueError('langs must be object')
    for key, value in langs.items():
        if not isinstance(key, str) or not key or _number(value, 'lang share') > 1:
            raise ValueError('invalid lang share')
    if langs and abs(sum(langs.values()) - 1) > 0.0100001:
        raise ValueError('lang shares must sum to one')
    if not isinstance(payload['segments'], list):
        raise ValueError('segments must be array')
    previous = -1
    for segment in payload['segments']:
        if not isinstance(segment, dict) or {'start_s', 'end_s', 'text', 'lang', 'speaker'} - segment.keys():
            raise ValueError('segment fields missing')
        start, end = (_number(segment[k], k) for k in ('start_s', 'end_s'))
        if start < previous or end < start or end > duration + 0.1:
            raise ValueError('segment timing invalid')
        previous = start
        if not isinstance(segment['text'], str):
            raise ValueError('segment text must be string')
        if any(segment[k] is not None and not isinstance(segment[k], str) for k in ('lang', 'speaker')):
            raise ValueError('lang and speaker must be string or null')
    if payload.get('cost_usd') is not None:
        _number(payload['cost_usd'], 'cost_usd')
    return payload

def select_audio(meeting):
    meeting = Path(meeting)
    for suffix in ('.m4a', '.mp4', '.wav'):
        candidates = sorted(p for p in meeting.iterdir() if p.suffix.lower() == suffix and p.is_file() and not p.is_symlink())
        if candidates:
            return candidates[0]
    raise FileNotFoundError(f'no m4a/mp4/wav in {meeting}')

def render_markdown(payload):
    validate_sidecar(payload)
    if not payload['segments']:
        return payload['text']
    records = []
    for seg in payload['segments']:
        seconds = int(seg['start_s'])
        stamp = f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'
        speaker = seg['speaker'] if seg['speaker'] is not None else 'unknown'
        records.append(f'[{stamp}] {speaker}: ' + seg['text'])
    return '\n'.join(records) + '\n'

def completed(self, directory, transcripts):
    """Embedding policy fixture: validated source-bound pair, no wanted claim."""
    try:
        audio = self.checked(select_audio(directory))
        digest = sha256(audio)
    except (ImportError, OSError, ValueError):
        return False
    for candidate in sorted(transcripts.glob('*.json')):
        try:
            candidate = self.checked(candidate)
            version = candidate.stem
            marker = self.checked(transcripts / (version + '.wanted'))
            claim = self.checked(transcripts / (version + '.wanted.lock'))
            markdown = self.checked(candidate.with_suffix('.md'))
            if marker.exists() or claim.exists():
                continue
            raw = candidate.read_bytes()
            payload = validate_sidecar(json.loads(raw), version=version, audio_sha256=digest)
            # The schema permits extra fields: explicit failed/wanted states
            # still cannot constitute a successfully produced transcript.
            if payload.get('status') not in (None, 'completed', 'done', 'success', 'succeeded', 'verified'):
                continue
            if markdown.read_bytes() != render_markdown(payload).encode('utf-8'):
                continue
            # Recheck markers and source after reading the pair. No writes,
            # repairs, claim deletion or raw text normalization are allowed.
            if (marker.exists() or claim.exists() or candidate.read_bytes() != raw
                    or sha256(audio) != digest):
                continue
            return True
        except (OSError, ValueError, TypeError, CollectorError):
            continue
    return False
