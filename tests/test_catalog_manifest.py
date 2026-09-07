import json

import pytest

from zoom_archiver.cli import CollectorError, update_manifest


@pytest.mark.parametrize('existing', [False, True])
def test_catalog_replacement_preserves_raw_other_members(tmp_path, existing):
    path = tmp_path / 'manifest.json'
    prefix = b'{\r\n "meeting" : {"topic":"\\u0041", "odd":"} , \\\"catalog\\\" :"},\r\n "files": [1,  2], "unknown":1.00e+2'
    suffix = b'\r\n}\r\n'
    old_catalog = b', "catalog" : {"tags":["old"],"people":["Someone"],"unknown":{"keep":true}}' if existing else b''
    path.write_bytes(prefix + old_catalog + suffix)
    update_manifest(path, catalog_replace={'tags': [], 'people': ['New'], 'custom_title': 'Title'})
    raw = path.read_bytes()
    assert raw.startswith(prefix)
    assert raw.endswith(suffix)
    data = json.loads(raw)
    assert data['catalog']['tags'] == []
    assert data['catalog']['people'] == ['New']
    if existing:
        assert data['catalog']['unknown'] == {'keep': True}
    update_manifest(path, catalog_replace={'people': []})
    assert json.loads(path.read_bytes())['catalog']['people'] == []
    assert path.read_bytes().startswith(prefix)
    assert not path.with_name('manifest.json.lock').exists()
    assert not path.with_name('manifest.json.part').exists()


def test_default_list_merge_unchanged(tmp_path):
    path = tmp_path / 'manifest.json'
    update_manifest(path, notes=['one'], catalog={'tags': ['one']})
    data = update_manifest(path, notes=['two'], catalog={'tags': ['two']})
    assert data['notes'] == ['one', 'two']
    assert data['catalog']['tags'] == ['one', 'two']


@pytest.mark.parametrize('raw,patch', [('{"catalog":null}', {'tags': []}), ('{"files":[],"files":[]}', {'tags': []}), ('{}', {'meeting': {}})])
def test_invalid_replacement_preserves_original(tmp_path, raw, patch):
    path = tmp_path / 'manifest.json'
    path.write_text(raw)
    with pytest.raises(CollectorError):
        update_manifest(path, catalog_replace=patch)
    assert path.read_text() == raw


def test_exclusive_and_existing_manifest(tmp_path):
    path = tmp_path / 'manifest.json'
    with pytest.raises(CollectorError):
        update_manifest(path, catalog_replace={})
    assert not path.exists()
    path.write_text('{}')
    with pytest.raises(CollectorError):
        update_manifest(path, catalog_replace={}, meeting={'id': 'fixture'})
    assert path.read_text() == '{}'
