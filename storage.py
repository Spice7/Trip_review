"""Durable JSON writes and an OS lock released automatically on process exit."""

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class StateError(RuntimeError):
    pass


def replace_with_retry(source, target):
    """Eight bounded atomic replace attempts for transient Windows sharing locks."""
    delays = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33)
            if not transient or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (ValueError, OSError) as exc:
        raise StateError(f"상태 파일을 읽을 수 없습니다. 삭제하지 말고 복구하세요: {path}") from exc


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(name, path)
    finally:
        if os.path.exists(name):
            try:
                os.unlink(name)
            except OSError:
                pass  # Preserve the original write error if cleanup is also locked.


@contextmanager
def output_lock(root: Path):
    """Lock the entire read/plan/collect/export transaction, including dry runs."""
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / ".collector.lock").open("a+b")
    stream.seek(0, 2)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise StateError("같은 output을 사용하는 수집기가 실행 중입니다.") from exc
    try:
        yield
    finally:
        stream.close()
