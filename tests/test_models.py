"""Unit conversion and the domain dataclasses.

The conversion tests check against values worked out by hand rather than
against the implementation, because a conversion that is wrong by a constant
factor still passes any test that reuses the same formula.
"""

from __future__ import annotations

import math

import pytest

from spool.models import (
    DENSITY_DEFAULTS,
    Job,
    Printer,
    Spool,
    density_for,
    is_known_material,
    length_to_mass_g,
    mass_to_length_mm,
    normalize_material,
    volume_cm3_to_mass_g,
)

# Hand calculation for one metre of 1.75 mm PLA at 1.24 g/cm3:
#   radius        = 1.75 / 2                  = 0.875 mm
#   cross section = pi * 0.875^2              = 2.405281875404685 mm2
#   volume        = 2.405281875... * 1000 mm  = 2405.281875404685 mm3
#   volume        = 2405.281875... / 1000     = 2.405281875404685 cm3
#   mass          = 2.405281875... * 1.24     = 2.9825495255018093 g
# which agrees with the roughly 3 g per metre figure every PLA datasheet gives.
PLA_175_ONE_METRE_G = 2.9825495255018093

# Same calculation at 2.85 mm:
#   radius        = 1.425 mm
#   cross section = pi * 1.425^2              = 6.379695... mm2
#   mass          = 6.379395... * 1.24        = 7.910451761922759 g
PLA_285_ONE_METRE_G = 7.910451761922759


class TestLengthToMass:
    def test_one_metre_of_175_pla_matches_hand_calculation(self):
        assert length_to_mass_g(1000.0, 1.75, 1.24) == pytest.approx(
            PLA_175_ONE_METRE_G, abs=1e-12
        )

    def test_one_metre_of_285_pla_matches_hand_calculation(self):
        assert length_to_mass_g(1000.0, 2.85, 1.24) == pytest.approx(
            PLA_285_ONE_METRE_G, abs=1e-12
        )

    def test_285_is_heavier_per_metre_than_175(self):
        # The area ratio is (2.85 / 1.75)^2 = 2.6522..., so the mass ratio must
        # be the same. A mix-up between radius and diameter would show here.
        ratio = length_to_mass_g(1000.0, 2.85, 1.24) / length_to_mass_g(1000.0, 1.75, 1.24)
        assert ratio == pytest.approx((2.85 / 1.75) ** 2, rel=1e-12)

    def test_mass_scales_linearly_with_length(self):
        single = length_to_mass_g(1000.0, 1.75, 1.24)
        assert length_to_mass_g(5000.0, 1.75, 1.24) == pytest.approx(single * 5.0, rel=1e-12)

    def test_mass_scales_linearly_with_density(self):
        pla = length_to_mass_g(1000.0, 1.75, 1.24)
        petg = length_to_mass_g(1000.0, 1.75, 1.27)
        assert petg / pla == pytest.approx(1.27 / 1.24, rel=1e-12)

    @pytest.mark.parametrize(
        "length,diameter,density",
        [(0.0, 1.75, 1.24), (-100.0, 1.75, 1.24), (1000.0, 0.0, 1.24), (1000.0, 1.75, 0.0)],
    )
    def test_non_positive_inputs_give_zero_not_an_exception(self, length, diameter, density):
        assert length_to_mass_g(length, diameter, density) == 0.0


class TestRoundTrip:
    @pytest.mark.parametrize("diameter", [1.75, 2.85])
    @pytest.mark.parametrize("density", [1.24, 1.27, 1.04])
    def test_length_to_mass_to_length_returns_the_original(self, diameter, density):
        original_mm = 12345.678
        mass = length_to_mass_g(original_mm, diameter, density)
        assert mass_to_length_mm(mass, diameter, density) == pytest.approx(
            original_mm, rel=1e-12
        )

    def test_mass_to_length_to_mass_returns_the_original(self):
        original_g = 247.5
        length = mass_to_length_mm(original_g, 1.75, 1.24)
        assert length_to_mass_g(length, 1.75, 1.24) == pytest.approx(original_g, rel=1e-12)

    def test_mass_to_length_of_zero_is_zero(self):
        assert mass_to_length_mm(0.0, 1.75, 1.24) == 0.0


class TestVolumeToMass:
    def test_one_cubic_centimetre_of_pla_weighs_its_density(self):
        assert volume_cm3_to_mass_g(1.0, 1.24) == pytest.approx(1.24)

    def test_slicer_volume_agrees_with_slicer_length(self):
        # PrusaSlicer writes both; they must describe the same filament.
        length_mm = 4321.0
        volume_cm3 = length_mm * math.pi * (1.75 / 2) ** 2 / 1000.0
        assert volume_cm3_to_mass_g(volume_cm3, 1.24) == pytest.approx(
            length_to_mass_g(length_mm, 1.75, 1.24), rel=1e-12
        )


class TestMaterials:
    def test_every_documented_density_is_present(self):
        assert set(DENSITY_DEFAULTS) == {"PLA", "PETG", "ABS", "ASA", "TPU"}

    @pytest.mark.parametrize(
        "material,expected",
        [("PLA", 1.24), ("PETG", 1.27), ("ABS", 1.04), ("ASA", 1.07), ("TPU", 1.21)],
    )
    def test_documented_densities_match_the_readme_table(self, material, expected):
        assert density_for(material) == expected

    def test_material_matching_is_case_insensitive(self):
        assert normalize_material("pla") == "PLA"
        assert density_for("petg") == 1.27

    def test_unknown_material_keeps_its_label_and_falls_back_to_pla_density(self):
        assert normalize_material("PLA-CF") == "PLA-CF"
        assert density_for("PLA-CF") == 1.24
        assert is_known_material("PLA-CF") is False

    def test_empty_material_becomes_other(self):
        assert normalize_material("   ") == "other"


class TestSpool:
    def test_price_per_gram_uses_the_as_new_weight(self, pla_spool: Spool):
        assert pla_spool.price_per_gram == pytest.approx(0.025)

    def test_a_new_spool_starts_full(self, pla_spool: Spool):
        assert pla_spool.remaining_g == 1000.0
        assert pla_spool.remaining_fraction == 1.0
        assert pla_spool.remaining_pct == 100.0

    def test_an_explicitly_empty_spool_stays_empty(self):
        # remaining_g=0 must not be mistaken for "unspecified".
        empty = Spool(material="PLA", spool_weight_g=1000.0, price=25.0, remaining_g=0.0)
        assert empty.remaining_g == 0.0
        assert empty.remaining_pct == 0.0

    def test_density_defaults_from_the_material(self):
        assert Spool(material="ABS", price=30.0).density_g_cm3 == 1.04

    def test_explicit_density_wins_over_the_default(self):
        assert Spool(material="PLA", price=30.0, density_g_cm3=1.31).density_g_cm3 == 1.31

    def test_remaining_fraction_is_clamped_to_one(self):
        over = Spool(material="PLA", spool_weight_g=1000.0, price=25.0, remaining_g=1200.0)
        assert over.remaining_fraction == 1.0

    def test_price_per_gram_of_a_weightless_spool_is_zero_not_an_error(self):
        weird = Spool(material="PLA", spool_weight_g=0.0, price=25.0)
        assert weird.price_per_gram == 0.0
        assert weird.remaining_fraction == 0.0

    def test_label_joins_brand_material_and_colour(self, pla_spool: Spool):
        assert pla_spool.label == "Testament PLA Signal Orange"

    def test_remaining_length_agrees_with_the_conversion(self, pla_spool: Spool):
        expected = mass_to_length_mm(1000.0, 1.75, 1.24)
        assert pla_spool.remaining_length_mm() == pytest.approx(expected)


class TestJob:
    def test_a_successful_job_uses_all_its_filament(self):
        job = Job(name="ok", filament_g=50.0, duration_s=3600, status="success")
        assert job.completed_fraction == 1.0
        assert job.grams_used() == 50.0
        assert job.hours_used() == pytest.approx(1.0)
        assert job.is_waste is False

    def test_a_failure_with_a_fraction_uses_only_that_fraction(self):
        job = Job(
            name="oops", filament_g=100.0, duration_s=14400, status="failed",
            failed_at_fraction=0.4,
        )
        assert job.grams_used() == pytest.approx(40.0)
        assert job.seconds_used() == pytest.approx(5760.0)
        assert job.is_waste is True

    def test_a_failure_with_no_fraction_is_assumed_total(self):
        job = Job(name="oops", filament_g=100.0, status="failed")
        assert job.completed_fraction == 1.0
        assert job.grams_used() == 100.0

    def test_the_fraction_is_clamped_into_range(self):
        assert Job(name="a", status="failed", failed_at_fraction=1.9).failed_at_fraction == 1.0
        assert Job(name="b", status="failed", failed_at_fraction=-0.5).failed_at_fraction == 0.0

    def test_an_unknown_status_is_treated_as_a_failure(self):
        assert Job(name="a", status="klippy_shutdown").status == "failed"

    def test_month_comes_from_the_start_timestamp(self):
        assert Job(name="a", started="2026-03-14T01:59:00+00:00").month == "2026-03"
        assert Job(name="a", started=None).month == "unknown"


class TestPrinter:
    def test_machine_cost_per_hour_is_straight_line_amortisation(self, voron: Printer):
        assert voron.machine_cost_per_hour() == pytest.approx(0.30)

    @pytest.mark.parametrize(
        "price,life", [(0.0, 3000.0), (900.0, 0.0), (0.0, 0.0), (-900.0, 3000.0)]
    )
    def test_incomplete_amortisation_inputs_give_zero(self, price, life):
        assert Printer(name="p", price=price, life_hours=life).machine_cost_per_hour() == 0.0
