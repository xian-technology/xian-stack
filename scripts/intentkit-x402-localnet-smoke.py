#!/usr/bin/env python3
"""Exercise IntentKit's Xian-native x402 buyer path against a live localnet."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import web
from xian_py import (
    XianAsync,
    XianX402PaymentPayload,
    XianX402PaymentRequirement,
    encode_json_header,
)
from xian_py.wallet import Wallet
from xian_py.x402 import (
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    XianX402Facilitator,
    verify_xian_x402_payment,
    xian_network_id,
)

REQUIRED_CONFIG_FIELDS = (
    "rpc_url",
    "chain_id",
    "settlement_contract",
    "buyer_private_key",
    "seller_private_key",
    "facilitator_private_key",
    "run_id",
)


def normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): normalize(value[key]) for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, tuple):
        return [normalize(item) for item in value]
    if hasattr(value, "model_dump"):
        return normalize(value.model_dump())
    if hasattr(value, "to_dict"):
        return normalize(value.to_dict())
    return value


def normalize_submission(submission: Any) -> dict[str, Any] | None:
    if submission is None:
        return None
    receipt = getattr(submission, "receipt", None)
    execution = getattr(receipt, "execution", None) if receipt is not None else None
    events = []
    if isinstance(execution, dict):
        events = execution.get("events") or []
    return normalize(
        {
            "submitted": getattr(submission, "submitted", None),
            "accepted": getattr(submission, "accepted", None),
            "finalized": getattr(submission, "finalized", None),
            "success": getattr(receipt, "success", None) if receipt is not None else None,
            "message": getattr(receipt, "message", None)
            if receipt is not None
            else getattr(submission, "message", None),
            "tx_hash": getattr(submission, "tx_hash", None),
            "nonce": getattr(submission, "nonce", None),
            "events": events,
        }
    )


class X402SellerService:
    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: str,
        requirement: XianX402PaymentRequirement,
        facilitator: Wallet,
        settlement_chi: int,
    ) -> None:
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.requirement = requirement
        self.facilitator = facilitator
        self.settlement_chi = settlement_chi
        self.payment_payload: XianX402PaymentPayload | None = None
        self.settlement_submission: Any | None = None
        self.request_count = 0

    async def handle_paid_resource(self, request: web.Request) -> web.Response:
        self.request_count += 1
        payment_header = request.headers.get(PAYMENT_SIGNATURE_HEADER)
        if payment_header is None:
            return web.json_response(
                {"error": "payment required"},
                status=402,
                headers={
                    "PAYMENT-REQUIRED": self.requirement.to_payment_required_header(),
                },
            )

        payload = XianX402PaymentPayload.from_header(payment_header)
        verification = verify_xian_x402_payment(payload, self.requirement)
        if not verification.valid:
            return web.json_response(
                {"error": verification.error or "invalid payment"},
                status=400,
            )

        async with XianAsync(
            node_url=self.rpc_url,
            chain_id=self.chain_id,
            wallet=self.facilitator,
        ) as client:
            facilitator_api = XianX402Facilitator(
                client=client,
                requirement=self.requirement,
            )
            settlement = await facilitator_api.settle(
                payload,
                mode="checktx",
                wait_for_tx=True,
                chi=self.settlement_chi,
            )

        if not settlement.success or settlement.submission is None:
            return web.json_response(
                {"error": str(settlement.error or "settlement failed")},
                status=500,
            )

        self.payment_payload = payload
        self.settlement_submission = settlement.submission
        return web.Response(
            text="intentkit paid content",
            headers={
                PAYMENT_RESPONSE_HEADER: encode_json_header(
                    {
                        "success": True,
                        "network": payload.network,
                        "asset": payload.asset,
                        "amount": payload.amount,
                        "amountText": payload.amount,
                        "paymentId": payload.payment_id,
                        "payer": payload.payer,
                        "payTo": payload.pay_to,
                        "transaction": settlement.submission.tx_hash,
                    }
                )
            },
        )


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    os.environ.setdefault("REDIS_HOST", "localhost")

    from intentkit.skills.x402.pay import X402Pay

    buyer = Wallet(private_key=args.buyer_private_key)
    seller = Wallet(private_key=args.seller_private_key)
    facilitator = Wallet(private_key=args.facilitator_private_key)
    resource_path = f"/x402/{args.run_id}/data"
    service_ref: dict[str, X402SellerService] = {}

    async def handle_paid_resource(request: web.Request) -> web.Response:
        return await service_ref["service"].handle_paid_resource(request)

    app = web.Application()
    app.router.add_get(resource_path, handle_paid_resource)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.seller_port)

    created_orders: list[Any] = []

    async def capture_order(order: Any) -> Any:
        created_orders.append(order)
        return SimpleNamespace(
            **order.model_dump(),
            id="intentkit-x402-localnet-order",
            created_at=datetime.now(UTC),
        )

    context = SimpleNamespace(
        agent=SimpleNamespace(wallet_provider="xian", network_id="xian-localnet"),
        agent_id=args.agent_id,
        chat_id=args.chat_id,
        user_id=args.user_id,
    )
    skill = X402Pay()

    try:
        await site.start()
        sockets = getattr(site._server, "sockets", None)
        if not sockets:
            raise RuntimeError("Could not determine x402 seller port")
        actual_port = int(sockets[0].getsockname()[1])
        resource_url = f"http://127.0.0.1:{actual_port}{resource_path}"
        requirement = XianX402PaymentRequirement(
            network=xian_network_id(args.chain_id),
            asset=args.asset,
            amount=args.amount,
            pay_to=seller.public_key,
            resource=resource_url,
            settlement_contract=args.settlement_contract,
            description="IntentKit x402 live localnet e2e",
        )
        service = X402SellerService(
            rpc_url=args.rpc_url,
            chain_id=args.chain_id,
            requirement=requirement,
            facilitator=facilitator,
            settlement_chi=args.settlement_chi,
        )
        service_ref["service"] = service
        with (
            patch(
                "intentkit.skills.base.IntentKitSkill.get_context",
                return_value=context,
            ),
            patch.object(X402Pay, "get_signer", new=AsyncMock(return_value=buyer)),
            patch(
                "intentkit.skills.x402.base.X402Order.create",
                new=capture_order,
            ),
        ):
            skill_output = await skill._arun(
                method="GET",
                url=resource_url,
                max_value=args.max_value,
                timeout=args.timeout_seconds,
                idempotency_key=f"intentkit-x402-{args.run_id}",
            )
    finally:
        await runner.cleanup()

    service = service_ref["service"]
    if service.payment_payload is None:
        raise RuntimeError("IntentKit x402 request did not submit a payment payload")
    if service.settlement_submission is None:
        raise RuntimeError("IntentKit x402 request did not settle on-chain")
    if not created_orders:
        raise RuntimeError("IntentKit x402 request did not record an order")

    order = created_orders[0]
    return normalize(
        {
            "ok": True,
            "resource": resource_url,
            "request_count": service.request_count,
            "buyer": buyer.public_key,
            "seller": seller.public_key,
            "facilitator": facilitator.public_key,
            "requirement": requirement.to_payment_required(),
            "payment": service.payment_payload.to_dict(),
            "payment_id": service.payment_payload.payment_id,
            "settlement": normalize_submission(service.settlement_submission),
            "order": order.model_dump(),
            "skill_output": skill_output,
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an IntentKit Xian-native x402 live localnet smoke test.",
    )
    parser.add_argument(
        "--config",
        help="JSON config file. Prefer this for private keys so they do not appear in argv.",
    )
    parser.add_argument("--rpc-url")
    parser.add_argument("--chain-id")
    parser.add_argument("--settlement-contract")
    parser.add_argument("--buyer-private-key")
    parser.add_argument("--seller-private-key")
    parser.add_argument("--facilitator-private-key")
    parser.add_argument("--run-id")
    parser.add_argument("--asset", default="currency")
    parser.add_argument("--amount", default="0.001")
    parser.add_argument("--max-value", type=int, default=1)
    parser.add_argument("--settlement-chi", type=int, default=15_000)
    parser.add_argument("--seller-port", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--agent-id", default="localnet-intentkit-x402-agent")
    parser.add_argument("--chat-id", default="localnet-intentkit-x402-chat")
    parser.add_argument("--user-id", default="localnet-e2e")
    return parser


def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    values = vars(args).copy()
    config_path = values.get("config")
    if config_path:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise SystemExit("Smoke config must be a JSON object")
        for raw_key, value in config.items():
            key = str(raw_key).replace("-", "_")
            if key not in values:
                raise SystemExit(f"Unknown smoke config key: {raw_key}")
            if value is not None:
                values[key] = value

    missing = [
        f"--{key.replace('_', '-')}"
        for key in REQUIRED_CONFIG_FIELDS
        if values.get(key) in (None, "")
    ]
    if missing:
        raise SystemExit(
            "Missing required smoke options: "
            + ", ".join(missing)
            + ". Provide them as CLI args or in --config."
        )

    for key in (
        "agent_id",
        "amount",
        "asset",
        "buyer_private_key",
        "chain_id",
        "chat_id",
        "facilitator_private_key",
        "rpc_url",
        "run_id",
        "seller_private_key",
        "settlement_contract",
        "user_id",
    ):
        values[key] = str(values[key])
    for key in ("max_value", "seller_port", "settlement_chi"):
        values[key] = int(values[key])
    values["timeout_seconds"] = float(values["timeout_seconds"])
    return argparse.Namespace(**values)


def main(argv: list[str] | None = None) -> int:
    args = resolve_args(build_parser().parse_args(argv))
    payload = asyncio.run(run_smoke(args))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
