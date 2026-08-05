"""G-code parsing, one test group per slicer flavour.

The fixtures live in conftest.py so the exact comment grammar each slicer
emits is written out in full rather than hidden behind a mock.
"""

from __future__ import annotations

import tracemalloc

import pytest

from spool.gcode import (
    FLAVOR_UNKNOWN,
    GcodeInfo,
    format_duration,
    parse_duration,
    parse_file,
    parse_lines,
)
from spool.models import length_to_mass_g
from tests.conftest import (
    bambustudio_gcode,
    bare_gcode,
    cura_gcode,
    cura_multi_gcode,
    orcaslicer_gcode,
    prusaslicer_gcode,
    superslicer_gcode,
)


class TestDurationParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1h 2m 3s", 3723),
            ("1h2m3s", 3723),
            ("45m", 2700),
            ("90s", 90),
            ("2h", 7200),
            ("1d 4h 30m 10s", 102610),
            ("3723", 3723),
            ("3723.0", 3723),
        ],
    )
    def test_known_forms(self, text, expected):
        assert parse_duration(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", "unknown", None])
    def test_unparseable_input_returns_none(self, text):
        assert parse_duration(text) is None

    @pytest.mark.parametrize(
        "seconds,expected",
        [(45, "45s"), (750, "12m 30s"), (3723, "1h 02m"), (102610, "1d 4h 30m")],
    )
    def test_formatting_is_readable(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestPrusaSlicer:
    @pytest.fixture
    def info(self, write_gcode) -> GcodeInfo:
        return parse_file(write_gcode("part.gcode", prusaslicer_gcode()))

    def test_flavor_is_detected(self, info):
        assert info.slicer == "prusaslicer"

    def test_all_three_filament_figures_are_read(self, info):
        assert info.filament_g == pytest.approx(12.88)
        assert info.filament_mm == pytest.approx(4321.0)
        assert info.filament_cm3 == pytest.approx(10.39)

    def test_estimated_time_is_seconds(self, info):
        assert info.duration_s == 3723

    def test_filament_profile_is_read(self, info):
        assert info.filament_type == "PLA"
        assert info.diameter_mm == pytest.approx(1.75)
        assert info.density_g_cm3 == pytest.approx(1.24)

    def test_grams_from_the_slicer_win_over_derivation(self, info):
        assert info.resolve_mass_g(diameter_mm=1.75, density_g_cm3=1.24) == pytest.approx(12.88)

    def test_the_job_name_defaults_to_the_file_stem(self, info):
        assert info.name == "part"


class TestSuperSlicer:
    @pytest.fixture
    def info(self, write_gcode) -> GcodeInfo:
        return parse_file(write_gcode("big.gcode", superslicer_gcode()))

    def test_flavor_is_detected(self, info):
        assert info.slicer == "superslicer"

    def test_multi_day_estimate_is_parsed(self, info):
        # 1d 4h 30m 10s = 86400 + 14400 + 1800 + 10
        assert info.duration_s == 102610

    def test_filament_figures(self, info):
        assert info.filament_g == pytest.approx(268.9)
        assert info.filament_mm == pytest.approx(90210.5)
        assert info.filament_type == "PETG"


class TestOrcaSlicer:
    @pytest.fixture
    def info(self, write_gcode) -> GcodeInfo:
        return parse_file(write_gcode("orca.gcode", orcaslicer_gcode()))

    def test_flavor_is_detected(self, info):
        assert info.slicer == "orcaslicer"

    def test_total_filament_used_is_read(self, info):
        assert info.filament_g == pytest.approx(45.51)
        assert info.filament_cm3 == pytest.approx(36.7)

    def test_the_longer_of_model_and_total_time_is_kept(self, info):
        # model 3h 12m 40s = 11560, total 3h 20m 5s = 12005. The machine is
        # busy for the total, so that is the one that matters.
        assert info.duration_s == 12005


class TestBambuStudio:
    @pytest.fixture
    def info(self, write_gcode) -> GcodeInfo:
        return parse_file(write_gcode("bambu.gcode", bambustudio_gcode()))

    def test_flavor_is_detected(self, info):
        assert info.slicer == "bambustudio"

    def test_multi_extruder_values_are_summed(self, info):
        assert info.filament_g == pytest.approx(13.50)
        assert info.filament_mm == pytest.approx(4500.0)

    def test_total_estimated_time_is_parsed(self, info):
        assert info.duration_s == 7500

    def test_the_first_filament_type_is_kept(self, info):
        assert info.filament_type == "PLA"


class TestCura:
    @pytest.fixture
    def info(self, write_gcode) -> GcodeInfo:
        return parse_file(write_gcode("cura.gcode", cura_gcode()))

    def test_flavor_is_detected(self, info):
        assert info.slicer == "cura"

    def test_metres_are_converted_to_millimetres(self, info):
        # ";Filament used: 4.321m" is METRES. Reading it as millimetres would
        # under-report this print by a factor of a thousand.
        assert info.filament_mm == pytest.approx(4321.0)

    def test_cura_gives_no_weight(self, info):
        assert info.filament_g is None

    def test_time_is_bare_seconds(self, info):
        assert info.duration_s == 3723

    def test_mass_is_derived_from_length_when_no_weight_is_given(self, info):
        expected = length_to_mass_g(4321.0, 1.75, 1.24)
        assert info.resolve_mass_g(diameter_mm=1.75, density_g_cm3=1.24) == pytest.approx(expected)
        assert expected == pytest.approx(12.887, abs=0.001)

    def test_mass_cannot_be_derived_without_a_diameter(self, info):
        assert info.resolve_mass_g() is None

    def test_multi_extruder_lengths_are_summed(self, write_gcode):
        info = parse_file(write_gcode("cura2.gcode", cura_multi_gcode()))
        assert info.filament_mm == pytest.approx(4500.0)
        assert info.duration_s == 7200


class TestNoMetadata:
    @pytest.fixture
    def info(self, write_gcode) -> GcodeInfo:
        return parse_file(write_gcode("bare.gcode", bare_gcode()))

    def test_parsing_does_not_crash(self, info):
        assert isinstance(info, GcodeInfo)

    def test_nothing_is_reported_as_found(self, info):
        assert info.found_anything() is False
        assert info.fields_found == []

    def test_every_field_is_none(self, info):
        assert info.filament_g is None
        assert info.filament_mm is None
        assert info.filament_cm3 is None
        assert info.duration_s is None

    def test_the_flavour_is_unknown(self, info):
        assert info.slicer == FLAVOR_UNKNOWN

    def test_mass_cannot_be_resolved(self, info):
        assert info.resolve_mass_g(diameter_mm=1.75, density_g_cm3=1.24) is None

    def test_the_file_was_actually_read(self, info):
        assert info.lines_scanned > 40

    def test_the_summary_says_so_instead_of_crashing(self, info):
        assert "unknown" in info.summary()


class TestMissingFile:
    def test_a_missing_file_raises_a_clear_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_file(tmp_path / "does-not-exist.gcode")


class TestStreaming:
    """A 200 MB gcode file is normal. It must never be read into memory."""

    @pytest.fixture
    def big_file(self, tmp_path):
        path = tmp_path / "big.gcode"
        with path.open("w", encoding="utf-8") as handle:
            handle.write("; generated by PrusaSlicer 2.8.0 on 2026-04-01\n")
            for i in range(50_000):
                handle.write("G1 X%0.3f Y%0.3f E%0.5f F1800\n" % (i % 200, i % 190, i * 0.00013))
            # The estimates live in the footer, so the whole file gets scanned.
            handle.write("; filament used [g] = 987.65\n")
            handle.write("; estimated printing time (normal mode) = 2d 3h 4m 5s\n")
        return path

    def test_a_fifty_thousand_line_file_parses_correctly(self, big_file):
        info = parse_file(big_file)
        # 1 banner + 50000 moves + 2 footer comments.
        assert info.lines_scanned == 50_003
        assert info.filament_g == pytest.approx(987.65)
        assert info.duration_s == 183845
        assert info.slicer == "prusaslicer"

    def test_peak_memory_stays_far_below_the_file_size(self, big_file):
        size = big_file.stat().st_size
        assert size > 1_000_000, "the fixture needs to be big enough to be meaningful"

        tracemalloc.start()
        try:
            parse_file(big_file)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # Reading the file into a string would allocate at least `size`. A
        # streaming read holds one buffered chunk at a time.
        assert peak < size / 8, "peak %d bytes for a %d byte file looks buffered" % (peak, size)


class TestParseLines:
    def test_an_arbitrary_iterable_can_be_parsed(self):
        info = parse_lines(["; filament used [g] = 4.20\n", "G1 X1 Y1\n"])
        assert info.filament_g == pytest.approx(4.20)
        assert info.lines_scanned == 2

    def test_prusa_style_keys_imply_the_prusa_family_without_a_banner(self):
        info = parse_lines(["; filament used [g] = 4.20"])
        assert info.slicer == "prusaslicer"

    def test_leading_whitespace_before_a_comment_is_tolerated(self):
        info = parse_lines(["   ; filament used [g] = 1.50"])
        assert info.filament_g == pytest.approx(1.50)

    def test_a_malformed_value_is_skipped_rather_than_crashing(self):
        info = parse_lines(["; filament used [g] = n/a", "; filament used [mm] = 100.0"])
        assert info.filament_g is None
        assert info.filament_mm == pytest.approx(100.0)
