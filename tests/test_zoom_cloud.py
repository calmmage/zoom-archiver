"""Synthetic HTTP fixtures only: this suite cannot contact Zoom or real NAS."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner
from zoom_archiver import cli as z

FIX = Path(__file__).parent / 'fixtures'
NOW = datetime(2026,9,6,tzinfo=timezone.utc)


@pytest.fixture
def meeting():
    return json.loads((FIX/'inventory.json').read_text())['meetings'][0]


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    def reject(*args,**kwargs):
        raise AssertionError('real network forbidden in Zoom tests')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',reject)


class Bytes(httpx.SyncByteStream):
    def __init__(self, data, fail=False):
        self.data, self.fail = data, fail
    def __iter__(self):
        yield self.data
        if self.fail:
            raise httpx.ReadError('fixture connection lost')


def client_for(handler):
    return z.ZoomClient(('account','client','secret'),http=httpx.Client(transport=httpx.MockTransport(handler)),sleep=lambda _:None)


def download_client(data=b'hello zoom\x00\xff\r\n', *, wrong_length=None, ignore_range=False):
    def handler(request):
        if request.url.path == '/oauth/token':
            return httpx.Response(200,json={'access_token':'fixture-token','expires_in':3600})
        assert request.headers['authorization']=='Bearer fixture-token'
        offset = int(request.headers.get('range','bytes=0-').split('=')[1].split('-')[0])
        code = 206 if offset and not ignore_range else 200
        body = data[offset:] if code==206 else data
        headers = {'content-length':str(wrong_length if wrong_length is not None else len(body))}
        if code==206:
            headers['content-range']=f'bytes {offset}-{len(data)-1}/{len(data)}'
        return httpx.Response(code,headers=headers,stream=Bytes(body))
    return client_for(handler)


class TranscriptArchive(z.Archive):
    """Test an embedding application's stricter source-bound transcript policy."""
    def _completed_transcript(self, directory, transcripts):
        from transcript_contract import completed
        return completed(self, directory, transcripts)


def seeded(tmp_path,meeting):
    archive=TranscriptArchive(tmp_path,write=True)
    archive.upsert([meeting])
    return archive


def transcript_pair(archive):
    from transcript_contract import render_markdown
    folder = archive.root / archive.rows()[0]['relative_dir'] / 'transcripts'
    folder.mkdir(exist_ok=True)
    payload = {
        'version': 'v1-openai-whisper1', 'engine': 'fixture', 'model': 'fixture',
        'host': 'fixture', 'created_at': '2026-09-06T20:00:00+00:00',
        'audio_sha256': z.sha256(archive.target(archive.rows()[0])),
        'duration_s': 1.0, 'rtf': 0.1, 'langs': {}, 'segments': [],
        'text': '  Точно «как есть» API\r\n', 'fixture': True,
    }
    path = folder / (payload['version'] + '.json')
    path.write_text(json.dumps(payload, ensure_ascii=False))
    path.with_suffix('.md').write_bytes(render_markdown(payload).encode('utf-8'))
    return path, payload


def roomy(_):
    return SimpleNamespace(free=z.FREE_RESERVE+1_000_000)


def test_token_flow_and_memory_cache():
    requests=[]
    def handler(req):
        requests.append(req)
        assert req.method=='POST'
        assert req.url.params['grant_type']=='account_credentials'
        assert req.url.params['account_id']=='account'
        assert req.headers['authorization']=='Basic '+base64.b64encode(b'client:secret').decode()
        return httpx.Response(200,json={'access_token':'fixture-token','expires_in':3600})
    client=client_for(handler)
    assert client.token()==client.token()=='fixture-token'
    assert len(requests)==1


@pytest.mark.parametrize('missing',z.KEY_NAMES)
def test_missing_keys(missing):
    with pytest.raises(z.AuthError,match='auth_state=missing'):
        z.load_keys(lambda name:None if name==missing else 'synthetic')


def test_resolver_errors_not_missing():
    def broken(_): raise RuntimeError('credential resolver unavailable')
    with pytest.raises(z.AuthError,match='auth_state=unknown'):
        z.load_keys(broken)


def test_windows_and_paging(meeting):
    calls=[]
    def handler(req):
        if req.url.path=='/oauth/token': return httpx.Response(200,json={'access_token':'fixture-token'})
        p=dict(req.url.params); calls.append(p)
        m=copy.deepcopy(meeting)
        m['uuid']=p['from']+p['next_page_token']
        return httpx.Response(200,json={'meetings':[m],'next_page_token':'page2' if not p['next_page_token'] else ''})
    result=client_for(handler).inventory('90d',now=NOW)
    spans=list(z.windows('90d',now=NOW))
    assert len(calls)==2*len(spans) and len(result)==2*len(spans)
    assert all((datetime.fromisoformat(b)-datetime.fromisoformat(a)).days<=z.WINDOW_DAYS-1 for a,b in spans)
    assert spans[-1][0]=='2026-06-08'
    assert all(p['page_size']=='300' for p in calls)


def test_windows_overlap_so_no_day_falls_between_them():
    # Zoom clamps `from` forward near its one-month limit; adjacent 30-day windows lost 10 Feb 2026 (07 Sep 2026).
    spans=list(z.windows('400d',now=NOW))
    dates=[(datetime.fromisoformat(a).date(),datetime.fromisoformat(b).date()) for a,b in spans]
    assert dates[0][1]==NOW.date()
    assert all(prev_start==cur_end for (prev_start,_),(_,cur_end) in zip(dates,dates[1:]))
    assert all((b-a).days<=z.WINDOW_DAYS-1 for a,b in dates)
    assert dates[-1][0]==z.parse_window('400d',now=NOW).date()


def test_inventory_refuses_clamped_window(meeting):
    def handler(req):
        if req.url.path=='/oauth/token': return httpx.Response(200,json={'access_token':'fixture-token'})
        p=dict(req.url.params); nxt=(datetime.fromisoformat(p['from'])+timedelta(days=1)).date().isoformat()
        return httpx.Response(200,json={'from':nxt,'to':p['to'],'meetings':[copy.deepcopy(meeting)],'next_page_token':''})
    with pytest.raises(z.CollectorError, match='clamped the listing window'):
        client_for(handler).inventory('30d',now=NOW)


def test_idempotent_inventory_and_exact_topic(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        a.upsert([meeting])
        for table in ('meetings','files','downloads'):
            assert a.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==1
        assert json.loads(a.rows()[0]['meeting_json'])['topic']==meeting['topic']


def test_verified_download_and_second_pass(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        assert a.download_one(a.rows()[0],download_client(),disk_usage=roomy)=='verified'
        r=a.rows()[0]; p=a.target(r)
        assert p.read_bytes()==(FIX/'recording.bin').read_bytes()
        assert r['sha256']==hashlib.sha256(p.read_bytes()).hexdigest()
        assert r['verified_at'] and r['bytes']==meeting['recording_files'][0]['file_size']
        assert p.with_name(p.name+'.part').read_bytes()==p.read_bytes()
        assert a.verify_one(r)=='verified'
        p.write_bytes(b'x'*p.stat().st_size)
        assert a.verify_one(r)=='mismatch(sha256)'
        assert a.download_one(a.rows()[0],download_client(),disk_usage=roomy)=='mismatch(existing-final-preserved)'
        assert p.read_bytes()==b'x'*meeting['recording_files'][0]['file_size']


@pytest.mark.parametrize('header', [1,999])
def test_wrong_header_keeps_partial(tmp_path,meeting,header):
    with seeded(tmp_path,meeting) as a:
        r=a.rows()[0]; p=a.target(r)
        assert a.download_one(r,download_client(wrong_length=header),disk_usage=roomy)=='mismatch(content-length)'
        assert p.with_name(p.name+'.part').exists() and not p.exists()


def test_wrong_actual_size_keeps_bytes(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        r=a.rows()[0]; p=a.target(r)
        c=download_client(b'short',wrong_length=r['file_size'])
        assert a.download_one(r,c,disk_usage=roomy)=='mismatch(size)'
        assert p.with_name(p.name+'.part').read_bytes()==b'short'
        assert not p.exists()


def test_resume_partial(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        r=a.rows()[0]; p=a.target(r); p.parent.mkdir(parents=True,exist_ok=True)
        p.with_name(p.name+'.part').write_bytes((FIX/'recording.bin').read_bytes()[:4])
        assert a.download_one(r,download_client(),disk_usage=roomy)=='verified'
        assert p.read_bytes()==(FIX/'recording.bin').read_bytes()


def test_ignored_range_preserves_partial(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        r=a.rows()[0]; p=a.target(r); p.parent.mkdir(parents=True,exist_ok=True)
        part=p.with_name(p.name+'.part'); part.write_bytes(b'hell')
        assert a.download_one(r,download_client(ignore_range=True),disk_usage=roomy).startswith('mismatch')
        assert part.read_bytes()==b'hell'


def test_interrupted_stream_retries_with_range(tmp_path,meeting):
    offsets=[]; data=(FIX/'recording.bin').read_bytes()
    def handler(req):
        if req.url.path=='/oauth/token': return httpx.Response(200,json={'access_token':'fixture-token'})
        offset=int(req.headers.get('range','bytes=0-').split('=')[1].split('-')[0]); offsets.append(offset)
        headers={'content-length':str(len(data)-offset)}
        if offset: headers['content-range']=f'bytes {offset}-{len(data)-1}/{len(data)}'
        return httpx.Response(206 if offset else 200,headers=headers,stream=Bytes(data[offset:] if offset else data[:4],fail=not offset))
    with seeded(tmp_path,meeting) as a:
        assert a.download_one(a.rows()[0],client_for(handler),disk_usage=roomy)=='verified'
        assert offsets==[0,4]


def test_free_space_refusal(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        with pytest.raises(z.CollectorError,match='50 GB'):
            a.download_one(a.rows()[0],download_client(),disk_usage=lambda _:SimpleNamespace(free=z.FREE_RESERVE-1))
        assert a.rows()[0]['download_status']=='queued'


def test_manifest_append_only(tmp_path):
    p=tmp_path/'manifest.json'
    initial={'meeting':{'uuid':'x','unknown':'keep'},'files':[{'zoom_file_id':'f','status':'queued','custom':'raw'}],'transcripts':{'v1':{'path':'transcripts/a.json','quotes':' «как есть» \r\n'}},'notes':['original'],'mail':{'id':'mail-1'}}
    p.write_text(json.dumps(initial))
    first=z.update_manifest(p,meeting={'uuid':'x','topic':'new'},file={'zoom_file_id':'f','status':'verified'},notes=['extra'])
    second=z.update_manifest(p,file={'zoom_file_id':'f','status':'verified'})
    assert first==second
    assert second['transcripts']==initial['transcripts'] and second['mail']==initial['mail']
    assert second['meeting']['unknown']=='keep'
    assert second['files'][0]['custom']=='raw'
    assert second['file_history'][0]==initial['files'][0]
    assert second['notes']==['original','extra']
    assert not p.with_name('manifest.json.lock').exists()


def test_safe_requires_verified_and_transcript(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        assert not a.safe_meetings()
        assert a.download_one(a.rows()[0],download_client(),disk_usage=roomy)=='verified'
        assert not a.safe_meetings()
        transcript_pair(a)
        assert len(a.safe_meetings())==1
        assert 'All files verified again' in a.safe_to_trash()
        assert (a.root/'_state/safe-to-trash.md').is_file()
        a.target(a.rows()[0]).write_bytes(b'bad')
        assert not a.safe_meetings()


def test_trash_gates_and_fresh_remote_inventory(tmp_path,meeting):
    calls=[]
    class Fake:
        def recording_files(self,_): return meeting
        def request(self,*args,**kwargs):
            calls.append((args,kwargs))
            return SimpleNamespace(status_code=204)
    with seeded(tmp_path,meeting) as a:
        with pytest.raises(z.CollectorError,match='--yes'): a.trash(Fake(),[meeting['uuid']])
        with pytest.raises(z.CollectorError,match='unsafe'): a.trash(Fake(),[meeting['uuid']],yes=True)
        a.download_one(a.rows()[0],download_client(),disk_usage=roomy)
        transcript_pair(a)
        assert a.trash(Fake(),[meeting['uuid']],yes=True,dry_run=True)==1
        assert not calls
        assert a.trash(Fake(),[meeting['uuid']],yes=True)==1
        assert calls[0][1]['params']=={'action':'trash'}
        assert calls[0][0][0]=='DELETE'
        meeting['recording_files'].append({'id':'new','status':'completed','file_size':1})
        with pytest.raises(z.CollectorError,match='changed'): a.trash(Fake(),[meeting['uuid']],yes=True)
        assert len(calls)==1


def test_preview_no_nas_writes(tmp_path,meeting):
    class Fake:
        def inventory(self,*args,**kwargs): return [meeting]
    r=z.execute('inventory',download=False,client=Fake(),root=tmp_path)
    assert r.ok and not list(tmp_path.iterdir())
    r=z.execute('download',download=True,dry_run=True,client=Fake(),root=tmp_path)
    assert r.ok and not list(tmp_path.iterdir())


def test_verify_preview_leaves_state_unchanged(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        a.download_one(a.rows()[0],download_client(),disk_usage=roomy)
        p=a.target(a.rows()[0]); p.write_bytes(b'bad')
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with z.Archive(tmp_path) as a:
        assert a.verify_one(a.rows()[0])=='mismatch(size)'
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_missing_on_zoom_retains_download(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        a.download_one(a.rows()[0],download_client(),disk_usage=roomy)
        meeting['recording_files']=[]; a.upsert([meeting])
        assert a.rows()[0]['zoom_status']=='missing-on-zoom'
        assert a.target(a.rows()[0]).exists()


def test_path_traversal_and_symlink_refused(tmp_path,meeting):
    meeting['recording_files'][0]['file_name']='../escape.mp4'
    with z.Archive(tmp_path,write=True) as a:
        with pytest.raises(z.CollectorError,match='filename'): a.upsert([meeting])
    other=tmp_path/'outside'; other.mkdir()
    (tmp_path/'bad').symlink_to(other)
    with z.Archive(tmp_path) as a:
        with pytest.raises(z.CollectorError,match='symlink'): a.checked(a.root/'bad/file')


def test_repeated_meeting_ids_and_names_do_not_collide(tmp_path,meeting):
    m2=copy.deepcopy(meeting); m2['uuid']='second'; m2['recording_files'][0]['id']='f2'
    with seeded(tmp_path,meeting) as a:
        a.upsert([m2])
        assert a.rows()[0]['relative_dir']!=a.rows()[1]['relative_dir']


def test_cli_missing_auth_and_trash_refusal(monkeypatch):
    def missing(): raise z.AuthError('missing')
    monkeypatch.setattr(z,'load_keys',missing)
    r=CliRunner().invoke(z.app,['inventory','--since','7d','--dry-run'])
    assert r.exit_code==1 and 'auth_state=missing' in r.stdout and z.SETUP_HELP in r.stdout
    r=CliRunner().invoke(z.app,['trash','--ids','123'])
    assert r.exit_code==1 and '--yes' in r.stdout


def test_uuid_double_encoding():
    assert z.meeting_key('/ab//c==')=='%252Fab%252F%252Fc%253D%253D'


def test_download_never_overwrites_verified_file(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        a.download_one(a.rows()[0],download_client(),disk_usage=roomy)
        class Never:
            def stream(self,*args): raise AssertionError('verified file must not be fetched')
        assert a.download_one(a.rows()[0],Never(),disk_usage=roomy)=='verified'


def test_recover_crash_after_promotion(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        a.download_one(a.rows()[0],download_client(),disk_usage=roomy)
        r=a.rows()[0]
        a.status(r['id'],'downloading')
        assert a.download_one(a.rows()[0],download_client(),disk_usage=roomy)=='verified'


def test_safe_list_updates_manifest_without_losing_notes(tmp_path,meeting):
    with seeded(tmp_path,meeting) as a:
        a.download_one(a.rows()[0],download_client(),disk_usage=roomy)
        d=a.root/a.rows()[0]['relative_dir']
        transcript_pair(a)
        z.update_manifest(d/'manifest.json',notes=['exact quote'])
        a.safe_to_trash()
        m=json.loads((d/'manifest.json').read_text())
        assert m['safe_to_trash'] is True and m['notes']==['exact quote']


def test_redirect_does_not_disclose_bearer_to_cdn():
    seen=[]
    def handler(req):
        if req.url.path=='/oauth/token': return httpx.Response(200,json={'access_token':'fixture-token'})
        seen.append((req.url.host,req.headers.get('authorization')))
        if req.url.host=='example.zoom.us':
            return httpx.Response(302,headers={'location':'https://cdn.example.invalid/signed'})
        return httpx.Response(200,stream=Bytes(b'bytes'))
    c=client_for(handler)
    with c.stream('https://example.zoom.us/source') as response:
        assert b''.join(response.iter_raw())==b'bytes'
    assert seen==[('example.zoom.us','Bearer fixture-token'),('cdn.example.invalid',None)]


def test_retry_exhaustion_keeps_partial(tmp_path,meeting):
    pauses=[]
    def handler(req):
        if req.url.path=='/oauth/token': return httpx.Response(200,json={'access_token':'fixture-token'})
        return httpx.Response(503)
    c=client_for(handler); c.sleep=pauses.append
    with seeded(tmp_path,meeting) as a:
        r=a.rows()[0]
        assert a.download_one(r,c,disk_usage=roomy)=='mismatch(transport)'
        assert a.target(r).with_name('original.mp4.part').exists()
        assert pauses==[2,4,8,16]


def test_live_s2s_requires_user_id_before_http(tmp_path):
    r=z.execute('inventory',credentials=('fixture-account','fixture-client','fixture-secret'))
    assert not r.ok and '--user-id' in r.reason


def test_unexpected_errors_do_not_disclose_values():
    class Bad:
        def inventory(self,*args,**kwargs): raise TypeError('private signed URL or credential')
    r=z.execute('inventory',client=Bad())
    assert not r.ok and 'private' not in r.reason


def test_missing_nas_is_not_created(tmp_path):
    target=tmp_path/'absent'
    with pytest.raises(z.CollectorError,match='root unavailable'):
        z.Archive(target,write=True)
    assert not target.exists()


def test_empty_trash_selection_refused(tmp_path):
    with z.Archive(tmp_path,write=True) as a:
        with pytest.raises(z.CollectorError,match='at least one'): a.trash(None,[],yes=True)


def test_setup_help_requires_no_credentials(monkeypatch):
    def reject(): raise AssertionError('help must not resolve credentials')
    monkeypatch.setattr(z, 'load_keys', reject)
    result = CliRunner().invoke(z.app, ['--help'])
    assert result.exit_code == 0 and 'inventory' in result.stdout


def test_readme_preserves_setup_stages_and_scopes():
    readme = (Path(__file__).resolve().parents[1] / 'README.md').read_text()
    for text in ('Server-to-Server OAuth', 'Information', 'Scopes', 'Activation',
                 'ZOOM_ACCOUNT_ID', 'inventory --since',
                 'cloud_recording:read:list_user_recordings:admin',
                 'cloud_recording:read:list_recording_files:admin',
                 'cloud_recording:read:recording:admin',
                 'cloud_recording:delete:meeting_recording:admin'):
        assert text in readme


def test_active_lock_not_stolen_after_stale_threshold(tmp_path):
    import socket
    import time
    p=tmp_path/'manifest.json.lock'
    p.write_text(json.dumps({'pid':os.getpid(),'host':socket.gethostname()}))
    os.utime(p,(time.time()-90,time.time()-90))
    with pytest.raises(z.CollectorError,match='lock busy'):
        with z.file_lock(p,timeout=0): pass
    assert p.exists()


def test_trash_non204_is_not_success(tmp_path,meeting):
    class Fake:
        def recording_files(self,_): return meeting
        def request(self,*args,**kwargs): return SimpleNamespace(status_code=200)
    with seeded(tmp_path,meeting) as a:
        a.download_one(a.rows()[0],download_client(),disk_usage=roomy)
        transcript_pair(a)
        with pytest.raises(z.CollectorError,match='acknowledgement'):
            a.trash(Fake(),[meeting['uuid']],yes=True)


@pytest.mark.parametrize('recovery', [False, True])
def test_racing_final_survives_both_publication_paths(tmp_path, meeting, monkeypatch, recovery):
    with seeded(tmp_path, meeting) as a:
        row = a.rows()[0]
        target = a.target(row)
        partial = target.with_name(target.name + '.part')
        data = (FIX / 'recording.bin').read_bytes()
        if recovery:
            partial.write_bytes(data)
            a.status(row['id'], 'downloading', path=str(target.relative_to(a.root)),
                     sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            row = a.rows()[0]
        status = a.status
        injected = []
        def inject_after_last_exists_check(fid, state, **values):
            result = status(fid, state, **values)
            if state == 'downloading' and (recovery or values.get('sha256')) and not injected:
                # Normal path: during the intervening digest/manifest update.
                # Recovery path: after its last existence check and before copy.
                target.write_bytes(b'concurrent final: NEVER REPLACE')
                injected.append(True)
            return result
        monkeypatch.setattr(a, 'status', inject_after_last_exists_check)
        assert a.download_one(row, download_client(), disk_usage=roomy) == 'mismatch(existing-final-preserved)'
        assert injected and target.read_bytes() == b'concurrent final: NEVER REPLACE'
        assert partial.read_bytes() == data


def test_exclusive_publication_refuses_dangling_symlink(tmp_path):
    partial = tmp_path / 'source.part'; partial.write_bytes(b'original')
    target = tmp_path / 'target'; outside = tmp_path / 'absent'
    target.symlink_to(outside)
    with pytest.raises(FileExistsError):
        z.publish_exclusive(partial, target, expected_size=8, expected_sha256=z.sha256(partial))
    assert partial.read_bytes() == b'original' and target.is_symlink() and not outside.exists()


def test_interrupted_publication_retains_verified_partial_and_incomplete_final(tmp_path, meeting, monkeypatch):
    with seeded(tmp_path, meeting) as a:
        row = a.rows()[0]; target = a.target(row)
        def fail_copy(source, output, **kwargs):
            output.write(source.read(3))
            raise OSError('synthetic interrupted NAS copy')
        monkeypatch.setattr(z.shutil, 'copyfileobj', fail_copy)
        assert a.download_one(row, download_client(), disk_usage=roomy) == 'mismatch(publication-failed-partial-retained)'
        assert target.read_bytes() == (FIX / 'recording.bin').read_bytes()[:3]
        assert target.with_name(target.name + '.part').read_bytes() == (FIX / 'recording.bin').read_bytes()
        assert a.rows()[0]['download_status'] != 'verified'
        # A retry preserves the incomplete final rather than truncating it.
        assert a.download_one(a.rows()[0], download_client(), disk_usage=roomy) == 'mismatch(existing-final-preserved)'
        assert target.read_bytes() == (FIX / 'recording.bin').read_bytes()[:3]


@pytest.mark.parametrize('defect', ['malformed', 'arbitrary', 'wrong-hash', 'wrong-version',
                                 'missing-markdown', 'wrong-markdown', 'wanted', 'claim', 'failed'])
def test_safe_to_trash_rejects_incomplete_transcript_evidence(tmp_path, meeting, defect):
    with seeded(tmp_path, meeting) as a:
        a.download_one(a.rows()[0], download_client(), disk_usage=roomy)
        path, payload = transcript_pair(a)
        if defect == 'malformed': path.write_text('{broken')
        elif defect == 'arbitrary': path.write_text('{"text":"not a completed transcript"}')
        elif defect == 'wrong-hash':
            payload['audio_sha256'] = '0' * 64; path.write_text(json.dumps(payload))
        elif defect == 'wrong-version':
            payload['version'] = 'v2-gemini'; path.write_text(json.dumps(payload))
        elif defect == 'missing-markdown': path.with_suffix('.md').rename(path.with_suffix('.md.retained'))
        elif defect == 'wrong-markdown': path.with_suffix('.md').write_text('rewritten text')
        elif defect == 'wanted': path.with_suffix('.wanted').write_text('{}')
        elif defect == 'claim': path.with_suffix('.wanted.lock').write_text('{}')
        elif defect == 'failed':
            payload['status'] = 'failed'; path.write_text(json.dumps(payload))
        before = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
        assert a.safe_meetings() == []
        assert before == {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}


def test_completed_silent_transcript_is_valid(tmp_path, meeting):
    with seeded(tmp_path, meeting) as a:
        a.download_one(a.rows()[0], download_client(), disk_usage=roomy)
        path, payload = transcript_pair(a)
        payload['text'] = ''
        path.write_text(json.dumps(payload)); path.with_suffix('.md').write_bytes(b'')
        assert len(a.safe_meetings()) == 1
