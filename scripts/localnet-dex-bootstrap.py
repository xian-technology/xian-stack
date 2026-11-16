#!/usr/bin/env python3
"""Deploy a pinned DEX contract bundle and demo pool to a running localnet."""

from __future__ import annotations

import argparse
import base64
import contextlib
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

from xian_py.config import XianClientConfig
from xian_py.models import TransactionSubmission
from xian_py.wallet import Wallet
from xian_py.xian import Xian

SCRIPT_DIR = Path(__file__).resolve().parent
STACK_DIR = SCRIPT_DIR.parent
ROOT_DIR = STACK_DIR.parent
WORKLOADS_DIR = STACK_DIR / "workloads"
NETWORK_PATH = STACK_DIR / ".localnet" / "network.json"
DEFAULT_DEX_BUNDLE_PATH = ROOT_DIR / "xian-dex" / "contract-bundle.json"
DEFAULT_VALIDATOR_KEY_PATH = STACK_DIR / ".cometbft" / "config" / "priv_validator_key.json"
XIAN_CLI_SRC = ROOT_DIR / "xian-cli" / "src"

sys.path.insert(0, str(XIAN_CLI_SRC))

try:
    from xian_cli.contract_bundles import (
        contract_by_role,
        read_contract_bundle,
        read_contract_source_from_bundle,
        validate_contract_bundle,
    )
except ImportError:  # pragma: no cover - only used outside the workspace layout.
    contract_by_role = None
    read_contract_bundle = None
    read_contract_source_from_bundle = None
    validate_contract_bundle = None

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


def private_key_from_key_file(path: Path) -> str:
    key = path.expanduser().read_text(encoding="utf-8").strip()
    if not key:
        raise DexBootstrapError(f"deployer key file {path} is empty")
    return key


def resolve_deployer_private_key(
    *,
    args: argparse.Namespace,
    network: dict[str, Any] | None,
) -> str:
    """The deployer key: never a command-line value (ps and shell history show those)."""
    if args.deployer_key_file is not None:
        return private_key_from_key_file(args.deployer_key_file)
    explicit = os.environ.get("XIAN_DEX_DEPLOYER_PRIVATE_KEY")
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
        "no deployer private key found; pass --deployer-key-file, set "
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
            latest_height = int(payload["result"]["sync_info"]["latest_block_height"])
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
        "chi_estimated": submission.chi_estimated,
        "success": receipt.success if receipt is not None else None,
        "message": receipt.message if receipt is not None else submission.message,
    }


def require_success(label: str, submission: TransactionSubmission) -> None:
    if not submission.submitted:
        raise DexBootstrapError(f"{label}: transaction was not submitted")
    if submission.accepted is False:
        raise DexBootstrapError(f"{label}: transaction was rejected: {submission.message}")
    if submission.receipt is not None and not submission.receipt.success:
        raise DexBootstrapError(f"{label}: transaction failed: {submission.receipt.message}")


def read_required(path: Path) -> str:
    if not path.exists():
        raise DexBootstrapError(f"required contract source not found: {path}")
    return path.read_text(encoding="utf-8")


def _source_env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return Path(value).expanduser()


def _require_bundle_support() -> None:
    if (
        contract_by_role is None
        or read_contract_bundle is None
        or read_contract_source_from_bundle is None
        or validate_contract_bundle is None
    ):
        raise DexBootstrapError(
            "xian-cli is required to read contract bundles; use the standard "
            "workspace layout or pass --dex-contracts-dir for a development "
            "source override"
        )


def _bundle_entry_by_role(
    bundle: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    _require_bundle_support()
    entry = contract_by_role(bundle, role)
    if entry is None:
        raise DexBootstrapError(f"DEX bundle missing role {role!r}")
    return entry


def _bundle_source_by_role(
    bundle_path: Path,
    bundle: dict[str, Any],
    role: str,
) -> tuple[dict[str, Any], str]:
    entry = _bundle_entry_by_role(bundle, role)
    assert read_contract_source_from_bundle is not None
    return entry, read_contract_source_from_bundle(bundle_path, entry)


def load_dex_contract_sources(
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.dex_contracts_dir is not None:
        dex_contracts_dir = args.dex_contracts_dir.expanduser().resolve()
        core_sources = {
            "con_pairs": read_required(dex_contracts_dir / "con_pairs.py"),
            "con_dex": read_required(dex_contracts_dir / "con_dex.py"),
        }
        if args.deploy_helper:
            core_sources["con_dex_helper"] = read_required(dex_contracts_dir / "con_dex_helper.py")

        return {
            "kind": "directory",
            "bundle_path": None,
            "contracts_dir": dex_contracts_dir,
            "core_sources": core_sources,
            "lp_token_source": read_required(dex_contracts_dir / "con_lp_token.py"),
            "demo_token_source": read_required(WORKLOADS_DIR / "dex_bootstrap" / "demo_token.py"),
        }

    _require_bundle_support()
    bundle_path = args.dex_bundle.expanduser().resolve()
    assert validate_contract_bundle is not None
    assert read_contract_bundle is not None
    validate_contract_bundle(bundle_path)
    bundle = read_contract_bundle(bundle_path)

    pairs_entry, pairs_source = _bundle_source_by_role(bundle_path, bundle, "pairs")
    router_entry, router_source = _bundle_source_by_role(bundle_path, bundle, "router")
    if pairs_entry["name"] != "con_pairs" or router_entry["name"] != "con_dex":
        raise DexBootstrapError("DEX bootstrap expects canonical con_pairs and con_dex names")

    core_sources = {"con_pairs": pairs_source, "con_dex": router_source}
    if args.deploy_helper:
        helper_entry, helper_source = _bundle_source_by_role(bundle_path, bundle, "helper")
        if helper_entry["name"] != "con_dex_helper":
            raise DexBootstrapError("DEX bootstrap expects canonical con_dex_helper name")
        core_sources["con_dex_helper"] = helper_source

    _, lp_source = _bundle_source_by_role(bundle_path, bundle, "lp_token_template")
    demo_entry = contract_by_role(bundle, "demo_token")
    demo_source = None
    if demo_entry is not None:
        assert read_contract_source_from_bundle is not None
        demo_source = read_contract_source_from_bundle(bundle_path, demo_entry)

    return {
        "kind": "bundle",
        "bundle_path": bundle_path,
        "contracts_dir": None,
        "core_sources": core_sources,
        "lp_token_source": lp_source,
        "demo_token_source": demo_source
        or read_required(WORKLOADS_DIR / "dex_bootstrap" / "demo_token.py"),
    }


def contract_exists(client: Xian, name: str) -> bool:
    return bool(client.get_contract_source(name))


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
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    if contract_exists(client, name):
        return {"contract": name, "action": "skipped", "reason": "already_exists"}

    print(f"Deploying {name}...")  # chi: xian-py simulates the deployment
    submission = client.submit_contract(
        name,
        code,
        args=constructor_args,
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
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    """Submit one call; xian-py simulates it and supplies the estimate plus its margin."""
    print(label)
    submission = client.send_tx(
        contract,
        function,
        kwargs,
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


def get_registered_lp_token(client: Xian, token_a: str, token_b: str) -> str | None:
    token0, token1 = pair_tokens(token_a, token_b)
    value = client.get_state("con_pairs", "registered_lp_tokens", token0, token1)
    if value in (None, 0, "0"):
        return None
    return str(value)


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
    amount: Decimal,
    mode: str,
    receipt_timeout_seconds: float,
) -> dict[str, Any]:
    return send_call(
        client,
        label=f"Approving {amount} {token} for con_dex...",
        contract=token,
        function="approve",
        kwargs={"amount": amount, "to": "con_dex"},
        mode=mode,
        receipt_timeout_seconds=receipt_timeout_seconds,
    )


def register_lp_token_if_missing(
    client: Xian,
    *,
    token_contract: str,
    lp_contract: str,
    mode: str,
    receipt_timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Register the demo LP token for the currency pair unless it is; refuse another one."""
    registered_lp_token = get_registered_lp_token(client, "currency", token_contract)
    if registered_lp_token == lp_contract:
        return []
    if registered_lp_token is not None:
        raise DexBootstrapError(
            f"registered demo LP token is {registered_lp_token!r}, expected {lp_contract!r}"
        )
    token0, token1 = pair_tokens("currency", token_contract)
    return [
        send_call(
            client,
            label="Registering DEX demo LP token...",
            contract="con_pairs",
            function="registerLpToken",
            kwargs={"tokenA": token0, "tokenB": token1, "lpToken": lp_contract},
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
        )
    ]


def seed_demo_pool(
    client: Xian,
    *,
    token_contract: str,
    lp_contract: str,
    liquidity_currency_amount: Decimal,
    liquidity_demo_token_amount: Decimal,
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
    else:
        operations += register_lp_token_if_missing(
            client,
            token_contract=token_contract,
            lp_contract=lp_contract,
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
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
        "amountAMin": liquidity_currency_amount * Decimal("0.95"),
        "amountBMin": liquidity_demo_token_amount * Decimal("0.95"),
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
    amount: Decimal,
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
                "amountOutMin": Decimal("0.0001"),
                "pair": pair_id,
                "src": "currency",
                "to": client.wallet.public_key,
                "deadline": deadline_value(seconds_from_now=300),
            },
            mode=mode,
            receipt_timeout_seconds=receipt_timeout_seconds,
        )
    )
    return {
        "action": "swap_emitted",
        "pair": get_pair_snapshot(client, pair_id),
        "operations": operations,
    }


def resolve_rpc_url(args: argparse.Namespace, network: dict[str, Any] | None) -> str:
    if args.rpc_url is None and network is None:
        raise DexBootstrapError(
            f"localnet metadata not found at {args.network_path}; pass --rpc-url "
            "when bootstrapping a single local stack node"
        )
    return args.rpc_url or localnet_rpc_url(network)


def wants_demo_contracts(args: argparse.Namespace) -> bool:
    return args.seed_demo_pool or args.emit_test_swap


def deploy_demo_contracts(
    client: Xian,
    args: argparse.Namespace,
    source_bundle: dict[str, Any],
    deployer: str,
) -> list[dict[str, Any]]:
    """The demo token and its LP token, each deployed unless it exists."""
    demo_contracts = (
        (
            args.demo_token_contract,
            source_bundle["demo_token_source"],
            {
                "owner": deployer,
                "supply": args.demo_token_supply,
                "token_name": args.demo_token_name,
                "token_symbol": args.demo_token_symbol,
                "precision": args.demo_token_precision,
            },
        ),
        (
            args.demo_lp_contract,
            source_bundle["lp_token_source"],
            {
                "token_name": f"{args.demo_token_symbol}/XIAN LP",
                "token_symbol": f"XIAN-{args.demo_token_symbol}-LP",
                "operator_address": deployer,
                "minter_address": "con_pairs",
            },
        ),
    )
    return [
        submit_contract_if_missing(
            client,
            name=name,
            code=code,
            constructor_args=constructor_args,
            mode=args.submission_mode,
            receipt_timeout_seconds=args.receipt_timeout_seconds,
        )
        for name, code, constructor_args in demo_contracts
    ]


def bootstrap_report(
    args: argparse.Namespace,
    *,
    source_bundle: dict[str, Any],
    chain_id: str,
    rpc_url: str,
    deployer: str,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    demo = wants_demo_contracts(args)
    bundle_path = source_bundle["bundle_path"]
    contracts_dir = source_bundle["contracts_dir"]
    return {
        "ok": True,
        "chain_id": chain_id,
        "rpc_url": rpc_url,
        "deployer": deployer,
        "execution_mode": "xian_vm_v2",
        "dex_source": source_bundle["kind"],
        "dex_bundle": str(bundle_path) if bundle_path is not None else None,
        "dex_contracts_dir": str(contracts_dir) if contracts_dir is not None else None,
        "contracts": {
            "router": "con_dex",
            "pairs": "con_pairs",
            "helper": "con_dex_helper" if args.deploy_helper else None,
            "demo_token": args.demo_token_contract if demo else None,
            "demo_lp_token": args.demo_lp_contract if demo else None,
        },
        **outcome,
    }


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    validate_contract_name(args.demo_token_contract, label="demo token contract")
    validate_contract_name(args.demo_lp_contract, label="demo LP contract")

    network = load_network(args.network_path)
    rpc_url = resolve_rpc_url(args, network)
    wait_for_rpc_ready(rpc_url, timeout_seconds=args.rpc_timeout_seconds)

    source_bundle = load_dex_contract_sources(args)
    deployer_wallet = Wallet(private_key=resolve_deployer_private_key(args=args, network=network))
    deployer = deployer_wallet.public_key

    chain_id = args.chain_id or (None if args.rpc_url else (network or {}).get("chain_id"))
    with Xian(
        rpc_url,
        chain_id=chain_id,
        wallet=deployer_wallet,
        config=XianClientConfig(vm_profile="xian_vm_v2"),
    ) as client:
        client.refresh_nonce()
        deployments = [
            submit_contract_if_missing(
                client,
                name=name,
                code=code,
                constructor_args=None,
                mode=args.submission_mode,
                receipt_timeout_seconds=args.receipt_timeout_seconds,
            )
            for name, code in source_bundle["core_sources"].items()
        ]
        if wants_demo_contracts(args):
            deployments += deploy_demo_contracts(client, args, source_bundle, deployer)
        outcome = {
            "deployments": deployments,
            "demo_pool": seed_demo_pool_for(client, args) if args.seed_demo_pool else None,
            "test_swap": emit_test_swap_for(client, args) if args.emit_test_swap else None,
        }
        pair_id = get_pair_id(client, "currency", args.demo_token_contract)
        outcome["pair"] = get_pair_snapshot(client, pair_id) if pair_id is not None else None
        resolved_chain_id = client.chain_id

    return bootstrap_report(
        args,
        source_bundle=source_bundle,
        chain_id=resolved_chain_id,
        rpc_url=rpc_url,
        deployer=deployer,
        outcome=outcome,
    )


def seed_demo_pool_for(client: Xian, args: argparse.Namespace) -> dict[str, Any]:
    return seed_demo_pool(
        client,
        token_contract=args.demo_token_contract,
        lp_contract=args.demo_lp_contract,
        liquidity_currency_amount=args.liquidity_currency_amount,
        liquidity_demo_token_amount=args.liquidity_demo_token_amount,
        top_up_liquidity=args.top_up_liquidity,
        mode=args.submission_mode,
        receipt_timeout_seconds=args.receipt_timeout_seconds,
    )


def emit_test_swap_for(client: Xian, args: argparse.Namespace) -> dict[str, Any]:
    return emit_test_swap(
        client,
        token_contract=args.demo_token_contract,
        amount=args.test_swap_amount,
        mode=args.submission_mode,
        receipt_timeout_seconds=args.receipt_timeout_seconds,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap canonical DEX contracts on a running localnet."
    )
    parser.add_argument("--network-path", type=Path, default=NETWORK_PATH)
    parser.add_argument("--rpc-url", default=None)
    parser.add_argument("--chain-id", default=None)
    parser.add_argument(
        "--deployer-key-file",
        type=Path,
        default=_source_env_path("XIAN_DEX_DEPLOYER_KEY_FILE"),
        help=(
            "file holding the deployer's hex private key; otherwise "
            "XIAN_DEX_DEPLOYER_PRIVATE_KEY or the local stack keys are used"
        ),
    )
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
        "--dex-bundle",
        type=Path,
        default=Path(os.environ.get("XIAN_DEX_BUNDLE", DEFAULT_DEX_BUNDLE_PATH)),
        help=("hash-pinned DEX contract bundle; defaults to xian-dex/contract-bundle.json"),
    )
    parser.add_argument(
        "--dex-contracts-dir",
        type=Path,
        default=_source_env_path("XIAN_DEX_CONTRACTS_DIR"),
        help=(
            "development override for raw xian-dex src/ files; when omitted --dex-bundle is used"
        ),
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
    parser.add_argument("--demo-token-supply", type=Decimal, default=Decimal("1000000"))
    parser.add_argument("--demo-token-precision", type=int, default=8)
    parser.add_argument(
        "--liquidity-currency-amount",
        type=Decimal,
        default=Decimal("10000"),
    )
    parser.add_argument(
        "--liquidity-demo-token-amount",
        type=Decimal,
        default=Decimal("10000"),
    )
    parser.add_argument("--test-swap-amount", type=Decimal, default=Decimal("10"))
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
