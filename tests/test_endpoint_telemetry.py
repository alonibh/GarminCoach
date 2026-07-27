import threading

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from sync.endpoint_telemetry import instrument_read, telemetry_scope


def test_success_empty_failure_and_totals_are_accounted():
    with telemetry_scope("incremental") as collector:
        assert instrument_read("sleep", lambda: {"ok": True}) == {"ok": True}
        assert instrument_read("sleep", lambda: []) == []
        with pytest.raises(RuntimeError, match="original"):
            instrument_read("hrv", lambda: (_ for _ in ()).throw(RuntimeError("original")))
        with pytest.raises(GarminConnectAuthenticationError):
            instrument_read("hrv", lambda: (_ for _ in ()).throw(GarminConnectAuthenticationError("401")))
        with pytest.raises(GarminConnectTooManyRequestsError):
            instrument_read("stress", lambda: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("429")))
        payload = collector.finish()
    assert payload["total_calls"] == 5
    assert payload["endpoints"]["sleep"]["calls"] == 2
    assert payload["endpoints"]["sleep"]["successes"] == 2
    assert payload["endpoints"]["sleep"]["valid_empty"] == 1
    assert payload["endpoints"]["hrv"]["failures"] == 1
    assert payload["endpoints"]["hrv"]["authentication_failures"] == 1
    assert payload["endpoints"]["stress"]["rate_limits"] == 1


def test_scopes_are_isolated_between_threads():
    results = []
    def run(endpoint):
        with telemetry_scope("scheduled") as collector:
            instrument_read(endpoint, lambda: {"ok": True})
            results.append(collector.finish())
    threads = [threading.Thread(target=run, args=(name,)) for name in ("sleep", "hrv")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert {next(iter(result["endpoints"])) for result in results} == {"sleep", "hrv"}


def test_no_call_scope_is_compact_and_empty():
    with telemetry_scope("full") as collector:
        payload = collector.finish()
    assert payload["run_kind"] == "full"
    assert payload["total_calls"] == 0
    assert payload["endpoints"] == {}
