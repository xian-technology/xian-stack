#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import urlopen

STACK_DIR = Path(__file__).resolve().parent.parent
LOCALNET_NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
LOCALNET_INIT_SCRIPT = STACK_DIR / "scripts" / "localnet-init.py"
LOCALNET_WORKLOAD_SCRIPT = STACK_DIR / "scripts" / "localnet-workload.py"
LOCALNET_BURST_SCRIPT = STACK_DIR / "scripts" / "localnet-burst-test.py"
LOCALNET_MEMWATCH_SCRIPT = STACK_DIR / "scripts" / "localnet-memwatch.py"
LOCALNET_LEAK_HUNT_SCRIPT = STACK_DIR / "scripts" / "localnet-leak-hunt.py"
DEFAULT_RPC_TIMEOUT_SECONDS = 90.0
DEFAULT_RPC_BASE_URL = "http://127.0.0.1:26657"
DEFAULT_RPC_STATUS_URL = f"{DEFAULT_RPC_BASE_URL}/status"
DEFAULT_COMETBFT_METRICS_URL = "http://127.0.0.1:26660/metrics"
DEFAULT_XIAN_METRICS_URL = "http://127.0.0.1:9108/metrics"
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090"
DEFAULT_GRAFANA_URL = "http://127.0.0.1:3000"
DEFAULT_GRAPHQL_URL = "http://127.0.0.1:5000/graphql"


def resolve_repo_dir(name: str, env_var: str) -> Path:
    explicit = os.environ.get(env_var)
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (STACK_DIR.parent / name).resolve()


def fetch_json(url: str, *, timeout: float = 10.0) -> dict:
    with urlopen(url, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset)
    return json.loads(payload)


def probe_http_endpoint(url: str, *, timeout: float = 2.0) -> dict:
    with urlopen(url, timeout=timeout) as response:
        return {
            "status": getattr(response, "status", 200),
            "content_type": response.headers.get_content_type(),
        }


def rpc_base_url(rpc_url: str) -> str:
    parsed = urlsplit(rpc_url)
    path = parsed.path
    if path.endswith("/status"):
        path = path[: -len("/status")]
    path = path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def build_abci_query_url(*, rpc_url: str, path: str) -> str:
    encoded_path = quote(json.dumps(path), safe="")
    return f"{rpc_base_url(rpc_url)}/abci_query?path={encoded_path}"


def fetch_abci_query_value(
    *,
    rpc_url: str,
    path: str,
    timeout: float = 2.0,
) -> dict:
    payload = fetch_json(
        build_abci_query_url(rpc_url=rpc_url, path=path),
        timeout=timeout,
    )
    response = payload.get("result", {}).get("response", {})
    response_code = int(response.get("code", 0) or 0)
    if response_code != 0:
        raise ValueError(
            f"ABCI query {path} failed with response code {response_code}"
        )
    encoded_value = response.get("value")
    if not isinstance(encoded_value, str) or not encoded_value:
        raise ValueError(f"ABCI query {path} returned no value")
    try:
        decoded_value = base64.b64decode(encoded_value).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"ABCI query {path} returned invalid base64") from exc
    try:
        result = json.loads(decoded_value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"ABCI query {path} returned invalid JSON payload"
        ) from exc
    if not isinstance(result, dict):
        raise ValueError(f"ABCI query {path} returned a non-object payload")
    return result


def wait_for_rpc_ready(
    *,
    rpc_url: str,
    timeout_seconds: float,
    poll_interval: float = 1.0,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            payload = fetch_json(rpc_url, timeout=poll_interval)
            if payload.get("result"):
                return payload
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc

        time.sleep(poll_interval)

    raise TimeoutError(f"RPC did not become ready at {rpc_url}") from last_error


def wait_for_abci_runtime(
    *,
    timeout_seconds: float,
    poll_interval: float = 2.0,
    service_node: bool = False,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    cmd = [
        "docker",
        "compose",
        "--profile",
        "integrated",
        "-f",
        "docker-compose-abci.yml",
    ]
    if service_node:
        cmd.extend(["-f", "docker-compose-abci-bds.yml"])
    cmd.extend(
        [
            "exec",
            "-T",
            "abci",
            "/bin/bash",
            "-lc",
            "python -c 'import contracting, xian'",
        ]
    )
    last_error: subprocess.CalledProcessError | None = None

    while time.monotonic() < deadline:
        result = subprocess.run(
            cmd,
            cwd=STACK_DIR,
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        if result.returncode == 0:
            return
        last_error = subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
        time.sleep(poll_interval)

    raise TimeoutError(
        "ABCI container runtime did not become ready in time"
    ) from last_error


def run_make_target(
    target: str,
    *,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", target],
        cwd=STACK_DIR,
        check=True,
        capture_output=capture_output,
        text=True,
        env=env,
    )


def runtime_env(
    *,
    dashboard_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
) -> dict[str, str]:
    env = os.environ.copy()
    env["XIAN_DASHBOARD_ENABLED"] = "1" if dashboard_enabled else "0"
    env["XIAN_DASHBOARD_HOST"] = dashboard_host
    env["XIAN_DASHBOARD_PORT"] = str(dashboard_port)
    return env


def run_python_script(
    script_path: Path,
    *args: str,
    capture_output: bool = False,
    uv_project: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(script_path), *args]
    if uv_project is not None:
        cmd = [
            "uv",
            "run",
            "--project",
            str(uv_project),
            "python3",
            str(script_path),
            *args,
        ]
    return subprocess.run(
        cmd,
        cwd=STACK_DIR,
        check=True,
        capture_output=capture_output,
        text=True,
    )


def load_localnet_metadata() -> dict:
    if not LOCALNET_NETWORK_PATH.exists():
        raise FileNotFoundError(
            f"localnet metadata not found: {LOCALNET_NETWORK_PATH}; "
            "run localnet-init first"
        )
    return json.loads(LOCALNET_NETWORK_PATH.read_text(encoding="utf-8"))


def localnet_node_status(node: dict, *, timeout: float) -> dict:
    rpc_url = f"http://127.0.0.1:{node['host_rpc_port']}/status"
    status = {
        "moniker": node["moniker"],
        "node_id": node["node_id"],
        "rpc_url": rpc_url,
        "rpc_port": node["host_rpc_port"],
        "p2p_port": node["host_p2p_port"],
        "metrics_port": node["host_metrics_port"],
        "up": False,
        "height": None,
        "peers": None,
        "voting_power": None,
    }

    try:
        payload = fetch_json(rpc_url, timeout=timeout)
        result = payload["result"]
        status.update(
            {
                "up": True,
                "height": result["sync_info"]["latest_block_height"],
                "peers": result["node_info"].get("other", {}).get("n_peers"),
                "voting_power": result["validator_info"]["voting_power"],
            }
        )
    except (
        OSError,
        KeyError,
        URLError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
    ):
        pass

    return status


def wait_for_localnet_ready(*, timeout_seconds: float) -> list[dict]:
    deadline = time.monotonic() + timeout_seconds
    metadata = load_localnet_metadata()

    while time.monotonic() < deadline:
        statuses = [
            localnet_node_status(node, timeout=1.0) for node in metadata["nodes"]
        ]
        if statuses and all(
            node["up"] and node["height"] not in {None, "0", 0} for node in statuses
        ):
            return statuses
        time.sleep(1.0)

    raise TimeoutError("localnet nodes did not become ready in time")


def backend_start(
    *,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    wait_for_health: bool,
    rpc_timeout_seconds: float,
    rpc_url: str,
) -> dict:
    container_target = "abci-bds-up" if service_node else "abci-up"
    node_target = "node-start-bds" if service_node else "node-start"
    dashboard_target = "dashboard-bds-up" if service_node else "dashboard-up"
    monitoring_target = (
        "monitoring-bds-up" if service_node else "monitoring-up"
    )
    env = runtime_env(
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )

    run_make_target(node_target, env=env)
    if monitoring_enabled:
        run_make_target(monitoring_target, env=env)
    if dashboard_enabled:
        run_make_target(dashboard_target, env=env)
    wait_for_abci_runtime(
        timeout_seconds=rpc_timeout_seconds,
        service_node=service_node,
    )

    result = {
        "stack_dir": str(STACK_DIR),
        "service_node": service_node,
        "container_target": container_target,
        "node_target": node_target,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "rpc_checked": wait_for_health,
    }
    if dashboard_enabled:
        result["dashboard_target"] = dashboard_target
        result["dashboard_url"] = f"http://{dashboard_host}:{dashboard_port}"
    if monitoring_enabled:
        result["monitoring_target"] = monitoring_target
        result["prometheus_url"] = "http://127.0.0.1:9090"
        result["grafana_url"] = "http://127.0.0.1:3000"
    if wait_for_health:
        result["rpc_status"] = wait_for_rpc_ready(
            rpc_url=rpc_url,
            timeout_seconds=rpc_timeout_seconds,
        )
    return result


def backend_stop(
    *,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
) -> dict:
    container_target = "abci-bds-down" if service_node else "abci-down"
    dashboard_target = "dashboard-bds-down" if service_node else "dashboard-down"
    monitoring_target = (
        "monitoring-bds-down" if service_node else "monitoring-down"
    )
    env = runtime_env(
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )
    if dashboard_enabled:
        run_make_target(dashboard_target, env=env)
    if monitoring_enabled:
        run_make_target(monitoring_target, env=env)
    run_make_target("node-stop", env=env)
    return {
        "stack_dir": str(STACK_DIR),
        "service_node": service_node,
        "container_target": container_target,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "dashboard_target": dashboard_target if dashboard_enabled else None,
        "monitoring_target": monitoring_target if monitoring_enabled else None,
    }


def backend_status(
    *,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
) -> dict:
    env = runtime_env(
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )
    env["XIAN_SERVICE_NODE"] = "1" if service_node else "0"
    result = run_make_target("node-status", capture_output=True, env=env)
    payload = json.loads(result.stdout)
    payload["dashboard_enabled"] = dashboard_enabled
    payload["monitoring_enabled"] = monitoring_enabled
    payload["endpoints"] = backend_endpoints(
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )["endpoints"]
    if dashboard_enabled:
        dashboard_url = f"http://{dashboard_host}:{dashboard_port}/api/status"
        payload["dashboard_url"] = dashboard_url
        try:
            payload["dashboard_status"] = fetch_json(dashboard_url, timeout=2.0)
            payload["dashboard_reachable"] = True
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            payload["dashboard_reachable"] = False
            payload["dashboard_error"] = str(exc)
    if monitoring_enabled:
        prometheus_url = "http://127.0.0.1:9090/api/v1/status/runtimeinfo"
        grafana_url = "http://127.0.0.1:3000/api/health"
        payload["prometheus_url"] = "http://127.0.0.1:9090"
        payload["grafana_url"] = "http://127.0.0.1:3000"
        try:
            payload["prometheus_status"] = fetch_json(
                prometheus_url,
                timeout=2.0,
            )
            payload["prometheus_reachable"] = True
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            payload["prometheus_reachable"] = False
            payload["prometheus_error"] = str(exc)
        try:
            payload["grafana_status"] = fetch_json(
                grafana_url,
                timeout=2.0,
            )
            payload["grafana_reachable"] = True
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            payload["grafana_reachable"] = False
            payload["grafana_error"] = str(exc)
    return payload


def backend_endpoints(
    *,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
) -> dict:
    endpoints = {
        "rpc": DEFAULT_RPC_BASE_URL,
        "rpc_status": DEFAULT_RPC_STATUS_URL,
        "abci_query": f"{DEFAULT_RPC_BASE_URL}/abci_query",
        "cometbft_metrics": DEFAULT_COMETBFT_METRICS_URL,
        "xian_metrics": DEFAULT_XIAN_METRICS_URL,
    }
    if service_node:
        endpoints["bds_status_query"] = build_abci_query_url(
            rpc_url=DEFAULT_RPC_STATUS_URL,
            path="/bds_status",
        )
        endpoints["bds_spool_query"] = build_abci_query_url(
            rpc_url=DEFAULT_RPC_STATUS_URL,
            path="/bds_spool/limit=20/offset=0",
        )
        endpoints["graphql"] = DEFAULT_GRAPHQL_URL
    if dashboard_enabled:
        endpoints["dashboard"] = f"http://{dashboard_host}:{dashboard_port}"
        endpoints["dashboard_status"] = (
            f"http://{dashboard_host}:{dashboard_port}/api/status"
        )
    if monitoring_enabled:
        endpoints["prometheus"] = DEFAULT_PROMETHEUS_URL
        endpoints["grafana"] = DEFAULT_GRAFANA_URL
    return {
        "stack_dir": str(STACK_DIR),
        "service_node": service_node,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "endpoints": endpoints,
    }


def backend_health(
    *,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    rpc_url: str,
    check_disk: bool,
) -> dict:
    status = backend_status(
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )
    endpoints = status["endpoints"]

    checks: dict[str, dict[str, object]] = {
        "backend": {
            "ok": bool(status.get("backend_running")),
            "detail": {
                "backend_running": bool(status.get("backend_running")),
                "node_id": status.get("node_id"),
            },
        }
    }

    try:
        rpc_status = fetch_json(rpc_url, timeout=2.0)
        result = rpc_status.get("result", {})
        sync_info = result.get("sync_info", {})
        node_info = result.get("node_info", {})
        checks["rpc"] = {
            "ok": True,
            "detail": {
                "network": node_info.get("network"),
                "height": sync_info.get("latest_block_height"),
                "catching_up": sync_info.get("catching_up"),
            },
        }
    except (
        OSError,
        URLError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        checks["rpc"] = {
            "ok": False,
            "detail": {"error": str(exc)},
        }

    for check_name, endpoint_key in (
        ("cometbft_metrics", "cometbft_metrics"),
        ("xian_metrics", "xian_metrics"),
    ):
        try:
            detail = probe_http_endpoint(endpoints[endpoint_key])
            checks[check_name] = {"ok": True, "detail": detail}
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
        ) as exc:
            checks[check_name] = {
                "ok": False,
                "detail": {"error": str(exc)},
            }

    if service_node:
        bds_query = endpoints.get("bds_status_query")
        bds_detail: dict[str, object] = {
            "query": bds_query,
        }
        if not checks["rpc"]["ok"]:
            checks["bds"] = {
                "ok": False,
                "detail": {
                    **bds_detail,
                    "error": "RPC is unavailable; cannot inspect BDS status",
                },
            }
        else:
            try:
                bds_status = fetch_abci_query_value(
                    rpc_url=rpc_url,
                    path="/bds_status",
                    timeout=2.0,
                )
                alerts = bds_status.get("alerts", [])
                error_alerts = [
                    alert
                    for alert in alerts
                    if isinstance(alert, dict)
                    and str(alert.get("level", "")).lower() == "error"
                ]
                indexed = (
                    bds_status.get("indexed", {})
                    if isinstance(bds_status.get("indexed"), dict)
                    else {}
                )
                checks["bds"] = {
                    "ok": (
                        str(bds_status.get("db_status")) == "ok"
                        and bool(bds_status.get("worker_running"))
                        and not bds_status.get("last_enqueue_error")
                        and not error_alerts
                    ),
                    "detail": {
                        **bds_detail,
                        "db_status": bds_status.get("db_status"),
                        "worker_running": bds_status.get("worker_running"),
                        "catchup_running": bds_status.get("catchup_running"),
                        "catching_up": bds_status.get("catching_up"),
                        "current_block_height": bds_status.get(
                            "current_block_height"
                        ),
                        "indexed_height": indexed.get("indexed_height"),
                        "height_lag": bds_status.get("height_lag"),
                        "queue_depth": bds_status.get("queue_depth"),
                        "queue_capacity": bds_status.get("queue_capacity"),
                        "queue_utilization": bds_status.get(
                            "queue_utilization"
                        ),
                        "spool_pending_count": bds_status.get(
                            "spool_pending_count"
                        ),
                        "spool_total_bytes": bds_status.get(
                            "spool_total_bytes"
                        ),
                        "storage": bds_status.get("storage"),
                        "last_enqueue_error": bds_status.get(
                            "last_enqueue_error"
                        ),
                        "alerts": alerts,
                    },
                }
            except (
                OSError,
                URLError,
                TimeoutError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                checks["bds"] = {
                    "ok": False,
                    "detail": {
                        **bds_detail,
                        "error": str(exc),
                    },
                }

    if dashboard_enabled:
        checks["dashboard"] = {
            "ok": bool(status.get("dashboard_reachable")),
            "detail": {
                "url": endpoints.get("dashboard"),
                "error": status.get("dashboard_error"),
            },
        }
    if monitoring_enabled:
        checks["prometheus"] = {
            "ok": bool(status.get("prometheus_reachable")),
            "detail": {
                "url": endpoints.get("prometheus"),
                "error": status.get("prometheus_error"),
            },
        }
        checks["grafana"] = {
            "ok": bool(status.get("grafana_reachable")),
            "detail": {
                "url": endpoints.get("grafana"),
                "error": status.get("grafana_error"),
            },
        }

    storage = None
    if check_disk:
        storage = backend_storage_report()
        checks["disk"] = {
            "ok": not storage.get("alerts"),
            "detail": {
                "alerts": storage.get("alerts", []),
                "paths": storage.get("paths", {}),
            },
        }

    if not checks["backend"]["ok"] or not checks["rpc"]["ok"]:
        state = "stopped"
    elif any(not check["ok"] for check in checks.values()):
        state = "degraded"
    else:
        state = "healthy"

    payload = {
        "stack_dir": str(STACK_DIR),
        "service_node": service_node,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "state": state,
        "checks": checks,
        "endpoints": endpoints,
    }
    if storage is not None:
        payload["storage"] = storage
    return payload


def backend_make_result(target: str) -> dict:
    result = run_make_target(target)
    payload = {
        "stack_dir": str(STACK_DIR),
        "target": target,
        "ok": True,
    }
    if result.stdout:
        payload["stdout"] = result.stdout
    if result.stderr:
        payload["stderr"] = result.stderr
    return payload


def backend_storage_report() -> dict:
    result = run_make_target("storage-report", capture_output=True)
    payload = json.loads(result.stdout)
    payload["target"] = "storage-report"
    return payload


def backend_localnet_init(*, nodes: int, clean: bool, topology: str) -> dict:
    args = ["--nodes", str(nodes), "--topology", topology]
    if clean:
        args.append("--clean")
    result = run_python_script(
        LOCALNET_INIT_SCRIPT,
        *args,
        capture_output=True,
        uv_project=resolve_repo_dir("xian-abci", "XIAN_ABCI_DIR"),
    )
    metadata = load_localnet_metadata()
    return {
        "stack_dir": str(STACK_DIR),
        "node_count": nodes,
        "clean": clean,
        "topology": topology,
        "network": metadata,
        "stdout": result.stdout,
    }


def backend_localnet_up(
    *,
    wait_for_health: bool,
    rpc_timeout_seconds: float,
) -> dict:
    run_make_target("localnet-up")
    result = {
        "stack_dir": str(STACK_DIR),
        "rpc_checked": wait_for_health,
    }
    if wait_for_health:
        result["nodes"] = wait_for_localnet_ready(timeout_seconds=rpc_timeout_seconds)
    else:
        result["network"] = load_localnet_metadata()
    return result


def backend_localnet_status(*, timeout_seconds: float) -> dict:
    metadata = load_localnet_metadata()
    nodes = [
        localnet_node_status(node, timeout=timeout_seconds)
        for node in metadata["nodes"]
    ]
    return {
        "stack_dir": str(STACK_DIR),
        "chain_id": metadata["chain_id"],
        "node_count": len(nodes),
        "all_up": all(node["up"] for node in nodes),
        "nodes": nodes,
    }


def backend_localnet_diagnostic(
    *,
    script_path: Path,
    duration_minutes: int | None = None,
    script_args: list[str] | None = None,
) -> dict:
    args: list[str] = []
    if duration_minutes is not None:
        args.append(str(duration_minutes))
    if script_args is not None:
        args.extend(script_args)
    result = run_python_script(
        script_path,
        *args,
        capture_output=True,
        uv_project=resolve_repo_dir("xian-py", "XIAN_PY_DIR"),
    )
    payload = {
        "stack_dir": str(STACK_DIR),
        "script": str(script_path.relative_to(STACK_DIR)),
        "ok": True,
    }
    if duration_minutes is not None:
        payload["duration_minutes"] = duration_minutes
    if script_args:
        payload["script_args"] = script_args
    if result.stdout:
        payload["stdout"] = result.stdout
    if result.stderr:
        payload["stderr"] = result.stderr
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stable machine-readable backend control surface for xian-stack"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument(
        "--service-node",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    start.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    start.add_argument(
        "--monitoring",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    start.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
    )
    start.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
    )
    start.add_argument(
        "--wait-for-health",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    start.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
    )
    start.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
    )

    stop = subparsers.add_parser("stop")
    stop.add_argument(
        "--service-node",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    stop.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    stop.add_argument(
        "--monitoring",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    stop.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
    )
    stop.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
    )

    status = subparsers.add_parser("status")
    status.add_argument(
        "--service-node",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    status.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    status.add_argument(
        "--monitoring",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    status.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
    )
    status.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
    )

    endpoints = subparsers.add_parser("endpoints")
    endpoints.add_argument(
        "--service-node",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    endpoints.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    endpoints.add_argument(
        "--monitoring",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    endpoints.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
    )
    endpoints.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
    )

    health = subparsers.add_parser("health")
    health.add_argument(
        "--service-node",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    health.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    health.add_argument(
        "--monitoring",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    health.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
    )
    health.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
    )
    health.add_argument(
        "--rpc-url",
        default=f"{DEFAULT_RPC_BASE_URL}/status",
    )
    health.add_argument(
        "--check-disk",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    subparsers.add_parser("validate")
    subparsers.add_parser("smoke")
    subparsers.add_parser("smoke-cli")
    subparsers.add_parser("storage-report")

    localnet_init = subparsers.add_parser("localnet-init")
    localnet_init.add_argument("--nodes", type=int, default=4)
    localnet_init.add_argument(
        "--topology",
        choices=("integrated", "fidelity"),
        default="integrated",
    )
    localnet_init.add_argument(
        "--clean",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    localnet_up = subparsers.add_parser("localnet-up")
    localnet_up.add_argument(
        "--wait-for-health",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    localnet_up.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
    )

    localnet_status = subparsers.add_parser("localnet-status")
    localnet_status.add_argument(
        "--timeout-seconds",
        type=float,
        default=2.0,
    )

    localnet_workload = subparsers.add_parser("localnet-workload")
    localnet_workload.add_argument(
        "--scenario",
        choices=("counter_basic", "dex_mixed"),
        default="counter_basic",
    )
    localnet_workload.add_argument(
        "--seed",
        default="xian-localnet-workload-v1",
    )
    localnet_workload.add_argument(
        "--counter-ops",
        type=int,
        default=180,
    )
    localnet_workload.add_argument(
        "--dex-rounds",
        type=int,
        default=6,
    )
    localnet_workload.add_argument(
        "--state-sample-nodes",
        type=int,
        default=2,
    )
    localnet_workload.add_argument(
        "--app-hash-window",
        type=int,
        default=3,
    )

    localnet_burst = subparsers.add_parser("localnet-burst")
    localnet_burst.add_argument(
        "--counter-ops",
        type=int,
        default=180,
    )
    localnet_burst.add_argument(
        "--state-sample-nodes",
        type=int,
        default=2,
    )
    localnet_burst.add_argument(
        "--app-hash-window",
        type=int,
        default=3,
    )

    localnet_memwatch = subparsers.add_parser("localnet-memwatch")
    localnet_memwatch.add_argument(
        "--duration-minutes",
        type=int,
        default=10,
    )

    localnet_leak_hunt = subparsers.add_parser("localnet-leak-hunt")
    localnet_leak_hunt.add_argument(
        "--duration-minutes",
        type=int,
        default=10,
    )

    subparsers.add_parser("localnet-build")
    subparsers.add_parser("localnet-down")
    subparsers.add_parser("localnet-clean")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        payload = backend_start(
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            wait_for_health=args.wait_for_health,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            rpc_url=args.rpc_url,
        )
    elif args.command == "stop":
        payload = backend_stop(
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
        )
    elif args.command == "status":
        payload = backend_status(
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
        )
    elif args.command == "endpoints":
        payload = backend_endpoints(
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
        )
    elif args.command == "health":
        payload = backend_health(
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            rpc_url=args.rpc_url,
            check_disk=args.check_disk,
        )
    elif args.command == "validate":
        payload = backend_make_result("validate")
    elif args.command == "smoke":
        payload = backend_make_result("smoke")
    elif args.command == "smoke-cli":
        payload = backend_make_result("smoke-cli")
    elif args.command == "storage-report":
        payload = backend_storage_report()
    elif args.command == "localnet-init":
        payload = backend_localnet_init(
            nodes=args.nodes,
            clean=args.clean,
            topology=args.topology,
        )
    elif args.command == "localnet-build":
        payload = backend_make_result("localnet-build")
    elif args.command == "localnet-up":
        payload = backend_localnet_up(
            wait_for_health=args.wait_for_health,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
        )
    elif args.command == "localnet-down":
        payload = backend_make_result("localnet-down")
    elif args.command == "localnet-clean":
        payload = backend_make_result("localnet-clean")
    elif args.command == "localnet-status":
        payload = backend_localnet_status(timeout_seconds=args.timeout_seconds)
    elif args.command == "localnet-workload":
        payload = backend_localnet_diagnostic(
            script_path=LOCALNET_WORKLOAD_SCRIPT,
            script_args=[
                "--scenario",
                args.scenario,
                "--seed",
                args.seed,
                "--counter-ops",
                str(args.counter_ops),
                "--dex-rounds",
                str(args.dex_rounds),
                "--state-sample-nodes",
                str(args.state_sample_nodes),
                "--app-hash-window",
                str(args.app_hash_window),
            ],
        )
    elif args.command == "localnet-burst":
        payload = backend_localnet_diagnostic(
            script_path=LOCALNET_WORKLOAD_SCRIPT,
            script_args=[
                "--scenario",
                "counter_basic",
                "--counter-ops",
                str(args.counter_ops),
                "--state-sample-nodes",
                str(args.state_sample_nodes),
                "--app-hash-window",
                str(args.app_hash_window),
            ],
        )
    elif args.command == "localnet-memwatch":
        payload = backend_localnet_diagnostic(
            script_path=LOCALNET_MEMWATCH_SCRIPT,
            duration_minutes=args.duration_minutes,
        )
    elif args.command == "localnet-leak-hunt":
        payload = backend_localnet_diagnostic(
            script_path=LOCALNET_LEAK_HUNT_SCRIPT,
            duration_minutes=args.duration_minutes,
        )
    else:
        raise ValueError(f"unsupported command: {args.command}")

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
