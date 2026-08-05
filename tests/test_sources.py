"""Source adapters.

No test in this file touches the network. The HTTP adapters take an ``opener``
callable, which the tests replace with a function returning canned bytes, so
the normalisation logic is exercised end to end without a printer or a server.

The API key assertions are the point of several of these tests. A key that
leaks into a log line, a traceback or a repr is a key that ends up pasted into
a bug report.
"""

from __future__ import annotations

import json
import socket
import urllib.error

import pytest

from spool.models import length_to_mass_g
from spool.sources import (
    DEFAULT_TIMEOUT,
    FixtureSource,
    JobSource,
    MoonrakerSource,
    OctoPrintSource,
    SourceError,
    _redact,
    http_get_json,
    resolve_grams,
)

#: A recognisable secret. Every assertion below looks for this exact string.
API_KEY = "SUPERSECRET-abc123-do-not-leak"

BASE = "http://printer.invalid:7125"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def make_opener(body, *, record: list | None = None):
    """Build a stand-in for urllib.request.urlopen.

    ``body`` may be bytes, a string, an object to JSON encode, or an exception
    instance to raise.
    """

    def opener(request, timeout=None):
        if record is not None:
            record.append((request, timeout))
        if isinstance(body, BaseException):
            raise body
        if isinstance(body, bytes):
            return _FakeResponse(body)
        if isinstance(body, str):
            return _FakeResponse(body.encode("utf-8"))
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    return opener


MOONRAKER_PAYLOAD = {
    "result": {
        "count": 3,
        "jobs": [
            {
                "job_id": "000001",
                "filename": "prints/benchy.gcode",
                "status": "completed",
                "start_time": 1767283200.0,
                "end_time": 1767286923.0,
                "print_duration": 3723.0,
                "total_duration": 3800.0,
                "filament_used": 4321.0,
                "metadata": {
                    "filament_total": 4321.0,
                    "filament_weight_total": 12.88,
                    "estimated_time": 3700,
                },
            },
            {
                "job_id": "000002",
                "filename": "prints/bracket.gcode",
                "status": "cancelled",
                "start_time": 1767369600.0,
                "print_duration": 600.0,
                "filament_used": 500.0,
                "metadata": {},
            },
            {
                "job_id": "000003",
                "filename": "prints/tall-tower.gcode",
                "status": "klippy_shutdown",
                "start_time": 1767456000.0,
                "print_duration": 4000.0,
                "filament_used": 8000.0,
                "metadata": {
                    "filament_weight_total": 40.0,
                    "estimated_time": 10000,
                },
            },
        ],
    }
}

OCTOPRINT_PAYLOAD = {
    "history": [
        {
            "id": 11,
            "fileName": "uploads/benchy.gcode",
            "success": 1,
            "printTime": 3723,
            "filamentLength": 4321.0,
            "filamentVolume": 10.39,
            "timestamp": 1767283200,
            "printerProfile": "Prusa MK4",
        },
        {
            "id": 12,
            "fileName": "uploads/failed.gcode",
            "success": 0,
            "printTime": 900,
            "filamentLength": 1000.0,
            "timestamp": 1767369600,
            "printerProfile": "Prusa MK4",
        },
    ]
}


class TestRedactor:
    def test_a_secret_in_text_is_blanked(self):
        assert API_KEY not in _redact("token=%s failed" % API_KEY, API_KEY)
        assert "***" in _redact("token=%s failed" % API_KEY, API_KEY)

    def test_text_without_the_secret_is_untouched(self):
        assert _redact("all fine", API_KEY) == "all fine"

    def test_no_secret_is_a_no_op(self):
        assert _redact("all fine", None) == "all fine"

    def test_a_trivially_short_secret_is_not_used_for_matching(self):
        # Blanking every "a" in a message would destroy it and reveal nothing.
        assert _redact("a message", "a") == "a message"


class TestUrlValidation:
    def test_an_empty_base_url_is_rejected(self):
        with pytest.raises(SourceError, match="base_url is required"):
            MoonrakerSource("")

    def test_a_non_http_scheme_is_rejected(self):
        # Otherwise a config string turns into an arbitrary file reader.
        with pytest.raises(SourceError, match="http"):
            MoonrakerSource("file:///etc/passwd")

    def test_a_missing_host_is_rejected(self):
        with pytest.raises(SourceError):
            MoonrakerSource("http://")

    def test_a_trailing_slash_is_trimmed(self):
        assert MoonrakerSource(BASE + "/").base_url == BASE


class TestApiKeySafety:
    def test_the_key_is_absent_from_the_repr(self):
        source = MoonrakerSource(BASE, API_KEY)
        assert API_KEY not in repr(source)
        assert API_KEY not in str(source)
        assert BASE in repr(source)

    def test_the_key_is_absent_from_the_octoprint_repr(self):
        source = OctoPrintSource(BASE, API_KEY)
        assert API_KEY not in repr(source)
        assert API_KEY not in str(source)

    def test_the_key_is_absent_from_a_network_error(self):
        source = MoonrakerSource(
            BASE, API_KEY, opener=make_opener(urllib.error.URLError("connection refused"))
        )
        with pytest.raises(SourceError) as caught:
            source.list_jobs()
        assert API_KEY not in str(caught.value)
        assert API_KEY not in repr(caught.value)

    def test_the_key_is_absent_from_a_json_error(self):
        source = OctoPrintSource(BASE, API_KEY, opener=make_opener(b"not json at all"))
        with pytest.raises(SourceError) as caught:
            source.list_jobs()
        assert API_KEY not in str(caught.value)

    def test_the_key_is_absent_from_an_http_status_error(self):
        error = urllib.error.HTTPError(BASE, 403, "Forbidden", {}, None)
        source = OctoPrintSource(BASE, API_KEY, opener=make_opener(error))
        with pytest.raises(SourceError) as caught:
            source.list_jobs()
        assert API_KEY not in str(caught.value)
        assert "403" in str(caught.value)

    def test_a_key_that_did_reach_the_url_is_redacted_anyway(self):
        # Defence in depth: spool sends keys as headers, but if a future change
        # ever put one in a URL, the error path still must not print it.
        url = "%s/server/history/list?token=%s" % (BASE, API_KEY)
        opener = make_opener(urllib.error.URLError("nope"))
        with pytest.raises(SourceError) as caught:
            http_get_json(url, opener=opener, secret=API_KEY)
        assert API_KEY not in str(caught.value)
        assert "***" in str(caught.value)

    def test_the_key_is_sent_as_a_header(self):
        record: list = []
        source = MoonrakerSource(BASE, API_KEY, opener=make_opener(MOONRAKER_PAYLOAD, record=record))
        source.list_jobs()
        request, _ = record[0]
        assert API_KEY in request.headers.values()
        assert API_KEY not in request.full_url

    def test_no_header_is_sent_when_there_is_no_key(self):
        record: list = []
        source = MoonrakerSource(BASE, None, opener=make_opener(MOONRAKER_PAYLOAD, record=record))
        source.list_jobs()
        request, _ = record[0]
        assert "X-api-key" not in request.headers


class TestTimeouts:
    def test_a_timeout_becomes_a_source_error(self):
        source = MoonrakerSource(BASE, opener=make_opener(TimeoutError("timed out")))
        with pytest.raises(SourceError, match="cannot reach"):
            source.list_jobs()

    def test_a_socket_timeout_becomes_a_source_error(self):
        source = OctoPrintSource(BASE, API_KEY, opener=make_opener(socket.timeout("timed out")))
        with pytest.raises(SourceError):
            source.list_jobs()

    def test_a_timeout_is_always_passed_to_the_opener(self):
        record: list = []
        source = MoonrakerSource(
            BASE, timeout=2.5, opener=make_opener(MOONRAKER_PAYLOAD, record=record)
        )
        source.list_jobs()
        assert record[0][1] == pytest.approx(2.5)

    def test_the_default_timeout_is_finite(self):
        record: list = []
        source = MoonrakerSource(BASE, opener=make_opener(MOONRAKER_PAYLOAD, record=record))
        source.list_jobs()
        assert record[0][1] == pytest.approx(DEFAULT_TIMEOUT)
        assert 0 < DEFAULT_TIMEOUT < 120


class TestBadPayloads:
    def test_a_non_json_body_raises(self):
        source = MoonrakerSource(BASE, opener=make_opener(b"<html>oops</html>"))
        with pytest.raises(SourceError, match="invalid JSON"):
            source.list_jobs()

    def test_truncated_json_raises(self):
        source = MoonrakerSource(BASE, opener=make_opener(b'{"result": {"jobs": ['))
        with pytest.raises(SourceError, match="invalid JSON"):
            source.list_jobs()

    def test_valid_json_of_the_wrong_shape_raises(self):
        source = MoonrakerSource(BASE, opener=make_opener({"error": "no history plugin"}))
        with pytest.raises(SourceError, match="unexpected Moonraker response"):
            source.list_jobs()

    def test_an_octoprint_payload_of_the_wrong_shape_raises(self):
        source = OctoPrintSource(BASE, API_KEY, opener=make_opener({"nope": 1}))
        with pytest.raises(SourceError, match="unexpected OctoPrint response"):
            source.list_jobs()

    def test_a_non_utf8_body_raises(self):
        source = MoonrakerSource(BASE, opener=make_opener(b"\xff\xfe\x00bad"))
        with pytest.raises(SourceError):
            source.list_jobs()


class TestMoonrakerNormalisation:
    @pytest.fixture
    def jobs(self):
        return MoonrakerSource(
            BASE, printer_name="Voron 2.4", opener=make_opener(MOONRAKER_PAYLOAD)
        ).list_jobs()

    def test_every_entry_is_normalised(self, jobs):
        assert len(jobs) == 3

    def test_the_filename_becomes_a_bare_job_name(self, jobs):
        assert jobs[0].name == "benchy.gcode"

    def test_completed_maps_to_success(self, jobs):
        assert jobs[0].status == "success"

    def test_cancelled_maps_to_cancelled(self, jobs):
        assert jobs[1].status == "cancelled"

    def test_an_unrecognised_terminal_state_maps_to_failed(self, jobs):
        # klippy_shutdown, server_exit, interrupted and friends are all failures.
        assert jobs[2].status == "failed"

    def test_the_slicer_weight_is_preferred_when_present(self, jobs):
        assert jobs[0].filament_g == pytest.approx(12.88)

    def test_length_is_carried_through_in_millimetres(self, jobs):
        assert jobs[0].filament_mm == pytest.approx(4321.0)

    def test_the_start_time_becomes_an_iso_string(self, jobs):
        assert jobs[0].started.startswith("2026-01-01T")
        assert jobs[0].month == "2026-01"

    def test_the_job_id_is_kept_for_idempotency(self, jobs):
        assert [j.source_job_id for j in jobs] == ["000001", "000002", "000003"]
        assert all(j.source == "moonraker" for j in jobs)

    def test_the_printer_name_is_stamped_on(self, jobs):
        assert jobs[0].printer == "Voron 2.4"

    def test_an_early_stop_records_how_far_it_got(self, jobs):
        # Job 3 ran 4000 s of an estimated 10000 s, so 40 percent.
        assert jobs[2].failed_at_fraction == pytest.approx(0.4)

    def test_the_recorded_usage_reconstructs_the_actual_consumption(self, jobs):
        # Moonraker reports actuals; Job.filament_g means "a full run", so the
        # fraction has to bring it back to the 40 g really extruded.
        assert jobs[2].grams_used() == pytest.approx(40.0)

    def test_a_completed_job_has_no_fraction(self, jobs):
        assert jobs[0].failed_at_fraction is None
        assert jobs[0].grams_used() == pytest.approx(12.88)

    def test_the_limit_reaches_the_query_string(self):
        record: list = []
        MoonrakerSource(BASE, limit=7, opener=make_opener(MOONRAKER_PAYLOAD, record=record)).list_jobs()
        assert "limit=7" in record[0][0].full_url
        assert record[0][0].full_url.startswith(BASE + "/server/history/list")


class TestOctoPrintNormalisation:
    @pytest.fixture
    def jobs(self):
        return OctoPrintSource(BASE, API_KEY, opener=make_opener(OCTOPRINT_PAYLOAD)).list_jobs()

    def test_every_entry_is_normalised(self, jobs):
        assert len(jobs) == 2

    def test_success_one_maps_to_success(self, jobs):
        assert jobs[0].status == "success"

    def test_success_zero_maps_to_failed(self, jobs):
        assert jobs[1].status == "failed"

    def test_the_filename_becomes_a_bare_job_name(self, jobs):
        assert jobs[0].name == "benchy.gcode"

    def test_length_and_duration_are_carried_through(self, jobs):
        assert jobs[0].filament_mm == pytest.approx(4321.0)
        assert jobs[0].duration_s == 3723

    def test_octoprint_reports_no_weight(self, jobs):
        # The plugin records length and volume only, so grams must be derived.
        assert jobs[0].filament_g == 0.0

    def test_the_printer_profile_becomes_the_printer_name(self, jobs):
        assert jobs[0].printer == "Prusa MK4"

    def test_ids_are_kept_for_idempotency(self, jobs):
        assert [j.source_job_id for j in jobs] == ["11", "12"]
        assert all(j.source == "octoprint" for j in jobs)

    def test_the_endpoint_is_the_history_api(self):
        record: list = []
        OctoPrintSource(BASE, API_KEY, opener=make_opener(OCTOPRINT_PAYLOAD, record=record)).list_jobs()
        assert record[0][0].full_url == BASE + "/api/history"

    def test_a_boolean_success_flag_is_understood(self):
        payload = {"history": [{"id": 1, "fileName": "a.gcode", "success": True, "printTime": 10}]}
        jobs = OctoPrintSource(BASE, API_KEY, opener=make_opener(payload)).list_jobs()
        assert jobs[0].status == "success"


class TestFixtureSource:
    def test_it_satisfies_the_source_protocol(self, fixture_jobs_file):
        assert isinstance(FixtureSource(fixture_jobs_file), JobSource)

    def test_every_entry_is_normalised(self, fixture_jobs_file):
        jobs = FixtureSource(fixture_jobs_file).list_jobs()
        assert len(jobs) == 3

    def test_fields_map_onto_the_job_dataclass(self, fixture_jobs_file):
        job = FixtureSource(fixture_jobs_file).list_jobs()[0]
        assert job.name == "benchy.gcode"
        assert job.printer == "Voron 2.4"
        assert job.filament_g == pytest.approx(12.9)
        assert job.duration_s == 3723
        assert job.status == "success"
        assert job.started == "2026-01-08T18:20:00+00:00"
        assert job.source == "fixture"
        assert job.source_job_id == "fx-1"

    def test_a_failure_fraction_is_read(self, fixture_jobs_file):
        job = FixtureSource(fixture_jobs_file).list_jobs()[1]
        assert job.status == "failed"
        assert job.failed_at_fraction == pytest.approx(0.4)
        assert job.grams_used() == pytest.approx(40.0)

    def test_aliases_are_accepted(self, fixture_jobs_file):
        job = FixtureSource(fixture_jobs_file).list_jobs()[2]
        assert job.filament_mm == pytest.approx(1000.0)  # from "length_mm"
        assert job.failed_at_fraction == pytest.approx(0.5)  # from "failed_at"

    def test_an_epoch_timestamp_is_converted(self, fixture_jobs_file):
        job = FixtureSource(fixture_jobs_file).list_jobs()[2]
        assert job.started.startswith("2026-")

    def test_a_bare_list_is_accepted(self, tmp_path):
        path = tmp_path / "bare.json"
        path.write_text(json.dumps([{"name": "a", "filament_g": 1.0}]), encoding="utf-8")
        assert len(FixtureSource(path).list_jobs()) == 1

    def test_a_missing_file_raises_a_source_error(self, tmp_path):
        with pytest.raises(SourceError, match="cannot read fixture"):
            FixtureSource(tmp_path / "nope.json").list_jobs()

    def test_invalid_json_raises_a_source_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(SourceError, match="invalid JSON"):
            FixtureSource(path).list_jobs()

    def test_json_of_the_wrong_shape_raises(self, tmp_path):
        path = tmp_path / "wrong.json"
        path.write_text(json.dumps({"jobs": "not a list"}), encoding="utf-8")
        with pytest.raises(SourceError, match="must hold a list"):
            FixtureSource(path).list_jobs()

    def test_the_shipped_example_fixture_parses(self):
        from pathlib import Path

        example = Path(__file__).resolve().parents[1] / "examples" / "jobs.json"
        jobs = FixtureSource(example).list_jobs()
        assert len(jobs) == 10
        assert all(j.source_job_id for j in jobs)


class TestResolveGrams:
    def test_lengths_become_grams_using_the_loaded_spool(self):
        jobs = OctoPrintSource(BASE, API_KEY, opener=make_opener(OCTOPRINT_PAYLOAD)).list_jobs()
        updated = resolve_grams(jobs, 1.75, 1.24)
        assert updated == 2
        assert jobs[0].filament_g == pytest.approx(length_to_mass_g(4321.0, 1.75, 1.24))

    def test_an_existing_weight_is_never_overwritten(self):
        jobs = MoonrakerSource(BASE, opener=make_opener(MOONRAKER_PAYLOAD)).list_jobs()
        before = jobs[0].filament_g
        resolve_grams(jobs, 1.75, 1.24)
        assert jobs[0].filament_g == pytest.approx(before)

    def test_jobs_with_no_length_are_left_alone(self):
        from spool.models import Job

        jobs = [Job(name="a")]
        assert resolve_grams(jobs, 1.75, 1.24) == 0
        assert jobs[0].filament_g == 0.0


class TestProtocol:
    @pytest.mark.parametrize("cls", [MoonrakerSource, OctoPrintSource])
    def test_http_sources_satisfy_the_protocol(self, cls):
        assert isinstance(cls(BASE, API_KEY), JobSource)
