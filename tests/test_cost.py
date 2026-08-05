"""The cost engine.

Every money assertion below is against a figure worked out by hand in the
comment above it, so a change in the implementation that quietly alters the
model fails here rather than silently reporting a different number.
"""

from __future__ import annotations

import dataclasses

import pytest

from spool.cost import (
    CostInputs,
    build_report,
    compute_job_cost,
    electricity_cost,
    format_money,
    round_money,
)
from spool.models import Job, Printer, Spool

# The scenario used throughout, chosen so every line is exact in decimal:
#   spool  1000 g for 25.00       -> 0.025 per gram
#   job     200 g over 4 hours
#   printer 150 W, 900.00 over 3000 h -> 0.30 per hour
#   tariff  0.30 per kWh
#
#   filament    200 * 0.025                 = 5.00
#   electricity 0.150 kW * 4 h * 0.30       = 0.18
#   machine     0.30 * 4 h                  = 1.20
#   total                                     6.38
EXPECTED_FILAMENT = 5.00
EXPECTED_ELECTRICITY = 0.18
EXPECTED_MACHINE = 1.20
EXPECTED_TOTAL = 6.38

INPUTS = CostInputs(tariff_per_kwh=0.30, currency="USD", default_watts=0.0)


@pytest.fixture
def scenario_spool() -> Spool:
    return Spool(material="PLA", spool_weight_g=1000.0, price=25.00, currency="USD")


@pytest.fixture
def scenario_job() -> Job:
    return Job(
        name="bracket",
        printer="Voron 2.4",
        spool_id=1,
        filament_g=200.0,
        duration_s=14400,
        status="success",
        started="2026-03-01T10:00:00+00:00",
    )


class TestRounding:
    def test_half_a_cent_rounds_up_not_to_even(self):
        # 0.125 is exactly representable in binary, so round() applies banker's
        # rounding and picks the even neighbour, 0.12. An invoice says 0.13.
        assert round_money(0.125) == 0.13
        assert round(0.125, 2) == 0.12

    def test_a_decimal_midpoint_rounds_the_way_a_person_reads_it(self):
        # 2.675 is stored as 2.67499999..., so round() gives 2.67. Working from
        # the decimal text the user typed gives the answer they expect.
        assert round_money(2.675) == 2.68
        assert round(2.675, 2) == 2.67

    def test_half_cents_round_away_from_zero_consistently(self):
        assert round_money(0.015) == 0.02
        assert round_money(0.025) == 0.03
        assert round_money(2.345) == 2.35

    def test_rounding_lands_exactly_on_cents(self):
        for raw in (0.1 + 0.2, 1.0 / 3.0, 6.375, 12.3456789):
            value = round_money(raw)
            assert abs(value * 100 - round(value * 100)) < 1e-9

    def test_format_money_shows_two_decimals_and_the_currency_code(self):
        assert format_money(6.375, "EUR") == "EUR 6.38"
        assert format_money(2, "USD") == "USD 2.00"


class TestElectricity:
    def test_known_kwh_calculation(self):
        # 150 W for 4 h = 0.6 kWh; at 0.30 per kWh that is 0.18.
        assert electricity_cost(14400, 150.0, 0.30) == pytest.approx(0.18)

    def test_one_kilowatt_for_one_hour_costs_exactly_the_tariff(self):
        assert electricity_cost(3600, 1000.0, 0.42) == pytest.approx(0.42)

    @pytest.mark.parametrize(
        "duration,watts,tariff",
        [(0, 150.0, 0.30), (14400, 0.0, 0.30), (14400, 150.0, 0.0), (-100, 150.0, 0.30)],
    )
    def test_missing_or_zero_inputs_cost_nothing(self, duration, watts, tariff):
        assert electricity_cost(duration, watts, tariff) == 0.0


class TestJobCost:
    def test_a_known_job_computes_the_expected_lines(self, scenario_job, scenario_spool, voron):
        cost = compute_job_cost(scenario_job, scenario_spool, voron, INPUTS)
        assert cost.filament_cost == pytest.approx(EXPECTED_FILAMENT)
        assert cost.electricity_cost == pytest.approx(EXPECTED_ELECTRICITY)
        assert cost.machine_cost == pytest.approx(EXPECTED_MACHINE)
        assert cost.total_cost == pytest.approx(EXPECTED_TOTAL)

    def test_the_displayed_money_matches_the_computed_money(
        self, scenario_job, scenario_spool, voron
    ):
        money = compute_job_cost(scenario_job, scenario_spool, voron, INPUTS).as_money()
        assert money["filament_cost"] == EXPECTED_FILAMENT
        assert money["electricity_cost"] == EXPECTED_ELECTRICITY
        assert money["machine_cost"] == EXPECTED_MACHINE
        assert money["total_cost"] == EXPECTED_TOTAL

    def test_the_displayed_lines_always_sum_to_the_displayed_total(
        self, scenario_spool, voron
    ):
        # Awkward numbers on purpose: each line lands mid-cent.
        job = Job(name="odd", filament_g=33.333, duration_s=4321, status="success")
        money = compute_job_cost(job, scenario_spool, voron, INPUTS).as_money()
        lines = money["filament_cost"] + money["electricity_cost"] + money["machine_cost"]
        assert money["total_cost"] == pytest.approx(lines, abs=1e-9)

    def test_a_successful_job_wastes_nothing(self, scenario_job, scenario_spool, voron):
        assert compute_job_cost(scenario_job, scenario_spool, voron, INPUTS).wasted_cost == 0.0

    def test_a_failure_at_04_costs_exactly_40_percent(self, scenario_job, scenario_spool, voron):
        full = compute_job_cost(scenario_job, scenario_spool, voron, INPUTS)
        scenario_job.status = "failed"
        scenario_job.failed_at_fraction = 0.4
        partial = compute_job_cost(scenario_job, scenario_spool, voron, INPUTS)

        assert partial.grams_used == pytest.approx(80.0)
        assert partial.total_cost == pytest.approx(full.total_cost * 0.4)
        assert partial.total_cost == pytest.approx(2.552)
        assert partial.filament_cost == pytest.approx(2.00)
        assert partial.electricity_cost == pytest.approx(0.072)
        assert partial.machine_cost == pytest.approx(0.48)

    def test_the_whole_cost_of_a_failure_is_waste(self, scenario_job, scenario_spool, voron):
        scenario_job.status = "failed"
        scenario_job.failed_at_fraction = 0.4
        cost = compute_job_cost(scenario_job, scenario_spool, voron, INPUTS)
        assert cost.wasted_cost == pytest.approx(cost.total_cost)

    def test_a_cancelled_job_is_waste_too(self, scenario_job, scenario_spool, voron):
        scenario_job.status = "cancelled"
        scenario_job.failed_at_fraction = 0.5
        cost = compute_job_cost(scenario_job, scenario_spool, voron, INPUTS)
        assert cost.wasted_cost == pytest.approx(cost.total_cost)
        assert cost.as_money()["wasted_cost"] == cost.as_money()["total_cost"]

    def test_a_job_with_no_spool_has_no_filament_cost_but_still_has_power(self, voron):
        job = Job(name="unknown filament", filament_g=200.0, duration_s=14400)
        cost = compute_job_cost(job, None, voron, INPUTS)
        assert cost.filament_cost == 0.0
        assert cost.electricity_cost == pytest.approx(EXPECTED_ELECTRICITY)
        assert cost.material == "unknown"

    def test_printer_watts_win_over_the_default(self, scenario_job, scenario_spool):
        inputs = CostInputs(tariff_per_kwh=0.30, default_watts=1000.0)
        cost = compute_job_cost(scenario_job, scenario_spool, Printer(name="p", watts=150.0), inputs)
        assert cost.electricity_cost == pytest.approx(EXPECTED_ELECTRICITY)

    def test_the_default_is_used_when_the_printer_is_unregistered(
        self, scenario_job, scenario_spool
    ):
        inputs = CostInputs(tariff_per_kwh=0.30, default_watts=150.0)
        cost = compute_job_cost(scenario_job, scenario_spool, None, inputs)
        assert cost.electricity_cost == pytest.approx(EXPECTED_ELECTRICITY)


class TestDegenerateJobs:
    def test_a_zero_duration_job_does_not_divide_by_zero(self, scenario_spool, voron):
        job = Job(name="instant", filament_g=10.0, duration_s=0)
        cost = compute_job_cost(job, scenario_spool, voron, INPUTS)
        assert cost.hours_used == 0.0
        assert cost.electricity_cost == 0.0
        assert cost.machine_cost == 0.0
        assert cost.filament_cost == pytest.approx(0.25)
        assert cost.cost_per_gram == pytest.approx(0.025)

    def test_a_zero_gram_job_does_not_divide_by_zero(self, scenario_spool, voron):
        job = Job(name="air", filament_g=0.0, duration_s=3600)
        cost = compute_job_cost(job, scenario_spool, voron, INPUTS)
        assert cost.filament_cost == 0.0
        assert cost.cost_per_gram == 0.0
        assert cost.total_cost == pytest.approx(0.045 + 0.30)

    def test_a_completely_empty_job_costs_nothing(self, scenario_spool):
        cost = compute_job_cost(Job(name="nothing"), scenario_spool, None, INPUTS)
        assert cost.total_cost == 0.0
        assert cost.cost_per_gram == 0.0

    def test_an_empty_report_has_no_failure_rate_and_no_average(self):
        report = build_report([], {}, {}, INPUTS)
        assert report.failure_rate == 0.0
        assert report.average_cost_per_gram == 0.0
        assert report.total_cost == 0.0


class TestReport:
    @pytest.fixture
    def report(self, scenario_spool, voron):
        petg = Spool(material="PETG", spool_weight_g=1000.0, price=20.00, id=2)
        spools = {1: scenario_spool, 2: petg}
        jobs = [
            Job(name="a", printer="Voron 2.4", spool_id=1, filament_g=200.0,
                duration_s=14400, status="success", started="2026-01-05T10:00:00+00:00"),
            Job(name="b", printer="Voron 2.4", spool_id=2, filament_g=100.0,
                duration_s=14400, status="failed", failed_at_fraction=0.4,
                started="2026-02-05T10:00:00+00:00"),
            Job(name="c", printer="Prusa MK4", spool_id=1, filament_g=50.0,
                duration_s=3600, status="success", started="2026-02-20T10:00:00+00:00"),
        ]
        return build_report(jobs, spools, {"Voron 2.4": voron}, INPUTS)

    def test_every_job_is_costed(self, report):
        assert report.total_jobs == 3
        assert len(report.job_costs) == 3

    def test_grams_reflect_the_partial_failure(self, report):
        # 200 used + 40 of a 100 g failure + 50 used = 290.
        assert report.total_grams == pytest.approx(290.0)

    def test_failure_rate_counts_failures_only(self, report):
        assert report.failed_jobs == 1
        assert report.successful_jobs == 2
        assert report.failure_rate == pytest.approx(1 / 3)

    def test_material_rollup_splits_by_spool_material(self, report):
        by_material = {r.key: r for r in report.rollup("material")}
        assert set(by_material) == {"PLA", "PETG"}
        assert by_material["PLA"].jobs == 2
        assert by_material["PLA"].grams == pytest.approx(250.0)
        assert by_material["PETG"].grams == pytest.approx(40.0)

    def test_printer_rollup_splits_by_printer_name(self, report):
        by_printer = {r.key: r for r in report.rollup("printer")}
        assert by_printer["Voron 2.4"].jobs == 2
        assert by_printer["Prusa MK4"].jobs == 1

    def test_month_rollup_is_in_chronological_order(self, report):
        assert [r.key for r in report.rollup("month")] == ["2026-01", "2026-02"]

    def test_month_rollup_sums_the_jobs_in_each_month(self, report):
        by_month = {r.key: r for r in report.rollup("month")}
        assert by_month["2026-01"].jobs == 1
        assert by_month["2026-02"].jobs == 2

    def test_spool_rollup_is_keyed_by_spool(self, report):
        keys = {r.key for r in report.rollup("spool")}
        assert keys == {"spool 1", "spool 2"}

    def test_rollup_totals_add_up_to_the_report_total(self, report):
        for key in ("material", "printer", "month", "spool", "status"):
            total = sum(r.total_cost for r in report.rollup(key))
            assert total == pytest.approx(report.total_cost), key

    def test_waste_is_only_the_failed_job(self, report):
        # Job b: 40 g of PETG at 0.02 = 0.80; 150 W for 1.6 h = 0.24 kWh at
        # 0.30 = 0.072; machine 0.30 * 1.6 = 0.48. Total 1.352.
        assert report.total_wasted_cost == pytest.approx(1.352)
        assert report.wasted_grams == pytest.approx(40.0)

    def test_report_lines_add_up_to_the_displayed_total(self, report):
        money = report.as_money()
        lines = money["filament_cost"] + money["electricity_cost"] + money["machine_cost"]
        assert money["total_cost"] == pytest.approx(lines, abs=1e-9)

    def test_a_wholly_failed_group_reports_waste_equal_to_its_total(self, report):
        petg = [r for r in report.rollup("material") if r.key == "PETG"][0]
        assert petg.as_money()["wasted_cost"] == petg.as_money()["total_cost"]

    def test_unknown_rollup_key_is_rejected_loudly(self, report):
        # A typo must not read as "that month had no spending".
        with pytest.raises(ValueError, match="unknown rollup key"):
            report.rollup("nonsense")


class TestCostInputs:
    def test_overrides_replace_only_what_is_given(self):
        base = CostInputs(tariff_per_kwh=0.30, currency="USD", default_watts=120.0)
        changed = base.replace(tariff_per_kwh=0.45, currency=None)
        assert changed.tariff_per_kwh == 0.45
        assert changed.currency == "USD"
        assert changed.default_watts == 120.0

    def test_inputs_are_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            CostInputs().tariff_per_kwh = 0.5  # type: ignore[misc]
