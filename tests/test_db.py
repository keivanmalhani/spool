"""Schema, migrations and the inventory arithmetic that touches the database."""

from __future__ import annotations

import sqlite3

import pytest

from spool.db import (
    MIGRATIONS,
    SCHEMA_VERSION,
    DBError,
    add_job,
    add_printer,
    add_spool,
    all_settings,
    connect,
    count_jobs,
    decrement_spool,
    find_job_by_source,
    get_job,
    get_printer,
    get_spool,
    list_jobs,
    list_printers,
    list_spools,
    load_settings,
    migrate,
    restock_spool,
    save_settings,
    schema_version,
    set_spool_archived,
    table_names,
)
from spool.models import Job, Printer, Settings, Spool


class TestSchema:
    def test_connecting_creates_the_file(self, db_path):
        assert not db_path.exists()
        connect(db_path).close()
        assert db_path.exists()

    def test_every_table_exists(self, conn):
        assert set(table_names(conn)) >= {"spools", "jobs", "printers", "settings"}

    def test_the_version_is_stamped(self, conn):
        assert schema_version(conn) == SCHEMA_VERSION
        assert SCHEMA_VERSION == MIGRATIONS[-1][0]

    def test_foreign_keys_are_on(self, conn):
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


class TestMigrations:
    def test_migrating_twice_changes_nothing(self, conn):
        first = schema_version(conn)
        assert migrate(conn) == first
        assert migrate(conn) == first
        assert schema_version(conn) == SCHEMA_VERSION

    def test_reopening_an_existing_database_preserves_data(self, db_path, pla_spool):
        first = connect(db_path)
        add_spool(first, pla_spool)
        first.close()

        second = connect(db_path)
        try:
            assert schema_version(second) == SCHEMA_VERSION
            assert len(list_spools(second)) == 1
        finally:
            second.close()

    def test_migrations_are_numbered_in_ascending_order(self):
        versions = [v for v, _ in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)

    def test_a_pre_sync_database_is_upgraded_in_place(self, db_path, pla_spool):
        # Stand up version 1 only, insert data, then let connect() run the rest.
        raw = sqlite3.connect(str(db_path))
        raw.executescript(MIGRATIONS[0][1])
        raw.execute("PRAGMA user_version = 1")
        raw.execute("INSERT INTO spools (material, price) VALUES ('PLA', 25.0)")
        raw.commit()
        raw.close()

        conn = connect(db_path)
        try:
            assert schema_version(conn) == SCHEMA_VERSION
            assert len(list_spools(conn)) == 1
            # The columns migration 2 adds must now be usable.
            job = Job(name="after upgrade", source="moonraker", source_job_id="m-1")
            add_job(conn, job)
            assert find_job_by_source(conn, "moonraker", "m-1") is not None
        finally:
            conn.close()

    def test_a_failed_migration_leaves_the_version_alone(self, conn, monkeypatch):
        broken = MIGRATIONS + ((SCHEMA_VERSION + 1, "CREATE TABLE ( this is not sql"),)
        monkeypatch.setattr("spool.db.MIGRATIONS", broken)
        with pytest.raises(DBError, match="migration to version"):
            migrate(conn)
        assert schema_version(conn) == SCHEMA_VERSION


class TestSpools:
    def test_insert_and_read_round_trip(self, conn, pla_spool):
        stored = add_spool(conn, pla_spool)
        assert stored.id is not None

        loaded = get_spool(conn, stored.id)
        assert loaded is not None
        assert loaded.material == "PLA"
        assert loaded.brand == "Testament"
        assert loaded.color == "Signal Orange"
        assert loaded.diameter_mm == pytest.approx(1.75)
        assert loaded.density_g_cm3 == pytest.approx(1.24)
        assert loaded.price == pytest.approx(25.00)
        assert loaded.currency == "USD"
        assert loaded.purchased == "2026-01-01"
        assert loaded.remaining_g == pytest.approx(1000.0)
        assert loaded.archived is False

    def test_an_unknown_id_reads_as_none(self, conn):
        assert get_spool(conn, 999) is None

    def test_listing_is_in_insertion_order(self, conn, stored_spools):
        assert [s.id for s in list_spools(conn)] == [1, 2]

    def test_archiving_hides_a_spool_from_the_default_listing(self, conn, stored_spools):
        assert set_spool_archived(conn, stored_spools[0].id, True) is True
        visible = list_spools(conn)
        assert [s.id for s in visible] == [2]

    def test_an_archived_spool_is_still_there_when_asked_for(self, conn, stored_spools):
        set_spool_archived(conn, stored_spools[0].id, True)
        every = list_spools(conn, include_archived=True)
        assert len(every) == 2
        assert every[0].archived is True

    def test_archiving_an_unknown_id_reports_failure(self, conn):
        assert set_spool_archived(conn, 999, True) is False

    def test_restocking_refills_and_unarchives(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        decrement_spool(conn, spool_id, 900.0)
        set_spool_archived(conn, spool_id, True)

        restocked = restock_spool(conn, spool_id)
        assert restocked.remaining_g == pytest.approx(1000.0)
        assert restocked.archived is False
        assert [s.id for s in list_spools(conn)] == [1, 2]

    def test_restocking_to_a_measured_weight(self, conn, stored_spools):
        restocked = restock_spool(conn, stored_spools[0].id, 613.0)
        assert restocked.remaining_g == pytest.approx(613.0)

    def test_restocking_an_unknown_id_returns_none(self, conn):
        assert restock_spool(conn, 999) is None


class TestDecrement:
    def test_a_normal_decrement(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        assert decrement_spool(conn, spool_id, 250.0) == 0.0
        assert get_spool(conn, spool_id).remaining_g == pytest.approx(750.0)

    def test_decrements_accumulate(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        decrement_spool(conn, spool_id, 100.0)
        decrement_spool(conn, spool_id, 50.5)
        assert get_spool(conn, spool_id).remaining_g == pytest.approx(849.5)

    def test_a_spool_never_goes_negative(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        decrement_spool(conn, spool_id, 1500.0)
        assert get_spool(conn, spool_id).remaining_g == 0.0

    def test_the_shortfall_is_reported_rather_than_hidden(self, conn, stored_spools):
        # Inventory that silently invents filament is worse than none at all.
        shortfall = decrement_spool(conn, stored_spools[0].id, 1500.0)
        assert shortfall == pytest.approx(500.0)

    def test_an_exact_decrement_reports_no_shortfall(self, conn, stored_spools):
        assert decrement_spool(conn, stored_spools[0].id, 1000.0) == 0.0

    def test_decrementing_an_unknown_spool_raises(self, conn):
        with pytest.raises(DBError, match="no spool with id"):
            decrement_spool(conn, 999, 10.0)


class TestJobs:
    def test_recording_a_job_decrements_its_spool(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        job, shortfall = add_job(conn, Job(name="cube", spool_id=spool_id, filament_g=42.0))
        assert job.id is not None
        assert shortfall == 0.0
        assert get_spool(conn, spool_id).remaining_g == pytest.approx(958.0)

    def test_a_partial_failure_only_costs_its_fraction(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        add_job(
            conn,
            Job(name="oops", spool_id=spool_id, filament_g=100.0, status="failed",
                failed_at_fraction=0.4),
        )
        assert get_spool(conn, spool_id).remaining_g == pytest.approx(960.0)

    def test_over_use_zeroes_the_spool_and_reports_the_shortfall(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        _, shortfall = add_job(conn, Job(name="huge", spool_id=spool_id, filament_g=1200.0))
        assert shortfall == pytest.approx(200.0)
        assert get_spool(conn, spool_id).remaining_g == 0.0

    def test_decrementing_can_be_skipped(self, conn, stored_spools):
        spool_id = stored_spools[0].id
        add_job(conn, Job(name="backfill", spool_id=spool_id, filament_g=42.0), decrement=False)
        assert get_spool(conn, spool_id).remaining_g == pytest.approx(1000.0)

    def test_job_round_trip_keeps_every_field(self, conn, stored_spools):
        original = Job(
            name="round trip",
            printer="Voron 2.4",
            spool_id=stored_spools[0].id,
            filament_g=33.3,
            filament_mm=11000.0,
            duration_s=5400,
            status="cancelled",
            started="2026-05-01T08:00:00+00:00",
            failed_at_fraction=0.25,
            source="fixture",
            source_job_id="fx-9",
            notes="hello",
        )
        stored, _ = add_job(conn, original)
        loaded = get_job(conn, stored.id)
        assert loaded.name == "round trip"
        assert loaded.printer == "Voron 2.4"
        assert loaded.filament_g == pytest.approx(33.3)
        assert loaded.filament_mm == pytest.approx(11000.0)
        assert loaded.duration_s == 5400
        assert loaded.status == "cancelled"
        assert loaded.started == "2026-05-01T08:00:00+00:00"
        assert loaded.failed_at_fraction == pytest.approx(0.25)
        assert loaded.source == "fixture"
        assert loaded.source_job_id == "fx-9"
        assert loaded.notes == "hello"

    def test_source_lookup_powers_idempotent_sync(self, conn):
        add_job(conn, Job(name="a", source="moonraker", source_job_id="000123"))
        assert find_job_by_source(conn, "moonraker", "000123") is not None
        assert find_job_by_source(conn, "moonraker", "000999") is None
        assert find_job_by_source(conn, "octoprint", "000123") is None

    def test_the_same_source_job_cannot_be_stored_twice(self, conn):
        add_job(conn, Job(name="a", source="moonraker", source_job_id="dup"))
        with pytest.raises(sqlite3.IntegrityError):
            add_job(conn, Job(name="a again", source="moonraker", source_job_id="dup"))

    def test_manual_jobs_are_not_constrained_by_the_unique_index(self, conn):
        add_job(conn, Job(name="one"))
        add_job(conn, Job(name="two"))
        assert count_jobs(conn) == 2

    def test_listing_filters_by_date(self, conn):
        for month in ("2026-01", "2026-02", "2026-03"):
            add_job(conn, Job(name=month, started="%s-15T10:00:00+00:00" % month))
        assert len(list_jobs(conn)) == 3
        assert len(list_jobs(conn, since="2026-02")) == 2
        assert len(list_jobs(conn, until="2026-02-28")) == 2
        assert len(list_jobs(conn, since="2026-02", until="2026-02-28")) == 1

    def test_listing_filters_by_spool(self, conn, stored_spools):
        add_job(conn, Job(name="a", spool_id=stored_spools[0].id, filament_g=1.0))
        add_job(conn, Job(name="b", spool_id=stored_spools[1].id, filament_g=1.0))
        assert len(list_jobs(conn, spool_id=stored_spools[0].id)) == 1


class TestPrinters:
    def test_add_and_read_back(self, conn, voron):
        add_printer(conn, voron)
        loaded = get_printer(conn, "Voron 2.4")
        assert loaded.watts == pytest.approx(150.0)
        assert loaded.machine_cost_per_hour() == pytest.approx(0.30)

    def test_adding_the_same_name_updates_rather_than_duplicating(self, conn, voron):
        add_printer(conn, voron)
        add_printer(conn, Printer(name="Voron 2.4", watts=210.0))
        assert len(list_printers(conn)) == 1
        assert get_printer(conn, "Voron 2.4").watts == pytest.approx(210.0)

    def test_an_unknown_printer_reads_as_none(self, conn):
        assert get_printer(conn, "nope") is None


class TestSettings:
    def test_defaults_apply_to_an_empty_database(self, conn):
        settings = load_settings(conn)
        assert settings.currency == "USD"
        assert settings.tariff_per_kwh == 0.0
        assert settings.low_stock_pct == 15.0

    def test_save_and_load_round_trip(self, conn):
        save_settings(conn, Settings(tariff_per_kwh=0.2815, currency="EUR", default_watts=95.0))
        loaded = load_settings(conn)
        assert loaded.tariff_per_kwh == pytest.approx(0.2815)
        assert loaded.currency == "EUR"
        assert loaded.default_watts == pytest.approx(95.0)

    def test_a_corrupt_value_falls_back_to_the_default(self, conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('tariff_per_kwh', 'free')")
        conn.commit()
        assert load_settings(conn).tariff_per_kwh == 0.0

    def test_unknown_keys_are_ignored(self, conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('nonsense', '1')")
        conn.commit()
        load_settings(conn)
        assert "nonsense" in all_settings(conn)

    def test_saving_the_same_key_twice_updates_it(self, conn):
        save_settings(conn, Settings(currency="EUR"))
        save_settings(conn, Settings(currency="GBP"))
        assert load_settings(conn).currency == "GBP"


class TestSpoolReferences:
    def test_a_job_can_exist_without_a_spool(self, conn):
        job, shortfall = add_job(conn, Job(name="unattributed", filament_g=10.0))
        assert job.spool_id is None
        assert shortfall == 0.0

    def test_a_job_pointing_at_a_missing_spool_is_rejected(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            add_job(conn, Job(name="bad", spool_id=999, filament_g=1.0))
