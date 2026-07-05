from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from stack_backend.net import display_host, http_url

STACK_DIR = Path(__file__).resolve().parent.parent
STACK_UV_PYTHON = "3.14"
DEFAULT_SHIELDED_RELAYER_PORT = 38180
DEFAULT_SHIELDED_RELAYER_HOST = "127.0.0.1"
_PROCESS_DIR = STACK_DIR / ".artifacts" / "shielded-relayer"
_PID_PATH = _PROCESS_DIR / "shielded-relayer.pid"
_LOG_PATH = _PROCESS_DIR / "shielded-relayer.log"


def resolve_repo_dir(
    name: str,
    env_var: str,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    source_env = os.environ if env is None else env
    explicit = source_env.get(env_var)
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (STACK_DIR.parent / name).resolve()


def resolve_shielded_relayer_pid_path() -> Path:
    return _PID_PATH


def resolve_shielded_relayer_log_path() -> Path:
    return _LOG_PATH


def shielded_relayer_endpoints(
    *,
    bind_host: str,
    port: int,
    public_host: str | None = None,
) -> dict[str, str]:
    public_host = public_host or display_host(bind_host)
    base_url = http_url(public_host, port)
    return {
        "shielded_relayer": base_url,
        "shielded_relayer_health": f"{base_url}/health",
        "shielded_relayer_info": f"{base_url}/v1/info",
        "shielded_relayer_metrics": f"{base_url}/metrics",
        "shielded_relayer_quote": f"{base_url}/v1/quote",
        "shielded_relayer_jobs": f"{base_url}/v1/jobs",
    }


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _process_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _probe_health(url: str, *, timeout: float = 1.5) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return int(status) < 500
    except (OSError, URLError, TimeoutError, ValueError):
        return False


def get_shielded_relayer_status(
    *,
    bind_host: str,
    port: int,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    pid = _read_pid(resolve_shielded_relayer_pid_path())
    running = _process_running(pid)
    if pid is not None and not running:
        try:
            resolve_shielded_relayer_pid_path().unlink()
        except OSError:
            pass
        pid = None

    endpoints = shielded_relayer_endpoints(bind_host=bind_host, port=port)
    health_ok = _probe_health(endpoints["shielded_relayer_health"]) if running else False
    source_env = os.environ if env is None else env

    return {
        "shielded_relayer_running": running,
        "shielded_relayer_pid": pid,
        "shielded_relayer_health_ok": health_ok,
        "shielded_relayer_log_path": str(resolve_shielded_relayer_log_path()),
        "shielded_relayer_pid_path": str(resolve_shielded_relayer_pid_path()),
        "shielded_relayer_auth_required": bool(source_env.get("XIAN_SHIELDED_RELAYER_AUTH_TOKEN")),
        **endpoints,
    }


def _build_runtime_env(env: dict[str, str] | None = None) -> dict[str, str]:
    source_env = os.environ.copy()
    if env is not None:
        source_env.update(env)
    source_env.setdefault("XIAN_STACK_DIR", str(STACK_DIR))
    source_env.setdefault(
        "XIAN_PY_DIR",
        str(resolve_repo_dir("xian-py", "XIAN_PY_DIR", env=source_env)),
    )
    source_env.setdefault(
        "XIAN_CONTRACTING_DIR",
        str(
            resolve_repo_dir(
                "xian-contracting",
                "XIAN_CONTRACTING_DIR",
                env=source_env,
            )
        ),
    )
    return source_env


def _require_relayer_credentials(env: dict[str, str]) -> None:
    private_key = (env.get("XIAN_SHIELDED_RELAYER_PRIVATE_KEY") or "").strip()
    key_file = (env.get("XIAN_SHIELDED_RELAYER_PRIVATE_KEY_FILE") or "").strip()
    if private_key or key_file:
        return
    raise RuntimeError(
        "shielded relayer requires XIAN_SHIELDED_RELAYER_PRIVATE_KEY or "
        "XIAN_SHIELDED_RELAYER_PRIVATE_KEY_FILE"
    )


def _wait_for_ready(url: str, *, timeout_seconds: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _probe_health(url):
            return
        time.sleep(0.25)
    raise TimeoutError(f"shielded relayer did not become ready at {url}")


def start_shielded_relayer_runtime(
    *,
    bind_host: str,
    port: int,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    source_env = _build_runtime_env(env)
    _require_relayer_credentials(source_env)

    existing = get_shielded_relayer_status(
        bind_host=bind_host,
        port=port,
        env=source_env,
    )
    if existing["shielded_relayer_running"]:
        return existing

    _PROCESS_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = resolve_shielded_relayer_log_path().open(
        "a",
        encoding="utf-8",
    )
    script_path = STACK_DIR / "scripts" / "shielded_relayer_service.py"
    command = [
        "uv",
        "run",
        "--project",
        source_env["XIAN_PY_DIR"],
        "--python",
        STACK_UV_PYTHON,
        "python3",
        str(script_path),
        "serve",
    ]
    process = subprocess.Popen(
        command,
        cwd=STACK_DIR,
        env=source_env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    resolve_shielded_relayer_pid_path().write_text(
        str(process.pid),
        encoding="utf-8",
    )
    try:
        _wait_for_ready(
            shielded_relayer_endpoints(bind_host=bind_host, port=port)["shielded_relayer_health"]
        )
    except Exception:
        stop_shielded_relayer_runtime(
            bind_host=bind_host,
            port=port,
            env=source_env,
        )
        raise
    finally:
        log_handle.close()

    return get_shielded_relayer_status(
        bind_host=bind_host,
        port=port,
        env=source_env,
    )


def stop_shielded_relayer_runtime(
    *,
    bind_host: str,
    port: int,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    pid_path = resolve_shielded_relayer_pid_path()
    pid = _read_pid(pid_path)
    if _process_running(pid):
        assert pid is not None
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and _process_running(pid):
            time.sleep(0.25)
        if _process_running(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
    if pid_path.exists():
        try:
            pid_path.unlink()
        except OSError:
            pass

    payload = get_shielded_relayer_status(
        bind_host=bind_host,
        port=port,
        env=env,
    )
    payload["shielded_relayer_stopped"] = True
    return payload
