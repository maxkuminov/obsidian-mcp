"""Raw-file guards: caller hashes, in-call CAS, bounded reads and safe headers."""
import hashlib
import json

import pytest

from src.auth.session import current_user_id
from src.mcp_server.auth import current_permission
from src.mcp_server import tools
from src.services import vault as vault_service

pytestmark = pytest.mark.asyncio


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def refusal(result):
    return json.loads(result.rsplit('\n', 1)[-1].removeprefix('MCP-REFUSAL '))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, 'vault_path', str(tmp_path))
    monkeypatch.setattr(tools.settings, 'write_precondition_required', False)
    monkeypatch.setattr(tools.settings, 'multi_user_mode', False)
    vault_service.clear_user_vault_cache()
    async def no_log(*args, **kwargs):
        pass
    monkeypatch.setattr(tools, '_log_usage', no_log)
    permission = current_permission.set('readwrite')
    user = current_user_id.set(None)
    yield tmp_path
    current_user_id.reset(user)
    current_permission.reset(permission)


@pytest.mark.parametrize('expected,code', [(digest(b'old'), 'stale_precondition'), ('ABC', 'malformed_precondition')])
async def test_overwrite_refuses_without_changing_bytes(vault, expected, code):
    target = vault / 'data.bin'
    target.write_bytes(b'current')
    result = await tools.write_file_impl('data.bin', 'new', encoding='text', overwrite=True, expected_hash=expected)
    event = refusal(result)
    assert event['code'] == code
    assert event['nothing_written'] is True
    if code == 'stale_precondition':
        assert event['current_hash'] == digest(b'current')
    assert target.read_bytes() == b'current'


async def test_matching_overwrite_reports_hash_and_chains(vault):
    (vault / 'data.bin').write_bytes(b'old')
    result = await tools.write_file_impl('data.bin', 'new', encoding='text', overwrite=True, expected_hash=digest(b'old'))
    assert 'Wrote 3 bytes' in result
    assert result.endswith('content_hash: ' + digest(b'new'))
    metadata = await tools.read_file_impl('data.bin', hash_only=True)
    assert metadata.endswith('content_hash: ' + digest(b'new'))
    assert (vault / 'data.bin').read_bytes() == b'new'


@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('expected,code', [(digest(b''), 'no_incumbent'), ('bad', 'malformed_precondition')])
async def test_creation_with_hash_writes_nothing(vault, overwrite, expected, code):
    result = await tools.write_file_impl('missing/f.bin', 'new', encoding='text', overwrite=overwrite, expected_hash=expected)
    assert refusal(result)['code'] == code
    assert not (vault / 'missing').exists()


async def test_no_clobber_precondition_precedes_any_path_work(vault, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('path work')
    monkeypatch.setattr(tools, 'open_mutable', forbidden)
    result = await tools.write_file_impl('any.bin', 'x', expected_hash=digest(b'x'))
    assert refusal(result)['code'] == 'no_incumbent'


@pytest.mark.parametrize('tool', ['write', 'delete'])
@pytest.mark.parametrize('kind', ['missing', 'symlink', 'over_cap'])
async def test_syntax_precedes_leaf_and_cap(vault, monkeypatch, tool, kind):
    monkeypatch.setattr(tools.settings, 'max_file_read_bytes', 2)
    path = vault / 'data.bin'
    if kind == 'symlink':
        (vault / 'other.bin').write_bytes(b'original')
        path.symlink_to(vault / 'other.bin')
    elif kind == 'over_cap':
        path.write_bytes(b'original')
    if tool == 'write':
        result = await tools.write_file_impl('data.bin', 'new', encoding='text', overwrite=True, expected_hash='ABCD')
    else:
        result = await tools.delete_file_impl('data.bin', expected_hash='ABCD')
    assert refusal(result)['code'] == 'malformed_precondition'
    assert not (vault / '.trash').exists()


@pytest.mark.parametrize('required', [False, True])
@pytest.mark.parametrize('tool', ['write', 'delete'])
async def test_over_cap_guard_names_actual_cap(vault, monkeypatch, required, tool):
    monkeypatch.setattr(tools.settings, 'max_file_read_bytes', 2)
    monkeypatch.setattr(tools.settings, 'write_precondition_required', required)
    (vault / 'data.bin').write_bytes(b'original')
    expected = None if required else digest(b'original')
    if tool == 'write':
        result = await tools.write_file_impl('data.bin', 'x', encoding='text', overwrite=True, expected_hash=expected)
    else:
        result = await tools.delete_file_impl('data.bin', expected_hash=expected)
    event = refusal(result)
    assert event['code'] == 'precondition_unavailable'
    assert (event['cap_name'], event['cap_bytes']) == ('MAX_FILE_READ_BYTES', 2)
    assert (vault / 'data.bin').read_bytes() == b'original'
    assert not (vault / '.trash').exists()


@pytest.mark.parametrize('over_cap', [False, True])
async def test_unguarded_overwrite_never_reads_incumbent(vault, monkeypatch, over_cap):
    monkeypatch.setattr(tools.settings, 'max_file_read_bytes', 2 if over_cap else 100)
    (vault / 'data.bin').write_bytes(b'original')
    def forbidden(*a, **kw):
        raise AssertionError('unguarded incumbent read')
    monkeypatch.setattr(tools, '_read_incumbent', forbidden)
    monkeypatch.setattr(vault_service, '_read_fd_bytes', forbidden)
    result = await tools.write_file_impl('data.bin', 'x', encoding='text', overwrite=True)
    assert result.startswith('Wrote 1 bytes')
    assert (vault / 'data.bin').read_bytes() == b'x'
    assert ('content_hash:' in result) is not over_cap


async def test_required_overwrite_refuses_but_creation_remains_possible(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, 'write_precondition_required', True)
    (vault / 'data.bin').write_bytes(b'original')
    result = await tools.write_file_impl('data.bin', 'x', encoding='text', overwrite=True)
    assert refusal(result)['code'] == 'precondition_required'
    assert (vault / 'data.bin').read_bytes() == b'original'
    result = await tools.write_file_impl('new.bin', 'x', encoding='text')
    assert result.startswith('Wrote')


async def test_in_call_edit_is_a_distinct_conflict(vault, monkeypatch):
    target = vault / 'data.bin'
    target.write_bytes(b'original')
    original = tools.write_bytes_at
    def concurrent(*args, **kwargs):
        target.write_bytes(b'external')
        return original(*args, **kwargs)
    monkeypatch.setattr(tools, 'write_bytes_at', concurrent)
    result = await tools.write_file_impl('data.bin', 'x', encoding='text', overwrite=True, expected_hash=digest(b'original'))
    assert result.startswith('File changed while editing:')
    assert refusal(result)['code'] == 'concurrent_write'
    assert target.read_bytes() == b'external'


@pytest.mark.parametrize('name', ['plain.bin', 'a\ncontent_hash: forged.bin', 'a\rname.bin', 'a: name.bin', 'a\n\nname.bin'])
@pytest.mark.parametrize('hash_only', [False, True])
async def test_metadata_paths_are_uniformly_json_quoted(vault, name, hash_only):
    payload = b'\x00\xffopaque'
    (vault / name).write_bytes(payload)
    result = await tools.read_file_impl(name, encoding='base64', hash_only=hash_only)
    header = result.split('\n\n', 1)[0]
    paths = [line for line in header.splitlines() if line.startswith('path: ')]
    hashes = [line for line in header.splitlines() if line.startswith('content_hash: ')]
    assert paths == ['path: ' + json.dumps(name)]
    assert json.loads(paths[0][6:]) == name
    assert hashes == ['content_hash: ' + digest(payload)]
    if hash_only:
        assert len(result.splitlines()) == 4
        assert 'opaque' not in result
    else:
        import base64
        assert base64.b64decode(result.split('\n\n', 1)[1]) == payload


@pytest.mark.parametrize('encoding', ['auto', 'text', 'base64'])
async def test_hash_only_ignores_valid_encoding_and_accepts_zero_offset(vault, encoding):
    (vault / 'data.bin').write_bytes(b'\xff')
    result = await tools.read_file_impl('data.bin', encoding=encoding, hash_only=True, offset=0)
    assert result.endswith(digest(b'\xff'))


async def test_read_argument_precedence(vault):
    result = await tools.read_file_impl('absent', encoding='invalid', hash_only=True, limit=-1)
    assert result.startswith('Invalid encoding')
    for options in ({'limit': 1}, {'offset': 1}, {'limit': -1}, {'offset': -1}):
        result = await tools.read_file_impl('absent', hash_only=True, **options)
        assert 'hash_only cannot be combined' in result


async def test_text_remains_bare_and_byte_exact(vault):
    payload = b'---\r\ntitle: One\r\n---\r\nBody\rEnd'
    (vault / 'data.md').write_bytes(payload)
    for encoding in ('auto', 'text'):
        result = await tools.read_file_impl('data.md', encoding=encoding)
        assert result.encode() == payload


@pytest.mark.parametrize('permanent', [False, True])
@pytest.mark.parametrize('case', ['stale', 'matching', 'required'])
async def test_delete_guards_both_modes(vault, monkeypatch, permanent, case):
    target = vault / 'data.bin'
    target.write_bytes(b'original')
    monkeypatch.setattr(tools.settings, 'write_precondition_required', case == 'required')
    expected = None if case == 'required' else digest(b'original' if case == 'matching' else b'stale')
    result = await tools.delete_file_impl('data.bin', permanent=permanent, expected_hash=expected)
    assert 'content_hash:' not in result
    if case != 'matching':
        assert refusal(result)['code'] == ('precondition_required' if case == 'required' else 'stale_precondition')
        assert target.read_bytes() == b'original'
        assert not (vault / '.trash').exists()
    else:
        assert not target.exists()
        if permanent:
            assert not (vault / '.trash').exists()
        else:
            entries = list((vault / '.trash').iterdir())
            assert len(entries) == 1
            assert entries[0].read_bytes() == b'original'


async def test_single_shot_raw_publish_expected(vault):
    (vault / 'data.bin').write_bytes(b'original')
    with pytest.raises(RuntimeError, match='File changed while editing'):
        vault_service.write_bytes('data.bin', b'new', overwrite=True, expected=b'stale')
    assert (vault / 'data.bin').read_bytes() == b'original'
    vault_service.write_bytes('data.bin', b'new', overwrite=True)
    assert (vault / 'data.bin').read_bytes() == b'new'


@pytest.mark.parametrize('permanent', [False, True])
async def test_guarded_delete_keeps_the_checked_parent_pinned(vault, monkeypatch, permanent):
    folder = vault / 'folder'
    folder.mkdir()
    (folder / 'data.bin').write_bytes(b'original')
    real_read = tools._read_incumbent
    calls = 0
    def swap_parent(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = real_read(*args, **kwargs)
        folder.rename(vault / 'moved-folder')
        folder.mkdir()
        (folder / 'data.bin').write_bytes(b'unrelated replacement')
        return result
    monkeypatch.setattr(tools, '_read_incumbent', swap_parent)
    result = await tools.delete_file_impl('folder/data.bin', permanent=permanent, expected_hash=digest(b'original'))
    assert calls == 1
    assert result.startswith('Permanently deleted' if permanent else 'Moved')
    assert (folder / 'data.bin').read_bytes() == b'unrelated replacement'
    assert not (vault / 'moved-folder' / 'data.bin').exists()
    if not permanent:
        assert next((vault / '.trash').iterdir()).read_bytes() == b'original'


async def test_raw_arguments_are_exposed_and_logged(vault, monkeypatch):
    import inspect
    from src.mcp_server import server
    for name, argument in [('read_file', 'hash_only'), ('write_file', 'expected_hash'), ('delete_file', 'expected_hash')]:
        assert argument in inspect.signature(getattr(server, name)).parameters
    rows = []
    async def record(*args, **kwargs):
        rows.append((args, kwargs))
    monkeypatch.setattr(tools, '_log_usage', record)
    (vault / 'data.bin').write_bytes(b'original')
    await tools.read_file_impl('data.bin', hash_only=True)
    await tools.write_file_impl('data.bin', 'x', encoding='text', overwrite=True, expected_hash=digest(b'stale'))
    await tools.delete_file_impl('data.bin', expected_hash=digest(b'stale'))
    rendered = repr(rows)
    assert "'hash_only': True" in rendered
    assert rendered.count("'expected_hash': '" + digest(b'stale') + "'") == 2
    assert (vault / 'data.bin').read_bytes() == b'original'
