from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shielded_relayer_backend import shielded_relayer_endpoints
from shielded_relayer_service import (
    RelayerApiError,
    ShieldedRelayerAccessPolicy,
    ShieldedRelayerPolicy,
    ShieldedRelayerService,
    ShieldedRelayerServiceConfig,
    _validate_config,
    load_config_from_env,
)


@dataclass
class FakeSubmission:
    submitted: bool = True
    accepted: bool | None = True
    finalized: bool = False
    tx_hash: str | None = "ABC123"
    mode: str = "checktx"
    nonce: int = 7
    chi_supplied: int = 100
    chi_estimated: int | None = 80
    message: object = None
    response: dict | None = None
    receipt: object = None

    def __post_init__(self) -> None:
        if self.response is None:
            self.response = {"result": {"hash": self.tx_hash}}


class FakeWallet:
    public_key = "a" * 64


class FakeXianClient:
    def __init__(self) -> None:
        self.wallet = FakeWallet()
        self.chain_id = None
        self.calls: list[tuple[str, str, dict, dict[str, object]]] = []

    async def ensure_chain_id(self) -> None:
        self.chain_id = "xian-local"

    async def send_tx(
        self,
        contract: str,
        function_name: str,
        kwargs: dict,
        **extra: object,
    ) -> FakeSubmission:
        self.calls.append((contract, function_name, kwargs, extra))
        return FakeSubmission()

    async def close(self) -> None:
        return None


class ShieldedRelayerServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_applies_policy_minimums(self) -> None:
        service = ShieldedRelayerService(
            ShieldedRelayerServiceConfig(
                relayer_private_key="1" * 64,
                policy=ShieldedRelayerPolicy(
                    min_note_relayer_fee=25,
                    default_expiry_seconds=120,
                    max_expiry_seconds=300,
                ),
            ),
            xian_client=FakeXianClient(),
            now_fn=lambda: datetime(2026, 4, 10, 12, 0, 0),
        )

        quote = await service.quote(
            {
                "kind": "shielded_note_relay_transfer",
                "contract": "con_shielded_note_token",
                "requested_relayer_fee": 5,
            }
        )

        self.assertEqual("xian-local", quote["chain_id"])
        self.assertEqual("a" * 64, quote["relayer_account"])
        self.assertEqual(25, quote["relayer_fee"])
        self.assertEqual("2026-04-10 12:02:00", quote["expires_at"])

    async def test_submit_shielded_command_converts_expiry_to_contract_time(
        self,
    ) -> None:
        client = FakeXianClient()
        service = ShieldedRelayerService(
            ShieldedRelayerServiceConfig(
                relayer_private_key="1" * 64,
                policy=ShieldedRelayerPolicy(
                    min_command_relayer_fee=3,
                    allowed_command_contracts=("con_shielded_commands",),
                    allowed_command_targets=("currency",),
                ),
            ),
            xian_client=client,
            now_fn=lambda: datetime(2026, 4, 10, 12, 0, 0),
        )

        job = await service.submit_shielded_command(
            {
                "client_request_id": "job-1",
                "contract": "con_shielded_commands",
                "target_contract": "currency",
                "old_root": "0xold",
                "input_nullifiers": ["0xnullifier"],
                "output_commitments": [],
                "output_payloads": [],
                "proof_hex": "0xproof",
                "relayer_fee": 3,
                "public_amount": 0,
                "payload": {"action": "approve"},
                "expires_at": "2026-04-10 12:05:00",
            }
        )

        self.assertEqual("accepted", job["status"])
        self.assertEqual(1, len(client.calls))
        contract, function_name, kwargs, _extra = client.calls[0]
        self.assertEqual("con_shielded_commands", contract)
        self.assertEqual("execute_command", function_name)
        self.assertEqual(
            {"__time__": [2026, 4, 10, 12, 5, 0]},
            kwargs["expires_at"],
        )
        replay = await service.submit_shielded_command(
            {
                "client_request_id": "job-1",
                "contract": "con_shielded_commands",
                "target_contract": "currency",
                "old_root": "0xold",
                "input_nullifiers": ["0xnullifier"],
                "output_commitments": [],
                "output_payloads": [],
                "proof_hex": "0xproof",
                "relayer_fee": 3,
                "public_amount": 0,
                "payload": {"action": "approve"},
                "expires_at": "2026-04-10 12:05:00",
            }
        )
        self.assertEqual(job["job_id"], replay["job_id"])
        self.assertEqual(1, len(client.calls))

    async def test_submit_job_forwards_conservative_chi_budgeting(self) -> None:
        client = FakeXianClient()
        service = ShieldedRelayerService(
            ShieldedRelayerServiceConfig(
                relayer_private_key="1" * 64,
                chi_margin=1.0,
                min_chi_headroom=1500,
                policy=ShieldedRelayerPolicy(
                    min_note_relayer_fee=1,
                    allowed_note_contracts=("con_shielded_note_token",),
                ),
            ),
            xian_client=client,
            now_fn=lambda: datetime(2026, 4, 10, 12, 0, 0),
        )

        job = await service.submit_shielded_note_transfer(
            {
                "contract": "con_shielded_note_token",
                "old_root": "0xold",
                "input_nullifiers": ["0xnullifier"],
                "output_commitments": ["0xcommitment"],
                "output_payloads": ["0xpayload"],
                "proof_hex": "0xproof",
                "relayer_fee": 1,
            }
        )

        self.assertEqual("accepted", job["status"])
        self.assertEqual(1, len(client.calls))
        contract, function_name, kwargs, extra = client.calls[0]
        self.assertEqual("con_shielded_note_token", contract)
        self.assertEqual("relay_transfer_shielded", function_name)
        self.assertEqual(["0xnullifier"], kwargs["input_nullifiers"])
        self.assertEqual(1.0, extra["chi_margin"])
        self.assertEqual(5000, extra["min_chi_headroom"])

    async def test_info_surfaces_auth_and_operations_policy(self) -> None:
        service = ShieldedRelayerService(
            ShieldedRelayerServiceConfig(
                relayer_private_key="1" * 64,
                auth_token="secret-token",
                access_policy=ShieldedRelayerAccessPolicy(
                    public_info=True,
                    public_quote=False,
                    public_job_lookup=False,
                    metrics_public=False,
                ),
                log_requests=False,
                rate_limit_requests_per_minute=15,
                rate_limit_burst=2,
                job_history_ttl_seconds=45,
            ),
            xian_client=FakeXianClient(),
            now_fn=lambda: datetime(2026, 4, 10, 12, 0, 0),
        )

        info = await service.info()

        self.assertEqual("bearer", info["auth"]["scheme"])
        self.assertTrue(info["auth"]["public_info"])
        self.assertFalse(info["auth"]["public_quote"])
        self.assertFalse(info["auth"]["public_job_lookup"])
        self.assertEqual(
            15,
            info["operations"]["rate_limit_requests_per_minute"],
        )
        self.assertEqual(45, info["operations"]["job_history_ttl_seconds"])
        self.assertTrue(info["capabilities"]["metrics"])

    async def test_job_history_ttl_expires_old_jobs(self) -> None:
        current_time = datetime(2026, 4, 10, 12, 0, 0)
        client = FakeXianClient()

        def now_fn() -> datetime:
            return current_time

        service = ShieldedRelayerService(
            ShieldedRelayerServiceConfig(
                relayer_private_key="1" * 64,
                job_history_ttl_seconds=1,
                policy=ShieldedRelayerPolicy(
                    allowed_command_contracts=("con_shielded_commands",),
                    allowed_command_targets=("currency",),
                ),
            ),
            xian_client=client,
            now_fn=now_fn,
        )

        job = await service.submit_shielded_command(
            {
                "contract": "con_shielded_commands",
                "target_contract": "currency",
                "old_root": "0xold",
                "input_nullifiers": ["0xnullifier"],
                "output_commitments": [],
                "output_payloads": [],
                "proof_hex": "0xproof",
                "relayer_fee": 0,
                "public_amount": 0,
                "payload": {"action": "approve"},
            }
        )

        current_time = datetime(2026, 4, 10, 12, 0, 2)
        with self.assertRaises(RelayerApiError):
            service.get_job(job["job_id"])

    def test_rate_limit_enforces_burst_and_refills(self) -> None:
        current_time = datetime(2026, 4, 10, 12, 0, 0)

        def now_fn() -> datetime:
            return current_time

        service = ShieldedRelayerService(
            ShieldedRelayerServiceConfig(
                relayer_private_key="1" * 64,
                rate_limit_requests_per_minute=60,
                rate_limit_burst=2,
            ),
            xian_client=FakeXianClient(),
            now_fn=now_fn,
        )

        allowed_one, remaining_one, retry_one = service.enforce_rate_limit(
            "client-1"
        )
        allowed_two, remaining_two, retry_two = service.enforce_rate_limit(
            "client-1"
        )
        allowed_three, remaining_three, retry_three = service.enforce_rate_limit(
            "client-1"
        )

        self.assertTrue(allowed_one)
        self.assertEqual(1, remaining_one)
        self.assertIsNone(retry_one)
        self.assertTrue(allowed_two)
        self.assertEqual(0, remaining_two)
        self.assertIsNone(retry_two)
        self.assertFalse(allowed_three)
        self.assertEqual(0, remaining_three)
        self.assertEqual(2, retry_three)

        current_time = datetime(2026, 4, 10, 12, 0, 2)
        allowed_four, remaining_four, retry_four = service.enforce_rate_limit(
            "client-1"
        )
        self.assertTrue(allowed_four)
        self.assertEqual(1, remaining_four)
        self.assertIsNone(retry_four)

    def test_metrics_render_request_and_job_counters(self) -> None:
        service = ShieldedRelayerService(
            ShieldedRelayerServiceConfig(
                relayer_private_key="1" * 64,
            ),
            xian_client=FakeXianClient(),
            now_fn=lambda: datetime(2026, 4, 10, 12, 0, 0),
        )
        service.record_request(
            method="POST",
            path="/v1/quote",
            status=200,
            duration_seconds=0.125,
        )
        service.record_auth_failure()
        service.record_rate_limited()
        service._job_outcomes[("shielded_command", "accepted")] = 1

        metrics = service.render_metrics()

        self.assertIn("xian_shielded_relayer_requests_total", metrics)
        self.assertIn('path="/v1/quote"', metrics)
        self.assertIn("xian_shielded_relayer_auth_failures_total 1", metrics)
        self.assertIn("xian_shielded_relayer_rate_limited_total 1", metrics)
        self.assertIn("xian_shielded_relayer_job_outcomes_total", metrics)

    def test_backend_endpoints_include_relayer_routes(self) -> None:
        endpoints = shielded_relayer_endpoints(bind_host="0.0.0.0", port=38180)

        self.assertEqual("http://127.0.0.1:38180", endpoints["shielded_relayer"])
        self.assertEqual(
            "http://127.0.0.1:38180/v1/info",
            endpoints["shielded_relayer_info"],
        )
        self.assertEqual(
            "http://127.0.0.1:38180/metrics",
            endpoints["shielded_relayer_metrics"],
        )

    def test_non_loopback_bind_requires_auth_token(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "auth token is required",
        ):
            _validate_config(
                ShieldedRelayerServiceConfig(
                    bind_host="0.0.0.0",
                    relayer_private_key="1" * 64,
                )
            )

    def test_load_config_from_env_uses_conservative_default_chi_budgeting(self) -> None:
        with patch.dict(
            os.environ,
            {
                "XIAN_SHIELDED_RELAYER_PRIVATE_KEY": "1" * 64,
                "XIAN_SHIELDED_RELAYER_NODE_URL": "http://127.0.0.1:26657",
            },
            clear=False,
        ):
            config = load_config_from_env()

        self.assertEqual(1.0, config.chi_margin)
        self.assertEqual(1500, config.min_chi_headroom)


if __name__ == "__main__":
    unittest.main()
