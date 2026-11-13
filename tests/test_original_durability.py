"""原件目录项的同步顺序与故障传播；只写临时文件，不做断电测试。"""

import errno
import hashlib
import os
import stat
from pathlib import Path

import pytest
from test_attachment_originals import (
    A,
    download,
    http_source,  # noqa: F401  (fixture)
)

from fetch import web_client as client


def prepare_path(tmp_path, monkeypatch, scenario):
    target = tmp_path / "original.pdf"
    if scenario == "existing":
        target.write_bytes(A)
    if scenario == "race":
        actual_link = client.os.link

        def concurrent_winner(source, destination):
            actual_link(source, destination)
            raise FileExistsError(errno.EEXIST, "synthetic concurrent winner")

        monkeypatch.setattr(client.os, "link", concurrent_winner)
    return target


def track_fsync(monkeypatch, *, fail_directory=None):
    events = []
    actual_fsync = client.os.fsync
    actual_close = client.os.close

    def fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        events.append(("directory" if directory else "file", path, fd))
        if directory and fail_directory is not None and fail_directory(path):
            raise OSError(errno.EIO, "synthetic directory fsync failure")
        return actual_fsync(fd)

    def close(fd):
        events.append(("close", None, fd))
        return actual_close(fd)

    monkeypatch.setattr(client.os, "fsync", fsync)
    monkeypatch.setattr(client.os, "close", close)
    return events


@pytest.mark.parametrize("scenario", ["new", "existing", "race"])
def test_all_success_paths_sync_parent_directory(tmp_path, monkeypatch, scenario):
    target = prepare_path(tmp_path, monkeypatch, scenario)
    events = track_fsync(monkeypatch)
    reused = client._save_original(target, A, hashlib.sha256(A).hexdigest())
    assert reused is (scenario != "new")
    assert target.read_bytes() == A
    assert ("directory", tmp_path) in [(kind, path) for kind, path, _fd in events]


@pytest.mark.parametrize("scenario", ["new", "existing", "race"])
def test_directory_sync_failure_is_not_success_and_closes_fd(tmp_path, monkeypatch, scenario):
    target = prepare_path(tmp_path, monkeypatch, scenario)
    events = track_fsync(monkeypatch, fail_directory=lambda _path: True)
    with pytest.raises(OSError, match="directory fsync"):
        client._save_original(target, A, hashlib.sha256(A).hexdigest())
    assert target.read_bytes() == A  # 完整原件保留，不能删除并发采用者的文件。
    assert not list(tmp_path.glob(".download-*"))
    failed_fd = next(fd for kind, _path, fd in events if kind == "directory")
    assert ("close", None, failed_fd) in events


def test_file_sync_link_cleanup_and_directory_sync_order(tmp_path, monkeypatch):
    target = tmp_path / "original.pdf"
    events = track_fsync(monkeypatch)
    actual_link = client.os.link
    actual_unlink = Path.unlink

    def link(source, destination):
        actual_link(source, destination)
        events.append(("link", Path(destination), None))

    def unlink(path, **kwargs):
        actual_unlink(path, **kwargs)
        if path.name.startswith(".download-"):
            events.append(("cleanup", path, None))

    monkeypatch.setattr(client.os, "link", link)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert client._save_original(target, A, hashlib.sha256(A).hexdigest()) is False
    kinds = [kind for kind, _path, _fd in events]
    assert kinds.index("file") < kinds.index("link") < kinds.index("cleanup") < kinds.index("directory")
    assert not list(tmp_path.glob(".download-*"))


def test_new_directory_chain_is_synced_even_after_failed_creation_attempt(
    http_source, monkeypatch, tmp_path
):
    root = tmp_path / "new-root" / "nested"
    monkeypatch.setattr(client.APP, "DATA_ROOT", root)
    with monkeypatch.context() as first:
        events = track_fsync(first, fail_directory=lambda path: path == root)
        with pytest.raises(OSError, match="directory fsync"):
            download()
    originals = list((root / "raw" / "synthetic_files").glob("*.pdf"))
    assert len(originals) == 1 and originals[0].read_bytes() == A
    assert any(kind == "directory" and path == root for kind, path, _fd in events)
    assert not list(root.rglob(".download-*"))

    # mkdir遗留目录、link遗留完整文件都已存在；重试仍须补齐父条目同步。
    with monkeypatch.context() as retry:
        events = track_fsync(retry)
        saved = download()
    assert saved["skipped"] is True and Path(saved["local_path"]) == originals[0]
    synced = [path for kind, path, _fd in events if kind == "directory"]
    required = [root / "raw" / "synthetic_files", root / "raw", root, root.parent, tmp_path]
    assert all(path in synced for path in required)
    assert [synced.index(path) for path in required] == sorted(synced.index(path) for path in required)
    assert len(http_source["requests"]) == 2


def test_directory_sync_failure_is_not_retried_and_stores_nothing(http_source, monkeypatch):
    """本地 I/O 错误（目录 fsync 失败）不是瞬时读取错误：不重试 HTTP，下载失败如实抛出。"""
    events = track_fsync(monkeypatch, fail_directory=lambda _path: True)
    with pytest.raises(OSError, match="directory fsync"):
        download()
    assert len(http_source["requests"]) == 1
    assert any(kind == "directory" for kind, _path, _fd in events)
