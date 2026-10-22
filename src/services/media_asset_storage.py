"""Descriptor-bound I/O on existing StorageRoot; no independent storage.

POSIX pins every directory with openat/O_NOFOLLOW. Windows pins every parent
without FILE_SHARE_WRITE/DELETE and opens the final file as a reparse object, so a
concurrent junction/symlink replacement never redirects reads or writes.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .storage_io import StorageError, check_io_cancelled
from .storage_roots import StorageRoot, is_link, relative_parts


def _windows_handle(path, *, directory=False, flags=os.O_RDONLY):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    access = 0 if directory else (0x40000000 if flags & os.O_WRONLY else 0x80000000)
    disposition = 1 if flags & os.O_EXCL else 3
    # Deny writable parent handles too: FSCTL_SET_REPARSE_POINT needs write
    # access but does not require delete access. Read sharing permits previews.
    handle = create(
        str(path),
        access,
        1,
        None,
        disposition,
        0x00200000 | (0x02000000 if directory else 0),
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    get_info = kernel.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    info = AttributeTag()
    try:
        if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
            raise StorageError(
                "unsafe_storage_path",
                "symlink / junction / reparse point は利用できません",
                403,
            )
        return handle, close
    except BaseException:
        close(handle)
        raise


@contextmanager
def pinned_parent(root: StorageRoot, relative: str, *, write=False):
    parts = relative_parts(relative)
    if not parts:
        raise StorageError("invalid_path", "ファイル名が必要です", 400)
    base = root.require_online(write=write)
    parent = base.joinpath(*parts[:-1])
    # Keep handles until the actual I/O/rename/delete completes.
    opened = []
    try:
        if os.name == "nt":
            cursor = Path(parent.anchor)
            for component in (None, *parent.parts[1:]):
                if component is not None:
                    cursor /= component
                handle, close = _windows_handle(cursor, directory=True)
                opened.append((handle, close))
            root.resolve(relative, write=write)
            yield None, parent, parts[-1]
        else:
            descriptor = os.open(
                parent.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            opened.append((descriptor, os.close))
            for part in parent.parts[1:]:
                descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                opened.append((descriptor, os.close))
            # Validate the storage mount identity once all parents are pinned.
            root.resolve(relative, write=write)
            check_io_cancelled()
            yield descriptor, parent, parts[-1]
    finally:
        for handle, close in reversed(opened):
            close(handle)


@contextmanager
def open_asset(
    root: StorageRoot, relative: str, *, flags=os.O_RDONLY, expected_inode=None
):
    with pinned_parent(
        root, relative, write=bool(flags & (os.O_WRONLY | os.O_CREAT))
    ) as (parent_fd, parent, name):
        if os.name == "nt":
            import msvcrt

            handle, close = _windows_handle(parent / name, flags=flags)
            try:
                descriptor = msvcrt.open_osfhandle(
                    handle,
                    os.O_BINARY | (os.O_APPEND if flags & os.O_WRONLY else os.O_RDONLY),
                )
            except BaseException:
                close(handle)
                raise
        else:
            descriptor = os.open(
                name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent_fd
            )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or is_link(info)
                or (
                    expected_inode is not None
                    and (info.st_dev, info.st_ino) != expected_inode
                )
            ):
                raise StorageError(
                    "unsafe_asset_file", "通常の管理ファイルだけを利用できます", 403
                )
            check_io_cancelled()
            handle = os.fdopen(descriptor, "ab" if flags & os.O_WRONLY else "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            yield handle, info


def make_asset_directory(root: StorageRoot, relative: str):
    with pinned_parent(root, relative, write=True) as (descriptor, parent, name):
        try:
            if descriptor is None:
                (parent / name).mkdir()
            else:
                os.mkdir(name, 0o700, dir_fd=descriptor)
        except FileExistsError:
            pass
    # Opening the new directory rejects a concurrently introduced link.
    with pinned_parent(root, relative + "/probe", write=True):
        pass


def remove_asset(root: StorageRoot, relative: str, *, expected_inode=None):
    with pinned_parent(root, relative, write=True) as (descriptor, parent, name):
        try:
            info = (
                (parent / name).lstat()
                if descriptor is None
                else os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or is_link(info)
                or (
                    expected_inode is not None
                    and (info.st_dev, info.st_ino) != expected_inode
                )
            ):
                raise StorageError(
                    "unsafe_asset_file", "管理ファイルが置き換えられています", 403
                )
            check_io_cancelled()
            if descriptor is None:
                (parent / name).unlink()
            else:
                os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            return


def publish_asset(root: StorageRoot, stage: str, destination: str, expected_inode):
    # Both names live in the same pinned parent; no path-based replace.
    if stage.rsplit("/", 1)[0] != destination.rsplit("/", 1)[0]:
        raise StorageError("invalid_path", "素材保存先が一致しません", 403)
    with open_asset(
        root, stage, flags=os.O_WRONLY | os.O_APPEND, expected_inode=expected_inode
    ) as (handle, _):
        os.fsync(handle.fileno())
    with pinned_parent(root, stage, write=True) as (descriptor, parent, name):
        target = destination.rsplit("/", 1)[-1]
        info = (
            (parent / name).lstat()
            if descriptor is None
            else os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        )
        if is_link(info) or (info.st_dev, info.st_ino) != expected_inode:
            raise StorageError(
                "unsafe_asset_file", "素材 staging が置き換えられています", 403
            )
        check_io_cancelled()
        if descriptor is None:
            # Windows rename refuses an existing destination.
            os.rename(parent / name, parent / target)
        else:
            # link is an atomic no-replace publication, followed by stage removal.
            os.link(
                name,
                target,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
                follow_symlinks=False,
            )
            os.unlink(name, dir_fd=descriptor)
