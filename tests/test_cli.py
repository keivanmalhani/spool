"""End to end command line behaviour.

Every test drives ``main([...])`` directly and asserts the exit code, because
the exit codes are a documented part of the interface: 0 done, 1 broken,
2 nothing to report.
"""

from __future__ import annotations

import json
import re

import pytest

from spool.cli import EXIT_EMPTY, EXIT_ERROR, EXIT_OK, main
from spool.db import connect, count_jobs, get_spool, list_spools
from tests.conftest import bare_gcode, cura_gcode, prusaslicer_gcode

API_KEY = "SUPERSECRET-abc123-do-not-leak"


@pytest.fixture
def db(tmp_path):
    """A database path plus a helper to run commands against it."""
    return tmp_path / "spool.db"


def run(db, *args) -> int:
    """Run one spool command against the given database."""
    return main(["--db", str(db), *args])


@pytest.fixture
def seeded(db):
    """A database with two spools and a registered printer."""
    assert run(db, "init") == EXIT_OK
    assert run(db, "add", "--material", "PLA", "--brand", "Testament",
               "--color", "Signal Orange", "--price", "25.00", "--weight", "1000") == EXIT_OK
    assert run(db, "add", "--material", "PETG", "--brand", "Overture",
               "--color", "Grey", "--price", "20.00", "--weight", "1000") == EXIT_OK
    assert run(db, "printer", "add", "Voron 2.4", "--watts", "150",
               "--price", "900", "--life-hours", "3000") == EXIT_OK
    assert run(db, "config", "--set", "tariff_per_kwh=0.30") == EXIT_OK
    return db


class TestInit:
    def test_init_creates_the_database(self, db, capsys):
        assert run(db, "init") == EXIT_OK
        assert db.exists()
        assert "schema version" in capsys.readouterr().out

    def test_init_is_safe_to_run_twice(self, db, capsys):
        run(db, "init")
        run(db, "add", "--material", "PLA", "--price", "25")
        capsys.readouterr()

        assert run(db, "init") == EXIT_OK
        out = capsys.readouterr().out
        assert "Existing data preserved: 1 spool(s)" in out

    def test_db_can_be_given_before_the_subcommand(self, db):
        assert main(["--db", str(db), "init"]) == EXIT_OK
        assert db.exists()

    def test_db_can_be_given_after_the_subcommand(self, db):
        assert main(["init", "--db", str(db)]) == EXIT_OK
        assert db.exists()

    def test_the_environment_variable_is_honoured(self, tmp_path, monkeypatch):
        target = tmp_path / "from-env.db"
        monkeypatch.setenv("SPOOL_DB", str(target))
        assert main(["init"]) == EXIT_OK
        assert target.exists()

    def test_no_arguments_prints_help(self, capsys):
        assert main([]) == EXIT_OK
        assert "usage: spool" in capsys.readouterr().out

    def test_an_unknown_command_is_rejected_by_argparse(self):
        with pytest.raises(SystemExit) as caught:
            main(["nonsense"])
        assert caught.value.code == 2


class TestAdd:
    def test_adding_a_spool(self, db, capsys):
        run(db, "init")
        capsys.readouterr()
        assert run(db, "add", "--material", "PLA", "--brand", "Testament",
                   "--price", "25.00") == EXIT_OK
        out = capsys.readouterr().out
        assert "Added spool #1" in out
        assert "0.0250 per gram" in out

    def test_the_stored_spool_matches_what_was_asked_for(self, db):
        run(db, "init")
        run(db, "add", "--material", "petg", "--price", "21.50", "--weight", "750",
            "--diameter", "2.85", "--color", "Grey", "--purchased", "2026-02-01")
        conn = connect(db)
        try:
            spool = get_spool(conn, 1)
        finally:
            conn.close()
        assert spool.material == "PETG"
        assert spool.density_g_cm3 == pytest.approx(1.27)
        assert spool.diameter_mm == pytest.approx(2.85)
        assert spool.spool_weight_g == pytest.approx(750.0)
        assert spool.remaining_g == pytest.approx(750.0)
        assert spool.purchased == "2026-02-01"

    def test_an_explicit_density_overrides_the_default(self, db):
        run(db, "init")
        run(db, "add", "--material", "PLA", "--price", "25", "--density", "1.31")
        conn = connect(db)
        try:
            assert get_spool(conn, 1).density_g_cm3 == pytest.approx(1.31)
        finally:
            conn.close()

    def test_an_unknown_material_warns_but_succeeds(self, db, capsys):
        run(db, "init")
        capsys.readouterr()
        assert run(db, "add", "--material", "PLA-CF", "--price", "39") == EXIT_OK
        assert "no density default" in capsys.readouterr().err

    def test_a_negative_price_is_rejected(self, db, capsys):
        run(db, "init")
        assert run(db, "add", "--material", "PLA", "--price", "-5") == EXIT_ERROR
        assert "cannot be negative" in capsys.readouterr().err

    def test_a_zero_weight_is_rejected(self, db):
        run(db, "init")
        assert run(db, "add", "--material", "PLA", "--price", "25", "--weight", "0") == EXIT_ERROR

    def test_material_is_required(self, db):
        run(db, "init")
        with pytest.raises(SystemExit):
            run(db, "add", "--price", "25")

    def test_price_is_required(self, db):
        run(db, "init")
        with pytest.raises(SystemExit):
            run(db, "add", "--material", "PLA")


class TestList:
    def test_an_empty_inventory_reports_nothing_found(self, db, capsys):
        run(db, "init")
        capsys.readouterr()
        assert run(db, "list") == EXIT_EMPTY
        assert "No spools" in capsys.readouterr().out

    def test_the_inventory_table_shows_each_spool(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "Testament" in out
        assert "Overture" in out
        assert "100.0%" in out

    def test_a_low_spool_is_flagged(self, seeded, capsys):
        run(seeded, "use", "big print", "--spool", "1", "--grams", "900", "--duration", "10h")
        capsys.readouterr()
        run(seeded, "list")
        out = capsys.readouterr().out
        assert "LOW" in out
        assert "Low stock" in out

    def test_archived_spools_are_hidden_unless_asked_for(self, seeded, capsys):
        run(seeded, "archive", "2")
        capsys.readouterr()
        run(seeded, "list")
        assert "Overture" not in capsys.readouterr().out

        run(seeded, "list", "--all")
        assert "Overture" in capsys.readouterr().out


class TestUse:
    def test_recording_a_successful_job(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "use", "cube", "--spool", "1", "--grams", "42",
                   "--duration", "1h30m", "--printer", "Voron 2.4") == EXIT_OK
        out = capsys.readouterr().out
        assert "Recorded job #1" in out
        assert "958 g left" in out

    def test_the_spool_is_decremented(self, seeded):
        run(seeded, "use", "cube", "--spool", "1", "--grams", "42", "--duration", "1h")
        conn = connect(seeded)
        try:
            assert get_spool(conn, 1).remaining_g == pytest.approx(958.0)
        finally:
            conn.close()

    def test_a_length_is_converted_using_the_spool(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "use", "long", "--spool", "1", "--length-mm", "1000",
                   "--duration", "30m") == EXIT_OK
        assert "2.98 g used" in capsys.readouterr().out

    def test_a_failure_only_consumes_its_fraction(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "use", "oops", "--spool", "1", "--grams", "100",
                   "--duration", "4h", "--status", "failed", "--failed-at", "0.4") == EXIT_OK
        out = capsys.readouterr().out
        assert "40.00 g used" in out
        assert "Stopped at 40%" in out
        assert "960 g left" in out

    def test_a_percentage_style_fraction_is_accepted(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "use", "oops", "--spool", "1", "--grams", "100",
                   "--duration", "4h", "--status", "failed", "--failed-at", "40%") == EXIT_OK
        assert "40.00 g used" in capsys.readouterr().out

    def test_over_use_warns_and_clamps_at_zero(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "use", "huge", "--spool", "1", "--grams", "1200",
                   "--duration", "40h") == EXIT_OK
        err = capsys.readouterr().err
        assert "short by 200.00 g" in err

    def test_neither_grams_nor_length_is_an_error(self, seeded, capsys):
        assert run(seeded, "use", "x", "--spool", "1", "--duration", "1h") == EXIT_ERROR
        assert "--grams or --length-mm" in capsys.readouterr().err

    def test_both_grams_and_length_is_an_error(self, seeded):
        assert run(seeded, "use", "x", "--spool", "1", "--grams", "10",
                   "--length-mm", "1000") == EXIT_ERROR

    def test_an_unknown_spool_is_an_error(self, seeded, capsys):
        assert run(seeded, "use", "x", "--spool", "99", "--grams", "10") == EXIT_ERROR
        assert "no spool with id 99" in capsys.readouterr().err

    def test_an_unreadable_duration_is_an_error(self, seeded):
        assert run(seeded, "use", "x", "--spool", "1", "--grams", "10",
                   "--duration", "soon") == EXIT_ERROR

    def test_an_invalid_status_is_rejected_by_argparse(self, seeded):
        with pytest.raises(SystemExit):
            run(seeded, "use", "x", "--spool", "1", "--grams", "10", "--status", "exploded")


class TestImport:
    def test_importing_a_prusaslicer_file(self, seeded, tmp_path, capsys):
        path = tmp_path / "part.gcode"
        path.write_text(prusaslicer_gcode(), encoding="utf-8")
        capsys.readouterr()

        assert run(seeded, "import", str(path), "--spool", "1",
                   "--printer", "Voron 2.4") == EXIT_OK
        out = capsys.readouterr().out
        assert "prusaslicer" in out
        assert "12.88 g" in out
        assert "1h 02m" in out

    def test_importing_a_cura_file_derives_the_weight(self, seeded, tmp_path, capsys):
        path = tmp_path / "cura.gcode"
        path.write_text(cura_gcode(), encoding="utf-8")
        capsys.readouterr()

        assert run(seeded, "import", str(path), "--spool", "1") == EXIT_OK
        # 4.321 m of 1.75 mm PLA at 1.24 g/cm3 is 12.89 g.
        assert "12.89 g" in capsys.readouterr().out

    def test_a_file_with_no_metadata_reports_nothing_found(self, seeded, tmp_path, capsys):
        path = tmp_path / "bare.gcode"
        path.write_text(bare_gcode(), encoding="utf-8")
        capsys.readouterr()

        assert run(seeded, "import", str(path), "--spool", "1") == EXIT_EMPTY
        captured = capsys.readouterr()
        assert "no slicer metadata found" in captured.out
        assert "pass --grams" in captured.err

    def test_grams_can_be_supplied_for_a_bare_file(self, seeded, tmp_path):
        path = tmp_path / "bare.gcode"
        path.write_text(bare_gcode(), encoding="utf-8")
        assert run(seeded, "import", str(path), "--spool", "1", "--grams", "33") == EXIT_OK

    def test_a_dry_run_writes_nothing(self, seeded, tmp_path, capsys):
        path = tmp_path / "part.gcode"
        path.write_text(prusaslicer_gcode(), encoding="utf-8")
        capsys.readouterr()

        assert run(seeded, "import", str(path), "--spool", "1", "--dry-run") == EXIT_OK
        assert "Dry run" in capsys.readouterr().out
        conn = connect(seeded)
        try:
            assert count_jobs(conn) == 0
            assert get_spool(conn, 1).remaining_g == pytest.approx(1000.0)
        finally:
            conn.close()

    def test_a_missing_file_is_an_error(self, seeded, tmp_path, capsys):
        assert run(seeded, "import", str(tmp_path / "nope.gcode"), "--spool", "1") == EXIT_ERROR
        assert "no such file" in capsys.readouterr().err


class TestSync:
    def test_syncing_a_fixture(self, seeded, fixture_jobs_file, capsys):
        capsys.readouterr()
        assert run(seeded, "sync", "--fixture", str(fixture_jobs_file),
                   "--spool", "1") == EXIT_OK
        assert "Added 3 job(s)" in capsys.readouterr().out

    def test_syncing_twice_adds_nothing_the_second_time(self, seeded, fixture_jobs_file, capsys):
        run(seeded, "sync", "--fixture", str(fixture_jobs_file), "--spool", "1")
        conn = connect(seeded)
        try:
            after_first = count_jobs(conn)
        finally:
            conn.close()
        capsys.readouterr()

        assert run(seeded, "sync", "--fixture", str(fixture_jobs_file),
                   "--spool", "1") == EXIT_EMPTY
        assert "skipped 3 already present" in capsys.readouterr().out

        conn = connect(seeded)
        try:
            assert count_jobs(conn) == after_first == 3
        finally:
            conn.close()

    def test_lengths_are_converted_using_the_named_spool(self, seeded, fixture_jobs_file, capsys):
        capsys.readouterr()
        run(seeded, "sync", "--fixture", str(fixture_jobs_file), "--spool", "1")
        assert "Derived a weight for 1 job(s)" in capsys.readouterr().out

    def test_a_dry_run_writes_nothing(self, seeded, fixture_jobs_file, capsys):
        capsys.readouterr()
        assert run(seeded, "sync", "--fixture", str(fixture_jobs_file),
                   "--spool", "1", "--dry-run") == EXIT_OK
        assert "Would add 3 job(s)" in capsys.readouterr().out
        conn = connect(seeded)
        try:
            assert count_jobs(conn) == 0
        finally:
            conn.close()

    def test_exactly_one_source_is_required(self, seeded, capsys):
        assert run(seeded, "sync") == EXIT_ERROR
        assert "exactly one of" in capsys.readouterr().err

    def test_two_sources_are_rejected(self, seeded, fixture_jobs_file):
        assert run(seeded, "sync", "--fixture", str(fixture_jobs_file),
                   "--moonraker", "http://printer.invalid:7125") == EXIT_ERROR

    def test_a_missing_fixture_is_a_clear_error(self, seeded, tmp_path, capsys):
        assert run(seeded, "sync", "--fixture", str(tmp_path / "nope.json")) == EXIT_ERROR
        assert "cannot read fixture" in capsys.readouterr().err


class TestApiKeyHandling:
    def test_an_unset_environment_variable_is_a_clear_error(self, seeded, capsys, monkeypatch):
        monkeypatch.delenv("SPOOL_TEST_KEY", raising=False)
        assert run(seeded, "sync", "--octoprint", "http://printer.invalid",
                   "--api-key-env", "SPOOL_TEST_KEY") == EXIT_ERROR
        err = capsys.readouterr().err
        assert "SPOOL_TEST_KEY is not set" in err
        assert "never accepts a key as a flag value" in err

    def test_there_is_no_flag_that_takes_a_key_directly(self, seeded):
        # The only way in is the name of an environment variable. Argparse
        # would otherwise accept --api-key as an abbreviation of --api-key-env
        # and swallow the secret, so prefix matching is disabled.
        with pytest.raises(SystemExit):
            run(seeded, "sync", "--octoprint", "http://printer.invalid", "--api-key", API_KEY)

    def test_a_key_typed_into_the_env_flag_is_refused_without_echoing_it(
        self, seeded, capsys
    ):
        assert run(seeded, "sync", "--octoprint", "http://printer.invalid",
                   "--api-key-env", API_KEY) == EXIT_ERROR
        captured = capsys.readouterr()
        assert API_KEY not in captured.out
        assert API_KEY not in captured.err
        assert "not the key itself" in captured.err

    def test_a_real_variable_name_is_still_named_in_the_error(self, seeded, capsys, monkeypatch):
        # Variable names are not secrets, and naming the missing one is the
        # whole point of the message.
        monkeypatch.delenv("MY_PRINTER_KEY", raising=False)
        assert run(seeded, "sync", "--octoprint", "http://printer.invalid",
                   "--api-key-env", "MY_PRINTER_KEY") == EXIT_ERROR
        assert "MY_PRINTER_KEY is not set" in capsys.readouterr().err

    def test_octoprint_requires_a_key(self, seeded, capsys):
        assert run(seeded, "sync", "--octoprint", "http://printer.invalid") == EXIT_ERROR
        assert "--api-key-env" in capsys.readouterr().err

    def test_the_key_never_appears_in_output(self, seeded, capsys, monkeypatch):
        monkeypatch.setenv("SPOOL_TEST_KEY", API_KEY)
        # printer.invalid does not resolve, so this fails at the network layer.
        assert run(seeded, "sync", "--octoprint", "http://printer.invalid:9999",
                   "--api-key-env", "SPOOL_TEST_KEY", "--timeout", "1") == EXIT_ERROR
        captured = capsys.readouterr()
        assert API_KEY not in captured.out
        assert API_KEY not in captured.err

    def test_a_bad_base_url_is_rejected_before_any_request(self, seeded, capsys):
        assert run(seeded, "sync", "--moonraker", "ftp://printer.invalid") == EXIT_ERROR
        assert "http" in capsys.readouterr().err


class TestCost:
    def test_no_jobs_reports_nothing_found(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "cost") == EXIT_EMPTY
        assert "No jobs in range" in capsys.readouterr().out

    def test_the_report_shows_every_section(self, seeded, capsys):
        run(seeded, "use", "cube", "--spool", "1", "--grams", "200",
            "--duration", "4h", "--printer", "Voron 2.4")
        capsys.readouterr()

        assert run(seeded, "cost") == EXIT_OK
        out = capsys.readouterr().out
        assert "Cost model inputs" in out
        assert "By material" in out
        assert "Recent jobs" in out
        assert "Summary" in out

    def test_the_known_scenario_produces_the_known_numbers(self, seeded, capsys):
        # 200 g of a 25.00 kilo = 5.00; 150 W for 4 h at 0.30 = 0.18;
        # a 900.00 machine over 3000 h = 0.30 per hour, so 1.20. Total 6.38.
        run(seeded, "use", "cube", "--spool", "1", "--grams", "200",
            "--duration", "4h", "--printer", "Voron 2.4")
        capsys.readouterr()
        run(seeded, "cost")
        out = capsys.readouterr().out
        assert "Filament cost   USD 5.00" in out
        assert "Electricity     USD 0.18" in out
        assert "Machine wear    USD 1.20" in out
        assert "TOTAL           USD 6.38" in out

    @pytest.mark.parametrize("by", ["material", "printer", "month", "spool", "status"])
    def test_every_grouping_renders(self, seeded, capsys, by):
        run(seeded, "use", "cube", "--spool", "1", "--grams", "50", "--duration", "1h")
        capsys.readouterr()
        assert run(seeded, "cost", "--by", by) == EXIT_OK
        assert "By " in capsys.readouterr().out

    def test_an_invalid_grouping_is_rejected(self, seeded):
        with pytest.raises(SystemExit):
            run(seeded, "cost", "--by", "colour")

    def test_a_tariff_override_changes_the_electricity_line(self, seeded, capsys):
        run(seeded, "use", "cube", "--spool", "1", "--grams", "200",
            "--duration", "4h", "--printer", "Voron 2.4")
        capsys.readouterr()
        run(seeded, "cost", "--tariff", "0.60")
        assert "Electricity     USD 0.36" in capsys.readouterr().out

    def test_the_date_filter_excludes_older_jobs(self, seeded, capsys):
        run(seeded, "use", "old", "--spool", "1", "--grams", "10",
            "--duration", "1h", "--started", "2020-01-01T00:00:00+00:00")
        capsys.readouterr()
        assert run(seeded, "cost", "--since", "2026-01-01") == EXIT_EMPTY

    def test_json_output_is_machine_readable(self, seeded, capsys):
        run(seeded, "use", "cube", "--spool", "1", "--grams", "200",
            "--duration", "4h", "--printer", "Voron 2.4")
        capsys.readouterr()

        assert run(seeded, "cost", "--json") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["total_cost"] == pytest.approx(6.38)
        assert payload["summary"]["jobs"] == 1
        assert payload["currency"] == "USD"
        assert payload["rollups"][0]["key"] == "PLA"
        assert payload["jobs"][0]["name"] == "cube"


class TestDashboardCommand:
    def test_writing_the_dashboard(self, seeded, tmp_path, capsys):
        run(seeded, "use", "cube", "--spool", "1", "--grams", "200", "--duration", "4h")
        out_file = tmp_path / "dash.html"
        capsys.readouterr()

        assert run(seeded, "dashboard", "--out", str(out_file)) == EXIT_OK
        assert out_file.exists()
        assert "no external references" in capsys.readouterr().out

    def test_the_written_dashboard_has_no_external_references(self, seeded, tmp_path):
        run(seeded, "use", "cube", "--spool", "1", "--grams", "200", "--duration", "4h")
        out_file = tmp_path / "dash.html"
        run(seeded, "dashboard", "--out", str(out_file))
        text = out_file.read_text(encoding="utf-8")
        assert "http" not in text
        assert "<script src" not in text
        assert "Testament PLA" in text

    def test_a_dashboard_can_be_written_with_no_jobs(self, seeded, tmp_path):
        assert run(seeded, "dashboard", "--out", str(tmp_path / "d.html")) == EXIT_OK

    def test_an_empty_database_reports_nothing_found(self, db, tmp_path):
        run(db, "init")
        assert run(db, "dashboard", "--out", str(tmp_path / "d.html")) == EXIT_EMPTY

    def test_the_title_can_be_set(self, seeded, tmp_path):
        out_file = tmp_path / "d.html"
        run(seeded, "dashboard", "--out", str(out_file), "--title", "Workshop filament")
        assert "Workshop filament" in out_file.read_text(encoding="utf-8")


class TestArchiveAndRestock:
    def test_archiving_then_restocking(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "archive", "2") == EXIT_OK
        assert "Archived spool #2" in capsys.readouterr().out

        assert run(seeded, "restock", "2") == EXIT_OK
        assert "restocked to 1000 g" in capsys.readouterr().out

        conn = connect(seeded)
        try:
            assert [s.id for s in list_spools(conn)] == [1, 2]
        finally:
            conn.close()

    def test_restocking_to_a_measured_weight(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "restock", "1", "--grams", "612") == EXIT_OK
        assert "restocked to 612 g (61%)" in capsys.readouterr().out

    def test_archiving_an_unknown_spool_is_an_error(self, seeded, capsys):
        assert run(seeded, "archive", "99") == EXIT_ERROR
        assert "no spool with id 99" in capsys.readouterr().err

    def test_restocking_an_unknown_spool_is_an_error(self, seeded):
        assert run(seeded, "restock", "99") == EXIT_ERROR


class TestPrinterAndConfig:
    def test_printers_are_listed(self, seeded, capsys):
        capsys.readouterr()
        assert run(seeded, "printer", "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "Voron 2.4" in out
        assert "0.3000" in out

    def test_no_printers_reports_nothing_found(self, db):
        run(db, "init")
        assert run(db, "printer", "list") == EXIT_EMPTY

    def test_settings_round_trip(self, db, capsys):
        run(db, "init")
        assert run(db, "config", "--set", "tariff_per_kwh=0.2815",
                   "--set", "currency=EUR") == EXIT_OK
        capsys.readouterr()
        assert run(db, "config") == EXIT_OK
        out = capsys.readouterr().out
        assert "0.2815" in out
        assert "EUR" in out

    def test_an_unknown_setting_is_rejected(self, db, capsys):
        run(db, "init")
        assert run(db, "config", "--set", "nonsense=1") == EXIT_ERROR
        assert "unknown setting" in capsys.readouterr().err

    def test_a_malformed_setting_is_rejected(self, db, capsys):
        run(db, "init")
        assert run(db, "config", "--set", "tariff_per_kwh") == EXIT_ERROR
        assert "key=value" in capsys.readouterr().err

    def test_an_unparseable_value_is_rejected(self, db):
        run(db, "init")
        assert run(db, "config", "--set", "tariff_per_kwh=cheap") == EXIT_ERROR


class TestEndToEnd:
    """The whole documented quick start, in order, in one temp directory."""

    def test_the_full_flow(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SPOOL_DB", raising=False)

        gcode = tmp_path / "benchy.gcode"
        gcode.write_text(prusaslicer_gcode(), encoding="utf-8")

        assert main(["init"]) == EXIT_OK
        assert main(["add", "--material", "PLA", "--brand", "Testament",
                     "--color", "Signal Orange", "--price", "25.00"]) == EXIT_OK
        assert main(["add", "--material", "PETG", "--brand", "Overture",
                     "--color", "Grey", "--price", "20.00"]) == EXIT_OK
        assert main(["printer", "add", "Voron 2.4", "--watts", "150",
                     "--price", "900", "--life-hours", "3000"]) == EXIT_OK
        assert main(["config", "--set", "tariff_per_kwh=0.30"]) == EXIT_OK
        assert main(["import", str(gcode), "--spool", "1",
                     "--printer", "Voron 2.4"]) == EXIT_OK
        assert main(["use", "bracket", "--spool", "2", "--grams", "100",
                     "--duration", "4h", "--status", "failed", "--failed-at", "0.4",
                     "--printer", "Voron 2.4"]) == EXIT_OK
        assert main(["list"]) == EXIT_OK
        assert main(["cost", "--by", "material"]) == EXIT_OK
        assert main(["dashboard", "--out", "dash.html"]) == EXIT_OK

        assert (tmp_path / "spool.db").exists()
        assert (tmp_path / "dash.html").exists()

        out = capsys.readouterr().out
        # The failed PETG print consumed 40 of its 100 g, so 960 g are left.
        assert "960 g left" in out
        # The Benchy took 12.88 g off the 1 kg PLA spool.
        assert "987 g left" in out

        text = (tmp_path / "dash.html").read_text(encoding="utf-8")
        assert "http" not in text
        assert "Testament PLA Signal Orange" in text
        assert "Overture PETG Grey" in text

    def test_the_report_after_the_full_flow_reconciles(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SPOOL_DB", raising=False)
        main(["init"])
        main(["add", "--material", "PLA", "--price", "25.00"])
        main(["config", "--set", "tariff_per_kwh=0.30", "--set", "default_watts=150"])
        main(["use", "a", "--spool", "1", "--grams", "200", "--duration", "4h"])
        main(["use", "b", "--spool", "1", "--grams", "100", "--duration", "4h",
              "--status", "failed", "--failed-at", "0.5"])
        capsys.readouterr()

        main(["cost", "--json"])
        payload = json.loads(capsys.readouterr().out)
        summary = payload["summary"]

        # 200 g used plus 50 g of the failure.
        assert summary["grams"] == pytest.approx(250.0)
        assert summary["hours"] == pytest.approx(6.0)
        assert summary["failed"] == 1
        assert summary["failure_rate"] == pytest.approx(0.5)
        # Filament 250 * 0.025 = 6.25; power 0.15 kW * 6 h * 0.30 = 0.27.
        assert summary["filament_cost"] == pytest.approx(6.25)
        assert summary["electricity_cost"] == pytest.approx(0.27)
        assert summary["total_cost"] == pytest.approx(6.52)
        # The failure: 50 * 0.025 = 1.25 plus 0.15 * 2 * 0.30 = 0.09.
        assert summary["wasted_cost"] == pytest.approx(1.34)

    def test_the_displayed_report_columns_add_up(self, seeded, capsys):
        run(seeded, "use", "a", "--spool", "1", "--grams", "37.7",
            "--duration", "2h13m", "--printer", "Voron 2.4")
        run(seeded, "use", "b", "--spool", "2", "--grams", "91.3",
            "--duration", "5h47m", "--printer", "Voron 2.4")
        capsys.readouterr()
        run(seeded, "cost", "--by", "material")
        out = capsys.readouterr().out

        for line in out.splitlines():
            cells = line.split()
            if len(cells) == 10 and cells[0] in ("PLA", "PETG"):
                filament, power, machine, total = (float(c) for c in cells[4:8])
                assert total == pytest.approx(filament + power + machine, abs=1e-9), line

        summary = re.search(r"TOTAL\s+USD\s+([\d.]+)", out)
        filament = re.search(r"Filament cost\s+USD\s+([\d.]+)", out)
        power = re.search(r"Electricity\s+USD\s+([\d.]+)", out)
        machine = re.search(r"Machine wear\s+USD\s+([\d.]+)", out)
        assert float(summary.group(1)) == pytest.approx(
            float(filament.group(1)) + float(power.group(1)) + float(machine.group(1)),
            abs=1e-9,
        )
