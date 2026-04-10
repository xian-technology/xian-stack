#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal

from aiohttp import web

from xian_py import (
    SubmissionConfig,
    Wallet,
    XianAsync,
    XianClientConfig,
    to_contract_time,
)

_SERVICE_NAME = "xian-shielded-relayer"
_PROTOCOL_VERSION = "v1"
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_ALWAYS_PUBLIC_PATHS = frozenset(("/health",))
_RATE_LIMITED_PATH_PREFIXES = frozenset(
    (
        "/v1/quote",
        "/v1/jobs/",
    )
)


class RelayerApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _env_csv(name: str) -> tuple[str, ...]:
    value = os.environ.get(name)
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _utc_now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None, microsecond=0)


def _format_contract_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime(_TIMESTAMP_FORMAT)


def _parse_contract_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return datetime.strptime(stripped, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise RelayerApiError(
            400,
            f"expires_at must use {_TIMESTAMP_FORMAT!r}",
        ) from exc


def _require_string(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RelayerApiError(400, f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RelayerApiError(400, f"{key} must be a string")
    stripped = value.strip()
    return stripped or None


def _require_int(
    body: dict[str, Any],
    key: str,
    *,
    minimum: int | None = None,
) -> int:
    value = body.get(key)
    if isinstance(value, bool):
        raise RelayerApiError(400, f"{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RelayerApiError(400, f"{key} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise RelayerApiError(400, f"{key} must be >= {minimum}")
    return parsed


def _optional_int(body: dict[str, Any], key: str) -> int | None:
    if key not in body or body.get(key) is None:
        return None
    return _require_int(body, key)


def _require_string_list(
    body: dict[str, Any],
    key: str,
    *,
    minimum_length: int = 0,
) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list):
        raise RelayerApiError(400, f"{key} must be a list of strings")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RelayerApiError(400, f"{key} must be a list of strings")
        items.append(item.strip())
    if len(items) < minimum_length:
        raise RelayerApiError(
            400,
            f"{key} must contain at least {minimum_length} item(s)",
        )
    return items


def _job_status(submission: Any) -> str:
    if not getattr(submission, "submitted", False):
        return "failed"
    if getattr(submission, "accepted", None) is False:
        return "failed"
    receipt = getattr(submission, "receipt", None)
    if receipt is not None and getattr(receipt, "success", None) is False:
        return "failed"
    if getattr(submission, "finalized", False):
        return "finalized"
    if getattr(submission, "accepted", None) is True:
        return "accepted"
    return "submitted"


def _submission_error(submission: Any) -> str | None:
    message = getattr(submission, "message", None)
    if message not in {None, ""}:
        return str(message)
    receipt = getattr(submission, "receipt", None)
    if receipt is not None and getattr(receipt, "message", None) not in {
        None,
        "",
    }:
        return str(receipt.message)
    return None


def _request_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _is_loopback_host(value: str) -> bool:
    host = value.strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _metric_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


@dataclass(frozen=True)
class ShieldedRelayerAccessPolicy:
    public_info: bool = True
    public_quote: bool = False
    public_job_lookup: bool = False
    metrics_public: bool = False

    def as_dict(self, *, auth_scheme: str) -> dict[str, Any]:
        all_public = auth_scheme == "none"
        return {
            "scheme": auth_scheme,
            "public_info": self.public_info or all_public,
            "public_quote": self.public_quote or all_public,
            "public_job_lookup": self.public_job_lookup or all_public,
            "metrics_public": self.metrics_public or all_public,
        }


@dataclass
class _RateLimitBucket:
    tokens: float
    updated_at: datetime


@dataclass(frozen=True)
class ShieldedRelayerPolicy:
    quote_ttl_seconds: int = 30
    default_expiry_seconds: int = 300
    max_expiry_seconds: int = 1800
    min_note_relayer_fee: int = 0
    min_command_relayer_fee: int = 0
    allowed_note_contracts: tuple[str, ...] = ()
    allowed_command_contracts: tuple[str, ...] = ()
    allowed_command_targets: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "quote_ttl_seconds": self.quote_ttl_seconds,
            "default_expiry_seconds": self.default_expiry_seconds,
            "max_expiry_seconds": self.max_expiry_seconds,
            "min_note_relayer_fee": self.min_note_relayer_fee,
            "min_command_relayer_fee": self.min_command_relayer_fee,
            "allowed_note_contracts": list(self.allowed_note_contracts),
            "allowed_command_contracts": list(self.allowed_command_contracts),
            "allowed_command_targets": list(self.allowed_command_targets),
        }


@dataclass(frozen=True)
class ShieldedRelayerServiceConfig:
    bind_host: str = "127.0.0.1"
    port: int = 38180
    node_url: str = "http://127.0.0.1:26657"
    relayer_private_key: str = ""
    auth_token: str | None = None
    access_policy: ShieldedRelayerAccessPolicy = field(
        default_factory=ShieldedRelayerAccessPolicy
    )
    submission_mode: Literal["async", "checktx", "commit"] = "checktx"
    wait_for_tx: bool = True
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.25
    chi_margin: float = 0.10
    min_chi_headroom: int = 10
    metrics_enabled: bool = True
    log_requests: bool = True
    rate_limit_requests_per_minute: int = 120
    rate_limit_burst: int = 30
    rate_limit_trust_proxy: bool = False
    job_history_limit: int = 256
    job_history_ttl_seconds: int = 86400
    policy: ShieldedRelayerPolicy = field(default_factory=ShieldedRelayerPolicy)


class ShieldedRelayerService:
    def __init__(
        self,
        config: ShieldedRelayerServiceConfig,
        *,
        xian_client: XianAsync | None = None,
        now_fn: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config
        self._now = now_fn
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._job_updated_at: dict[str, datetime] = {}
        self._client_request_index: dict[str, tuple[str, str]] = {}
        self._rate_limit_buckets: dict[str, _RateLimitBucket] = {}
        self._request_counts: dict[tuple[str, str, int], int] = {}
        self._request_duration: dict[tuple[str, str], dict[str, float]] = {}
        self._job_outcomes: dict[tuple[str, str], int] = {}
        self._auth_failures_total = 0
        self._rate_limited_total = 0
        self._chain_id_lock = asyncio.Lock()
        self._owns_client = xian_client is None
        if xian_client is None:
            wallet = Wallet(private_key=config.relayer_private_key)
            submission = SubmissionConfig(
                mode=config.submission_mode,
                wait_for_tx=config.wait_for_tx,
                timeout_seconds=config.timeout_seconds,
                poll_interval_seconds=config.poll_interval_seconds,
                chi_margin=config.chi_margin,
                min_chi_headroom=config.min_chi_headroom,
            )
            xian_client = XianAsync(
                config.node_url,
                wallet=wallet,
                config=XianClientConfig(submission=submission),
            )
        self._xian = xian_client
        self._policy_version = hashlib.sha256(
            json.dumps(
                {
                    "node_url": config.node_url,
                    "access_policy": config.access_policy.as_dict(
                        auth_scheme=self.auth_scheme
                    ),
                    "submission_mode": config.submission_mode,
                    "wait_for_tx": config.wait_for_tx,
                    "policy": config.policy.as_dict(),
                    "relayer_account": getattr(
                        getattr(self._xian, "wallet", None),
                        "public_key",
                        None,
                    ),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]

    @property
    def auth_scheme(self) -> str:
        return "bearer" if self.config.auth_token else "none"

    @property
    def relayer_account(self) -> str | None:
        wallet = getattr(self._xian, "wallet", None)
        public_key = getattr(wallet, "public_key", None)
        return public_key if isinstance(public_key, str) else None

    def is_public_request(self, request: web.Request) -> bool:
        path = request.path
        if path in _ALWAYS_PUBLIC_PATHS:
            return True
        if self.auth_scheme == "none":
            return True
        if path == "/v1/info":
            return self.config.access_policy.public_info
        if path == "/v1/quote":
            return self.config.access_policy.public_quote
        if path == "/metrics":
            return self.config.metrics_enabled and self.config.access_policy.metrics_public
        if path.startswith("/v1/jobs/") and request.method == "GET":
            return self.config.access_policy.public_job_lookup
        return False

    def should_rate_limit(self, request: web.Request) -> bool:
        if self.config.rate_limit_requests_per_minute <= 0:
            return False
        if request.method == "GET" and request.path in {"/health", "/v1/info"}:
            return False
        if request.path == "/metrics":
            return False
        return any(
            request.path == prefix.rstrip("/")
            or request.path.startswith(prefix)
            for prefix in _RATE_LIMITED_PATH_PREFIXES
        )

    def request_client_id(self, request: web.Request) -> str:
        if self.config.rate_limit_trust_proxy:
            forwarded_for = request.headers.get("X-Forwarded-For", "")
            forwarded_value = forwarded_for.split(",", 1)[0].strip()
            if forwarded_value:
                return forwarded_value
        return (request.remote or "unknown").strip() or "unknown"

    def enforce_rate_limit(self, client_id: str) -> tuple[bool, int, int | None]:
        rate_per_minute = self.config.rate_limit_requests_per_minute
        burst = max(self.config.rate_limit_burst, 1)
        if rate_per_minute <= 0:
            return True, burst, None

        now = self._now()
        bucket = self._rate_limit_buckets.get(client_id)
        if bucket is None:
            tokens = float(burst)
        else:
            elapsed_seconds = max(
                0.0,
                (now - bucket.updated_at).total_seconds(),
            )
            tokens = min(
                float(burst),
                bucket.tokens + elapsed_seconds * (rate_per_minute / 60.0),
            )
        if tokens < 1.0:
            retry_after_seconds = max(
                1,
                int((1.0 - max(tokens, 0.0)) / (rate_per_minute / 60.0)) + 1,
            )
            self._rate_limit_buckets[client_id] = _RateLimitBucket(
                tokens=tokens,
                updated_at=now,
            )
            return False, 0, retry_after_seconds

        tokens -= 1.0
        self._rate_limit_buckets[client_id] = _RateLimitBucket(
            tokens=tokens,
            updated_at=now,
        )
        return True, int(tokens), None

    def record_request(
        self,
        *,
        method: str,
        path: str,
        status: int,
        duration_seconds: float,
    ) -> None:
        request_key = (method, path, status)
        self._request_counts[request_key] = (
            self._request_counts.get(request_key, 0) + 1
        )
        duration_key = (method, path)
        duration = self._request_duration.setdefault(
            duration_key,
            {"count": 0.0, "sum": 0.0},
        )
        duration["count"] += 1.0
        duration["sum"] += max(duration_seconds, 0.0)

    def record_auth_failure(self) -> None:
        self._auth_failures_total += 1

    def record_rate_limited(self) -> None:
        self._rate_limited_total += 1

    def render_metrics(self) -> str:
        self._expire_jobs()
        lines = [
            "# HELP xian_shielded_relayer_requests_total Total HTTP requests handled by the relayer.",
            "# TYPE xian_shielded_relayer_requests_total counter",
        ]
        for (method, path, status), value in sorted(self._request_counts.items()):
            labels = (
                f'method="{_metric_escape(method)}",'
                f'path="{_metric_escape(path)}",'
                f'status="{status}"'
            )
            lines.append(
                f"xian_shielded_relayer_requests_total{{{labels}}} {value}"
            )

        lines.extend(
            [
                "# HELP xian_shielded_relayer_request_duration_seconds_count Total counted request observations.",
                "# TYPE xian_shielded_relayer_request_duration_seconds_count counter",
            ]
        )
        for (method, path), value in sorted(self._request_duration.items()):
            labels = (
                f'method="{_metric_escape(method)}",'
                f'path="{_metric_escape(path)}"'
            )
            lines.append(
                "xian_shielded_relayer_request_duration_seconds_count"
                f"{{{labels}}} {int(value['count'])}"
            )
        lines.extend(
            [
                "# HELP xian_shielded_relayer_request_duration_seconds_sum Total request duration in seconds.",
                "# TYPE xian_shielded_relayer_request_duration_seconds_sum counter",
            ]
        )
        for (method, path), value in sorted(self._request_duration.items()):
            labels = (
                f'method="{_metric_escape(method)}",'
                f'path="{_metric_escape(path)}"'
            )
            lines.append(
                "xian_shielded_relayer_request_duration_seconds_sum"
                f"{{{labels}}} {value['sum']:.6f}"
            )

        lines.extend(
            [
                "# HELP xian_shielded_relayer_auth_failures_total Total failed authentication attempts.",
                "# TYPE xian_shielded_relayer_auth_failures_total counter",
                f"xian_shielded_relayer_auth_failures_total {self._auth_failures_total}",
                "# HELP xian_shielded_relayer_rate_limited_total Total requests rejected by relayer rate limits.",
                "# TYPE xian_shielded_relayer_rate_limited_total counter",
                f"xian_shielded_relayer_rate_limited_total {self._rate_limited_total}",
                "# HELP xian_shielded_relayer_job_history_size Current number of retained relayer jobs.",
                "# TYPE xian_shielded_relayer_job_history_size gauge",
                f"xian_shielded_relayer_job_history_size {len(self._jobs)}",
                "# HELP xian_shielded_relayer_rate_limit_bucket_count Current number of active rate-limit buckets.",
                "# TYPE xian_shielded_relayer_rate_limit_bucket_count gauge",
                f"xian_shielded_relayer_rate_limit_bucket_count {len(self._rate_limit_buckets)}",
            ]
        )

        current_jobs: dict[tuple[str, str], int] = {}
        for job in self._jobs.values():
            kind = str(job.get("kind") or "unknown")
            status = str(job.get("status") or "unknown")
            current_jobs[(kind, status)] = current_jobs.get((kind, status), 0) + 1

        lines.extend(
            [
                "# HELP xian_shielded_relayer_current_jobs Current retained jobs by kind and status.",
                "# TYPE xian_shielded_relayer_current_jobs gauge",
            ]
        )
        for (kind, status), value in sorted(current_jobs.items()):
            labels = (
                f'kind="{_metric_escape(kind)}",'
                f'status="{_metric_escape(status)}"'
            )
            lines.append(f"xian_shielded_relayer_current_jobs{{{labels}}} {value}")

        lines.extend(
            [
                "# HELP xian_shielded_relayer_job_outcomes_total Total submitted job outcomes by kind and status.",
                "# TYPE xian_shielded_relayer_job_outcomes_total counter",
            ]
        )
        for (kind, status), value in sorted(self._job_outcomes.items()):
            labels = (
                f'kind="{_metric_escape(kind)}",'
                f'status="{_metric_escape(status)}"'
            )
            lines.append(
                f"xian_shielded_relayer_job_outcomes_total{{{labels}}} {value}"
            )
        return "\n".join(lines) + "\n"

    async def close(self) -> None:
        if self._owns_client:
            await self._xian.close()

    async def ensure_chain_id(self) -> str:
        async with self._chain_id_lock:
            await self._xian.ensure_chain_id()
            chain_id = getattr(self._xian, "chain_id", None)
            if not isinstance(chain_id, str) or not chain_id:
                raise RuntimeError("relayer client did not resolve a chain id")
            return chain_id

    async def health(self) -> dict[str, Any]:
        try:
            chain_id = await self.ensure_chain_id()
            available = True
            error = None
        except Exception as exc:
            chain_id = None
            available = False
            error = str(exc)
        return {
            "ok": available,
            "service": _SERVICE_NAME,
            "protocol_version": _PROTOCOL_VERSION,
            "available": available,
            "chain_id": chain_id,
            "relayer_account": self.relayer_account,
            "auth_required": self.auth_scheme != "none",
            "auth_scheme": self.auth_scheme,
            "metrics_enabled": self.config.metrics_enabled,
            "error": error,
        }

    async def info(self) -> dict[str, Any]:
        health = await self.health()
        return {
            "service": _SERVICE_NAME,
            "protocol_version": _PROTOCOL_VERSION,
            "available": health["available"],
            "chain_id": health["chain_id"],
            "relayer_account": health["relayer_account"],
            "submission_mode": self.config.submission_mode,
            "wait_for_tx": self.config.wait_for_tx,
            "auth": self.config.access_policy.as_dict(
                auth_scheme=self.auth_scheme
            ),
            "capabilities": {
                "quote": True,
                "shielded_note_relay_transfer": True,
                "shielded_command": True,
                "job_lookup": True,
                "idempotent_client_request_ids": True,
                "metrics": self.config.metrics_enabled,
            },
            "operations": {
                "request_logging_enabled": self.config.log_requests,
                "metrics_enabled": self.config.metrics_enabled,
                "job_history_limit": self.config.job_history_limit,
                "job_history_ttl_seconds": self.config.job_history_ttl_seconds,
                "rate_limit_requests_per_minute": (
                    self.config.rate_limit_requests_per_minute
                ),
                "rate_limit_burst": self.config.rate_limit_burst,
                "rate_limit_trust_proxy": self.config.rate_limit_trust_proxy,
            },
            "policy": self.config.policy.as_dict(),
            "error": health["error"],
        }

    def _validate_allowlist(
        self,
        *,
        value: str,
        configured: tuple[str, ...],
        label: str,
    ) -> None:
        if configured and value not in configured:
            raise RelayerApiError(
                400,
                f"{label} is not allowed by this relayer policy",
            )

    def _resolve_quote_expiry(
        self,
        requested_seconds: int | None,
    ) -> datetime | None:
        policy = self.config.policy
        if requested_seconds is None:
            requested_seconds = policy.default_expiry_seconds
        if requested_seconds < 0:
            raise RelayerApiError(
                400,
                "requested_expires_in_seconds must be >= 0",
            )
        if (
            policy.max_expiry_seconds > 0
            and requested_seconds > policy.max_expiry_seconds
        ):
            raise RelayerApiError(
                400,
                "requested_expires_in_seconds exceeds relayer policy",
            )
        if requested_seconds == 0:
            return None
        return self._now() + timedelta(seconds=requested_seconds)

    async def quote(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = _require_string(body, "kind")
        contract = _require_string(body, "contract")
        target_contract = _optional_string(body, "target_contract")
        requested_fee = _optional_int(body, "requested_relayer_fee")
        requested_expires_in = _optional_int(
            body,
            "requested_expires_in_seconds",
        )
        chain_id = await self.ensure_chain_id()

        if kind == "shielded_note_relay_transfer":
            self._validate_allowlist(
                value=contract,
                configured=self.config.policy.allowed_note_contracts,
                label="contract",
            )
            min_fee = self.config.policy.min_note_relayer_fee
        elif kind == "shielded_command":
            self._validate_allowlist(
                value=contract,
                configured=self.config.policy.allowed_command_contracts,
                label="contract",
            )
            if target_contract is None:
                raise RelayerApiError(
                    400,
                    "target_contract is required for shielded_command quotes",
                )
            self._validate_allowlist(
                value=target_contract,
                configured=self.config.policy.allowed_command_targets,
                label="target_contract",
            )
            min_fee = self.config.policy.min_command_relayer_fee
        else:
            raise RelayerApiError(
                400,
                "unsupported shielded relayer quote kind",
            )

        if requested_fee is not None and requested_fee < 0:
            raise RelayerApiError(
                400,
                "requested_relayer_fee must be >= 0",
            )
        relayer_fee = max(min_fee, requested_fee or 0)
        issued_at = self._now()
        expires_at = self._resolve_quote_expiry(requested_expires_in)
        return {
            "kind": kind,
            "contract": contract,
            "target_contract": target_contract,
            "chain_id": chain_id,
            "relayer_account": self.relayer_account,
            "relayer_fee": relayer_fee,
            "issued_at": _format_contract_timestamp(issued_at),
            "expires_at": _format_contract_timestamp(expires_at),
            "policy_version": self._policy_version,
            "quote_ttl_seconds": self.config.policy.quote_ttl_seconds,
        }

    def _evict_job(self, job_id: str, job: dict[str, Any] | None = None) -> None:
        if job is None:
            job = self._jobs.pop(job_id, None)
        else:
            self._jobs.pop(job_id, None)
        self._job_updated_at.pop(job_id, None)
        if isinstance(job, dict):
            client_request_id = job.get("client_request_id")
            if client_request_id:
                self._client_request_index.pop(client_request_id, None)

    def _expire_jobs(self) -> None:
        ttl_seconds = self.config.job_history_ttl_seconds
        if ttl_seconds <= 0:
            return
        cutoff = self._now() - timedelta(seconds=ttl_seconds)
        expired_ids = [
            job_id
            for job_id, updated_at in self._job_updated_at.items()
            if updated_at <= cutoff
        ]
        for job_id in expired_ids:
            self._evict_job(job_id)

    def _remember_job(self, job: dict[str, Any]) -> None:
        self._expire_jobs()
        self._jobs[job["job_id"]] = job
        self._jobs.move_to_end(job["job_id"])
        self._job_updated_at[job["job_id"]] = self._now()
        while len(self._jobs) > self.config.job_history_limit:
            expired_job_id, expired_job = self._jobs.popitem(last=False)
            self._evict_job(expired_job_id, expired_job)

    def get_job(self, job_id: str) -> dict[str, Any]:
        self._expire_jobs()
        job = self._jobs.get(job_id)
        if job is None:
            raise RelayerApiError(404, f"unknown job_id: {job_id}")
        return dict(job)

    def _prepare_idempotent_job(
        self,
        *,
        request_body: dict[str, Any],
        kind: str,
        contract: str,
        function_name: str,
    ) -> dict[str, Any]:
        client_request_id = _optional_string(request_body, "client_request_id")
        fingerprint_body = {
            key: value
            for key, value in request_body.items()
            if key != "client_request_id"
        }
        request_fingerprint = _request_fingerprint(fingerprint_body)
        if client_request_id is not None:
            existing = self._client_request_index.get(client_request_id)
            if existing is not None:
                existing_fingerprint, existing_job_id = existing
                if existing_fingerprint != request_fingerprint:
                    raise RelayerApiError(
                        409,
                        "client_request_id has already been used with a different payload",
                    )
                return self.get_job(existing_job_id)

        timestamp = _format_contract_timestamp(self._now())
        job = {
            "job_id": uuid.uuid4().hex,
            "kind": kind,
            "status": "pending",
            "chain_id": getattr(self._xian, "chain_id", None),
            "relayer_account": self.relayer_account,
            "contract": contract,
            "function_name": function_name,
            "tx_hash": None,
            "submitted_at": timestamp,
            "updated_at": timestamp,
            "error": None,
            "submission": None,
            "client_request_id": client_request_id,
        }
        self._remember_job(job)
        if client_request_id is not None:
            self._client_request_index[client_request_id] = (
                request_fingerprint,
                job["job_id"],
            )
        return job

    async def _submit_job(
        self,
        *,
        request_body: dict[str, Any],
        kind: str,
        contract: str,
        function_name: str,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        job = self._prepare_idempotent_job(
            request_body=request_body,
            kind=kind,
            contract=contract,
            function_name=function_name,
        )
        if job["status"] != "pending":
            return job

        try:
            chain_id = await self.ensure_chain_id()
            submission = await self._xian.send_tx(
                contract,
                function_name,
                kwargs,
                mode=self.config.submission_mode,
                wait_for_tx=self.config.wait_for_tx,
                timeout_seconds=self.config.timeout_seconds,
                poll_interval_seconds=self.config.poll_interval_seconds,
                chi_margin=self.config.chi_margin,
                min_chi_headroom=self.config.min_chi_headroom,
            )
            job["chain_id"] = chain_id
            job["submission"] = asdict(submission)
            job["tx_hash"] = submission.tx_hash
            job["status"] = _job_status(submission)
            self._job_outcomes[(kind, job["status"])] = (
                self._job_outcomes.get((kind, job["status"]), 0) + 1
            )
            if job["status"] == "failed":
                job["error"] = _submission_error(submission)
        except Exception as exc:
            job["chain_id"] = getattr(self._xian, "chain_id", None)
            job["status"] = "failed"
            job["error"] = str(exc)
            self._job_outcomes[(kind, "failed")] = (
                self._job_outcomes.get((kind, "failed"), 0) + 1
            )
        finally:
            job["updated_at"] = _format_contract_timestamp(self._now())
            self._remember_job(job)
        return dict(job)

    def _validate_common_submission(
        self,
        body: dict[str, Any],
        *,
        minimum_output_commitments: int,
    ) -> dict[str, Any]:
        contract = _require_string(body, "contract")
        old_root = _require_string(body, "old_root")
        input_nullifiers = _require_string_list(
            body,
            "input_nullifiers",
            minimum_length=1,
        )
        output_commitments = _require_string_list(
            body,
            "output_commitments",
            minimum_length=minimum_output_commitments,
        )
        proof_hex = _require_string(body, "proof_hex")
        relayer_fee = _require_int(body, "relayer_fee", minimum=0)
        output_payloads = _require_string_list(
            body,
            "output_payloads",
            minimum_length=0,
        )
        if len(output_payloads) != len(output_commitments):
            raise RelayerApiError(
                400,
                "output_payloads length must match output_commitments length",
            )
        expires_at = _parse_contract_timestamp(
            _optional_string(body, "expires_at")
        )
        return {
            "contract": contract,
            "old_root": old_root,
            "input_nullifiers": input_nullifiers,
            "output_commitments": output_commitments,
            "proof_hex": proof_hex,
            "relayer_fee": relayer_fee,
            "output_payloads": output_payloads,
            "expires_at": expires_at,
        }

    async def submit_shielded_note_transfer(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        validated = self._validate_common_submission(
            body,
            minimum_output_commitments=1,
        )
        self._validate_allowlist(
            value=validated["contract"],
            configured=self.config.policy.allowed_note_contracts,
            label="contract",
        )
        if validated["relayer_fee"] < self.config.policy.min_note_relayer_fee:
            raise RelayerApiError(
                400,
                "relayer_fee is below the minimum note relay fee",
            )
        kwargs = {
            "old_root": validated["old_root"],
            "input_nullifiers": validated["input_nullifiers"],
            "output_commitments": validated["output_commitments"],
            "proof_hex": validated["proof_hex"],
            "relayer_fee": validated["relayer_fee"],
            "expires_at": (
                to_contract_time(validated["expires_at"])
                if validated["expires_at"] is not None
                else None
            ),
            "output_payloads": validated["output_payloads"],
        }
        return await self._submit_job(
            request_body=body,
            kind="shielded_note_relay_transfer",
            contract=validated["contract"],
            function_name="relay_transfer_shielded",
            kwargs=kwargs,
        )

    async def submit_shielded_command(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        validated = self._validate_common_submission(
            body,
            minimum_output_commitments=0,
        )
        target_contract = _require_string(body, "target_contract")
        if "public_amount" in body and body.get("public_amount") is not None:
            public_amount = _require_int(body, "public_amount", minimum=0)
        else:
            public_amount = 0
        payload = body.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise RelayerApiError(400, "payload must be a JSON object")
        self._validate_allowlist(
            value=validated["contract"],
            configured=self.config.policy.allowed_command_contracts,
            label="contract",
        )
        self._validate_allowlist(
            value=target_contract,
            configured=self.config.policy.allowed_command_targets,
            label="target_contract",
        )
        if (
            validated["relayer_fee"]
            < self.config.policy.min_command_relayer_fee
        ):
            raise RelayerApiError(
                400,
                "relayer_fee is below the minimum command relay fee",
            )
        kwargs = {
            "target_contract": target_contract,
            "old_root": validated["old_root"],
            "input_nullifiers": validated["input_nullifiers"],
            "output_commitments": validated["output_commitments"],
            "proof_hex": validated["proof_hex"],
            "relayer_fee": validated["relayer_fee"],
            "public_amount": public_amount,
            "payload": payload,
            "expires_at": (
                to_contract_time(validated["expires_at"])
                if validated["expires_at"] is not None
                else None
            ),
            "output_payloads": validated["output_payloads"],
        }
        return await self._submit_job(
            request_body=body,
            kind="shielded_command",
            contract=validated["contract"],
            function_name="execute_command",
            kwargs=kwargs,
        )


def _request_metric_path(request: web.Request) -> str:
    route = getattr(request.match_info, "route", None)
    resource = getattr(route, "resource", None)
    canonical = getattr(resource, "canonical", None)
    if isinstance(canonical, str) and canonical:
        return canonical
    return request.path


@web.middleware
async def _error_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Any],
) -> web.StreamResponse:
    started_at = time.monotonic()
    path = _request_metric_path(request)
    service = request.app.get("service")
    response: web.StreamResponse
    try:
        response = await handler(request)
    except RelayerApiError as exc:
        response = web.json_response({"error": exc.message}, status=exc.status)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.reason}, status=exc.status)
    except json.JSONDecodeError:
        response = web.json_response(
            {"error": "request body must be valid JSON"},
            status=400,
        )
    except Exception:
        logging.exception("shielded relayer request failed")
        response = web.json_response(
            {"error": "internal server error"},
            status=500,
        )

    if isinstance(service, ShieldedRelayerService):
        duration_seconds = time.monotonic() - started_at
        service.record_request(
            method=request.method,
            path=path,
            status=response.status,
            duration_seconds=duration_seconds,
        )
        if service.config.log_requests:
            logging.info(
                "shielded-relayer method=%s path=%s status=%s duration_ms=%s",
                request.method,
                path,
                response.status,
                int(duration_seconds * 1000),
            )
    return response


def _auth_middleware(service: ShieldedRelayerService):
    @web.middleware
    async def middleware(
        request: web.Request,
        handler: Callable[[web.Request], Any],
    ) -> web.StreamResponse:
        if service.is_public_request(request):
            return await handler(request)
        expected = f"Bearer {service.config.auth_token}"
        if request.headers.get("Authorization") != expected:
            service.record_auth_failure()
            raise RelayerApiError(401, "missing or invalid bearer token")
        return await handler(request)

    return middleware


def _rate_limit_middleware(service: ShieldedRelayerService):
    @web.middleware
    async def middleware(
        request: web.Request,
        handler: Callable[[web.Request], Any],
    ) -> web.StreamResponse:
        if not service.should_rate_limit(request):
            return await handler(request)
        allowed, remaining, retry_after_seconds = service.enforce_rate_limit(
            service.request_client_id(request)
        )
        if not allowed:
            service.record_rate_limited()
            return web.json_response(
                {"error": "request rate limit exceeded"},
                status=429,
                headers={"Retry-After": str(retry_after_seconds or 1)},
            )
        response = await handler(request)
        if isinstance(response, web.StreamResponse):
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    return middleware


def _build_app(service: ShieldedRelayerService) -> web.Application:
    app = web.Application(
        middlewares=[
            _error_middleware,
            _auth_middleware(service),
            _rate_limit_middleware(service),
        ]
    )
    app["service"] = service

    async def service_context(_: web.Application):
        yield
        await service.close()

    app.cleanup_ctx.append(service_context)

    async def health(_: web.Request) -> web.Response:
        return web.json_response(await service.health())

    async def info(_: web.Request) -> web.Response:
        return web.json_response(await service.info())

    async def metrics(_: web.Request) -> web.Response:
        return web.Response(
            text=service.render_metrics(),
            headers={
                "Content-Type": "text/plain; version=0.0.4; charset=utf-8"
            },
        )

    async def quote(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict):
            raise RelayerApiError(400, "request body must be a JSON object")
        return web.json_response(await service.quote(body))

    async def submit_note(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict):
            raise RelayerApiError(400, "request body must be a JSON object")
        return web.json_response(
            await service.submit_shielded_note_transfer(body)
        )

    async def submit_command(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict):
            raise RelayerApiError(400, "request body must be a JSON object")
        return web.json_response(await service.submit_shielded_command(body))

    async def get_job(request: web.Request) -> web.Response:
        return web.json_response(service.get_job(request.match_info["job_id"]))

    app.router.add_get("/health", health)
    app.router.add_get("/v1/info", info)
    if service.config.metrics_enabled:
        app.router.add_get("/metrics", metrics)
    app.router.add_post("/v1/quote", quote)
    app.router.add_post("/v1/jobs/shielded-note-transfer", submit_note)
    app.router.add_post("/v1/jobs/shielded-command", submit_command)
    app.router.add_get("/v1/jobs/{job_id}", get_job)
    return app


def _load_private_key() -> str:
    private_key = (
        os.environ.get("XIAN_SHIELDED_RELAYER_PRIVATE_KEY") or ""
    ).strip()
    if private_key:
        return private_key
    key_file = (
        os.environ.get("XIAN_SHIELDED_RELAYER_PRIVATE_KEY_FILE") or ""
    ).strip()
    if not key_file:
        raise RuntimeError(
            "set XIAN_SHIELDED_RELAYER_PRIVATE_KEY or "
            "XIAN_SHIELDED_RELAYER_PRIVATE_KEY_FILE"
        )
    with open(key_file, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def _validate_config(config: ShieldedRelayerServiceConfig) -> None:
    if config.port <= 0:
        raise RuntimeError("shielded relayer port must be > 0")
    if not _is_loopback_host(config.bind_host) and not config.auth_token:
        raise RuntimeError(
            "shielded relayer auth token is required for non-loopback binds"
        )
    if config.rate_limit_requests_per_minute < 0:
        raise RuntimeError(
            "shielded relayer rate_limit_requests_per_minute must be >= 0"
        )
    if config.rate_limit_burst <= 0:
        raise RuntimeError("shielded relayer rate_limit_burst must be > 0")
    if config.job_history_limit <= 0:
        raise RuntimeError("shielded relayer job history limit must be > 0")
    if config.job_history_ttl_seconds < 0:
        raise RuntimeError(
            "shielded relayer job_history_ttl_seconds must be >= 0"
        )
    if config.policy.default_expiry_seconds < 0:
        raise RuntimeError("default expiry seconds must be >= 0")
    if config.policy.max_expiry_seconds < 0:
        raise RuntimeError("max expiry seconds must be >= 0")
    if (
        config.policy.max_expiry_seconds > 0
        and config.policy.default_expiry_seconds
        > config.policy.max_expiry_seconds
    ):
        raise RuntimeError(
            "default expiry seconds must not exceed max expiry seconds"
        )
    if config.policy.min_note_relayer_fee < 0:
        raise RuntimeError("minimum note relayer fee must be >= 0")
    if config.policy.min_command_relayer_fee < 0:
        raise RuntimeError("minimum command relayer fee must be >= 0")
    if config.submission_mode not in {"async", "checktx", "commit"}:
        raise RuntimeError(
            "submission mode must be one of async, checktx, commit"
        )


def load_config_from_env() -> ShieldedRelayerServiceConfig:
    config = ShieldedRelayerServiceConfig(
        bind_host=os.environ.get("XIAN_SHIELDED_RELAYER_HOST", "127.0.0.1"),
        port=_env_int("XIAN_SHIELDED_RELAYER_PORT", 38180),
        node_url=os.environ.get(
            "XIAN_SHIELDED_RELAYER_NODE_URL",
            "http://127.0.0.1:26657",
        ).rstrip("/"),
        relayer_private_key=_load_private_key(),
        auth_token=(
            os.environ.get("XIAN_SHIELDED_RELAYER_AUTH_TOKEN") or ""
        ).strip()
        or None,
        access_policy=ShieldedRelayerAccessPolicy(
            public_info=_env_bool(
                "XIAN_SHIELDED_RELAYER_PUBLIC_INFO",
                True,
            ),
            public_quote=_env_bool(
                "XIAN_SHIELDED_RELAYER_PUBLIC_QUOTE",
                False,
            ),
            public_job_lookup=_env_bool(
                "XIAN_SHIELDED_RELAYER_PUBLIC_JOB_LOOKUP",
                False,
            ),
            metrics_public=_env_bool(
                "XIAN_SHIELDED_RELAYER_METRICS_PUBLIC",
                False,
            ),
        ),
        submission_mode=os.environ.get(
            "XIAN_SHIELDED_RELAYER_SUBMISSION_MODE",
            "checktx",
        )
        .strip()
        .lower(),
        wait_for_tx=_env_bool("XIAN_SHIELDED_RELAYER_WAIT_FOR_TX", True),
        metrics_enabled=_env_bool(
            "XIAN_SHIELDED_RELAYER_METRICS_ENABLED",
            True,
        ),
        log_requests=_env_bool("XIAN_SHIELDED_RELAYER_LOG_REQUESTS", True),
        timeout_seconds=_env_float(
            "XIAN_SHIELDED_RELAYER_TIMEOUT_SECONDS",
            30.0,
        ),
        poll_interval_seconds=_env_float(
            "XIAN_SHIELDED_RELAYER_POLL_INTERVAL_SECONDS",
            0.25,
        ),
        chi_margin=_env_float(
            "XIAN_SHIELDED_RELAYER_CHI_MARGIN",
            0.10,
        ),
        min_chi_headroom=_env_int(
            "XIAN_SHIELDED_RELAYER_MIN_CHI_HEADROOM",
            10,
        ),
        rate_limit_requests_per_minute=_env_int(
            "XIAN_SHIELDED_RELAYER_RATE_LIMIT_REQUESTS_PER_MINUTE",
            120,
        ),
        rate_limit_burst=_env_int(
            "XIAN_SHIELDED_RELAYER_RATE_LIMIT_BURST",
            30,
        ),
        rate_limit_trust_proxy=_env_bool(
            "XIAN_SHIELDED_RELAYER_RATE_LIMIT_TRUST_PROXY",
            False,
        ),
        job_history_limit=_env_int(
            "XIAN_SHIELDED_RELAYER_JOB_HISTORY_LIMIT",
            256,
        ),
        job_history_ttl_seconds=_env_int(
            "XIAN_SHIELDED_RELAYER_JOB_HISTORY_TTL_SECONDS",
            86400,
        ),
        policy=ShieldedRelayerPolicy(
            quote_ttl_seconds=_env_int(
                "XIAN_SHIELDED_RELAYER_QUOTE_TTL_SECONDS",
                30,
            ),
            default_expiry_seconds=_env_int(
                "XIAN_SHIELDED_RELAYER_DEFAULT_EXPIRY_SECONDS",
                300,
            ),
            max_expiry_seconds=_env_int(
                "XIAN_SHIELDED_RELAYER_MAX_EXPIRY_SECONDS",
                1800,
            ),
            min_note_relayer_fee=_env_int(
                "XIAN_SHIELDED_RELAYER_MIN_NOTE_RELAYER_FEE",
                0,
            ),
            min_command_relayer_fee=_env_int(
                "XIAN_SHIELDED_RELAYER_MIN_COMMAND_RELAYER_FEE",
                0,
            ),
            allowed_note_contracts=_env_csv(
                "XIAN_SHIELDED_RELAYER_ALLOWED_NOTE_CONTRACTS"
            ),
            allowed_command_contracts=_env_csv(
                "XIAN_SHIELDED_RELAYER_ALLOWED_COMMAND_CONTRACTS"
            ),
            allowed_command_targets=_env_csv(
                "XIAN_SHIELDED_RELAYER_ALLOWED_COMMAND_TARGETS"
            ),
        ),
    )
    _validate_config(config)
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Xian shielded relayer")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=("serve",),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("XIAN_SHIELDED_RELAYER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config_from_env()
    service = ShieldedRelayerService(config)
    app = _build_app(service)
    web.run_app(
        app,
        host=config.bind_host,
        port=config.port,
        access_log=None,
        print=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
