import copy
import hashlib
import json
import sys
import unicodedata
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner
from zoom_archiver import cli as z
from zoom_archiver.mirror import mirror
from test_zoom_cloud import download_client, roomy

FIX = Path(__file__).parent / 'fixtures'


@pytest.fixture
def archived(tmp_path):
    root = tmp_path / 'archive'; root.mkdir()
    meeting = json.loads((FIX / 'inventory.json').read_text())['meetings'][0]
    with z.Archive(root, write=True) as archive:
        archive.upsert([meeting])
        archive.download_one(archive.rows()[0], download_client(), disk_usage=roomy)
        yield archive, meeting


def test_real_shape_manifest_fixture_roundtrip(archived):
    archive, _ = archived
    manifest = archive.root / archive.rows()[0]['relative_dir'] / 'manifest.json'
    generated = json.loads(manifest.read_bytes())
    fixture = json.loads((FIX / 'manifest.json').read_bytes())
    assert set(generated) == set(fixture)
    assert set(generated['meeting']) == set(fixture['meeting'])
    assert set(generated['files'][0]) == set(fixture['files'][0])
    assert generated['meeting']['source'] == fixture['meeting']['source'] == 'zoom-cloud'
    assert generated['files'][0]['download_route'] == 'zoom-s2s-api'
    manifest.write_text(json.dumps(fixture, ensure_ascii=False))
    assert z.update_manifest(manifest) == fixture
    result = z.update_manifest(manifest, file={**fixture['files'][0], 'status': 'verified'}, notes=['new'])
    assert result['transcripts'] == fixture['transcripts'] and result['mail'] == fixture['mail']
    assert result['notes'] == fixture['notes'] + ['new']


def test_nfd_slug_preserves_manifest_topic(archived):
    archive, meeting = archived
    meeting = copy.deepcopy(meeting); meeting['uuid'] = 'accented-instance'; meeting['id'] = 987654321
    meeting['recording_files'][0]['id'] = 'accented-file'; meeting['topic'] = 'Café réunion'
    directory = z.meeting_directory(meeting)
    assert unicodedata.is_normalized('NFD', directory.name)
    assert 'Cafe\u0301' in directory.name
    archive.upsert([meeting])
    data = json.loads((archive.root / directory / 'manifest.json').read_text())
    assert data['meeting']['topic'] == 'Café réunion'


def test_resolver_hook_is_authoritative_and_silent(monkeypatch, capsys):
    provider = ModuleType('fixture_credential_provider')
    calls = []
    def resolve(name):
        calls.append(name)
        print('synthetic provider output')
        print('synthetic provider error output', file=sys.stderr)
        return 'synthetic-' + name
    provider.resolve = resolve
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    monkeypatch.setenv('ZOOM_ARCHIVER_KEY_RESOLVER', provider.__name__ + ':resolve')
    for name in z.KEY_NAMES: monkeypatch.setenv(name, 'unused synthetic fallback')
    assert z.load_keys() == tuple('synthetic-' + name for name in z.KEY_NAMES)
    assert tuple(calls) == z.KEY_NAMES
    assert capsys.readouterr() == ('', '')
    provider.resolve = lambda _: None
    with pytest.raises(z.AuthError, match='missing'): z.load_keys()


@pytest.mark.parametrize('hook', ['invalid', 'missing_module:resolve', 'json:no_such_function'])
def test_invalid_resolver_hook_is_unknown(monkeypatch, hook):
    monkeypatch.setenv('ZOOM_ARCHIVER_KEY_RESOLVER', hook)
    with pytest.raises(z.AuthError, match='auth_state=unknown'): z.load_keys()


def test_environment_credentials(monkeypatch):
    for name in z.KEY_NAMES: monkeypatch.setenv(name, 'synthetic-' + name)
    assert z.load_keys() == tuple('synthetic-' + name for name in z.KEY_NAMES)


def test_factories_keep_subclass_policy_without_global_mutation(tmp_path):
    before = z.Archive
    seen = []
    class Policy(z.Archive):
        def safe_to_trash(self):
            seen.append(self.root)
            return 'custom eligibility evidence'
    class Result(z.RunResult): pass
    result = z.execute('safe-to-trash', root=tmp_path, archive_factory=Policy,
                       key_loader=lambda: (_ for _ in ()).throw(AssertionError('offline')),
                       result_factory=Result)
    assert result.ok and isinstance(result, Result)
    assert result.payload['lines'] == ['custom eligibility evidence']
    assert seen == [tmp_path] and z.Archive is before


def test_injected_credentials_and_client_factory(tmp_path):
    calls = []
    class Client:
        def __init__(self, credentials, *, user_id):
            calls.append((credentials, user_id))
            self.http = SimpleNamespace(close=lambda: calls.append('closed'))
        def inventory(self, *args, **kwargs): return []
    assert z.execute('inventory', key_loader=lambda: ('a', 'b', 'c'),
                     client_factory=Client, user_id='synthetic-user').ok
    assert calls == [(('a', 'b', 'c'), 'synthetic-user'), 'closed']


def test_public_default_requires_no_transcript(archived):
    archive, meeting = archived
    assert len(archive.safe_meetings()) == 1
    calls = []
    client = SimpleNamespace(recording_files=lambda _: meeting,
                             request=lambda *a, **kw: calls.append(kw) or SimpleNamespace(status_code=204))
    assert archive.trash(client, [meeting['uuid']], yes=True) == 1
    assert calls == [{'params': {'action': 'trash'}}]


@pytest.mark.parametrize('name', ['v1.wanted', 'v1.wanted.lock', 'worker.lock', 'nested/v2.wanted'])
def test_optional_markers_block_safe_and_trash(archived, tmp_path, name):
    archive, meeting = archived
    folder = tmp_path / 'markers'; folder.mkdir()
    archive.transcript_marker_dir = folder
    assert len(archive.safe_meetings()) == 1
    marker = folder / name; marker.parent.mkdir(exist_ok=True); marker.write_bytes(b'fixture')
    assert not archive.safe_meetings()
    with pytest.raises(z.CollectorError, match='unsafe'):
        archive.trash(None, [meeting['uuid']], yes=True)
    assert marker.read_bytes() == b'fixture'


def test_marker_appearing_during_remote_refresh_blocks_delete(archived, tmp_path):
    archive, meeting = archived
    folder = tmp_path / 'markers'; folder.mkdir(); archive.transcript_marker_dir = folder
    def refresh(_):
        (folder / 'late.wanted').write_bytes(b'fixture')
        return meeting
    client = SimpleNamespace(recording_files=refresh,
                             request=lambda *a, **kw: pytest.fail('must not trash'))
    with pytest.raises(z.CollectorError, match='marker directory busy'):
        archive.trash(client, [meeting['uuid']], yes=True)


def test_marker_missing_directory_fails_closed(archived, tmp_path):
    archive, _ = archived
    archive.transcript_marker_dir = tmp_path / 'absent'
    assert not archive.safe_meetings()


def test_verify_and_root_env_without_credentials(archived, monkeypatch):
    archive, _ = archived
    monkeypatch.setenv('ZOOM_ARCHIVER_ROOT', str(archive.root))
    result = CliRunner().invoke(z.app, ['verify'])
    assert result.exit_code == 0 and 'verified' in result.stdout


def test_mirror_copies_and_rechecks_without_deleting(archived, tmp_path):
    archive, _ = archived
    dest = tmp_path / 'mirror'; dest.mkdir()
    extra = dest / 'unlisted.bin'; extra.write_bytes(b'preserve')
    result = mirror(archive.root, dest)
    assert result == dict(ok=1, copied=1, skipped=0, bad=0, unverified=0)
    row = archive.rows()[0]; relative = archive.target(row).relative_to(archive.root)
    assert z.sha256(dest / relative) == row['sha256']
    assert (dest / relative.parent / 'manifest.json').read_bytes() == (archive.root / relative.parent / 'manifest.json').read_bytes()
    assert not (dest / '_state').exists()
    assert mirror(archive.root, dest) == dict(ok=1, copied=0, skipped=1, bad=0, unverified=0)
    assert extra.read_bytes() == b'preserve'
    # Source corruption cannot be hidden by an already correct destination.
    archive.target(row).write_bytes(b'x' * row['file_size'])
    assert mirror(archive.root, dest)['bad'] == 1
    assert z.sha256(dest / relative) == row['sha256']


def test_mirror_unverified_cli_exit(archived, tmp_path):
    archive, _ = archived
    archive.status(archive.rows()[0]['id'], 'downloading')
    dest = tmp_path / 'mirror'; dest.mkdir()
    result = CliRunner().invoke(z.app, ['mirror', '--root', str(archive.root), '--to', str(dest)])
    assert result.exit_code == 1
    assert json.loads(result.stdout)['unverified'] == 1
    assert not list(dest.rglob('manifest.json'))


def test_mirror_preserves_mismatching_destination(archived, tmp_path):
    archive, _ = archived
    dest = tmp_path / 'mirror'; dest.mkdir()
    row = archive.rows()[0]
    target = dest / archive.target(row).relative_to(archive.root)
    target.parent.mkdir(parents=True); target.write_bytes(b'prior bytes')
    assert mirror(archive.root, dest)['bad'] == 1
    assert target.read_bytes() == b'prior bytes'


@pytest.mark.parametrize('name', ['../../outside.bin', '/outside.bin', '..\\outside.bin', 'manifest.json'])
def test_mirror_refuses_manifest_path_escape(archived, tmp_path, name):
    archive, _ = archived
    path = archive.root / archive.rows()[0]['relative_dir'] / 'manifest.json'
    data = json.loads(path.read_text()); data['files'][0]['name'] = name
    path.write_text(json.dumps(data))
    dest = tmp_path / 'mirror'; dest.mkdir()
    assert mirror(archive.root, dest)['bad'] == 1
    assert not (tmp_path / 'outside.bin').exists()


def test_mirror_refuses_destination_symlink(archived, tmp_path):
    archive, _ = archived
    dest = tmp_path / 'mirror'; dest.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    (dest / archive.rows()[0]['relative_dir'].split('/')[0]).symlink_to(outside)
    assert mirror(archive.root, dest)['bad'] == 1
    assert not list(outside.iterdir())


def test_mirror_failed_copy_retains_partial(archived, tmp_path, monkeypatch):
    archive, _ = archived
    dest = tmp_path / 'mirror'; dest.mkdir()
    import zoom_archiver.mirror as module
    def interrupted(source, output, **kwargs):
        output.write(source.read(3)); raise OSError('synthetic interruption')
    monkeypatch.setattr(module.shutil, 'copyfileobj', interrupted)
    assert mirror(archive.root, dest)['bad'] == 1
    partials = list(dest.rglob('*.part'))
    assert len(partials) == 1 and partials[0].stat().st_size == 3
    assert archive.target(archive.rows()[0]).is_file()


def test_mirror_changed_manifest_preserves_previous_snapshot(archived, tmp_path):
    archive, _ = archived
    dest = tmp_path / 'mirror'; dest.mkdir()
    assert mirror(archive.root, dest)['ok'] == 1
    manifest = archive.root / archive.rows()[0]['relative_dir'] / 'manifest.json'
    before = manifest.read_bytes()
    z.update_manifest(manifest, notes=['new annotation'])
    assert mirror(archive.root, dest)['ok'] == 1
    snapshots = list(dest.rglob('manifest.*.json'))
    assert len(snapshots) == 1 and snapshots[0].read_bytes() == before


def test_mirror_overlapping_roots_refused(tmp_path):
    with pytest.raises(z.CollectorError, match='disjoint'): mirror(tmp_path, tmp_path)


def test_mirror_racing_final_is_never_replaced(archived, tmp_path, monkeypatch):
    archive, _ = archived
    dest = tmp_path / 'mirror'; dest.mkdir()
    import zoom_archiver.mirror as module
    publish = module.publish_exclusive
    raced = []
    def race(partial, destination, **kwargs):
        destination.write_bytes(b'concurrent final preserved')
        raced.append(destination)
        return publish(partial, destination, **kwargs)
    monkeypatch.setattr(module, 'publish_exclusive', race)
    assert mirror(archive.root, dest)['bad'] == 1
    assert len(raced) == 1 and raced[0].read_bytes() == b'concurrent final preserved'
    partials = list(dest.rglob('*.part'))
    assert len(partials) == 1 and z.sha256(partials[0]) == archive.rows()[0]['sha256']
    assert not list(dest.rglob('manifest.json'))
