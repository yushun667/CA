"""Native file-event backends for ``ca watch`` (no third-party dependencies).

Two platform implementations are provided behind :func:`create_native_watcher`:

* Linux: ``libc`` inotify via :mod:`ctypes`; recursive directory watches, with
  new directories watched as they appear and deleted/moved directories pruned.
* macOS: CoreServices FSEvents via :mod:`ctypes`; file-level events on a private
  run-loop thread with ~0.1 s latency.

Both enqueue absolute file paths that pass :func:`is_watched_file`; the polling
backend in :mod:`ca.cli.watch` remains the fallback when the platform/framework
is unavailable or initialization fails. The watcher API is intentionally small:

``start()``, ``stop()``, ``wait(timeout) -> list[str]`` and ``backend``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import select
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".c++"})
HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".inc"})
WATCHED_SUFFIXES = SOURCE_SUFFIXES | HEADER_SUFFIXES
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".tmp",
        "build",
        "out",
        "node_modules",
        ".ca-cache",
        ".venv",
        "dist",
        "target",
    }
)
_MAX_DIRS = 50_000


class NativeWatchError(RuntimeError):
    """Native watcher initialization failed (callers fall back to polling)."""


def is_watched_file(path: str | os.PathLike[str]) -> bool:
    """True for a file whose suffix is source/header (case-insensitive)."""

    return Path(path).suffix.lower() in WATCHED_SUFFIXES


def should_skip_path(path: str | os.PathLike[str]) -> bool:
    """True when any path component is a skipped build/VCS directory."""

    for part in Path(path).parts:
        if part in SKIP_DIR_NAMES:
            return True
    return False


def filter_event_paths(
    repo: str | os.PathLike[str], paths: Iterable[str | os.PathLike[str]]
) -> list[str]:
    """Normalize, dedupe and filter raw backend event paths."""

    root = Path(repo).expanduser().resolve()
    selected: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve()
        except OSError:
            continue
        key = str(candidate)
        if key in seen or should_skip_path(candidate) or not is_watched_file(candidate):
            continue
        seen.add(key)
        selected.append(key)
    return selected


def _walk_directories(root: Path) -> Iterable[Path]:
    """Yield watched directories under ``root`` (skipping build/VCS trees)."""

    count = 0
    for current, dirs, _files in os.walk(root):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if name not in SKIP_DIR_NAMES]
        yield current_path
        count += 1
        if count >= _MAX_DIRS:
            return


class NativeWatcher:
    """Common watcher lifecycle (subclasses implement platform details)."""

    backend = "native"
    platform = ""

    def __init__(self, repo: str | os.PathLike[str]) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self._events: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def wait(self, timeout: float = 1.0) -> list[str]:
        """Drain deduplicated paths seen within ``timeout`` seconds."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        found: list[str] = []
        seen: set[str] = set()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and found:
                break
            try:
                path = self._events.get(timeout=max(0.0, remaining))
            except queue.Empty:
                break
            if path and path not in seen:
                seen.add(path)
                found.append(path)
            if time.monotonic() >= deadline:
                # Drain immediately-available events without waiting again.
                while True:
                    try:
                        path = self._events.get_nowait()
                    except queue.Empty:
                        break
                    if path and path not in seen:
                        seen.add(path)
                        found.append(path)
                break
        return found

    def _push(self, path: str) -> None:
        if self._stop.is_set():
            return
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            return
        if not should_skip_path(resolved) and is_watched_file(resolved):
            self._events.put(resolved)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class InotifyWatcher(NativeWatcher):
    """Linux inotify backend using libc through ctypes."""

    backend = "native"
    platform = "linux"
    _MASK = (
        0x00000002  # IN_MODIFY
        | 0x00000004  # IN_ATTRIB
        | 0x00000008  # IN_CLOSE_WRITE
        | 0x00000040  # IN_MOVED_FROM
        | 0x00000080  # IN_MOVED_TO
        | 0x00000100  # IN_CREATE
        | 0x00000200  # IN_DELETE
        | 0x00000400  # IN_DELETE_SELF
        | 0x00000800  # IN_MOVE_SELF
    )
    _IN_IGNORED = 0x00008000
    _IN_ISDIR = 0x40000000

    def __init__(self, repo: str | os.PathLike[str]) -> None:
        super().__init__(repo)
        self._libc: ctypes.CDLL | None = None
        self._fd: int | None = None
        self._wd_paths: dict[int, Path] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError as exc:  # pragma: no cover - platform guard
            raise NativeWatchError(f"libc unavailable: {exc}") from exc
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.inotify_rm_watch.restype = ctypes.c_int
        # IN_NONBLOCK(0x800) | IN_CLOEXEC(0x80000)
        flags = 0x800 | 0x80000
        fd = libc.inotify_init1(flags)
        if fd < 0:  # pragma: no cover - platform guard
            errno = ctypes.get_errno()
            raise NativeWatchError(f"inotify_init1 failed (errno {errno})")
        self._libc = libc
        self._fd = int(fd)
        for directory in _walk_directories(self.repo):
            self._add_watch(directory)
        self._thread = threading.Thread(
            target=self._run, name="ca-inotify", daemon=True
        )
        self._started = True
        self._thread.start()

    def _add_watch(self, directory: Path) -> None:
        if self._libc is None or self._fd is None:
            return
        if should_skip_path(directory):
            return
        with self._lock:
            if directory in self._wd_paths.values() or len(self._wd_paths) >= _MAX_DIRS:
                return
            wd = self._libc.inotify_add_watch(
                self._fd, str(directory).encode("utf-8", "surrogatepass"), self._MASK
            )
            if wd >= 0:
                self._wd_paths[int(wd)] = directory

    def _remove_watch(self, wd: int) -> None:
        if self._libc is None or self._fd is None:
            return
        with self._lock:
            self._wd_paths.pop(wd, None)
        try:
            self._libc.inotify_rm_watch(self._fd, wd)
        except Exception:  # pragma: no cover - best effort
            pass

    def _remove_tree(self, directory: Path) -> None:
        prefix = str(directory) + os.sep
        with self._lock:
            stale = [
                wd
                for wd, path in self._wd_paths.items()
                if str(path) == str(directory) or str(path).startswith(prefix)
            ]
        for wd in stale:
            self._remove_watch(wd)

    def _run(self) -> None:
        if self._fd is None:
            return
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(self._fd, 256 * 1024)
            except OSError:
                break
            offset = 0
            while offset + 16 <= len(data):
                wd, mask, _cookie, name_len = struct.unpack_from("iIII", data, offset)
                offset += 16
                raw_name = data[offset : offset + name_len].split(b"\x00", 1)[0]
                offset += name_len
                try:
                    name = raw_name.decode("utf-8", "surrogateescape")
                except Exception:  # pragma: no cover - defensive
                    name = ""
                with self._lock:
                    directory = self._wd_paths.get(int(wd))
                if directory is None:
                    if mask & self._IN_IGNORED:
                        self._remove_watch(int(wd))
                    continue
                event_path = directory / name if name else directory
                if mask & self._IN_IGNORED:
                    self._remove_watch(int(wd))
                    continue
                if mask & self._IN_ISDIR:
                    if (mask & 0x00000100) or (mask & 0x00000080):  # create/moved_to
                        for child in _walk_directories(event_path):
                            self._add_watch(child)
                    elif (mask & 0x00000200) or (mask & 0x00000040):  # delete/moved_from
                        self._remove_tree(event_path)
                    continue
                self._push(str(event_path))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


# --------------------------------------------------------------------------- #
# macOS FSEvents
# --------------------------------------------------------------------------- #

_CF_STRING_ENCODING_UTF8 = 0x08000100
_KFSEVENT_STREAM_CREATE_FLAG_FILE_EVENTS = 0x00000010
_FSEVENT_FLAG_ITEM_CREATED = 0x00000100
_FSEVENT_FLAG_ITEM_REMOVED = 0x00000200
_FSEVENT_FLAG_ITEM_MODIFIED = 0x00000400
_FSEVENT_FLAG_ITEM_RENAMED = 0x00000800
_FSEVENT_FLAG_ITEM_INODE_META_MOD = 0x00000010


class FSEventsWatcher(NativeWatcher):
    """macOS FSEvents backend using CoreServices/CoreFoundation via ctypes."""

    backend = "native"
    platform = "darwin"

    def __init__(self, repo: str | os.PathLike[str]) -> None:
        super().__init__(repo)
        self._cf: ctypes.CDLL | None = None
        self._cs: ctypes.CDLL | None = None
        self._stream: ctypes.c_void_p | None = None
        self._loop: ctypes.c_void_p | None = None
        self._callback_ref: ctypes.CFUNCTYPE | None = None
        self._run_loop_ready = threading.Event()
        self._thread: threading.Thread | None = None

    def _load(self) -> None:
        try:
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        except OSError as exc:
            raise NativeWatchError(f"CoreServices/CoreFoundation unavailable: {exc}") from exc
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p]
        cf.CFArrayCreate.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFRunLoopGetCurrent.argtypes = []
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopRun.argtypes = []
        cf.CFRunLoopRun.restype = None
        cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
        cf.CFRunLoopStop.restype = None
        cs.FSEventStreamCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint64, ctypes.c_double, ctypes.c_uint32,
        ]
        cs.FSEventStreamCreate.restype = ctypes.c_void_p
        cs.FSEventStreamScheduleWithRunLoop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cs.FSEventStreamScheduleWithRunLoop.restype = None
        cs.FSEventStreamStart.argtypes = [ctypes.c_void_p]
        cs.FSEventStreamStart.restype = ctypes.c_bool
        cs.FSEventStreamStop.argtypes = [ctypes.c_void_p]
        cs.FSEventStreamStop.restype = None
        cs.FSEventStreamInvalidate.argtypes = [ctypes.c_void_p]
        cs.FSEventStreamInvalidate.restype = None
        cs.FSEventStreamRelease.argtypes = [ctypes.c_void_p]
        cs.FSEventStreamRelease.restype = None
        self._cf = cf
        self._cs = cs

    def _event_callback(self, _stream, _info, count, event_paths, flags, _ids) -> None:
        try:
            if not event_paths:
                return
            if flags and count:
                for index in range(int(count)):
                    flag = int(flags[index])
                    if not (
                        flag & _FSEVENT_FLAG_ITEM_CREATED
                        or flag & _FSEVENT_FLAG_ITEM_REMOVED
                        or flag & _FSEVENT_FLAG_ITEM_MODIFIED
                        or flag & _FSEVENT_FLAG_ITEM_RENAMED
                        or flag & _FSEVENT_FLAG_ITEM_INODE_META_MOD
                    ):
                        continue
                    raw = event_paths[index]
                    if raw:
                        self._push(raw.decode("utf-8", "surrogateescape"))
            else:  # pragma: no cover - defensive older callback shape
                for index in range(int(count)):
                    raw = event_paths[index]
                    if raw:
                        self._push(raw.decode("utf-8", "surrogateescape"))
        except Exception:
            # FSEvents callbacks must never raise into the run loop.
            pass

    def _run_loop(self) -> None:
        assert self._cf is not None and self._cs is not None
        cf, cs = self._cf, self._cs
        callback_type = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
        )
        self._callback_ref = callback_type(self._event_callback)
        context = (ctypes.c_void_p * 5)()
        context[0] = ctypes.c_void_p()  # version
        context[1] = ctypes.c_void_p()  # info
        context[2] = ctypes.c_void_p()  # retain
        context[3] = ctypes.c_void_p()  # release
        context[4] = ctypes.c_void_p()  # copyDescription
        cf_string = cf.CFStringCreateWithCString(
            None, str(self.repo).encode("utf-8", "surrogatepass"), _CF_STRING_ENCODING_UTF8
        )
        if not cf_string:
            return
        cf_array = cf.CFArrayCreate(None, ctypes.byref(ctypes.c_void_p(cf_string)), 1, None)
        if not cf_array:
            cf.CFRelease(cf_string)
            return
        try:
            stream = cs.FSEventStreamCreate(
                None,
                self._callback_ref,
                ctypes.byref(context),
                cf_array,
                0xFFFFFFFFFFFFFFFF,  # kFSEventStreamEventIdSinceNow
                0.1,  # latency seconds
                _KFSEVENT_STREAM_CREATE_FLAG_FILE_EVENTS,
            )
            if not stream:
                return
            loop = cf.CFRunLoopGetCurrent()
            self._loop = ctypes.c_void_p(loop) if loop else None
            mode_value = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
            cs.FSEventStreamScheduleWithRunLoop(stream, loop, mode_value)
            if not cs.FSEventStreamStart(stream):
                return
            self._stream = ctypes.c_void_p(stream)
            self._run_loop_ready.set()
            cf.CFRunLoopRun()
        finally:
            if self._stream:
                cs.FSEventStreamStop(self._stream)
                cs.FSEventStreamInvalidate(self._stream)
                cs.FSEventStreamRelease(self._stream)
                self._stream = None
            cf.CFRelease(cf_array)
            cf.CFRelease(cf_string)

    def start(self) -> None:
        self._load()
        self._thread = threading.Thread(
            target=self._run_loop, name="ca-fsevents", daemon=True
        )
        self._started = True
        self._thread.start()
        # Give the stream a moment to start so init failures surface as None.
        self._run_loop_ready.wait(timeout=2.0)

    def stop(self) -> None:
        self._stop.set()
        if self._cf is not None and self._loop is not None:
            try:
                self._cf.CFRunLoopStop(self._loop)
            except Exception:  # pragma: no cover
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def create_native_watcher(
    repo: str | os.PathLike[str], *, start: bool = True
) -> NativeWatcher | None:
    """Create and (by default) start the platform-native watcher.

    Returns ``None`` when the platform or framework is unavailable or
    initialization fails; callers must fall back to polling. ``start=False`` is
    for tests that need to inspect availability without starting threads.
    """

    root = Path(repo).expanduser()
    if not root.is_dir():
        return None
    if not start:
        available, _reason = availability()
        if not available:
            return None
    if sys.platform.startswith("linux"):
        watcher: NativeWatcher = InotifyWatcher(root)
    elif sys.platform == "darwin":
        watcher = FSEventsWatcher(root)
    else:
        return None
    if not start:
        return watcher
    try:
        watcher.start()
    except Exception:
        try:
            watcher.stop()
        except Exception:
            pass
        return None
    # FSEvents can fail asynchronously in the run-loop thread; give it a
    # moment and verify the stream/thread is alive.
    if sys.platform == "darwin":
        if not watcher.is_alive():
            watcher.stop()
            return None
    return watcher


def availability() -> tuple[bool, str]:
    """Best-effort availability check without starting a watcher."""

    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if not hasattr(libc, "inotify_init1"):
                return False, "libc has no inotify_init1"
            return True, ""
        except OSError as exc:
            return False, f"libc unavailable: {exc}"
    if sys.platform == "darwin":
        try:
            ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        except OSError as exc:
            return False, f"CoreServices unavailable: {exc}"
        return True, ""
    return False, f"native watcher unsupported on {sys.platform}"
