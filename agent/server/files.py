"""Read-only workspace browsing. Every opened path stays below its directory FD."""
import base64
from contextlib import contextmanager
import errno
import heapq
import os
from pathlib import Path, PurePosixPath
import stat

TEXT_LIMIT = 256 * 1024
IMAGE_LIMIT = 4 * 1024 * 1024


class FileRequestError(ValueError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _parts(path: str) -> list[str]:
    if '\x00' in path or '\\' in path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
        raise FileRequestError('路径必须位于当前工作区内', 422)
    return list(PurePosixPath(path).parts)


@contextmanager
def _directory(workspace: Path, parts: list[str]):
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(workspace, flags)
        for part in parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except FileRequestError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise FileRequestError('文件或目录不存在', 404) from None
        if exc.errno in (errno.EACCES, errno.EPERM, errno.ELOOP, errno.ENOTDIR):
            raise FileRequestError('无法访问该路径，不跟随符号链接或越过工作区边界', 403) from None
        raise FileRequestError('无法读取该文件或目录', 422) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def directory_files(workspace: Path, path: str = '', offset: int = 0, limit: int = 200) -> dict:
    parts = _parts(path)
    if not 0 <= offset <= 100000 or not 1 <= limit <= 500:
        raise FileRequestError('无效的目录分页参数', 422)
    with _directory(workspace, parts) as descriptor:
        def row(entry):
            if entry.is_symlink():
                kind = 'symlink'
            elif entry.is_dir(follow_symlinks=False):
                kind = 'directory'
            elif entry.is_file(follow_symlinks=False):
                kind = 'file'
            else:
                kind = 'other'
            return {'name': entry.name, 'path': '/'.join([*parts, entry.name]), 'kind': kind}
        def key(item):
            return (item['kind'] != 'directory', item['name'].casefold(), item['name'])
        with os.scandir(descriptor) as iterator:
            entries = heapq.nsmallest(offset + limit + 1, (row(entry) for entry in iterator), key=key)
        rows = entries[offset:offset + limit]
        return {'path': '/'.join(parts), 'entries': rows,
                'next_offset': offset + limit if len(entries) > offset + limit else None}


def _image_type(data: bytes) -> str | None:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def preview_file(workspace: Path, path: str) -> dict:
    parts = _parts(path)
    if not parts:
        raise FileRequestError('请选择文件', 422)
    with _directory(workspace, parts[:-1]) as directory:
        info = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        base = {'path': '/'.join(parts), 'size': info.st_size, 'truncated': False}
        if stat.S_ISLNK(info.st_mode):
            return {**base, 'kind': 'symlink', 'target': os.readlink(parts[-1], dir_fd=directory)}
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise FileRequestError('仅支持预览普通文件', 422)
            base['size'] = info.st_size
            data = file.read(IMAGE_LIMIT + 1)
        mime = _image_type(data)
        if mime and len(data) <= IMAGE_LIMIT:
            return {**base, 'kind': 'image', 'mime': mime,
                    'data_url': f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')}
        if mime or b'\0' in data:
            return {**base, 'kind': 'binary', 'truncated': len(data) > IMAGE_LIMIT}
        sample = data[:TEXT_LIMIT]
        try:
            # A complete UTF-8 character may straddle the preview boundary.
            import codecs
            content = codecs.getincrementaldecoder('utf-8')().decode(sample, final=len(data) <= TEXT_LIMIT)
        except UnicodeDecodeError:
            return {**base, 'kind': 'binary'}
        return {**base, 'kind': 'text', 'content': content, 'truncated': len(data) > TEXT_LIMIT}
