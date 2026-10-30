"""``brain doctor``'s ``reranker`` row (RM-14b): is the local ``llama-server`` up?

The probe runs only for ``BRAIN_RERANKER=llamacpp``, as ``GET {BRAIN_RERANK_URL}/health`` with a
2 s timeout, through the ``transport`` seam (an ``httpx.MockTransport``), so no test opens a
socket. A reranker that is down is a WARN, never a FAIL: search skips it and stays in hybrid order.
"""
from __future__ import annotations

import httpx
import pytest

from brain.doctor_reranker import HEALTH_TIMEOUT_S, reranker_doctor_check

LLAMACPP = {"BRAIN_RERANKER": "llamacpp", "BRAIN_RERANK_URL": "http://127.0.0.1:8099"}


def _transport(*, status: int = 200) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"status": "ok"})

    return httpx.MockTransport(respond), seen


def _raising(exc: Exception) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(respond)


def test_none_is_not_configured_and_probes_nothing() -> None:
    transport, seen = _transport()

    (check,) = reranker_doctor_check(env={"BRAIN_RERANKER": "none"}, transport=transport)

    assert (check.check, check.status) == ("reranker", "ok")
    assert check.detail == "not configured"
    assert seen == []


def test_voyage_is_ok_and_probes_nothing() -> None:
    transport, seen = _transport()

    (check,) = reranker_doctor_check(
        env={"BRAIN_RERANKER": "voyage", "VOYAGE_API_KEY": "synthetic-key"}, transport=transport
    )

    assert check.status == "ok"
    assert "voyage" in check.detail
    assert "synthetic-key" not in check.detail
    assert seen == []


def test_llamacpp_healthy_is_ok_and_hits_health() -> None:
    transport, seen = _transport()

    (check,) = reranker_doctor_check(env=LLAMACPP, transport=transport)

    assert check.status == "ok"
    assert "http://127.0.0.1:8099" in check.detail
    assert [(r.method, str(r.url)) for r in seen] == [("GET", "http://127.0.0.1:8099/health")]


def test_the_probe_uses_a_two_second_timeout() -> None:
    timeouts: list[object] = []

    def respond(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(200)

    reranker_doctor_check(env=LLAMACPP, transport=httpx.MockTransport(respond))

    assert HEALTH_TIMEOUT_S == 2.0
    assert timeouts == [{"connect": 2.0, "read": 2.0, "write": 2.0, "pool": 2.0}]


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow")],
    ids=["refused", "connect-timeout", "read-timeout"],
)
def test_llamacpp_unreachable_is_not_running_and_warns(exc: Exception) -> None:
    (check,) = reranker_doctor_check(env=LLAMACPP, transport=_raising(exc))

    assert check.status == "warn"
    assert check.detail.startswith("not running")
    assert "http://127.0.0.1:8099" in check.detail
    assert check.remedy and "--with-reranker" in check.remedy


def test_llamacpp_loading_model_is_not_ready() -> None:
    transport, _seen = _transport(status=503)

    (check,) = reranker_doctor_check(env=LLAMACPP, transport=transport)

    assert check.status == "warn"
    assert check.detail.startswith("not ready (HTTP 503)")


def test_a_config_error_warns_with_the_message_never_the_value() -> None:
    (check,) = reranker_doctor_check(
        env={"BRAIN_RERANKER": "llamacpp", "BRAIN_RERANK_URL": "http://10.9.8.7:8081"},
        transport=_raising(AssertionError("must not probe")),
    )

    assert check.status == "warn"
    assert "BRAIN_RERANK_URL" in check.detail
    assert "10.9.8.7" not in check.detail


def test_runtime_checks_include_the_row(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from brain.doctor_runtime import runtime_doctor_checks

    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    checks = runtime_doctor_checks(launchd_dir=tmp_path, launchctl="launchctl", platform="linux")

    assert [c for c in checks if c.check == "reranker"][0].detail == "not configured"
