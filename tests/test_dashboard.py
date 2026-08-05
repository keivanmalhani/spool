"""The self-contained HTML dashboard.

The "no external references" property is asserted mechanically rather than
described in a comment: the finished file is searched for the substring
``http``, which no inline-only document can contain. Every chart is also parsed
as XML, which catches an unbalanced tag or an unescaped ampersand that a
browser would silently paper over.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from spool.cost import CostInputs, build_report
from spool.dashboard import (
    _nice_ceiling,
    bar_chart_svg,
    category_colors,
    donut_svg,
    esc,
    month_label,
    render_dashboard,
    write_dashboard,
)
from spool.models import Job, Spool

INPUTS = CostInputs(tariff_per_kwh=0.30, currency="USD", default_watts=150.0)

SVG_CHUNK = re.compile(r"<svg\b.*?</svg>", re.DOTALL)


@pytest.fixture
def spools() -> list[Spool]:
    return [
        Spool(material="PLA", brand="Testament", color="Signal Orange",
              spool_weight_g=1000.0, price=25.00, remaining_g=640.0, id=1),
        Spool(material="PETG", brand="Overture", color="Grey",
              spool_weight_g=1000.0, price=20.00, remaining_g=90.0, id=2),
        Spool(material="ABS", brand="Retired", color="Black",
              spool_weight_g=1000.0, price=22.00, remaining_g=0.0, archived=True, id=3),
    ]


@pytest.fixture
def report(spools):
    lookup = {s.id: s for s in spools}
    jobs = [
        Job(name="benchy.gcode", printer="Voron 2.4", spool_id=1, filament_g=12.9,
            duration_s=3723, status="success", started="2026-01-08T18:20:00+00:00"),
        Job(name="bracket.gcode", printer="Voron 2.4", spool_id=2, filament_g=100.0,
            duration_s=14400, status="failed", failed_at_fraction=0.4,
            started="2026-02-11T09:00:00+00:00"),
        Job(name="planter.gcode", printer="Prusa MK4", spool_id=1, filament_g=148.2,
            duration_s=27180, status="success", started="2026-03-14T09:00:00+00:00"),
    ]
    return build_report(jobs, lookup, {}, INPUTS)


@pytest.fixture
def html(report, spools) -> str:
    return render_dashboard(report, spools, generated_at="2026-04-01 09:00")


class TestSelfContained:
    def test_there_is_no_external_script(self, html):
        assert "<script src" not in html
        assert "<script" in html  # the inline one is still there

    def test_there_is_no_url_anywhere_in_the_document(self, html):
        # A single "http" would mean a CDN, a web font, a tracking pixel or an
        # xmlns. None of those belong in a file meant to work offline.
        assert "http" not in html
        assert "//cdn" not in html

    def test_there_is_no_external_stylesheet(self, html):
        assert "<link" not in html
        assert "@import" not in html

    def test_styles_and_scripts_are_inline(self, html):
        assert "<style>" in html
        assert "</style>" in html
        assert "</script>" in html

    def test_the_written_file_is_also_clean(self, tmp_path, report, spools):
        path = write_dashboard(tmp_path / "d.html", report, spools)
        text = path.read_text(encoding="utf-8")
        assert "http" not in text
        assert "<script src" not in text


class TestStructure:
    def test_it_is_a_complete_html_document(self, html):
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")
        assert "<meta charset=\"utf-8\" />" in html

    def test_writing_produces_a_file(self, tmp_path, report, spools):
        path = write_dashboard(tmp_path / "sub" / "dash.html", report, spools)
        assert path.exists()
        assert path.stat().st_size > 5000

    def test_the_output_is_ascii(self, tmp_path, report, spools):
        path = write_dashboard(tmp_path / "d.html", report, spools)
        path.read_bytes().decode("ascii")  # raises if a smart quote crept in

    def test_the_generated_timestamp_is_shown(self, html):
        assert "2026-04-01 09:00" in html


class TestContent:
    def test_active_spool_names_are_present(self, html):
        assert "Testament PLA Signal Orange" in html
        assert "Overture PETG Grey" in html

    def test_the_caller_decides_which_spools_are_shown(self, report, spools):
        # The CLI passes archived spools only for `spool dashboard --all`, so
        # the renderer must show exactly what it is handed.
        live_only = render_dashboard(
            report, [s for s in spools if not s.archived], generated_at="x"
        )
        assert "Retired ABS Black" not in live_only

        with_archived = render_dashboard(report, spools, generated_at="x")
        assert "Retired ABS Black" in with_archived
        assert "archived" in with_archived

    def test_an_archived_spool_is_not_flagged_as_low_stock(self, report, spools):
        # It is at zero on purpose; telling the user to reorder it is noise.
        only_archived = [s for s in spools if s.archived]
        html = render_dashboard(report, only_archived, generated_at="x")
        assert ">LOW<" not in html
        assert "0 spool(s), 0 low" in html

    def test_job_names_appear_in_the_table(self, html):
        assert "benchy.gcode" in html
        assert "bracket.gcode" in html
        assert "planter.gcode" in html

    def test_the_summary_strip_is_present(self, html):
        for label in ("Total spent", "Filament printed", "Printer time",
                      "Failure rate", "Inventory value"):
            assert label in html

    def test_a_low_spool_is_flagged(self, html):
        # PETG is at 9 percent, under the 15 percent threshold.
        assert "LOW" in html

    def test_nothing_is_flagged_when_everything_is_full(self, report):
        full = [Spool(material="PLA", brand="Full", spool_weight_g=1000.0,
                      price=25.0, remaining_g=1000.0, id=1)]
        assert ">LOW<" not in render_dashboard(report, full, generated_at="x")

    def test_the_failure_rate_is_shown(self, html):
        assert "33%" in html  # one failure out of three jobs

    def test_the_cost_model_inputs_are_disclosed(self, html):
        assert "0.3000" in html  # the tariff
        assert "per kWh" in html


class TestSvg:
    def test_every_svg_chunk_parses_as_xml(self, html):
        chunks = SVG_CHUNK.findall(html)
        assert len(chunks) >= 2, "expected at least the bar chart and the donut"
        for chunk in chunks:
            ET.fromstring(chunk)

    def test_the_charts_have_accessible_titles(self, html):
        chunks = SVG_CHUNK.findall(html)
        titles = [ET.fromstring(c).find("title") for c in chunks]
        assert all(t is not None and t.text for t in titles)

    def test_the_bar_chart_has_one_group_per_month(self, report):
        svg = bar_chart_svg(report.rollup("month"), currency="USD")
        root = ET.fromstring(svg)
        assert len(root.findall("g")) == 3  # January, February, March

    def test_the_bar_chart_labels_the_months(self, report):
        svg = bar_chart_svg(report.rollup("month"), currency="USD")
        texts = [t.text for t in ET.fromstring(svg).findall("text")]
        assert "Jan" in texts and "Feb" in texts and "Mar" in texts

    def test_taller_bars_mean_more_money(self, report):
        svg = bar_chart_svg(report.rollup("month"), currency="USD")
        root = ET.fromstring(svg)
        heights = []
        for group in root.findall("g"):
            heights.append(sum(float(r.get("height")) for r in group.findall("rect")))
        totals = [r.total_cost for r in report.rollup("month")]
        assert (heights[0] < heights[1]) == (totals[0] < totals[1])

    def test_the_donut_has_one_segment_per_material_plus_a_track(self, report):
        svg = donut_svg(report.rollup("material"), currency="USD")
        circles = ET.fromstring(svg).findall("circle")
        assert len(circles) == 3  # background track plus PLA and PETG

    def test_the_donut_segments_fill_the_circumference(self, report):
        svg = donut_svg(report.rollup("material"), currency="USD")
        circles = [c for c in ET.fromstring(svg).findall("circle") if c.get("stroke-dasharray")]
        radius = float(circles[0].get("r"))
        circumference = 2 * 3.141592653589793 * radius
        drawn = sum(float(c.get("stroke-dasharray").split()[0]) for c in circles)
        # Each segment gives up a small gap for separation.
        assert drawn == pytest.approx(circumference, rel=0.05)

    def test_empty_data_still_produces_valid_svg(self):
        for svg in (bar_chart_svg([]), donut_svg([])):
            root = ET.fromstring(svg)
            assert root.tag == "svg"
            assert "http" not in svg

    def test_a_dashboard_with_no_data_still_renders(self):
        empty = build_report([], {}, {}, INPUTS)
        html = render_dashboard(empty, [], generated_at="x")
        assert "http" not in html
        for chunk in SVG_CHUNK.findall(html):
            ET.fromstring(chunk)


class TestEscaping:
    def test_html_in_user_data_is_escaped(self, report):
        nasty = [
            Spool(material="PLA", brand='<script>alert("x")</script>',
                  color="A & B", spool_weight_g=1000.0, price=25.0, id=1)
        ]
        html = render_dashboard(report, nasty, generated_at="x")
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html
        assert "A &amp; B" in html

    def test_an_ampersand_in_a_job_name_does_not_break_the_svg(self, spools):
        lookup = {s.id: s for s in spools}
        jobs = [
            Job(name="nuts & bolts", spool_id=1, filament_g=10.0, duration_s=600,
                started="2026-01-02T00:00:00+00:00")
        ]
        report = build_report(jobs, lookup, {}, INPUTS)
        html = render_dashboard(report, spools, generated_at="x")
        assert "nuts &amp; bolts" in html
        for chunk in SVG_CHUNK.findall(html):
            ET.fromstring(chunk)

    def test_esc_handles_quotes_and_none(self):
        assert esc('a "b" c') == "a &quot;b&quot; c"
        assert esc(None) == ""


class TestChartMath:
    @pytest.mark.parametrize(
        "value,expected",
        [(0.0, 1.0), (0.9, 1.0), (1.0, 1.0), (12.0, 15.0), (23.0, 25.0), (260.0, 300.0)],
    )
    def test_axis_ceilings_are_readable_numbers(self, value, expected):
        assert _nice_ceiling(value) == pytest.approx(expected)

    def test_the_ceiling_is_never_below_the_value(self):
        for value in (0.01, 3.7, 88.0, 1234.5, 99999.0):
            assert _nice_ceiling(value) >= value

    def test_month_labels_are_short(self):
        assert month_label("2026-03") == "Mar"
        assert month_label("2026-12") == "Dec"
        assert month_label("unknown") == "unknown"

    def test_category_colours_are_one_per_category(self):
        assert len(category_colors(4)) == 4
        assert category_colors(0) == []

    def test_category_colours_are_all_distinct_hex(self):
        colors = category_colors(6)
        assert len(set(colors)) == 6
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in colors)
