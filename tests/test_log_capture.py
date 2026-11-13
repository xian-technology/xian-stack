from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/log_capture.py"


# Generous bound for slow process start-up and teardown on a loaded host. Each
# wait returns as soon as its condition holds, so only a real hang reaches it.
WAIT_SECONDS = 30


def wait_until(condition, timeout=WAIT_SECONDS):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def process_gone(pid):
    # A killed process lingers until the kernel finishes tearing it down, then as
    # a zombie until launchd/init reaps it; either way it can no longer run.
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return not result.stdout.strip() or result.stdout.strip().startswith("Z")


def load_capture():
    spec = importlib.util.spec_from_file_location("capture_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_size_age_and_ownership(tmp_path):
    module = load_capture()
    path = tmp_path / "service.log"
    unrelated = tmp_path / "other.log"
    unrelated.write_text("keep")
    with module.CaptureSink(
        path, max_file_bytes=8, max_total_bytes=24, retention_seconds=10
    ) as sink:
        for _ in range(10):
            sink.write(b"12345678")
        owned = [path, *sink.archives()]
        assert sum(p.stat().st_size for p in owned) <= 24
        for p in sink.archives():
            os.utime(p, (0, 0))
        sink.maintain(now=time.time() + 20)
        assert not sink.archives()
    assert unrelated.read_text() == "keep"


def test_second_writer_is_rejected(tmp_path):
    module = load_capture()
    with module.CaptureSink(tmp_path / "service.log"):
        with pytest.raises(BlockingIOError):
            with module.CaptureSink(tmp_path / "service.log"):
                pass


def test_wrapper_preserves_child_status_and_stderr(tmp_path):
    path = tmp_path / "service.log"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--log",
            str(path),
            "--",
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
        ],
        start_new_session=True,
        timeout=WAIT_SECONDS,
    )
    assert result.returncode == 7
    assert "out" in path.read_text() and "err" in path.read_text()
    assert "exit_code=7" in path.read_text()


def test_direct_term_stops_child_and_drains_shutdown(tmp_path):
    path = tmp_path / "service.log"
    child = (
        "import signal,time; signal.signal(signal.SIGTERM, "
        "lambda *_: (print('stopped',flush=True),exit(0))); "
        "print('ready',flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--log", str(path), "--", sys.executable, "-c", child],
        start_new_session=True,
    )
    try:
        assert wait_until(lambda: path.exists() and "ready" in path.read_text())
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=WAIT_SECONDS) == -signal.SIGKILL
        assert "stopped" in path.read_text()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_missing_child_fails_without_leaking_arguments(tmp_path):
    path = tmp_path / "service.log"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--log", str(path), "--", "/missing-program", "secret"],
        start_new_session=True,
        timeout=WAIT_SECONDS,
    )
    assert result.returncode != 0
    assert "FileNotFoundError" in path.read_text()
    assert "secret" not in path.read_text()


def test_idle_capture_expires_logs_without_another_write(tmp_path):
    source = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from log_capture import CaptureSink, capture; "
        "sink=CaptureSink(sys.argv[2], retention_seconds=0.1); "
        "sink.__enter__(); "
        "capture([sys.executable, '-c', \"import time; print('old',flush=True); time.sleep(.5)\"], "
        "sink, cleanup_interval=.02); sink.__exit__()"
    )
    path = tmp_path / "service.log"
    subprocess.run(
        [sys.executable, "-c", source, str(SCRIPT.parent), str(path)],
        start_new_session=True,
        check=True,
        timeout=WAIT_SECONDS,
    )
    assert path.read_bytes() == b""
    assert not load_capture().CaptureSink(path).archives()


@pytest.mark.parametrize("failure", ["sink", "forced_kill"])
def test_capture_failure_and_forced_stop_kill_stubborn_descendants(tmp_path, failure):
    import json

    pids = tmp_path / "pids.json"
    child_source = (
        "import json,os,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "grand=subprocess.Popen([sys.executable,'-c', "
        "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
        # Publish both pids atomically: the test reads the file once it exists.
        f"open({str(pids) + '.tmp'!r},'w').write(json.dumps([os.getpid(),grand.pid])); "
        f"os.replace({str(pids) + '.tmp'!r},{str(pids)!r}); "
        "print('ready',flush=True); time.sleep(60)"
    )
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
        "from log_capture import CaptureSink,capture\n"
        "class BrokenSink:\n"
        " def write(self, data): raise OSError('simulated disk failure')\n"
        " def maintain(self): pass\n"
        f"sink=BrokenSink() if {failure!r} == 'sink' else "
        f"CaptureSink({str(tmp_path / 'out.log')!r}).__enter__()\n"
        f"capture([sys.executable,'-c',{child_source!r}],sink)\n"
    )
    process = subprocess.Popen([sys.executable, str(runner)], start_new_session=True)
    try:
        assert wait_until(pids.exists)
        children = json.loads(pids.read_text())
        if failure == "forced_kill":
            os.killpg(process.pid, signal.SIGKILL)
        assert process.wait(timeout=WAIT_SECONDS) == -signal.SIGKILL
        for pid in children:
            assert wait_until(lambda pid=pid: process_gone(pid))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_direct_term_after_pipe_eof_remains_bounded(tmp_path):
    ready = tmp_path / "ready"
    path = tmp_path / "service.log"
    child = (
        "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"open({str(ready)!r},'w').write('ready'); "
        "os.close(1); os.close(2); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--log", str(path), "--", sys.executable, "-c", child],
        start_new_session=True,
    )
    try:
        assert wait_until(ready.exists)
        # Let the wrapper observe EOF before signalling it.
        time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=WAIT_SECONDS) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
