#!/usr/bin/env python3
"""Single-owner bounded stdout capture for detached local service groups."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

MIB = 1024 * 1024


class CaptureSink:
    def __init__(
        self,
        path,
        *,
        max_file_bytes=50 * MIB,
        max_total_bytes=250 * MIB,
        retention_seconds=7 * 86400,
    ):
        if max_file_bytes <= 0 or max_total_bytes < max_file_bytes or retention_seconds <= 0:
            raise ValueError("positive log limits required; total must cover one file")
        self.path = Path(path)
        self.max_file = max_file_bytes
        self.max_total = max_total_bytes
        self.retention = retention_seconds
        self.fd = None
        self.lock = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self._archive()
            self._open()
            self.maintain()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _open(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        self.started = time.time()
        self.size = os.fstat(self.fd).st_size

    def _archive(self):
        if self.path.is_symlink():
            raise ValueError("log path cannot be a symlink")
        destination = self.path.with_name(f"{self.path.name}.{time.time_ns():020d}")
        os.replace(self.path, destination)

    def archives(self):
        pattern = re.compile(re.escape(self.path.name) + r"\.[0-9]{20}")
        return sorted(
            p
            for p in self.path.parent.iterdir()
            if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        )

    def rotate(self):
        os.close(self.fd)
        self.fd = None
        self._archive()
        self._open()

    def maintain(self, now=None):
        now = time.time() if now is None else now
        if now - self.started >= min(3600, self.retention):
            self.rotate()
        archives = self.archives()
        remaining = sum(p.stat().st_size for p in archives)
        budget = self.max_total - self.max_file
        for index, path in enumerate(archives):
            stat = path.stat()
            if (
                now - stat.st_mtime >= self.retention
                or remaining > budget
                or stat.st_size == 0
                or stat.st_size > self.max_file
                or len(archives) - index > 512
            ):
                path.unlink()
                remaining -= stat.st_size

    def write(self, data):
        # Fixed chunks preserve arbitrary child output without unbounded readline buffers.
        view = memoryview(data)
        while view:
            if self.size >= self.max_file:
                self.rotate()
                self.maintain()
            count = os.write(self.fd, view[: self.max_file - self.size])
            self.size += count
            view = view[count:]

    def __exit__(self, *_args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


def _stop_group(child):
    # The wrapper and all children share one owned group, including the backend's
    # SIGKILL fallback. Never leave a writer behind after losing its log sink.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Descendants can ignore TERM even after their immediate parent exits.
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _drain(child, sink, stopping, cleanup_interval):
    next_cleanup = time.monotonic() + cleanup_interval
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        # EOF only means output closed. Keep maintenance and the shutdown deadline
        # alive until the process itself exits, even when it has no open pipe.
        while selector.get_map() or child.poll() is None:
            for key, _ in selector.select(timeout=min(1, cleanup_interval)):
                data = os.read(key.fd, 65536)
                if data:
                    sink.write(data)
                else:
                    selector.unregister(key.fileobj)
            now = time.monotonic()
            if now >= next_cleanup:
                sink.maintain()
                next_cleanup = now + cleanup_interval
            if stopping and now - stopping[0] >= 5:
                _stop_group(child)


def _exit_record(sink, code):
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    context = f"exit_code={code}" if code >= 0 else f"signal={-code}"
    sink.write(
        (
            f"{timestamp} | ERROR | stack/log-capture | Child exited unsuccessfully | {context}\n"
        ).encode()
    )


def capture(command, sink, *, cleanup_interval=300):
    if os.getpgrp() != os.getpid():
        raise ValueError("capture requires its own process group")
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        sink.write(
            f"{timestamp} | ERROR | stack/log-capture | Child startup failed | "
            f"error_type={type(exc).__name__}\n".encode()
        )
        return 127
    stopping = []

    def terminate(signum, _frame):
        if not stopping:
            stopping.append(time.monotonic())
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            os.killpg(os.getpgrp(), signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        _drain(child, sink, stopping, cleanup_interval)
        code = child.wait()
        if stopping:
            _stop_group(child)
        if code:
            _exit_record(sink, code)
        return code
    except BaseException:
        _stop_group(child)
        raise
    finally:
        child.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("child command required")
    if os.getpgrp() != os.getpid():
        parser.error("capture must start in its own process group")
    with CaptureSink(
        args.log,
        max_file_bytes=int(os.environ.get("XIAN_SERVICE_LOG_MAX_FILE_MB", "50")) * MIB,
        max_total_bytes=int(os.environ.get("XIAN_SERVICE_LOG_MAX_TOTAL_MB", "250")) * MIB,
        retention_seconds=int(os.environ.get("XIAN_SERVICE_LOG_RETENTION_DAYS", "7")) * 86400,
    ) as sink:
        return capture(command, sink)


if __name__ == "__main__":
    sys.exit(main())
