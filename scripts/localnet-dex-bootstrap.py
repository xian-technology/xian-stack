#!/usr/bin/env python3
"""Deploy canonical DEX contracts and a local demo pool to a running localnet."""

from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.request import urlopen
import tomllib

from xian_py.models import TransactionSubmission
from xian_py.wallet import Wallet
from xian_py.xian import Xian

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
ROOT_DIR = STACK_DIR.parent
WORKLOADS_DIR = STACK_DIR / "workloads"
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
DEFAULT_DEX_CONTRACTS_DIR = ROOT_DIR / "xian-dex" / "src"
DEFAULT_XIAN_CONFIG_PATH = STACK_DIR / ".cometbft" / "config" / "xian.toml"
DEFAULT_VALIDATOR_KEY_PATH = (
    STACK_DIR / ".cometbft" / "config" / "priv_validator_key.json"
)
XIAN_CONTRACTING_SRC = ROOT_DIR / "xian-contracting" / "src"

sys.path.insert(0, str(XIAN_CONTRACTING_SRC))

try:
    from contracting.compilation.artifacts import build_contract_artifacts
except ImportError:  # pragma: no cover - only used when xian-contracting is absent.
    build_contract_artifacts = None


CORE_DEPLOY_CHI = {
    "con_pairs": 300_000,
    "con_dex": 200_000,
    "con_dex_helper": 160_000,
}
TOKEN_DEPLOY_CHI = 160_000
LP_TOKEN_DEPLOY_CHI = 160_000
TOKEN_TX_CHI = 7_500
DEX_TX_CHI = 75_000
CONTRACT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DexBootstrapError(RuntimeError):
    pass


def load_network(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def localnet_rpc_url(network: dict[str, Any]) -> str:
    nodes = network.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise DexBootstrapError("localnet metadata has no node entries")
    return f"http://127.0.0.1:{nodes[0]['host_rpc_port']}"


def load_xian_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def private_key_from_validator_key(path: Path) -> str | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    value = payload.get("priv_key", {}).get("value")
    if not isinstance(value, str) or not value:
        return None
    raw = base64.b64decode(value)
    if len(raw) < 32:
        raise DexBootstrapError(f"validator key at {path} is too short")
    return raw[:32].hex()


def resolve_deployer_private_key(
    *,
    args: argparse.Namespace,
    network: dict[str, Any] | None,
) -> str:
    explicit = args.deployer_private_key or os.environ.get(
        "XIAN_DEX_DEPLOYER_PRIVATE_KEY"
    )
    if explicit:
        return explicit

    if args.rpc_url:
        validator_key = private_key_from_validator_key(args.validator_key_path)
        if validator_key is not None:
            return validator_key

    if network is not None and network.get("founder_key"):
        return str(network["founder_key"])

    validator_key = private_key_from_validator_key(args.validator_key_path)
    if validator_key is not None:
        return validator_key

    raise DexBootstrapError(
        "no deployer private key found; pass --deployer-private-key, set "
        "XIAN_DEX_DEPLOYER_PRIVATE_KEY, or run against a local stack with "
        f"{args.validator_key_path}"
    )


def fetch_json(url: str, *, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_rpc_ready(rpc_url: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload = fetch_json(f"{rpc_url}/status", timeout=2.0)
            latest_height = int(
                payload["result"]["sync_info"]["latest_block_height"]
            )
            if latest_height >= 1:
                return
        except Exception as exc:  # noqa: PERF203
            last_error = exc
        time.sleep(1.0)
    raise DexBootstrapError(f"localnet RPC did not become ready at {rpc_url}") from last_error


def deadline_value(*, seconds_from_now: int) -> dict[str, list[int]]:
    future = datetime.now(UTC) + timedelta(seconds=seconds_from_now)
    return {
        "__time__": [
            future.year,
            future.month,
            future.day,
            future.hour,
            future.minute,
            future.second,
            future.microsecond,
        ]
    }


def validate_contract_name(value: str, *, label: str) -> str:
    if not CONTRACT_NAME_RE.match(value):
        raise DexBootstrapError(f"{label} must be a valid contract name, got {value!r}")
    return value


def coerce_number(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise DexBootstrapError(f"expected numeric state value, got {value!r}")


def receipt_payload(submission: TransactionSubmission) -> dict[str, Any]:
    receipt = submission.receipt
    return {
        "tx_hash": submission.tx_hash,
        "nonce": submission.nonce,
        "chi_supplied": submission.chi_supplied,
        "success": receipt.success if receipt is not None else None,
        "message": receipt.message if receipt is not None else submission.message,
    }


def require_success(label: str, submission: TransactionSubmission) -> None:
    if not submission.submitted:
        raise DexBootstrapError(f"{label}: transaction was not submitted")
    if submission.accepted is False:
        raise DexBootstrapError(f"{label}: transaction was rejected: {submission.message}")
    if submission.receipt is None:
        raise DexBootstrapError(f"{label}: transaction did not return a receipt")
    if not submission.receipt.success:
        raise DexBootstrapError(
            f"{label}: transaction failed: {submission.receipt.message}"
        )


def read_required(path: Path) -> str:
    if not path.exists():
        raise DexBootstrapError(f"required contract source not found: {path}")
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=128)
def build_deployment_artifacts_for(module_name: str, source: str) -> dict[str, Any]:
    if build_contract_artifacts is None:
        raise DexBootstrapError(
            "xian-contracting is required to build VM deployment artifacts"
        )
    return build_contract_artifacts(module_name=module_name, source=source)


def deployment_artifacts(
    *,
    execution_mode: str,
    module_name: str,
    source: str,
) -> dict[str, Any] | None:
    if execution_mode != "xian_vm_v1":
        return None
    return build_deployment_artifacts_for(module_name, source)


def execution_mode(
    network: dict[str, Any] | None,
    *,
    xian_config_path: Path,
) -> str:
    if network is None:
        config = load_xian_config(xian_config_path)
        execution = config.get("execution", {})
        engine = execution.get("engine", {}) if isinstance(execution, dict) else {}
        mode = engine.get("mode") if isinstance(engine, dict) else None
        return str(mode or config.get("tracer_mode") or "python_line_v1")

    execution = network.get("execution", {})
    mode = execution.get("mode") if isinstance(execution, dict) else None
    return str(mode or network.get("tracer_mode") or "python_line_v1")


def contract_exists(client: Xian, name: str) -> bool:
    return bool(client.get_contract(name))


def wait_for_contract(client: Xian, name: str, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if contract_exists(client, name):
            return
        time.sleep(0.5)
    raise DexBootstrapError(f"contract {name!r} did not become visible")


def submit_contract_if_missing(
    client: Xian,
    *,
    name: str,
    code: str,
    constructor_args: dict[str, Any] | None,
    chi: int,
    execution_mode_name: str,
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    if contract_exists(client, name):
        return {"contract": name, "action": "skipped", "reason": "already_exists"}

    print(f"Deploying {name}...")
    submission = client.submit_contract(
        name,
        code,
        args=constructor_args,
        deployment_artifacts=deployment_artifacts(
            execution_mode=execution_mode_name,
            module_name=name,
            source=code,
        ),
        chi=chi,
        mode=mode,
        wait_for_tx=True,
        timeout_seconds=receipt_timeout_seconds,
    )
    require_success(f"deploy {name}", submission)
    wait_for_contract(client, name, timeout_seconds=receipt_timeout_seconds)
    return {
        "contract": name,
        "action": "deployed",
        "tx": receipt_payload(submission),
    }


def send_call(
    client: Xian,
    *,
    label: str,
    contract: str,
    function: str,
    kwargs: dict[str, Any],
    chi: int,
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    print(label)
    submission = client.send_tx(
        contract,
        function,
        kwargs,
        chi=chi,
        mode=mode,
        wait_for_tx=True,
        timeout_seconds=receipt_timeout_seconds,
    )
    require_success(label, submission)
    return {
        "label": label,
        "contract": contract,
        "function": function,
        "tx": receipt_payload(submission),
    }


def pair_tokens(token_a: str, token_b: str) -> tuple[str, str]:
    if token_a == token_b:
        raise DexBootstrapError("DEX demo pool requires two different tokens")
    return tuple(sorted((token_a, token_b)))


def get_pair_id(client: Xian, token_a: str, token_b: str) -> int | None:
    token0, token1 = pair_tokens(token_a, token_b)
    value = client.get_state("con_pairs", "toks_to_pair", token0, token1)
    if value in (None, 0, "0"):
        return None
    return int(value)


def get_pair_snapshot(client: Xian, pair_id: int) -> dict[str, Any]:
    token0 = client.get_state("con_pairs", "pairs", pair_id, "token0")
    token1 = client.get_state("con_pairs", "pairs", pair_id, "token1")
    reserve0 = client.get_state("con_pairs", "pairs", pair_id, "reserve0")
    reserve1 = client.get_state("con_pairs", "pairs", pair_id, "reserve1")
    lp_token = client.get_state("con_pairs", "pairs", pair_id, "lpToken")
    total_supply = client.get_state("con_pairs", "pairs", pair_id, "totalSupply")
    return {
        "pair_id": pair_id,
        "token0": token0,
        "token1": token1,
        "reserve0": str(reserve0),
        "reserve1": str(reserve1),
        "lp_token": lp_token,
        "total_supply": str(total_supply),
    }


def approve_for_dex(
    client: Xian,
    *,
    token: str,
    amount: float,
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    return send_call(
        client,
        label=f"Approving {amount} {token} for con_dex...",
        contract=token,
        function="approve",
        kwargs={"amount": amount, "to": "con_dex"},
        chi=TOKEN_TX_CHI,
        mode=mode,
        receipt_timeout_seconds=receipt_timeout_seconds,
    )


def seed_demo_pool(
    client: Xian,
    *,
    token_contract: str,
    lp_contract: str,
    liquidity_currency_amount: float,
    liquidity_demo_token_amount: float,
    top_up_liquidity: bool,
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    pair_id = get_pair_id(client, "currency", token_contract)
    existing_pair = pair_id is not None
    if pair_id is not None:
        snapshot = get_pair_snapshot(client, pair_id)
        reserve0 = coerce_number(snapshot["reserve0"])
        reserve1 = coerce_number(snapshot["reserve1"])
        if reserve0 > 0 and reserve1 > 0 and not top_up_liquidity:
            return {
                "action": "skipped",
                "reason": "pair_already_seeded",
                "pair": snapshot,
                "operations": operations,
            }

        if snapshot["lp_token"] != lp_contract:
            raise DexBootstrapError(
                f"existing demo pair uses LP token {snapshot['lp_token']!r}, "
                f"expected {lp_contract!r}"
            )

    operations.append(
        approve_for_dex(
            client,
            token="currency",
            amount=liquidity_currency_amount,
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
        )
    )
    operations.append(
        approve_for_dex(
            client,
            token=token_contract,
            amount=liquidity_demo_token_amount,
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
        )
    )

    kwargs: dict[str, Any] = {
        "tokenA": "currency",
        "tokenB": token_contract,
        "amountADesired": liquidity_currency_amount,
        "amountBDesired": liquidity_demo_token_amount,
        "amountAMin": liquidity_currency_amount * 0.95,
        "amountBMin": liquidity_demo_token_amount * 0.95,
        "to": client.wallet.public_key,
        "deadline": deadline_value(seconds_from_now=300),
        "lpToken": lp_contract,
    }
    operations.append(
        send_call(
            client,
            label="Adding DEX demo liquidity...",
            contract="con_dex",
            function="addLiquidity",
            kwargs=kwargs,
            chi=DEX_TX_CHI,
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
        )
    )

    pair_id = get_pair_id(client, "currency", token_contract)
    if pair_id is None:
        raise DexBootstrapError("DEX demo pair was not created")
    return {
        "action": "topped_up" if existing_pair else "seeded",
        "pair": get_pair_snapshot(client, pair_id),
        "operations": operations,
    }


def emit_test_swap(
    client: Xian,
    *,
    token_contract: str,
    amount: float,
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    pair_id = get_pair_id(client, "currency", token_contract)
    if pair_id is None:
        raise DexBootstrapError("cannot emit a test swap before the demo pair exists")
    operations = [
        approve_for_dex(
            client,
            token="currency",
            amount=amount,
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
        )
    ]
    operations.append(
        send_call(
            client,
            label="Emitting DEX bootstrap test swap...",
            contract="con_dex",
            function="swapExactTokenForToken",
            kwargs={
                "amountIn": amount,
                "amountOutMin": 0.0001,
                "pair": pair_id,
                "src": "currency",
                "to": client.wallet.public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            chi=DEX_TX_CHI,
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
        )
    )
    return {
        "action": "swap_emitted",
        "pair": get_pair_snapshot(client, pair_id),
        "operations": operations,
    }


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    validate_contract_name(args.demo_token_contract, label="demo token contract")
    validate_contract_name(args.demo_lp_contract, label="demo LP contract")

    network = load_network(args.network_path)
    if args.rpc_url is None and network is None:
        raise DexBootstrapError(
            f"localnet metadata not found at {args.network_path}; pass --rpc-url "
            "when bootstrapping a single local stack node"
        )
    rpc_url = args.rpc_url or localnet_rpc_url(network)
    wait_for_rpc_ready(rpc_url, timeout_seconds=args.rpc_timeout_seconds)

    dex_contracts_dir = args.dex_contracts_dir.resolve()
    metadata_for_execution = None if args.rpc_url else network
    execution_mode_name = execution_mode(
        metadata_for_execution,
        xian_config_path=args.xian_config_path,
    )
    deployer_wallet = Wallet(
        private_key=resolve_deployer_private_key(args=args, network=network)
    )

    core_sources = {
        "con_pairs": read_required(dex_contracts_dir / "con_pairs.py"),
        "con_dex": read_required(dex_contracts_dir / "con_dex.py"),
    }
    if args.deploy_helper:
        core_sources["con_dex_helper"] = read_required(
            dex_contracts_dir / "con_dex_helper.py"
        )

    demo_token_source = read_required(WORKLOADS_DIR / "dex_bootstrap" / "demo_token.py")
    lp_token_source = read_required(dex_contracts_dir / "con_lp_token.py")

    chain_id = args.chain_id or (
        None if args.rpc_url else (network or {}).get("chain_id")
    )
    with Xian(rpc_url, chain_id=chain_id, wallet=deployer_wallet) as client:
        client.refresh_nonce()
        resolved_chain_id = client.chain_id
        deployments: list[dict[str, Any]] = []

        for name, source in core_sources.items():
            deployments.append(
                submit_contract_if_missing(
                    client,
                    name=name,
                    code=source,
                    constructor_args=None,
                    chi=CORE_DEPLOY_CHI[name],
                    execution_mode_name=execution_mode_name,
                    mode=args.submission_mode,
                    receipt_timeout_seconds=args.receipt_timeout_seconds,
                )
            )

        if args.seed_demo_pool or args.emit_test_swap:
            deployments.append(
                submit_contract_if_missing(
                    client,
                    name=args.demo_token_contract,
                    code=demo_token_source,
                    constructor_args={
                        "owner": deployer_wallet.public_key,
                        "supply": args.demo_token_supply,
                        "token_name": args.demo_token_name,
                        "token_symbol": args.demo_token_symbol,
                        "precision": args.demo_token_precision,
                    },
                    chi=TOKEN_DEPLOY_CHI,
                    execution_mode_name=execution_mode_name,
                    mode=args.submission_mode,
                    receipt_timeout_seconds=args.receipt_timeout_seconds,
                )
            )
            deployments.append(
                submit_contract_if_missing(
                    client,
                    name=args.demo_lp_contract,
                    code=lp_token_source,
                    constructor_args={
                        "token_name": f"{args.demo_token_symbol}/XIAN LP",
                        "token_symbol": f"XIAN-{args.demo_token_symbol}-LP",
                        "operator_address": deployer_wallet.public_key,
                        "minter_address": "con_pairs",
                    },
                    chi=LP_TOKEN_DEPLOY_CHI,
                    execution_mode_name=execution_mode_name,
                    mode=args.submission_mode,
                    receipt_timeout_seconds=args.receipt_timeout_seconds,
                )
            )

        pool_result = None
        if args.seed_demo_pool:
            pool_result = seed_demo_pool(
                client,
                token_contract=args.demo_token_contract,
                lp_contract=args.demo_lp_contract,
                liquidity_currency_amount=args.liquidity_currency_amount,
                liquidity_demo_token_amount=args.liquidity_demo_token_amount,
                top_up_liquidity=args.top_up_liquidity,
                mode=args.submission_mode,
                receipt_timeout_seconds=args.receipt_timeout_seconds,
            )

        test_swap_result = None
        if args.emit_test_swap:
            test_swap_result = emit_test_swap(
                client,
                token_contract=args.demo_token_contract,
                amount=args.test_swap_amount,
                mode=args.submission_mode,
                receipt_timeout_seconds=args.receipt_timeout_seconds,
            )

        pair_id = get_pair_id(client, "currency", args.demo_token_contract)
        pair = get_pair_snapshot(client, pair_id) if pair_id is not None else None

    return {
        "ok": True,
        "chain_id": resolved_chain_id,
        "rpc_url": rpc_url,
        "deployer": deployer_wallet.public_key,
        "execution_mode": execution_mode_name,
        "dex_contracts_dir": str(dex_contracts_dir),
        "contracts": {
            "router": "con_dex",
            "pairs": "con_pairs",
            "helper": "con_dex_helper" if args.deploy_helper else None,
            "demo_token": args.demo_token_contract
            if args.seed_demo_pool or args.emit_test_swap
            else None,
            "demo_lp_token": args.demo_lp_contract
            if args.seed_demo_pool or args.emit_test_swap
            else None,
        },
        "deployments": deployments,
        "demo_pool": pool_result,
        "test_swap": test_swap_result,
        "pair": pair,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap canonical DEX contracts on a running localnet."
    )
    parser.add_argument("--network-path", type=Path, default=NETWORK_PATH)
    parser.add_argument("--rpc-url", default=None)
    parser.add_argument("--chain-id", default=None)
    parser.add_argument("--deployer-private-key", default=None)
    parser.add_argument(
        "--validator-key-path",
        type=Path,
        default=Path(
            os.environ.get(
                "XIAN_DEX_VALIDATOR_KEY_PATH",
                DEFAULT_VALIDATOR_KEY_PATH,
            )
        ),
    )
    parser.add_argument(
        "--xian-config-path",
        type=Path,
        default=Path(os.environ.get("XIAN_CONFIG_PATH", DEFAULT_XIAN_CONFIG_PATH)),
    )
    parser.add_argument(
        "--dex-contracts-dir",
        type=Path,
        default=Path(os.environ.get("XIAN_DEX_CONTRACTS_DIR", DEFAULT_DEX_CONTRACTS_DIR)),
    )
    parser.add_argument(
        "--deploy-helper",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--seed-demo-pool",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--top-up-liquidity",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--emit-test-swap",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--demo-token-contract", default="con_dex_demo_token")
    parser.add_argument("--demo-lp-contract", default="con_dex_demo_lp")
    parser.add_argument("--demo-token-name", default="Xian DEX Demo Token")
    parser.add_argument("--demo-token-symbol", default="XDT")
    parser.add_argument("--demo-token-supply", type=float, default=1_000_000.0)
    parser.add_argument("--demo-token-precision", type=int, default=8)
    parser.add_argument("--liquidity-currency-amount", type=float, default=10_000.0)
    parser.add_argument("--liquidity-demo-token-amount", type=float, default=10_000.0)
    parser.add_argument("--test-swap-amount", type=float, default=10.0)
    parser.add_argument(
        "--submission-mode",
        choices=("checktx", "commit"),
        default="checktx",
    )
    parser.add_argument("--rpc-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--receipt-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="write progress to stderr and the final JSON summary to stdout",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.json_only:
            with contextlib.redirect_stdout(sys.stderr):
                summary = bootstrap(args)
        else:
            summary = bootstrap(args)
    except DexBootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
