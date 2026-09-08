"""Fail-closed Anthropic proxy with one cumulative campaign budget."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Final, cast
from urllib.parse import SplitResult, urlsplit

from tools.controlled_spend.manifest import PilotManifest, canonical_json

_UPSTREAM_URL: Final = "https://api.anthropic.com"
_RUN_PATH = re.compile(r"^/run/(?P<run_id>r\d{3})(?P<path>/v1/messages)(?P<query>\?.*)?$")
_HOP_BY_HOP = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_MAX_REQUEST_BYTES = 16 * 1024 * 1024


class BudgetGatewayError(RuntimeError):
    """Base error for the controlled-spend gateway."""


class BudgetStoppedError(BudgetGatewayError):
    """Raised when the campaign has entered a terminal stop state."""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    cache_read_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Reservation:
    sequence: int
    run_id: str
    forwarded_body: bytes
    amount_usd: Decimal
    request_sha256: str


@dataclass(frozen=True, slots=True)
class GatewaySnapshot:
    spent_usd: Decimal
    reserved_usd: Decimal
    request_count: int
    halted_reason: str | None


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise BudgetGatewayError(f"{field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise BudgetGatewayError(f"{field} must be a decimal string") from error
    if not result.is_finite() or result <= 0:
        raise BudgetGatewayError(f"{field} must be finite and positive")
    return result


def _token_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BudgetGatewayError(f"{field} must be a non-negative integer")
    return value


def _usage_objects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[object] = [payload.get("usage")]
    message = payload.get("message")
    if isinstance(message, dict):
        candidates.append(message.get("usage"))
    return [cast(dict[str, Any], value) for value in candidates if isinstance(value, dict)]


def parse_anthropic_usage(body: bytes) -> Usage:
    """Extract billed token classes from a JSON or SSE Messages response."""
    payloads: list[dict[str, Any]] = []
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        for raw_line in body.splitlines():
            if not raw_line.startswith(b"data:"):
                continue
            data = raw_line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                event = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BudgetGatewayError("provider returned malformed SSE usage") from error
            if isinstance(event, dict):
                payloads.append(cast(dict[str, Any], event))
    else:
        if isinstance(decoded, dict):
            payloads.append(cast(dict[str, Any], decoded))

    usage_objects = [usage for payload in payloads for usage in _usage_objects(payload)]
    if not usage_objects:
        raise BudgetGatewayError("provider response did not contain usage")

    maxima = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    saw_input = False
    saw_output = False
    cache_5m = 0
    cache_1h = 0
    saw_cache_breakdown = False
    for index, usage in enumerate(usage_objects):
        for field in maxima:
            if field not in usage:
                continue
            token_count = _token_count(usage[field], f"usage[{index}].{field}")
            maxima[field] = max(maxima[field], token_count)
            saw_input = saw_input or field == "input_tokens"
            saw_output = saw_output or field == "output_tokens"
        creation = usage.get("cache_creation")
        if creation is not None:
            if not isinstance(creation, dict):
                raise BudgetGatewayError("usage.cache_creation must be an object")
            saw_cache_breakdown = True
            cache_5m = max(
                cache_5m,
                _token_count(
                    creation.get("ephemeral_5m_input_tokens", 0),
                    f"usage[{index}].cache_creation.ephemeral_5m_input_tokens",
                ),
            )
            cache_1h = max(
                cache_1h,
                _token_count(
                    creation.get("ephemeral_1h_input_tokens", 0),
                    f"usage[{index}].cache_creation.ephemeral_1h_input_tokens",
                ),
            )
    if not saw_input or not saw_output:
        raise BudgetGatewayError("provider response usage is incomplete")
    if not saw_cache_breakdown:
        cache_1h = maxima["cache_creation_input_tokens"]
    elif cache_5m + cache_1h != maxima["cache_creation_input_tokens"]:
        raise BudgetGatewayError("provider cache creation usage is inconsistent")
    return Usage(
        input_tokens=maxima["input_tokens"],
        cache_write_5m_tokens=cache_5m,
        cache_write_1h_tokens=cache_1h,
        cache_read_tokens=maxima["cache_read_input_tokens"],
        output_tokens=maxima["output_tokens"],
    )


class BudgetLedger:
    """Concurrency-safe reservation and actual-usage accounting."""

    def __init__(self, manifest: PilotManifest, receipt_path: Path) -> None:
        limits = cast(dict[str, Any], manifest.payload["limits"])
        catalog = cast(dict[str, Any], cast(dict[str, Any], manifest.payload["omp"])["catalog"])
        self._cap = _decimal(limits["total_spend_usd"], "limits.total_spend_usd")
        self._request_limit = cast(int, limits["provider_requests_per_run"])
        self._max_output_tokens = cast(
            int, cast(dict[str, Any], manifest.payload["omp"])["max_output_tokens"]
        )
        self._model = cast(str, cast(dict[str, Any], manifest.payload["omp"])["model"])
        self._rates = {
            "input": _decimal(catalog["input_per_mtok"], "catalog.input_per_mtok"),
            "cache_write_5m": _decimal(
                catalog["cache_write_5m_per_mtok"], "catalog.cache_write_5m_per_mtok"
            ),
            "cache_write_1h": _decimal(
                catalog["cache_write_1h_per_mtok"], "catalog.cache_write_1h_per_mtok"
            ),
            "cache_read": _decimal(catalog["cache_read_per_mtok"], "catalog.cache_read_per_mtok"),
            "output": _decimal(catalog["output_per_mtok"], "catalog.output_per_mtok"),
        }
        self._receipt_path = receipt_path
        self._lock = threading.Lock()
        self._spent = Decimal(0)
        self._reserved = Decimal(0)
        self._requests_by_run: dict[str, int] = {}
        self._next_sequence = 1
        self._halted_reason: str | None = None
        self._load_existing_receipts()

    def _load_existing_receipts(self) -> None:
        if not self._receipt_path.exists():
            self._receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._receipt_path.parent, 0o700)
            return
        if not self._receipt_path.is_file() or self._receipt_path.is_symlink():
            raise BudgetGatewayError("gateway receipt path must be an ordinary file")
        receipts: list[tuple[int, str, Decimal, str | None]] = []
        for line_number, raw_line in enumerate(
            self._receipt_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            try:
                payload = json.loads(raw_line)
                sequence = payload["sequence"]
                run_id = payload["run_id"]
                charged = Decimal(payload["charged_cost_usd"])
                stop_reason = payload["stop_reason"]
            except (KeyError, TypeError, InvalidOperation, json.JSONDecodeError) as error:
                raise BudgetGatewayError(
                    f"gateway receipt line {line_number} is malformed"
                ) from error
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
                or not isinstance(run_id, str)
                or not run_id
                or not charged.is_finite()
                or charged < 0
                or (stop_reason is not None and not isinstance(stop_reason, str))
            ):
                raise BudgetGatewayError(f"gateway receipt line {line_number} is invalid")
            receipts.append((sequence, run_id, charged, stop_reason))
        receipts.sort(key=lambda item: item[0])
        if [item[0] for item in receipts] != list(range(1, len(receipts) + 1)):
            raise BudgetGatewayError("gateway receipt sequences must be unique and contiguous")
        for sequence, run_id, charged, stop_reason in receipts:
            self._spent += charged
            self._requests_by_run[run_id] = self._requests_by_run.get(run_id, 0) + 1
            self._next_sequence = sequence + 1
            if stop_reason is not None:
                self._stop(stop_reason)
        if self._spent > self._cap:
            raise BudgetGatewayError("existing gateway receipts exceed the campaign cap")

    def snapshot(self) -> GatewaySnapshot:
        with self._lock:
            return GatewaySnapshot(
                spent_usd=self._spent,
                reserved_usd=self._reserved,
                request_count=self._next_sequence - 1,
                halted_reason=self._halted_reason,
            )

    def _stop(self, reason: str) -> None:
        if self._halted_reason is None:
            self._halted_reason = reason

    def reserve(self, run_id: str, incoming_body: bytes) -> Reservation:
        try:
            payload = json.loads(incoming_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            with self._lock:
                self._stop("task_or_configuration_drift")
            raise BudgetStoppedError("request body is not valid JSON") from error
        if not isinstance(payload, dict) or payload.get("model") != self._model:
            with self._lock:
                self._stop("task_or_configuration_drift")
            raise BudgetStoppedError("request model differs from the frozen pilot")
        requested_max = payload.get("max_tokens")
        if (
            isinstance(requested_max, bool)
            or not isinstance(requested_max, int)
            or requested_max < 1
        ):
            with self._lock:
                self._stop("task_or_configuration_drift")
            raise BudgetStoppedError("request max_tokens is invalid")
        payload["max_tokens"] = self._max_output_tokens
        forwarded_body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        input_upper_bound = len(forwarded_body)
        reservation_cost = (
            Decimal(input_upper_bound) * self._rates["cache_write_1h"]
            + Decimal(self._max_output_tokens) * self._rates["output"]
        ) / Decimal(1_000_000)

        with self._lock:
            if self._halted_reason is not None:
                raise BudgetStoppedError(f"campaign stopped: {self._halted_reason}")
            run_requests = self._requests_by_run.get(run_id, 0)
            if run_requests >= self._request_limit:
                self._stop("per_run_request_limit_reached")
                raise BudgetStoppedError("per-run provider request limit reached")
            if self._spent + self._reserved + reservation_cost > self._cap:
                self._stop("request_reservation_exceeds_total_cap")
                raise BudgetStoppedError("campaign spend reservation would exceed total cap")
            sequence = self._next_sequence
            self._next_sequence += 1
            self._requests_by_run[run_id] = run_requests + 1
            self._reserved += reservation_cost
        return Reservation(
            sequence=sequence,
            run_id=run_id,
            forwarded_body=forwarded_body,
            amount_usd=reservation_cost,
            request_sha256=hashlib.sha256(forwarded_body).hexdigest(),
        )

    def _cost(self, usage: Usage) -> Decimal:
        return (
            Decimal(usage.input_tokens) * self._rates["input"]
            + Decimal(usage.cache_write_5m_tokens) * self._rates["cache_write_5m"]
            + Decimal(usage.cache_write_1h_tokens) * self._rates["cache_write_1h"]
            + Decimal(usage.cache_read_tokens) * self._rates["cache_read"]
            + Decimal(usage.output_tokens) * self._rates["output"]
        ) / Decimal(1_000_000)

    def finish(
        self,
        reservation: Reservation,
        *,
        status: int,
        response_body: bytes,
        failure_reason: str | None = None,
    ) -> None:
        usage: Usage | None = None
        usage_valid = False
        charged = reservation.amount_usd
        reason = failure_reason
        if reason is None and 200 <= status < 300:
            try:
                usage = parse_anthropic_usage(response_body)
                charged = self._cost(usage)
                usage_valid = True
            except BudgetGatewayError:
                reason = "provider_usage_missing_or_malformed"
        elif reason is None:
            reason = "provider_usage_missing_or_malformed"

        with self._lock:
            self._reserved -= reservation.amount_usd
            self._spent += charged
            if reason is not None or charged > reservation.amount_usd or self._spent > self._cap:
                self._stop(reason or "provider_usage_missing_or_malformed")
            payload = {
                "charged_cost_usd": format(charged, "f"),
                "request_sha256": reservation.request_sha256,
                "reserved_cost_usd": format(reservation.amount_usd, "f"),
                "run_id": reservation.run_id,
                "sequence": reservation.sequence,
                "status": status,
                "stop_reason": reason,
                "usage": None
                if usage is None
                else {
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_1h_tokens": usage.cache_write_1h_tokens,
                    "cache_write_5m_tokens": usage.cache_write_5m_tokens,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
                "usage_valid": usage_valid,
            }
            descriptor = os.open(
                self._receipt_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, canonical_json(payload))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], ledger: BudgetLedger, upstream: SplitResult):
        super().__init__(address, _GatewayHandler)
        self.ledger = ledger
        self.upstream = upstream


class _GatewayHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _ = (format, args)

    def _error(self, status: int, message: str) -> None:
        body = canonical_json({"error": {"message": message, "type": "gateway_error"}})
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        server = cast(_GatewayServer, self.server)
        match = _RUN_PATH.fullmatch(self.path)
        if match is None:
            self._error(404, "unknown gateway route")
            return
        length_header = self.headers.get("content-length")
        try:
            length = int(length_header or "")
        except ValueError:
            self._error(411, "content-length is required")
            return
        if not 0 < length <= _MAX_REQUEST_BYTES:
            self._error(413, "request body size is invalid")
            return
        incoming_body = self.rfile.read(length)
        try:
            reservation = server.ledger.reserve(match.group("run_id"), incoming_body)
        except BudgetStoppedError as error:
            self._error(429, str(error))
            return

        upstream_path = match.group("path") + (match.group("query") or "")
        headers = {
            key: value for key, value in self.headers.items() if key.lower() not in _HOP_BY_HOP
        }
        headers["content-length"] = str(len(reservation.forwarded_body))
        connection_class = (
            http.client.HTTPSConnection
            if server.upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            cast(str, server.upstream.hostname),
            server.upstream.port,
            timeout=180,
        )
        try:
            connection.request(
                "POST",
                upstream_path,
                body=reservation.forwarded_body,
                headers=headers,
            )
            response = connection.getresponse()
            response_body = response.read()
            response_headers = [
                (key, value)
                for key, value in response.getheaders()
                if key.lower() not in _HOP_BY_HOP
            ]
        except (OSError, http.client.HTTPException) as error:
            server.ledger.finish(
                reservation,
                status=502,
                response_body=b"",
                failure_reason="provider_usage_missing_or_malformed",
            )
            self._error(502, f"upstream transport failed: {type(error).__name__}")
            return
        finally:
            connection.close()

        server.ledger.finish(
            reservation,
            status=response.status,
            response_body=response_body,
        )
        self.send_response(response.status, response.reason)
        for key, value in response_headers:
            self.send_header(key, value)
        self.send_header("content-length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


class BudgetGateway(AbstractContextManager["BudgetGateway"]):
    """Own one loopback gateway thread for a controlled campaign."""

    def __init__(
        self,
        manifest: PilotManifest,
        receipt_path: Path,
        *,
        upstream_url: str = _UPSTREAM_URL,
    ) -> None:
        upstream = urlsplit(upstream_url)
        if upstream.scheme not in {"http", "https"} or not upstream.hostname:
            raise BudgetGatewayError("upstream URL must be absolute HTTP(S)")
        if upstream.path not in {"", "/"} or upstream.query or upstream.fragment:
            raise BudgetGatewayError("upstream URL must not contain a path, query, or fragment")
        self.ledger = BudgetLedger(manifest, receipt_path)
        self._server = _GatewayServer(("127.0.0.1", 0), self.ledger, upstream)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="laconic-controlled-spend-gateway",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return cast(int, self._server.server_port)

    def base_url(self, run_id: str) -> str:
        if not re.fullmatch(r"r\d{3}", run_id):
            raise BudgetGatewayError("run_id is invalid")
        return f"http://127.0.0.1:{self.port}/run/{run_id}"

    def __enter__(self) -> BudgetGateway:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
