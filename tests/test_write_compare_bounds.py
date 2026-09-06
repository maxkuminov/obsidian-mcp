"""The final optimistic comparison stays bounded if the incumbent grows."""
import json
import os

import pytest

from src.auth.session import current_user_id
from src.mcp_server.auth import current_permission
from src.mcp_server import tools
from src.services import vault


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, 'vault_path', str(tmp_path))
    monkeypatch.setattr(tools.settings, 'multi_user_mode', False)
    monkeypatch.setattr(tools.settings, 'write_precondition_required', False)
    vault.clear_user_vault_cache()
    return tmp_path


@pytest.mark.parametrize('raw', [False, True])
@pytest.mark.parametrize('change', ['larger', 'same_length', 'missing', 'directory', 'symlink'])
def test_shared_comparison_keeps_conflict_and_leaf_error_behavior(root, monkeypatch, raw, change):
    path = root / ('data.bin' if raw else 'note.md')
    path.write_bytes(b'old')
    original = vault._read_fd_bytes
    calls = []

    def changed(dir_fd, name, max_bytes=None):
        calls.append(max_bytes)
        if change == 'larger':
            path.write_bytes(b'external bytes')
        elif change == 'same_length':
            path.write_bytes(b'new')
        else:
            path.unlink()
            if change == 'directory':
                path.mkdir()
            elif change == 'symlink':
                (root / 'outside').write_bytes(b'untouched')
                path.symlink_to(root / 'outside')
        return original(dir_fd, name, max_bytes=max_bytes)

    monkeypatch.setattr(vault, '_read_fd_bytes', changed)
    error = ValueError if change == 'directory' else OSError if change == 'symlink' else RuntimeError
    with pytest.raises(error) as caught:
        if raw:
            vault.write_bytes(path.name, b'replacement', overwrite=True, expected=b'old')
        else:
            vault.write_file(path.name, 'replacement', expected=b'old')
    assert calls == [3]
    if error is RuntimeError:
        assert str(caught.value).startswith('File changed while editing:')
    if change == 'larger':
        assert path.read_bytes() == b'external bytes'
    elif change == 'same_length':
        assert path.read_bytes() == b'new'
    elif change == 'directory':
        assert path.is_dir()
    elif change == 'symlink':
        assert path.is_symlink()
        assert (root / 'outside').read_bytes() == b'untouched'
    else:
        assert not path.exists()
    assert not list(root.glob('.tmp-*'))


@pytest.mark.parametrize('initial', [b'', b'old'])
def test_growth_after_fstat_reads_only_expectation_plus_one(root, monkeypatch, initial):
    path = root / 'data.bin'
    path.write_bytes(initial)
    real_fstat = os.fstat
    real_fdopen = os.fdopen
    reads = []
    target_inode = path.stat().st_ino

    def grow_after_stat(fd):
        info = real_fstat(fd)
        if info.st_ino == target_inode:
            path.write_bytes(b'x' * 100_000)
        return info

    class BoundedStream:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, count=None):
            reads.append(count)
            assert count == len(initial) + 1
            return self.stream.read(count)

    def observed_fdopen(fd, mode, **kwargs):
        stream = real_fdopen(fd, mode, **kwargs)
        return BoundedStream(stream) if mode == 'rb' else stream

    monkeypatch.setattr(vault.os, 'fstat', grow_after_stat)
    monkeypatch.setattr(vault.os, 'fdopen', observed_fdopen)
    with pytest.raises(RuntimeError, match='File changed while editing:'):
        vault.write_bytes(path.name, b'replacement', overwrite=True, expected=initial)
    assert reads == [len(initial) + 1]
    assert path.read_bytes() == b'x' * 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [False, True])
async def test_tool_growth_returns_typed_conflict(root, monkeypatch, raw):
    path = root / ('data.bin' if raw else 'note.md')
    path.write_bytes(b'old')
    monkeypatch.setattr(tools.settings, 'max_file_read_bytes', 4)
    publish_name = 'write_bytes_at' if raw else 'write_file_at'
    original = getattr(tools, publish_name)

    def grow(*args, **kwargs):
        path.write_bytes(b'external bytes')
        return original(*args, **kwargs)

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, publish_name, grow)
    monkeypatch.setattr(tools, '_log_usage', no_log)
    permission = current_permission.set('readwrite')
    user = current_user_id.set(None)
    try:
        options = {'expected_hash': vault.content_hash_for_bytes(b'old')}
        if raw:
            result = await tools.write_file_impl(path.name, 'replacement', encoding='text', overwrite=True, **options)
        else:
            result = await tools.edit_note_impl(path.name, 'replacement', **options)
    finally:
        current_user_id.reset(user)
        current_permission.reset(permission)
    assert result.startswith('File changed while editing:')
    payload = json.loads(result.rsplit('MCP-REFUSAL ', 1)[1])
    assert payload['code'] == 'concurrent_write'
    assert payload['nothing_written'] is True
    assert path.read_bytes() == b'external bytes'
