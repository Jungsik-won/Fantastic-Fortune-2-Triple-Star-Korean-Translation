"""Streaming, offline FF2 patch application with immutable source checks."""
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

CHUNK = 4 * 1024 * 1024
PATCH_SHA256 = '1676aee08026c267bf65c103531b0f7e338ee87f6ba12fba6d1ecf60dff20978'
SOURCE_SHA256 = 'a92f19c3402592aaabd0f1c4fdd67a839c32cde5cd133e861c16bacca8322ac7'
OUTPUT_SHA256 = '8c71d294615233d5fedd326d49249da0203c2025b0b4ae4b7168d7edfc49c3fa'
SOURCE_SIZE = 3231907840


class PatchError(Exception):
    pass


class Cancelled(PatchError):
    pass


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise Cancelled('패치를 취소했습니다. 원본 ISO는 그대로입니다.')


def file_hash(path, cancel=None, progress=None):
    digest = hashlib.sha256()
    size = Path(path).stat().st_size
    pos = 0
    with open(path, 'rb') as f:
        while True:
            check_cancel(cancel)
            block = f.read(CHUNK)
            if not block:
                break
            digest.update(block)
            pos += len(block)
            if progress:
                progress(pos / max(1, size))
    return digest.hexdigest()


def load_patch(path, expected_digest=PATCH_SHA256, cancel=None):
    check_cancel(cancel)
    if file_hash(path, cancel) != expected_digest:
        raise PatchError('포함된 패치 파일이 손상됐습니다. GUI 꾸러미를 다시 받아 주세요.')
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            patch = json.load(f)
        if patch.get('format') != 'ff2-ko-patch-v1':
            raise ValueError('format')
        size = patch['source_size']
        if type(size) is not int or size <= 0:
            raise ValueError('size')
        if len(patch['source_sha256']) != 64:
            raise ValueError('hash')
        spans = []
        last = 0
        for h in patch['hunks']:
            check_cancel(cancel)
            off = h['iso_offset']
            before = bytes.fromhex(h['before'])
            after = bytes.fromhex(h['after'])
            if type(off) is not int or off < last or len(before) != len(after) or off + len(after) > size:
                raise ValueError('span')
            spans.append((off, before, after))
            last = off + len(after)
        return {'source_size': size, 'source_sha256': patch['source_sha256'], 'spans': spans}
    except (ValueError, KeyError, TypeError, OSError) as e:
        raise PatchError('패치 구조가 올바르지 않습니다. GUI 꾸러미를 다시 받아 주세요.') from e


def next_output(source, folder=None):
    source = Path(source)
    folder = Path(folder) if folder else source.parent
    stem = source.stem + ' (Korean v0.9.1)'
    candidate = folder / (stem + '.iso')
    n = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = folder / (stem + ' (' + str(n) + ').iso')
        n += 1
    return candidate


def publish_no_replace(temp, output, cancel=None, progress=None, expected_digest=None):
    """Atomically link on APFS/NTFS; use exclusive copying on exFAT/FAT."""
    check_cancel(cancel)
    try:
        os.link(temp, output)
        return
    except OSError as e:
        if e.errno == errno.EEXIST or output.exists() or output.is_symlink():
            raise PatchError('저장할 파일이 이미 생겼습니다. 기존 파일은 덮어쓰지 않습니다.') from e
        if e.errno not in (errno.EPERM, errno.EACCES, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EINVAL):
            raise
    # Some USB filesystems do not support hard links. Exclusive open protects
    # any existing file, and partial output is removed on cancellation/failure.
    created = False
    try:
        with open(output, 'xb') as dst:
            created = True
            size = temp.stat().st_size
            pos = 0
            with open(temp, 'rb') as src:
                while True:
                    check_cancel(cancel)
                    block = src.read(CHUNK)
                    if not block:
                        break
                    dst.write(block)
                    pos += len(block)
                    if progress:
                        progress(pos / size)
            dst.flush()
            os.fsync(dst.fileno())
        if expected_digest and file_hash(output, cancel) != expected_digest:
            raise PatchError('완성 파일 저장 중 검증에 실패했습니다. 디스크 상태를 확인해 주세요.')
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def apply(source, patch_path, output, cancel=None, progress=None,
          patch_digest=PATCH_SHA256, output_digest=OUTPUT_SHA256):
    source, output = Path(source), Path(output)
    def emit(stage, pct):
        check_cancel(cancel)
        if progress:
            progress(stage, pct)
    if source.resolve() == output.resolve() or output.exists() or output.is_symlink():
        raise PatchError('기존 파일과 원본 ISO는 덮어쓰지 않습니다.')
    if not source.is_file():
        raise PatchError('선택한 ISO 파일을 찾을 수 없습니다.')
    emit('포함된 패치를 확인하고 있습니다', 0)
    patch = load_patch(patch_path, patch_digest, cancel)
    size = patch['source_size']
    if source.stat().st_size != size:
        raise PatchError('원본 ISO 크기가 다릅니다. 일본판 SLPS-25396 원본 ISO를 선택해 주세요.')
    emit('원본 ISO를 확인하고 있습니다', 3)
    digest = file_hash(source, cancel, lambda f: emit('원본 ISO를 확인하고 있습니다', 3 + 27*f))
    if digest != patch['source_sha256']:
        if digest == output_digest:
            raise PatchError('이미 한글 패치가 적용된 ISO입니다. 일본판 원본 ISO를 선택해 주세요.')
        raise PatchError('원본 ISO의 SHA-256이 다릅니다. 일본판 SLPS-25396의 수정하지 않은 원본 ISO가 필요합니다.')
    if not output.parent.is_dir():
        raise PatchError('저장 폴더를 찾을 수 없습니다.')
    if shutil.disk_usage(output.parent).free < size + CHUNK:
        raise PatchError('저장 공간이 부족합니다. ISO 한 개 크기인 약 3.1 GiB 이상의 여유 공간이 필요합니다.')
    emit('한글판 ISO를 만들고 있습니다', 30)
    fd, temp_name = tempfile.mkstemp(prefix='.ff2-ko-', suffix='.building', dir=str(output.parent))
    temp = Path(temp_name)
    original_hash = hashlib.sha256()
    patched_hash = hashlib.sha256()
    last_report = [0.0]
    def update(pos):
        now = time.monotonic()
        if now - last_report[0] >= 0.1 or pos == size:
            emit('한글판 ISO를 만들고 있습니다', 30 + 50*pos/size)
            last_report[0] = now
    try:
        with os.fdopen(fd, 'wb') as dst, source.open('rb') as src:
            pos = 0
            for off, before, after in patch['spans'] + [(size, b'', b'')]:
                check_cancel(cancel)
                while pos < off:
                    check_cancel(cancel)
                    block = src.read(min(CHUNK, off-pos))
                    if not block:
                        raise PatchError('작업 중 원본 ISO가 변경되거나 읽기가 중단됐습니다.')
                    original_hash.update(block)
                    patched_hash.update(block)
                    dst.write(block)
                    pos += len(block)
                    update(pos)
                actual = src.read(len(before))
                if actual != before:
                    raise PatchError('원본 ISO의 변경 구간이 예상과 다릅니다. 패치를 중단했습니다.')
                original_hash.update(actual)
                patched_hash.update(after)
                dst.write(after)
                pos += len(after)
                update(pos)
            if src.read(1):
                raise PatchError('작업 중 원본 ISO의 크기가 변경됐습니다.')
            dst.flush()
            os.fsync(dst.fileno())
        if original_hash.hexdigest() != patch['source_sha256']:
            raise PatchError('작업 중 원본 ISO가 변경됐습니다. 패치를 중단했습니다.')
        if patched_hash.hexdigest() != output_digest or temp.stat().st_size != size:
            raise PatchError('패치 결과가 예상과 다릅니다. 완성 파일을 저장하지 않았습니다.')
        emit('완성 ISO를 검증하고 있습니다', 80)
        if file_hash(temp, cancel, lambda f: emit('완성 ISO를 검증하고 있습니다', 80 + 17*f)) != output_digest:
            raise PatchError('저장된 ISO의 검증에 실패했습니다. 디스크 상태를 확인해 주세요.')
        emit('완성 파일을 저장하고 있습니다', 97)
        publish_no_replace(temp, output, cancel,
                           lambda f: emit('완성 파일을 저장하고 있습니다', 97 + 2*f),
                           expected_digest=output_digest)
        # No cancellation check after commit: a verified output now exists.
        if progress:
            progress('한글 패치가 완료됐습니다', 100)
        return output
    finally:
        temp.unlink(missing_ok=True)
