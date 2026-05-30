from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

STACK_DIR = Path(__file__).resolve().parent.parent
STACK_UV_PYTHON = "3.14"
DEFAULT_DEX_AUTOMATION_PORT = 38280
DEFAULT_DEX_AUTOMATION_HOST = "127.0.0.1"
_PROCESS_DIR = STACK_DIR / ".artifacts" / "dex-automation"
_PID_PATH = _PROCESS_DIR / "dex-automation.pid"
_LOG_PATH = _PROCESS_DIR / "dex-automation.log"
_CONFIG_PATH = _PROCESS_DIR / "config.yaml"
_WALLET_KEY_PATH = _PROCESS_DIR / "wallet.key"


def display_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


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


def resolve_dex_automation_repo_dir(*, env: dict[str, str] | None = None) -> Path:
    return resolve_repo_dir(
        "xian-dex-automation",
        "XIAN_DEX_AUTOMATION_DIR",
        env=env,
    )


def resolve_dex_automation_config_path(*, env: dict[str, str] | None = None) -> Path:
    source_env = os.environ if env is None else env
    explicit = source_env.get("XIAN_DEX_AUTOMATION_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _CONFIG_PATH


def resolve_dex_automation_wallet_key_path(*, env: dict[str, str] | None = None) -> Path:
    source_env = os.environ if env is None else env
    explicit = source_env.get("XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _WALLET_KEY_PATH


def require_dex_automation_repo(*, env: dict[str, str] | None = None) -> Path:
    repo_dir = resolve_dex_automation_repo_dir(env=env)
    pyproject = repo_dir / "pyproject.toml"
    missing = [path for path in (repo_dir, pyproject) if not path.exists()]
    if missing:
        missing_list = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(
            f"xian-dex-automation integration requires the sibling repo. Missing: {missing_list}"
        )
    return repo_dir


def dex_automation_endpoints(
    *,
    bind_host: str,
    port: int,
    public_host: str | None = None,
) -> dict[str, str]:
    public_host = public_host or display_host(bind_host)
    base_url = f"http://{public_host}:{port}"
    return {
        "dex_automation": base_url,
        "dex_automation_health": f"{base_url}/health",
        "dex_automation_wallet": f"{base_url}/wallet",
        "dex_automation_rules": f"{base_url}/rules",
        "dex_automation_runs": f"{base_url}/runs",
    }


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
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
    except OSError, URLError, TimeoutError, ValueError:
        return False


def _ensure_wallet_key(path: Path) -> None:
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{secrets.token_hex(32)}\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def ensure_dex_automation_config(
    *,
    rpc_url: str,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    source_env = os.environ if env is None else env
    repo_dir = require_dex_automation_repo(env=source_env)
    config_path = resolve_dex_automation_config_path(env=source_env)
    wallet_key_path = resolve_dex_automation_wallet_key_path(env=source_env)
    state_path = _PROCESS_DIR / "state.sqlite3"

    _PROCESS_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_wallet_key(wallet_key_path)

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "network": {
                "rpc_url": rpc_url,
                "chain_id": None,
                "watcher_mode": "auto",
                "poll_interval_seconds": 1.0,
            },
            "dex": {
                "router_contract": "con_dex",
                "pairs_contract": "con_pairs",
            },
            "wallet": {
                "private_key_env": "XIAN_DEX_AUTOMATION_PRIVATE_KEY",
                "private_key_file": str(wallet_key_path),
                "private_key_file_env": ("XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE"),
                "execute": False,
                "recipient": None,
            },
            "database_path": str(state_path),
            "rules": [
                {
                    "id": "demo-price-move",
                    "enabled": True,
                    "trigger": {
                        "type": "price_move",
                        "pair_id": 1,
                        "direction": "either",
                        "threshold_bps": 100,
                        "cooldown_seconds": 300,
                    },
                    "action": {
                        "type": "swap_exact_in",
                        "src": "currency",
                        "amount_in": "1",
                        "max_slippage_bps": 100,
                        "deadline_seconds": 300,
                    },
                }
            ],
        }
        config_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "dex_automation_repo_dir": str(repo_dir),
        "dex_automation_config_path": str(config_path),
        "dex_automation_wallet_key_path": str(wallet_key_path),
    }


def _build_runtime_env(env: dict[str, str] | None = None) -> dict[str, str]:
    source_env = os.environ.copy()
    if env is not None:
        source_env.update(env)
    source_env.setdefault("XIAN_STACK_DIR", str(STACK_DIR))
    source_env.setdefault(
        "XIAN_DEX_AUTOMATION_DIR",
        str(resolve_dex_automation_repo_dir(env=source_env)),
    )
    source_env.setdefault(
        "XIAN_DEX_AUTOMATION_CONFIG",
        str(resolve_dex_automation_config_path(env=source_env)),
    )
    source_env.setdefault(
        "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE",
        str(resolve_dex_automation_wallet_key_path(env=source_env)),
    )
    source_env.setdefault(
        "XIAN_PY_DIR",
        str(resolve_repo_dir("xian-py", "XIAN_PY_DIR", env=source_env)),
    )
    return source_env


def _wait_for_ready(url: str, *, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _probe_health(url):
            return
        time.sleep(0.25)
    raise TimeoutError(f"xian-dex-automation did not become ready at {url}")


def get_dex_automation_status(
    *,
    bind_host: str,
    port: int,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    pid = _read_pid(_PID_PATH)
    running = _process_running(pid)
    if pid is not None and not running:
        try:
            _PID_PATH.unlink()
        except OSError:
            pass
        pid = None

    endpoints = dex_automation_endpoints(bind_host=bind_host, port=port)
    health_ok = _probe_health(endpoints["dex_automation_health"]) if running else False
    source_env = os.environ if env is None else env

    return {
        "dex_automation_running": running,
        "dex_automation_pid": pid,
        "dex_automation_health_ok": health_ok,
        "dex_automation_log_path": str(_LOG_PATH),
        "dex_automation_pid_path": str(_PID_PATH),
        "dex_automation_config_path": str(resolve_dex_automation_config_path(env=source_env)),
        "dex_automation_wallet_key_path": str(
            resolve_dex_automation_wallet_key_path(env=source_env)
        ),
        **endpoints,
    }


def start_dex_automation_runtime(
    *,
    bind_host: str,
    port: int,
    rpc_url: str,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    source_env = _build_runtime_env(env)
    ensure_dex_automation_config(rpc_url=rpc_url, env=source_env)

    existing = get_dex_automation_status(
        bind_host=bind_host,
        port=port,
        env=source_env,
    )
    if existing["dex_automation_running"]:
        return existing

    _PROCESS_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = _LOG_PATH.open("a", encoding="utf-8")
    command = [
        "uv",
        "run",
        "--project",
        source_env["XIAN_DEX_AUTOMATION_DIR"],
        "--with",
        source_env["XIAN_PY_DIR"],
        "--python",
        STACK_UV_PYTHON,
        "python",
        "-m",
        "xian_dex_automation.cli",
        "--config",
        source_env["XIAN_DEX_AUTOMATION_CONFIG"],
        "serve",
        "--host",
        bind_host,
        "--port",
        str(port),
        "--with-worker",
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
    _PID_PATH.write_text(str(process.pid), encoding="utf-8")
    try:
        _wait_for_ready(
            dex_automation_endpoints(bind_host=bind_host, port=port)["dex_automation_health"]
        )
    except Exception:
        stop_dex_automation_runtime(
            bind_host=bind_host,
            port=port,
            env=source_env,
        )
        raise
    finally:
        log_handle.close()

    return get_dex_automation_status(
        bind_host=bind_host,
        port=port,
        env=source_env,
    )


def stop_dex_automation_runtime(
    *,
    bind_host: str,
    port: int,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    pid = _read_pid(_PID_PATH)
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
    if _PID_PATH.exists():
        try:
            _PID_PATH.unlink()
        except OSError:
            pass

    payload = get_dex_automation_status(
        bind_host=bind_host,
        port=port,
        env=env,
    )
    payload["dex_automation_stopped"] = True
    return payload
