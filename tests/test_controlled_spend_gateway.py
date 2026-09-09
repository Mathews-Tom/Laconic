from __future__ import annotations

import http.client
import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from tools.controlled_spend.budget_gateway import (
    BudgetGateway,
    BudgetLedger,
    BudgetStoppedError,
    parse_anthropic_usage,
)
from tools.controlled_spend.manifest import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_V2_MANIFEST_PATH,
    validate_manifest_file,
)


def _request_body(*, padding: str = "") -> bytes:
    return json.dumps(
        {
            "max_tokens": 128_000,
            "messages": [{"content": f"hello{padding}", "role": "user"}],
            "model": "claude-sonnet-5",
        }
    ).encode()


def _response_body() -> bytes:
    return json.dumps(
        {
            "content": [{"text": "ok", "type": "text"}],
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "type": "message",
            "usage": {
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 3,
                    "ephemeral_5m_input_tokens": 2,
                },
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
                "input_tokens": 11,
                "output_tokens": 13,
            },
        }
    ).encode()


def test_sse_usage_combines_message_start_and_delta() -> None:
    body = b"\n".join(
        [
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
            b'{"input_tokens":11,"cache_creation_input_tokens":5,'
            b'"cache_read_input_tokens":7,"output_tokens":0,'
            b'"cache_creation":{"ephemeral_5m_input_tokens":2,'
            b'"ephemeral_1h_input_tokens":3}}}}',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":13}}',
        ]
    )

    usage = parse_anthropic_usage(body)

    assert usage.input_tokens == 11
    assert usage.cache_write_5m_tokens == 2
    assert usage.cache_write_1h_tokens == 3
    assert usage.cache_read_tokens == 7
    assert usage.output_tokens == 13


def test_total_cap_is_reserved_before_a_second_large_request(tmp_path: Path) -> None:
    ledger = BudgetLedger(
        validate_manifest_file(DEFAULT_MANIFEST_PATH), tmp_path / "receipts.jsonl"
    )
    first = ledger.reserve("r001", _request_body(padding="x" * 1_500_000))

    with pytest.raises(BudgetStoppedError, match="exceed total cap"):
        ledger.reserve("r002", _request_body(padding="x" * 1_500_000))

    assert first.amount_usd < Decimal("10")
    assert ledger.snapshot().halted_reason == "request_reservation_exceeds_total_cap"


def test_per_run_request_limit_stops_before_ninth_request(tmp_path: Path) -> None:
    ledger = BudgetLedger(
        validate_manifest_file(DEFAULT_MANIFEST_PATH), tmp_path / "receipts.jsonl"
    )
    reservations = [ledger.reserve("r001", _request_body()) for _ in range(8)]

    with pytest.raises(BudgetStoppedError, match="per-run provider request limit"):
        ledger.reserve("r001", _request_body())

    assert len(reservations) == 8
    assert ledger.snapshot().request_count == 8
    assert ledger.snapshot().halted_reason == "per_run_request_limit_reached"


def test_v2_per_run_request_limit_stops_before_seventeenth_request(
    tmp_path: Path,
) -> None:
    ledger = BudgetLedger(
        validate_manifest_file(DEFAULT_V2_MANIFEST_PATH),
        tmp_path / "receipts.jsonl",
    )
    reservations = [ledger.reserve("r001", _request_body()) for _ in range(16)]

    with pytest.raises(BudgetStoppedError, match="per-run provider request limit"):
        ledger.reserve("r001", _request_body())

    assert len(reservations) == 16
    assert ledger.snapshot().request_count == 16
    assert ledger.snapshot().halted_reason == "per_run_request_limit_reached"


def test_normalized_request_cannot_exceed_reservation_size_ceiling(
    tmp_path: Path,
) -> None:
    payload = {
        "max_tokens": 1,
        "messages": [{"content": "", "role": "user"}],
        "model": "claude-sonnet-5",
    }
    base = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["messages"][0]["content"] = "x" * (16 * 1024 * 1024 - len(base))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert len(body) == 16 * 1024 * 1024
    ledger = BudgetLedger(
        validate_manifest_file(DEFAULT_V2_MANIFEST_PATH),
        tmp_path / "receipts.jsonl",
    )

    with pytest.raises(BudgetStoppedError, match="normalized request body"):
        ledger.reserve("r001", body)

    assert ledger.snapshot().request_count == 0
    assert ledger.snapshot().halted_reason == "task_or_configuration_drift"


def test_receipt_reload_accepts_out_of_order_concurrent_completions(tmp_path: Path) -> None:
    manifest = validate_manifest_file(DEFAULT_MANIFEST_PATH)
    receipt_path = tmp_path / "receipts.jsonl"
    ledger = BudgetLedger(manifest, receipt_path)
    first = ledger.reserve("r001", _request_body())
    second = ledger.reserve("r002", _request_body())
    ledger.finish(second, status=200, response_body=_response_body())
    ledger.finish(first, status=200, response_body=_response_body())

    reloaded = BudgetLedger(manifest, receipt_path).snapshot()

    assert reloaded.request_count == 2
    assert reloaded.spent_usd == Decimal("0.0003408")
    assert reloaded.halted_reason is None


class _FakeUpstream(ThreadingHTTPServer):
    request_path: str | None = None
    request_json: dict[str, Any] | None = None


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        _ = (format, args)

    def do_POST(self) -> None:  # noqa: N802
        server = cast(_FakeUpstream, self.server)
        length = int(self.headers["content-length"])
        server.request_path = self.path
        server.request_json = cast(dict[str, Any], json.loads(self.rfile.read(length)))
        response = _response_body()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def test_gateway_rewrites_output_cap_and_records_content_free_usage(tmp_path: Path) -> None:
    upstream = _FakeUpstream(("127.0.0.1", 0), _FakeUpstreamHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_port}"
        receipt_path = tmp_path / "receipts.jsonl"
        with BudgetGateway(
            validate_manifest_file(DEFAULT_MANIFEST_PATH), receipt_path, upstream_url=upstream_url
        ) as gateway:
            connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=5)
            connection.request(
                "POST",
                "/run/r001/v1/messages?beta=true",
                body=_request_body(),
                headers={"content-type": "application/json"},
            )
            response = connection.getresponse()
            body = response.read()
            connection.close()
            snapshot = gateway.ledger.snapshot()
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert body == _response_body()
    assert upstream.request_path == "/v1/messages?beta=true"
    assert upstream.request_json is not None
    assert upstream.request_json["max_tokens"] == 4096
    assert snapshot.halted_reason is None
    assert snapshot.spent_usd == Decimal("0.0001704")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == {
        "charged_cost_usd",
        "request_sha256",
        "reserved_cost_usd",
        "run_id",
        "sequence",
        "status",
        "stop_reason",
        "usage",
        "usage_valid",
    }
    assert "messages" not in receipt
