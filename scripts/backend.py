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

from intentkit_backend import (
    SUPPORTED_INTENTKIT_NETWORK_IDS,
    ensure_intentkit_env,
    get_intentkit_status,
    intentkit_endpoints,
    start_intentkit_runtime,
    stop_intentkit_runtime,
)
from shielded_relayer_backend import (
    DEFAULT_SHIELDED_RELAYER_HOST,
    DEFAULT_SHIELDED_RELAYER_PORT,
    get_shielded_relayer_status,
    shielded_relayer_endpoints,
    start_shielded_relayer_runtime,
    stop_shielded_relayer_runtime,
)

STACK_DIR = Path(__file__).resolve().parent.parent
LOCALNET_NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
LOCALNET_INIT_SCRIPT = STACK_DIR / "scripts" / "localnet-init.py"
LOCALNET_WORKLOAD_SCRIPT = STACK_DIR / "scripts" / "localnet-workload.py"
LOCALNET_E2E_SCRIPT = STACK_DIR / "scripts" / "localnet-e2e.py"
LOCALNET_VALIDATOR_GOVERNANCE_SCRIPT = (
    STACK_DIR / "scripts" / "localnet-validator-governance.py"
)
LOCALNET_BURST_SCRIPT = STACK_DIR / "scripts" / "localnet-burst-test.py"
LOCALNET_MEMWATCH_SCRIPT = STACK_DIR / "scripts" / "localnet-memwatch.py"
LOCALNET_LEAK_HUNT_SCRIPT = STACK_DIR / "scripts" / "localnet-leak-hunt.py"
STACK_UV_PYTHON = "3.14"
DEFAULT_RPC_TIMEOUT_SECONDS = 90.0
DEFAULT_RPC_BASE_URL = "http://127.0.0.1:26657"
DEFAULT_RPC_STATUS_URL = f"{DEFAULT_RPC_BASE_URL}/status"
DEFAULT_COMETBFT_METRICS_URL = "http://127.0.0.1:26660/metrics"
DEFAULT_XIAN_METRICS_URL = "http://127.0.0.1:9108/metrics"
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090"
DEFAULT_GRAFANA_URL = "http://127.0.0.1:3000"
DEFAULT_GRAPHQL_URL = "http://127.0.0.1:5000/graphql"
DEFAULT_SHIELDED_RELAYER_URL = (
    f"http://{DEFAULT_SHIELDED_RELAYER_HOST}:{DEFAULT_SHIELDED_RELAYER_PORT}"
)
DEFAULT_BDS_SNAPSHOT_PATH = (
    STACK_DIR / ".cometbft" / "snapshots" / "xian-bds-snapshot.tar.gz"
)


def display_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def resolve_repo_dir(name: str, env_var: str) -> Path:
    explicit = os.environ.get(env_var)
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (STACK_DIR.parent / name).resolve()


def resolve_cometbft_home() -> Path:
    explicit = os.environ.get("XIAN_COMETBFT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (STACK_DIR / ".cometbft").resolve()


def resolve_bds_snapshot_path(path: str | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return DEFAULT_BDS_SNAPSHOT_PATH.resolve()


def ensure_path_within_cometbft_home(path: Path) -> None:
    cometbft_home = resolve_cometbft_home()
    try:
        path.relative_to(cometbft_home)
    except ValueError as exc:
        raise RuntimeError(
            "BDS snapshot paths must live under XIAN_COMETBFT_HOME so the "
            "stack container can access them"
        ) from exc


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


def rpc_chain_id(payload: dict) -> str:
    chain_id = payload.get("result", {}).get("node_info", {}).get("network")
    if not isinstance(chain_id, str) or not chain_id:
        raise ValueError("RPC status did not include node_info.network")
    return chain_id


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
    shell_script = """
source ./scripts/stack-env.sh
export_stack_env
compose_cmd=(docker compose --profile integrated -f docker-compose-abci.yml)
if [[ "${XIAN_SERVICE_NODE:-0}" == "1" ]]; then
  compose_cmd+=(-f docker-compose-abci-bds.yml)
fi
"${compose_cmd[@]}" exec -T abci /bin/bash -lc "python -c 'import contracting, xian'"
""".strip()
    last_error: subprocess.CalledProcessError | None = None

    while time.monotonic() < deadline:
        result = subprocess.run(
            ["bash", "-lc", shell_script],
            cwd=STACK_DIR,
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ.copy(),
                "XIAN_SERVICE_NODE": "1" if service_node else "0",
            },
        )
        if result.returncode == 0:
            return
        last_error = subprocess.CalledProcessError(
            result.returncode,
            ["bash", "-lc", shell_script],
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


def format_subprocess_error(exc: subprocess.CalledProcessError) -> str:
    command = exc.cmd
    if isinstance(command, (list, tuple)):
        command_str = " ".join(str(part) for part in command)
    else:
        command_str = str(command)

    lines = [f"command failed with exit code {exc.returncode}: {command_str}"]
    stdout = (exc.stdout or "").strip()
    stderr = (exc.stderr or "").strip()
    if stdout:
        lines.append("")
        lines.append("stdout:")
        lines.append(stdout)
    if stderr:
        lines.append("")
        lines.append("stderr:")
        lines.append(stderr)
    return "\n".join(lines)


def _docker_compose_container_id(*, service: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project.working_dir={STACK_DIR}",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    container_ids = result.stdout.strip().splitlines()
    return container_ids[0] if container_ids else None


def _docker_compose_binding(
    *,
    service: str,
    container_port: int,
) -> tuple[str, int] | None:
    container_id = _docker_compose_container_id(service=service)
    if container_id is None:
        return None

    try:
        result = subprocess.run(
            ["docker", "inspect", container_id],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ):
        return None

    if not payload or not isinstance(payload, list):
        return None
    container = payload[0]
    if not isinstance(container, dict):
        return None

    ports = container.get("NetworkSettings", {}).get("Ports", {})
    bindings = ports.get(f"{container_port}/tcp")
    if not isinstance(bindings, list) or not bindings:
        return None

    binding = bindings[0]
    if not isinstance(binding, dict):
        return None

    host_ip = str(binding.get("HostIp") or "127.0.0.1")
    host_port = binding.get("HostPort")
    if host_port is None:
        return None
    return host_ip, int(host_port)


def _discover_runtime_endpoints(
    *,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
) -> dict[str, str]:
    endpoints: dict[str, str] = {}

    abci_rpc_binding = _docker_compose_binding(service="abci", container_port=26657)
    if abci_rpc_binding is not None:
        rpc_host = display_host(abci_rpc_binding[0])
        rpc_port = abci_rpc_binding[1]
        rpc_base = f"http://{rpc_host}:{rpc_port}"
        endpoints["rpc"] = rpc_base
        endpoints["rpc_status"] = f"{rpc_base}/status"
        endpoints["abci_query"] = f"{rpc_base}/abci_query"

    comet_metrics_binding = _docker_compose_binding(
        service="abci",
        container_port=26660,
    )
    if comet_metrics_binding is not None:
        metrics_host = display_host(comet_metrics_binding[0])
        metrics_port = comet_metrics_binding[1]
        endpoints["cometbft_metrics"] = (
            f"http://{metrics_host}:{metrics_port}/metrics"
        )

    xian_metrics_binding = _docker_compose_binding(
        service="abci",
        container_port=9108,
    )
    if xian_metrics_binding is not None:
        metrics_host = display_host(xian_metrics_binding[0])
        metrics_port = xian_metrics_binding[1]
        endpoints["xian_metrics"] = f"http://{metrics_host}:{metrics_port}/metrics"

    if service_node:
        graphql_binding = _docker_compose_binding(
            service="postgraphile",
            container_port=5000,
        )
        if graphql_binding is not None:
            graphql_host = display_host(graphql_binding[0])
            graphql_port = graphql_binding[1]
            endpoints["graphql"] = f"http://{graphql_host}:{graphql_port}/graphql"

    if dashboard_enabled:
        dashboard_binding = _docker_compose_binding(
            service="dashboard",
            container_port=8080,
        )
        if dashboard_binding is not None:
            dashboard_host = display_host(dashboard_binding[0])
            dashboard_port = dashboard_binding[1]
            dashboard_url = f"http://{dashboard_host}:{dashboard_port}"
            endpoints["dashboard"] = dashboard_url
            endpoints["dashboard_status"] = f"{dashboard_url}/api/status"

    if monitoring_enabled:
        prometheus_binding = _docker_compose_binding(
            service="prometheus",
            container_port=9090,
        )
        if prometheus_binding is not None:
            prometheus_host = display_host(prometheus_binding[0])
            prometheus_port = prometheus_binding[1]
            endpoints["prometheus"] = f"http://{prometheus_host}:{prometheus_port}"

        grafana_binding = _docker_compose_binding(
            service="grafana",
            container_port=3000,
        )
        if grafana_binding is not None:
            grafana_host = display_host(grafana_binding[0])
            grafana_port = grafana_binding[1]
            endpoints["grafana"] = f"http://{grafana_host}:{grafana_port}"

    return endpoints


def runtime_env(
    *,
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
    dashboard_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    intentkit_enabled: bool,
    intentkit_network_id: str,
    intentkit_host: str,
    intentkit_port: int,
    intentkit_api_port: int,
    shielded_relayer_enabled: bool,
    shielded_relayer_host: str,
    shielded_relayer_port: int,
) -> dict[str, str]:
    env = os.environ.copy()
    env["XIAN_NODE_IMAGE_MODE"] = node_image_mode
    if node_integrated_image is not None:
        env["XIAN_NODE_INTEGRATED_IMAGE"] = node_integrated_image
    if node_split_image is not None:
        env["XIAN_NODE_SPLIT_IMAGE"] = node_split_image
    env["XIAN_DASHBOARD_ENABLED"] = "1" if dashboard_enabled else "0"
    env["XIAN_DASHBOARD_HOST"] = dashboard_host
    env["XIAN_DASHBOARD_PORT"] = str(dashboard_port)
    env["XIAN_INTENTKIT_ENABLED"] = "1" if intentkit_enabled else "0"
    env["XIAN_INTENTKIT_DIR"] = str(
        resolve_repo_dir("xian-intentkit", "XIAN_INTENTKIT_DIR")
    )
    env["XIAN_INTENTKIT_RELEASE"] = env.get("XIAN_INTENTKIT_RELEASE", "local")
    env["XIAN_INTENTKIT_PROJECT_NAME"] = env.get(
        "XIAN_INTENTKIT_PROJECT_NAME", "xian-intentkit-stack"
    )
    env["XIAN_INTENTKIT_ENV_FILE"] = env.get(
        "XIAN_INTENTKIT_ENV_FILE",
        str(resolve_repo_dir("xian-intentkit", "XIAN_INTENTKIT_DIR") / "deployment" / ".env"),
    )
    env["XIAN_INTENTKIT_NETWORK_ID"] = intentkit_network_id
    env["XIAN_INTENTKIT_HOST"] = intentkit_host
    env["XIAN_INTENTKIT_PUBLIC_HOST"] = display_host(intentkit_host)
    env["XIAN_INTENTKIT_PORT"] = str(intentkit_port)
    env["XIAN_INTENTKIT_API_PORT"] = str(intentkit_api_port)
    env["XIAN_INTENTKIT_S3_PORT"] = env.get("XIAN_INTENTKIT_S3_PORT", "39000")
    env["XIAN_SHIELDED_RELAYER_ENABLED"] = (
        "1" if shielded_relayer_enabled else "0"
    )
    env["XIAN_SHIELDED_RELAYER_HOST"] = shielded_relayer_host
    env["XIAN_SHIELDED_RELAYER_PUBLIC_HOST"] = display_host(
        shielded_relayer_host
    )
    env["XIAN_SHIELDED_RELAYER_PORT"] = str(shielded_relayer_port)
    return env


def add_node_image_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--node-image-mode",
        choices=("local_build", "registry"),
        default="local_build",
    )
    parser.add_argument("--node-integrated-image")
    parser.add_argument("--node-split-image")


def run_python_script(
    script_path: Path,
    *args: str,
    capture_output: bool = False,
    uv_project: Path | None = None,
    uv_with: list[Path] | None = None,
    uv_python: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(script_path), *args]
    if uv_project is not None:
        cmd = [
            "uv",
            "run",
            "--project",
            str(uv_project),
        ]
        if uv_with is not None:
            for dependency in uv_with:
                cmd.extend(["--with", str(dependency)])
        if uv_python is not None:
            cmd.extend(["--python", uv_python])
        cmd.extend(["python3", str(script_path), *args])
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
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    intentkit_enabled: bool,
    intentkit_network_id: str,
    intentkit_host: str,
    intentkit_port: int,
    intentkit_api_port: int,
    shielded_relayer_enabled: bool,
    shielded_relayer_host: str,
    shielded_relayer_port: int,
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
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
    )
    env["XIAN_SHIELDED_RELAYER_NODE_URL"] = rpc_base_url(rpc_url)

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
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "container_target": container_target,
        "node_target": node_target,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "intentkit_enabled": intentkit_enabled,
        "shielded_relayer_enabled": shielded_relayer_enabled,
        "rpc_checked": wait_for_health,
    }
    if dashboard_enabled:
        result["dashboard_target"] = dashboard_target
        result["dashboard_url"] = (
            f"http://{display_host(dashboard_host)}:{dashboard_port}"
        )
    if monitoring_enabled:
        result["monitoring_target"] = monitoring_target
        result["prometheus_url"] = "http://127.0.0.1:9090"
        result["grafana_url"] = "http://127.0.0.1:3000"
    if shielded_relayer_enabled:
        result["shielded_relayer_url"] = (
            f"http://{display_host(shielded_relayer_host)}:"
            f"{shielded_relayer_port}"
        )
    rpc_status = None
    if wait_for_health or intentkit_enabled or shielded_relayer_enabled:
        rpc_status = wait_for_rpc_ready(
            rpc_url=rpc_url,
            timeout_seconds=rpc_timeout_seconds,
        )
    if wait_for_health and rpc_status is not None:
        result["rpc_status"] = rpc_status
    if intentkit_enabled:
        if rpc_status is None:
            raise RuntimeError("xian-intentkit requires RPC readiness")
        result.update(
            ensure_intentkit_env(
                network_id=intentkit_network_id,
                chain_id=rpc_chain_id(rpc_status),
                rpc_status_url=rpc_url,
                bind_host=intentkit_host,
                frontend_port=intentkit_port,
                api_port=intentkit_api_port,
                env=env,
            )
        )
        result["intentkit_status"] = start_intentkit_runtime(env=env)
    if shielded_relayer_enabled:
        if rpc_status is None:
            raise RuntimeError("shielded relayer requires RPC readiness")
        result["shielded_relayer_status"] = start_shielded_relayer_runtime(
            bind_host=shielded_relayer_host,
            port=shielded_relayer_port,
            env=env,
        )
    return result


def backend_stop(
    *,
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    intentkit_enabled: bool,
    intentkit_network_id: str,
    intentkit_host: str,
    intentkit_port: int,
    intentkit_api_port: int,
    shielded_relayer_enabled: bool,
    shielded_relayer_host: str,
    shielded_relayer_port: int,
) -> dict:
    container_target = "abci-bds-down" if service_node else "abci-down"
    dashboard_target = "dashboard-bds-down" if service_node else "dashboard-down"
    monitoring_target = (
        "monitoring-bds-down" if service_node else "monitoring-down"
    )
    env = runtime_env(
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
    )
    intentkit_result = None
    if intentkit_enabled:
        intentkit_result = stop_intentkit_runtime(env=env)
    shielded_relayer_result = None
    if shielded_relayer_enabled:
        shielded_relayer_result = stop_shielded_relayer_runtime(
            bind_host=shielded_relayer_host,
            port=shielded_relayer_port,
            env=env,
        )
    if dashboard_enabled:
        run_make_target(dashboard_target, env=env)
    if monitoring_enabled:
        run_make_target(monitoring_target, env=env)
    run_make_target("node-stop", env=env)
    return {
        "stack_dir": str(STACK_DIR),
        "service_node": service_node,
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "container_target": container_target,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "intentkit_enabled": intentkit_enabled,
        "shielded_relayer_enabled": shielded_relayer_enabled,
        "dashboard_target": dashboard_target if dashboard_enabled else None,
        "monitoring_target": monitoring_target if monitoring_enabled else None,
        "intentkit_status": intentkit_result,
        "shielded_relayer_status": shielded_relayer_result,
    }


def backend_status(
    *,
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    intentkit_enabled: bool,
    intentkit_network_id: str,
    intentkit_host: str,
    intentkit_port: int,
    intentkit_api_port: int,
    shielded_relayer_enabled: bool,
    shielded_relayer_host: str,
    shielded_relayer_port: int,
) -> dict:
    env = runtime_env(
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
    )
    env["XIAN_SERVICE_NODE"] = "1" if service_node else "0"
    result = run_make_target("node-status", capture_output=True, env=env)
    payload = json.loads(result.stdout)
    payload["dashboard_enabled"] = dashboard_enabled
    payload["monitoring_enabled"] = monitoring_enabled
    payload["intentkit_enabled"] = intentkit_enabled
    payload["shielded_relayer_enabled"] = shielded_relayer_enabled
    payload["node_image_mode"] = node_image_mode
    payload["node_integrated_image"] = node_integrated_image
    payload["node_split_image"] = node_split_image
    endpoints = backend_endpoints(
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
    )["endpoints"]
    payload["endpoints"] = endpoints
    if dashboard_enabled:
        dashboard_base_url = str(endpoints.get("dashboard", ""))
        dashboard_status_url = str(endpoints.get("dashboard_status", ""))
        payload["dashboard_url"] = dashboard_base_url
        try:
            payload["dashboard_status"] = fetch_json(
                dashboard_status_url,
                timeout=2.0,
            )
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
        prometheus_base = str(endpoints.get("prometheus", DEFAULT_PROMETHEUS_URL))
        grafana_base = str(endpoints.get("grafana", DEFAULT_GRAFANA_URL))
        prometheus_url = f"{prometheus_base}/api/v1/status/runtimeinfo"
        grafana_url = f"{grafana_base}/api/health"
        payload["prometheus_url"] = prometheus_base
        payload["grafana_url"] = grafana_base
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
    if intentkit_enabled:
        payload["intentkit_url"] = str(endpoints.get("intentkit", ""))
        payload["intentkit_api_url"] = str(endpoints.get("intentkit_api", ""))
        try:
            payload["intentkit_status"] = get_intentkit_status(env=env)
            payload["intentkit_running"] = payload["intentkit_status"].get(
                "intentkit_running"
            )
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            payload["intentkit_running"] = False
            payload["intentkit_error"] = str(exc)
        try:
            payload["intentkit_probe"] = probe_http_endpoint(
                str(endpoints["intentkit"]),
                timeout=2.0,
            )
            payload["intentkit_reachable"] = True
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
        ) as exc:
            payload["intentkit_reachable"] = False
            payload["intentkit_probe_error"] = str(exc)
        try:
            payload["intentkit_api_status"] = fetch_json(
                str(endpoints["intentkit_api_health"]),
                timeout=2.0,
            )
            payload["intentkit_api_reachable"] = True
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            payload["intentkit_api_reachable"] = False
            payload["intentkit_api_error"] = str(exc)
    if shielded_relayer_enabled:
        payload["shielded_relayer_url"] = str(
            endpoints.get("shielded_relayer", DEFAULT_SHIELDED_RELAYER_URL)
        )
        payload["shielded_relayer_status"] = get_shielded_relayer_status(
            bind_host=shielded_relayer_host,
            port=shielded_relayer_port,
            env=env,
        )
        payload["shielded_relayer_running"] = bool(
            payload["shielded_relayer_status"].get(
                "shielded_relayer_running"
            )
        )
        try:
            payload["shielded_relayer_info"] = fetch_json(
                str(endpoints["shielded_relayer_info"]),
                timeout=2.0,
            )
            payload["shielded_relayer_reachable"] = True
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            payload["shielded_relayer_reachable"] = False
            payload["shielded_relayer_error"] = str(exc)
    return payload


def backend_endpoints(
    *,
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    intentkit_enabled: bool,
    intentkit_network_id: str,
    intentkit_host: str,
    intentkit_port: int,
    intentkit_api_port: int,
    shielded_relayer_enabled: bool,
    shielded_relayer_host: str,
    shielded_relayer_port: int,
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
        display_dashboard_host = display_host(dashboard_host)
        endpoints["dashboard"] = (
            f"http://{display_dashboard_host}:{dashboard_port}"
        )
        endpoints["dashboard_status"] = (
            f"http://{display_dashboard_host}:{dashboard_port}/api/status"
        )
    if monitoring_enabled:
        endpoints["prometheus"] = DEFAULT_PROMETHEUS_URL
        endpoints["grafana"] = DEFAULT_GRAFANA_URL
    if intentkit_enabled:
        endpoints.update(
            intentkit_endpoints(
                bind_host=intentkit_host,
                frontend_port=intentkit_port,
                api_port=intentkit_api_port,
            )
        )
    if shielded_relayer_enabled:
        endpoints.update(
            shielded_relayer_endpoints(
                bind_host=shielded_relayer_host,
                port=shielded_relayer_port,
            )
        )
    endpoints.update(
        _discover_runtime_endpoints(
            service_node=service_node,
            dashboard_enabled=dashboard_enabled,
            monitoring_enabled=monitoring_enabled,
        )
    )
    if service_node:
        endpoints["bds_status_query"] = build_abci_query_url(
            rpc_url=endpoints["rpc_status"],
            path="/bds_status",
        )
        endpoints["bds_spool_query"] = build_abci_query_url(
            rpc_url=endpoints["rpc_status"],
            path="/bds_spool/limit=20/offset=0",
        )
    return {
        "stack_dir": str(STACK_DIR),
        "service_node": service_node,
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "intentkit_enabled": intentkit_enabled,
        "shielded_relayer_enabled": shielded_relayer_enabled,
        "intentkit_network_id": intentkit_network_id,
        "endpoints": endpoints,
    }


def backend_health(
    *,
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
    service_node: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
    intentkit_enabled: bool,
    intentkit_network_id: str,
    intentkit_host: str,
    intentkit_port: int,
    intentkit_api_port: int,
    shielded_relayer_enabled: bool,
    shielded_relayer_host: str,
    shielded_relayer_port: int,
    rpc_url: str,
    check_disk: bool,
) -> dict:
    status = backend_status(
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
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
    if intentkit_enabled:
        checks["intentkit"] = {
            "ok": bool(status.get("intentkit_reachable")),
            "detail": {
                "url": endpoints.get("intentkit"),
                "error": status.get("intentkit_probe_error")
                or status.get("intentkit_error"),
            },
        }
        checks["intentkit_api"] = {
            "ok": bool(status.get("intentkit_api_reachable")),
            "detail": {
                "url": endpoints.get("intentkit_api"),
                "error": status.get("intentkit_api_error"),
            },
        }
        checks["grafana"] = {
            "ok": bool(status.get("grafana_reachable")),
            "detail": {
                "url": endpoints.get("grafana"),
                "error": status.get("grafana_error"),
            },
        }
    if shielded_relayer_enabled:
        checks["shielded_relayer"] = {
            "ok": bool(status.get("shielded_relayer_running"))
            and bool(status.get("shielded_relayer_reachable")),
            "detail": {
                "url": endpoints.get("shielded_relayer"),
                "error": status.get("shielded_relayer_error"),
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
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "dashboard_enabled": dashboard_enabled,
        "monitoring_enabled": monitoring_enabled,
        "intentkit_enabled": intentkit_enabled,
        "intentkit_network_id": intentkit_network_id,
        "shielded_relayer_enabled": shielded_relayer_enabled,
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


def backend_bds_snapshot_export(
    *,
    output_path: str | None,
    force: bool,
) -> dict:
    resolved_output = resolve_bds_snapshot_path(output_path)
    ensure_path_within_cometbft_home(resolved_output)
    env = {
        **os.environ.copy(),
        "XIAN_BDS_SNAPSHOT_PATH": str(resolved_output),
        "XIAN_BDS_SNAPSHOT_FORCE": "1" if force else "0",
    }
    result = run_make_target(
        "bds-snapshot-export",
        capture_output=True,
        env=env,
    )
    payload = {
        "stack_dir": str(STACK_DIR),
        "target": "bds-snapshot-export",
        "ok": True,
        "output_path": str(resolved_output),
        "force": force,
    }
    if result.stdout:
        payload["stdout"] = result.stdout
    if result.stderr:
        payload["stderr"] = result.stderr
    return payload


def backend_bds_snapshot_import(
    *,
    input_path: str | None,
    clear_spool: bool,
) -> dict:
    resolved_input = resolve_bds_snapshot_path(input_path)
    ensure_path_within_cometbft_home(resolved_input)
    env = {
        **os.environ.copy(),
        "XIAN_BDS_SNAPSHOT_PATH": str(resolved_input),
        "XIAN_BDS_SNAPSHOT_CLEAR_SPOOL": "1" if clear_spool else "0",
    }
    result = run_make_target(
        "bds-snapshot-import",
        capture_output=True,
        env=env,
    )
    payload = {
        "stack_dir": str(STACK_DIR),
        "target": "bds-snapshot-import",
        "ok": True,
        "input_path": str(resolved_input),
        "clear_spool": clear_spool,
    }
    if result.stdout:
        payload["stdout"] = result.stdout
    if result.stderr:
        payload["stderr"] = result.stderr
    return payload


def backend_localnet_init(
    *,
    nodes: int,
    clean: bool,
    topology: str,
    genesis_network: str,
) -> dict:
    args = [
        "--nodes",
        str(nodes),
        "--topology",
        topology,
        "--genesis-network",
        genesis_network,
    ]
    if clean:
        args.append("--clean")
    try:
        result = run_python_script(
            LOCALNET_INIT_SCRIPT,
            *args,
            capture_output=True,
            uv_project=resolve_repo_dir("xian-abci", "XIAN_ABCI_DIR"),
            uv_python=STACK_UV_PYTHON,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"localnet-init failed: {detail}") from exc
    metadata = load_localnet_metadata()
    return {
        "stack_dir": str(STACK_DIR),
        "node_count": nodes,
        "clean": clean,
        "topology": topology,
        "genesis_network": genesis_network,
        "network": metadata,
        "stdout": result.stdout,
    }


def backend_localnet_up(
    *,
    wait_for_health: bool,
    rpc_timeout_seconds: float,
) -> dict:
    try:
        run_make_target("localnet-up")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"localnet-up failed: {detail}") from exc
    result = {
        "stack_dir": str(STACK_DIR),
        "rpc_checked": wait_for_health,
    }
    if wait_for_health:
        try:
            result["nodes"] = wait_for_localnet_ready(
                timeout_seconds=rpc_timeout_seconds
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"localnet-up failed: {exc}"
            ) from exc
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
    uv_project: Path | None = None,
    uv_with: list[Path] | None = None,
) -> dict:
    args: list[str] = []
    if duration_minutes is not None:
        args.append(str(duration_minutes))
    if script_args is not None:
        args.extend(script_args)
    try:
        result = run_python_script(
            script_path,
            *args,
            capture_output=True,
            uv_project=uv_project or resolve_repo_dir("xian-py", "XIAN_PY_DIR"),
            uv_with=uv_with,
            uv_python=STACK_UV_PYTHON,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(
            f"{script_path.name} failed: {detail}"
        ) from exc
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


def backend_localnet_e2e(
    *,
    bootstrap: bool,
    build: bool,
    nodes: int,
    topology: str,
    genesis_network: str,
    bds_node_index: int,
    port_offset: int,
    seed: str,
    log_level: str,
    rpc_timeout_seconds: float,
    state_sample_nodes: int,
    app_hash_window: int,
    receipt_workers: int,
    periodic_rounds: int,
    periodic_interval_seconds: float,
    burst_counter_ops: int,
    dex_rounds: int,
    start_phase: str,
    resume_dir: str | None,
) -> dict:
    args = [
        "--nodes",
        str(nodes),
        "--topology",
        topology,
        "--genesis-network",
        genesis_network,
        "--bds-node-index",
        str(bds_node_index),
        "--seed",
        seed,
        "--port-offset",
        str(port_offset),
        "--log-level",
        log_level,
        "--rpc-timeout-seconds",
        str(rpc_timeout_seconds),
        "--state-sample-nodes",
        str(state_sample_nodes),
        "--app-hash-window",
        str(app_hash_window),
        "--receipt-workers",
        str(receipt_workers),
        "--periodic-rounds",
        str(periodic_rounds),
        "--periodic-interval-seconds",
        str(periodic_interval_seconds),
        "--burst-counter-ops",
        str(burst_counter_ops),
        "--dex-rounds",
        str(dex_rounds),
        "--start-phase",
        start_phase,
        "--bootstrap" if bootstrap else "--no-bootstrap",
        "--build" if build else "--no-build",
    ]
    if resume_dir:
        args.extend(["--resume-dir", resume_dir])
    try:
        result = run_python_script(
            LOCALNET_E2E_SCRIPT,
            *args,
            capture_output=True,
            uv_project=resolve_repo_dir("xian-py", "XIAN_PY_DIR"),
            uv_python=STACK_UV_PYTHON,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"localnet-e2e failed: {detail}") from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "localnet-e2e did not return a JSON summary"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("localnet-e2e returned a non-object summary")
    return payload


def add_intentkit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--intentkit",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--intentkit-network-id",
        choices=SUPPORTED_INTENTKIT_NETWORK_IDS,
        default="xian-localnet",
    )
    parser.add_argument(
        "--intentkit-host",
        default="127.0.0.1",
    )
    parser.add_argument(
        "--intentkit-port",
        type=int,
        default=38000,
    )
    parser.add_argument(
        "--intentkit-api-port",
        type=int,
        default=38080,
    )


def add_shielded_relayer_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--shielded-relayer",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--shielded-relayer-host",
        default=DEFAULT_SHIELDED_RELAYER_HOST,
    )
    parser.add_argument(
        "--shielded-relayer-port",
        type=int,
        default=DEFAULT_SHIELDED_RELAYER_PORT,
    )


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
    add_node_image_args(start)
    add_intentkit_args(start)
    add_shielded_relayer_args(start)
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
    add_node_image_args(stop)
    add_intentkit_args(stop)
    add_shielded_relayer_args(stop)

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
    add_node_image_args(status)
    add_intentkit_args(status)
    add_shielded_relayer_args(status)

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
    add_node_image_args(endpoints)
    add_intentkit_args(endpoints)
    add_shielded_relayer_args(endpoints)

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
    add_node_image_args(health)
    add_intentkit_args(health)
    add_shielded_relayer_args(health)
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

    bds_snapshot_export = subparsers.add_parser("bds-snapshot-export")
    bds_snapshot_export.add_argument(
        "--output-path",
        help=(
            "Host path under XIAN_COMETBFT_HOME for the exported BDS snapshot "
            f"(default: {DEFAULT_BDS_SNAPSHOT_PATH})"
        ),
    )
    bds_snapshot_export.add_argument(
        "--force",
        action=argparse.BooleanOptionalAction,
        default=False,
    )

    bds_snapshot_import = subparsers.add_parser("bds-snapshot-import")
    bds_snapshot_import.add_argument(
        "--input-path",
        help=(
            "Host path under XIAN_COMETBFT_HOME for the BDS snapshot archive "
            f"(default: {DEFAULT_BDS_SNAPSHOT_PATH})"
        ),
    )
    bds_snapshot_import.add_argument(
        "--clear-spool",
        action=argparse.BooleanOptionalAction,
        default=False,
    )

    localnet_init = subparsers.add_parser("localnet-init")
    localnet_init.add_argument(
        "--nodes",
        type=int,
        default=4,
        help="number of validator nodes for the localnet (minimum 4)",
    )
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
    localnet_init.add_argument(
        "--genesis-network",
        default="local",
        help="contract bundle preset used to seed localnet genesis",
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

    localnet_validator_governance = subparsers.add_parser(
        "localnet-validator-governance"
    )
    localnet_validator_governance.add_argument(
        "--seed",
        default="xian-localnet-testnet-governance-v1",
    )
    localnet_validator_governance.add_argument(
        "--nodes",
        type=int,
        default=5,
        help="validator count for the governance exercise (expects 5)",
    )
    localnet_validator_governance.add_argument(
        "--port-offset",
        type=int,
        default=1000,
    )
    localnet_validator_governance.add_argument(
        "--topology",
        choices=("integrated", "fidelity"),
        default="integrated",
    )
    localnet_validator_governance.add_argument(
        "--genesis-network",
        default="testnet",
        help="contract bundle preset used to seed localnet genesis",
    )
    localnet_validator_governance.add_argument(
        "--tracer-mode",
        default="native_instruction_v1",
    )
    localnet_validator_governance.add_argument(
        "--log-level",
        default="INFO",
    )
    localnet_validator_governance.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=180.0,
    )
    localnet_validator_governance.add_argument(
        "--bootstrap",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    localnet_validator_governance.add_argument(
        "--build",
        action=argparse.BooleanOptionalAction,
        default=False,
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

    localnet_e2e = subparsers.add_parser("localnet-e2e")
    localnet_e2e.add_argument(
        "--bootstrap",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    localnet_e2e.add_argument(
        "--build",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    localnet_e2e.add_argument(
        "--nodes",
        type=int,
        default=5,
    )
    localnet_e2e.add_argument(
        "--topology",
        choices=("integrated", "fidelity"),
        default="integrated",
    )
    localnet_e2e.add_argument(
        "--genesis-network",
        default="testnet",
    )
    localnet_e2e.add_argument(
        "--bds-node-index",
        type=int,
        default=0,
    )
    localnet_e2e.add_argument(
        "--port-offset",
        type=int,
        default=1000,
    )
    localnet_e2e.add_argument(
        "--seed",
        default="xian-localnet-testnet-e2e-v1",
    )
    localnet_e2e.add_argument(
        "--log-level",
        default="INFO",
    )
    localnet_e2e.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=180.0,
    )
    localnet_e2e.add_argument(
        "--state-sample-nodes",
        type=int,
        default=5,
    )
    localnet_e2e.add_argument(
        "--app-hash-window",
        type=int,
        default=5,
    )
    localnet_e2e.add_argument(
        "--receipt-workers",
        type=int,
        default=24,
    )
    localnet_e2e.add_argument(
        "--periodic-rounds",
        type=int,
        default=8,
    )
    localnet_e2e.add_argument(
        "--periodic-interval-seconds",
        type=float,
        default=0.35,
    )
    localnet_e2e.add_argument(
        "--burst-counter-ops",
        type=int,
        default=260,
    )
    localnet_e2e.add_argument(
        "--dex-rounds",
        type=int,
        default=8,
    )
    localnet_e2e.add_argument(
        "--start-phase",
        default="00-bootstrap",
    )
    localnet_e2e.add_argument(
        "--resume-dir",
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
            node_image_mode=args.node_image_mode,
            node_integrated_image=args.node_integrated_image,
            node_split_image=args.node_split_image,
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            intentkit_enabled=args.intentkit,
            intentkit_network_id=args.intentkit_network_id,
            intentkit_host=args.intentkit_host,
            intentkit_port=args.intentkit_port,
            intentkit_api_port=args.intentkit_api_port,
            shielded_relayer_enabled=args.shielded_relayer,
            shielded_relayer_host=args.shielded_relayer_host,
            shielded_relayer_port=args.shielded_relayer_port,
            wait_for_health=args.wait_for_health,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            rpc_url=args.rpc_url,
        )
    elif args.command == "stop":
        payload = backend_stop(
            node_image_mode=args.node_image_mode,
            node_integrated_image=args.node_integrated_image,
            node_split_image=args.node_split_image,
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            intentkit_enabled=args.intentkit,
            intentkit_network_id=args.intentkit_network_id,
            intentkit_host=args.intentkit_host,
            intentkit_port=args.intentkit_port,
            intentkit_api_port=args.intentkit_api_port,
            shielded_relayer_enabled=args.shielded_relayer,
            shielded_relayer_host=args.shielded_relayer_host,
            shielded_relayer_port=args.shielded_relayer_port,
        )
    elif args.command == "status":
        payload = backend_status(
            node_image_mode=args.node_image_mode,
            node_integrated_image=args.node_integrated_image,
            node_split_image=args.node_split_image,
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            intentkit_enabled=args.intentkit,
            intentkit_network_id=args.intentkit_network_id,
            intentkit_host=args.intentkit_host,
            intentkit_port=args.intentkit_port,
            intentkit_api_port=args.intentkit_api_port,
            shielded_relayer_enabled=args.shielded_relayer,
            shielded_relayer_host=args.shielded_relayer_host,
            shielded_relayer_port=args.shielded_relayer_port,
        )
    elif args.command == "endpoints":
        payload = backend_endpoints(
            node_image_mode=args.node_image_mode,
            node_integrated_image=args.node_integrated_image,
            node_split_image=args.node_split_image,
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            intentkit_enabled=args.intentkit,
            intentkit_network_id=args.intentkit_network_id,
            intentkit_host=args.intentkit_host,
            intentkit_port=args.intentkit_port,
            intentkit_api_port=args.intentkit_api_port,
            shielded_relayer_enabled=args.shielded_relayer,
            shielded_relayer_host=args.shielded_relayer_host,
            shielded_relayer_port=args.shielded_relayer_port,
        )
    elif args.command == "health":
        payload = backend_health(
            node_image_mode=args.node_image_mode,
            node_integrated_image=args.node_integrated_image,
            node_split_image=args.node_split_image,
            service_node=args.service_node,
            dashboard_enabled=args.dashboard,
            monitoring_enabled=args.monitoring,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            intentkit_enabled=args.intentkit,
            intentkit_network_id=args.intentkit_network_id,
            intentkit_host=args.intentkit_host,
            intentkit_port=args.intentkit_port,
            intentkit_api_port=args.intentkit_api_port,
            shielded_relayer_enabled=args.shielded_relayer,
            shielded_relayer_host=args.shielded_relayer_host,
            shielded_relayer_port=args.shielded_relayer_port,
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
    elif args.command == "bds-snapshot-export":
        payload = backend_bds_snapshot_export(
            output_path=args.output_path,
            force=args.force,
        )
    elif args.command == "bds-snapshot-import":
        payload = backend_bds_snapshot_import(
            input_path=args.input_path,
            clear_spool=args.clear_spool,
        )
    elif args.command == "localnet-init":
        payload = backend_localnet_init(
            nodes=args.nodes,
            clean=args.clean,
            topology=args.topology,
            genesis_network=args.genesis_network,
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
    elif args.command == "localnet-validator-governance":
        payload = backend_localnet_diagnostic(
            script_path=LOCALNET_VALIDATOR_GOVERNANCE_SCRIPT,
            script_args=[
                "--seed",
                args.seed,
                "--nodes",
                str(args.nodes),
                "--port-offset",
                str(args.port_offset),
                "--topology",
                args.topology,
                "--genesis-network",
                args.genesis_network,
                "--tracer-mode",
                args.tracer_mode,
                "--log-level",
                args.log_level,
                "--rpc-timeout-seconds",
                str(args.rpc_timeout_seconds),
                "--bootstrap" if args.bootstrap else "--no-bootstrap",
                "--build" if args.build else "--no-build",
            ],
            uv_project=resolve_repo_dir("xian-abci", "XIAN_ABCI_DIR"),
            uv_with=[resolve_repo_dir("xian-py", "XIAN_PY_DIR")],
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
    elif args.command == "localnet-e2e":
        payload = backend_localnet_e2e(
            bootstrap=args.bootstrap,
            build=args.build,
            nodes=args.nodes,
            topology=args.topology,
            genesis_network=args.genesis_network,
            bds_node_index=args.bds_node_index,
            port_offset=args.port_offset,
            seed=args.seed,
            log_level=args.log_level,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
            state_sample_nodes=args.state_sample_nodes,
            app_hash_window=args.app_hash_window,
            receipt_workers=args.receipt_workers,
            periodic_rounds=args.periodic_rounds,
            periodic_interval_seconds=args.periodic_interval_seconds,
            burst_counter_ops=args.burst_counter_ops,
            dex_rounds=args.dex_rounds,
            start_phase=args.start_phase,
            resume_dir=args.resume_dir,
        )
    else:
        raise ValueError(f"unsupported command: {args.command}")

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(format_subprocess_error(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
