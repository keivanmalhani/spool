"""Writes a single self-contained HTML dashboard.

One file, no dependencies, no network. Inline CSS, inline JavaScript, and
charts drawn as inline SVG whose geometry is computed here in Python. There is
no chart library, no web font, no CDN and no analytics beacon: the output opens
correctly on a machine that has never been online, and it cannot phone home
about what you print.

That is not a stunt. A dashboard you can email to yourself, drop on a USB
stick, or open from a Raspberry Pi with no internet is more useful than one
that needs a build step, and the absence of external references is mechanically
verifiable, which the test suite does.

Implementation note: the inline ``<svg>`` elements deliberately carry no
``xmlns`` attribute. HTML5 puts inline SVG in the SVG namespace automatically,
so it is unnecessary, and leaving it out means the finished file contains no
URL of any kind. That turns "no external references" into a property a test can
assert by searching for a substring.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .cost import CostReport, Rollup, format_money, round_money
from .gcode import format_duration
from .models import Spool

# --------------------------------------------------------------------------
# Design tokens
# --------------------------------------------------------------------------

BG = "#0d0f14"
PANEL = "#151922"
PANEL_2 = "#1b202b"
BORDER = "#252c3a"
TEXT = "#e7eaf0"
MUTED = "#8b94a7"
DIM = "#5d6779"
ACCENT = "#ff8a3d"
GOOD = "#4fb783"

#: Everything low-stock, failed or over-budget uses the accent. One accent
#: colour, used only where it means something, is what stops a dashboard from
#: looking like a jellybean jar.
LOW_STOCK_PCT = 15.0


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _mix(color_a: str, color_b: str, t: float) -> str:
    """Blend two hex colours; ``t=0`` is all A, ``t=1`` is all B."""
    a, b = _hex_to_rgb(color_a), _hex_to_rgb(color_b)
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def category_colors(count: int) -> list[str]:
    """A ramp of ``count`` colours derived from the single accent.

    Rather than pick a second and third hue, categories are the accent walked
    toward the panel background. The result stays visibly one palette and
    degrades gracefully when a user has nine materials.
    """
    if count <= 0:
        return []
    if count == 1:
        return [ACCENT]
    return [_mix(ACCENT, "#4a5568", i / (count - 1) * 0.82) for i in range(count)]


def esc(value: object) -> str:
    """HTML-escape any value, including quotes, for safe attribute use.

    Every string that reaches the template goes through here. Spool names,
    printer names and job names are user data, and user data in a document is
    an injection until it has been escaped.
    """
    return html.escape(str(value if value is not None else ""), quote=True)


# --------------------------------------------------------------------------
# SVG charts
# --------------------------------------------------------------------------


def _nice_ceiling(value: float) -> float:
    """Round a maximum up to a readable axis top (1, 2, 2.5 or 5 times 10^n)."""
    if value <= 0:
        return 1.0
    exponent = 0
    scaled = float(value)
    while scaled >= 10:
        scaled /= 10.0
        exponent += 1
    while scaled < 1:
        scaled *= 10.0
        exponent -= 1
    for step in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0):
        if scaled <= step:
            return step * (10.0**exponent)
    return 10.0 * (10.0**exponent)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_label(key: str) -> str:
    """Turn ``2026-03`` into ``Mar``, leaving anything else alone."""
    parts = str(key).split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        index = int(parts[1])
        if 1 <= index <= 12:
            return _MONTHS[index - 1]
    return str(key)[:7]


def bar_chart_svg(
    rollups: Sequence[Rollup],
    *,
    width: int = 660,
    height: int = 240,
    currency: str = "USD",
) -> str:
    """Hand-drawn monthly spend chart.

    Total spend is the full bar; the portion lost to failed and cancelled
    prints is drawn on top in the accent, so a bad month is visible without
    reading a single number.
    """
    if not rollups:
        return _empty_svg(width, height, "No jobs recorded yet")

    pad_l, pad_r, pad_t, pad_b = 58, 14, 18, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [r.total_cost for r in rollups]
    ceiling = _nice_ceiling(max(values) if values else 1.0)

    slot = plot_w / max(1, len(rollups))
    bar_w = max(6.0, min(54.0, slot * 0.58))

    parts: list[str] = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" height="%d" role="img">' % (width, height, height)]
    parts.append("<title>Cost per month</title>")

    # Horizontal gridlines with value labels.
    for step in range(5):
        value = ceiling * step / 4.0
        y = pad_t + plot_h - (plot_h * step / 4.0)
        parts.append(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1" />'
            % (pad_l, y, width - pad_r, y, BORDER)
        )
        parts.append(
            '<text x="%.1f" y="%.1f" fill="%s" font-size="10" text-anchor="end" '
            'font-family="monospace">%s</text>' % (pad_l - 8, y + 3.5, DIM, esc("%.0f" % value))
        )

    for index, rollup in enumerate(rollups):
        x = pad_l + slot * index + (slot - bar_w) / 2.0
        total_h = (rollup.total_cost / ceiling) * plot_h if ceiling else 0.0
        waste_h = (rollup.wasted_cost / ceiling) * plot_h if ceiling else 0.0
        waste_h = min(waste_h, total_h)
        good_h = max(0.0, total_h - waste_h)

        # One group per bar so the hover tooltip covers the whole column.
        parts.append("<g>")
        parts.append(
            "<title>%s</title>"
            % esc(
                "%s: %s (%s wasted)"
                % (
                    rollup.key,
                    format_money(rollup.total_cost, currency),
                    format_money(rollup.wasted_cost, currency),
                )
            )
        )
        if good_h > 0:
            parts.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="%s" />'
                % (x, pad_t + plot_h - good_h, bar_w, good_h, _mix(ACCENT, PANEL, 0.55))
            )
        if waste_h > 0:
            parts.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="%s" />'
                % (x, pad_t + plot_h - total_h, bar_w, waste_h, ACCENT)
            )
        parts.append("</g>")
        parts.append(
            '<text x="%.1f" y="%.1f" fill="%s" font-size="10" text-anchor="middle">%s</text>'
            % (x + bar_w / 2.0, height - 12, MUTED, esc(month_label(rollup.key)))
        )
        if total_h > 16:
            parts.append(
                '<text x="%.1f" y="%.1f" fill="%s" font-size="10" text-anchor="middle" '
                'font-family="monospace">%s</text>'
                % (
                    x + bar_w / 2.0,
                    pad_t + plot_h - total_h - 5,
                    TEXT,
                    esc("%.0f" % round_money(rollup.total_cost)),
                )
            )

    # Baseline.
    parts.append(
        '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1" />'
        % (pad_l, pad_t + plot_h, width - pad_r, pad_t + plot_h, BORDER)
    )
    parts.append("</svg>")
    return "".join(parts)


def donut_svg(rollups: Sequence[Rollup], *, size: int = 190, currency: str = "USD") -> str:
    """Material breakdown as a donut, segments computed with dash offsets.

    ``stroke-dasharray`` on a circle is the least error-prone way to draw a
    donut: one circumference, one running offset, no arc-flag arithmetic.
    """
    total = sum(r.total_cost for r in rollups)
    if not rollups or total <= 0:
        return _empty_svg(size, size, "No cost data")

    radius = size / 2.0 - 22.0
    circumference = 2.0 * 3.141592653589793 * radius
    center = size / 2.0
    colors = category_colors(len(rollups))

    parts = ['<svg class="donut" viewBox="0 0 %d %d" width="%d" height="%d" role="img">' % (size, size, size, size)]
    parts.append("<title>Cost by material</title>")
    parts.append(
        '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="%s" stroke-width="22" />'
        % (center, center, radius, PANEL_2)
    )

    offset = 0.0
    for index, rollup in enumerate(rollups):
        fraction = rollup.total_cost / total
        length = circumference * fraction
        # A hairline gap between segments reads as separation without a border.
        gap = min(2.0, length / 3.0)
        parts.append(
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="%s" stroke-width="22" '
            'stroke-dasharray="%.3f %.3f" stroke-dashoffset="%.3f" '
            'transform="rotate(-90 %.1f %.1f)"><title>%s</title></circle>'
            % (
                center,
                center,
                radius,
                colors[index],
                max(0.0, length - gap),
                circumference - max(0.0, length - gap),
                -offset,
                center,
                center,
                esc(
                    "%s: %s (%.0f%%)"
                    % (rollup.key, format_money(rollup.total_cost, currency), fraction * 100.0)
                ),
            )
        )
        offset += length

    parts.append(
        '<text x="%.1f" y="%.1f" fill="%s" font-size="17" text-anchor="middle" '
        'font-family="monospace">%s</text>'
        % (center, center - 1, TEXT, esc("%.0f" % round_money(total)))
    )
    parts.append(
        '<text x="%.1f" y="%.1f" fill="%s" font-size="9" text-anchor="middle">%s</text>'
        % (center, center + 14, DIM, esc(currency + " total"))
    )
    parts.append("</svg>")
    return "".join(parts)


def _empty_svg(width: int, height: int, message: str) -> str:
    """A valid, well-formed placeholder so empty states are not blank boxes."""
    return (
        '<svg class="chart" viewBox="0 0 %d %d" width="100%%" height="%d" role="img">'
        '<title>%s</title>'
        '<text x="%.1f" y="%.1f" fill="%s" font-size="12" text-anchor="middle">%s</text>'
        "</svg>"
        % (width, height, height, esc(message), width / 2.0, height / 2.0, DIM, esc(message))
    )


# --------------------------------------------------------------------------
# HTML fragments
# --------------------------------------------------------------------------


def _stat(label: str, value: str, sub: str = "", accent: bool = False) -> str:
    cls = "stat accent" if accent else "stat"
    sub_html = '<div class="stat-sub">%s</div>' % esc(sub) if sub else ""
    return (
        '<div class="%s"><div class="stat-label">%s</div>'
        '<div class="stat-value">%s</div>%s</div>'
        % (cls, esc(label), esc(value), sub_html)
    )


def _spool_card(spool: Spool, low_stock_pct: float) -> str:
    pct = spool.remaining_pct
    # An archived spool is finished on purpose, so flagging it as low stock
    # would be noise rather than a signal to order more.
    low = pct < low_stock_pct and not spool.archived
    fill = ACCENT if low else _mix(ACCENT, "#4a5568", 0.45)
    if spool.archived:
        badge = '<span class="chip">archived</span>'
    elif low:
        badge = '<span class="badge">LOW</span>'
    else:
        badge = ""
    value_left = spool.remaining_g * spool.price_per_gram
    return """      <article class="card%(lowcls)s">
        <header>
          <span class="chip">%(material)s</span>
          %(badge)s
        </header>
        <h3>%(label)s</h3>
        <div class="meter"><span style="width:%(pct).1f%%;background:%(fill)s"></span></div>
        <div class="meter-row"><span class="num">%(remaining).0f g</span><span class="muted">of %(total).0f g</span><span class="num pct">%(pct).0f%%</span></div>
        <dl>
          <div><dt>Diameter</dt><dd class="num">%(dia).2f mm</dd></div>
          <div><dt>Density</dt><dd class="num">%(density).2f g/cm3</dd></div>
          <div><dt>Cost per gram</dt><dd class="num">%(pergram).4f</dd></div>
          <div><dt>Value left</dt><dd class="num">%(valueleft)s</dd></div>
        </dl>
      </article>
""" % {
        "lowcls": " low" if low else "",
        "material": esc(spool.material),
        "badge": badge,
        "label": esc(spool.label),
        "pct": pct,
        "fill": fill,
        "remaining": spool.remaining_g,
        "total": spool.spool_weight_g,
        "dia": spool.diameter_mm,
        "density": spool.density_g_cm3,
        "pergram": spool.price_per_gram,
        "valueleft": esc(format_money(value_left, spool.currency)),
    }


def _legend(rollups: Sequence[Rollup], currency: str) -> str:
    if not rollups:
        return '<p class="muted">No material data.</p>'
    total = sum(r.total_cost for r in rollups) or 1.0
    colors = category_colors(len(rollups))
    rows = []
    for index, rollup in enumerate(rollups):
        rows.append(
            '<li><span class="dot" style="background:%s"></span>'
            '<span class="legend-key">%s</span>'
            '<span class="num legend-val">%s</span>'
            '<span class="num legend-pct">%.0f%%</span></li>'
            % (
                colors[index],
                esc(rollup.key),
                esc(format_money(rollup.total_cost, currency)),
                rollup.total_cost / total * 100.0,
            )
        )
    return '<ul class="legend">%s</ul>' % "".join(rows)


def _jobs_table(report: CostReport, limit: int) -> str:
    costs = list(reversed(report.job_costs))[:limit]
    if not costs:
        return '<p class="muted">No jobs recorded yet. Try: spool import model.gcode --spool 1</p>'
    rows = []
    for cost in costs:
        money = cost.as_money()
        status_class = {
            "success": "ok",
            "failed": "bad",
            "cancelled": "meh",
        }.get(cost.status, "meh")
        rows.append(
            "<tr>"
            '<td class="num">%s</td>'
            "<td>%s</td>"
            "<td>%s</td>"
            "<td>%s</td>"
            '<td><span class="pill %s">%s</span></td>'
            '<td class="num">%.1f</td>'
            '<td class="num">%s</td>'
            '<td class="num strong">%.2f</td>'
            "</tr>"
            % (
                esc(cost.job_id if cost.job_id is not None else "-"),
                esc(cost.name),
                esc(cost.printer or "-"),
                esc(cost.material),
                status_class,
                esc(cost.status),
                cost.grams_used,
                esc(format_duration(cost.hours_used * 3600.0)),
                money["total_cost"],
            )
        )
    return """<table id="jobs-table">
        <thead><tr><th>ID</th><th>Job</th><th>Printer</th><th>Material</th><th>Status</th><th>Grams</th><th>Time</th><th>Cost</th></tr></thead>
        <tbody>%s</tbody>
      </table>""" % "".join(rows)


# --------------------------------------------------------------------------
# CSS and JS
# --------------------------------------------------------------------------

_CSS = """
:root {
  --bg: %(bg)s; --panel: %(panel)s; --panel2: %(panel2)s; --border: %(border)s;
  --text: %(text)s; --muted: %(muted)s; --dim: %(dim)s; --accent: %(accent)s;
  --good: %(good)s;
  --gap: 18px; --radius: 12px;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
}
.num, .mono, table { font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; }
.num { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }

header.top { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); padding-bottom: 18px; margin-bottom: var(--gap); }
header.top h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
header.top h1 .mark { color: var(--accent); }
header.top .tagline { color: var(--muted); font-size: 13px; }
header.top .generated { margin-left: auto; color: var(--dim); font-size: 12px; }

.strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: var(--gap); margin-bottom: var(--gap); }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 14px 16px; }
.stat.accent { border-color: %(accent_border)s; }
.stat-label { color: var(--muted); font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.08em; margin-bottom: 6px; }
.stat-value { font-size: 24px; font-weight: 600; letter-spacing: -0.02em;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums; }
.stat.accent .stat-value { color: var(--accent); }
.stat-sub { color: var(--dim); font-size: 12px; margin-top: 4px; }

section.panel { background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 18px 20px; margin-bottom: var(--gap); }
section.panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 0 0 4px; font-weight: 600; }
section.panel .sub { color: var(--dim); font-size: 12px; margin: 0 0 14px; }

.split { display: grid; grid-template-columns: 2fr 1fr; gap: var(--gap); align-items: start; }
@media (max-width: 820px) { .split { grid-template-columns: 1fr; } }
.chart { display: block; width: 100%%; }
.donut-wrap { display: flex; flex-direction: column; align-items: center; gap: 10px; }

.legend { list-style: none; margin: 0; padding: 0; width: 100%%; }
.legend li { display: flex; align-items: center; gap: 8px; padding: 5px 0;
  border-top: 1px solid var(--border); font-size: 13px; }
.legend li:first-child { border-top: none; }
.dot { width: 9px; height: 9px; border-radius: 2px; flex: none; }
.legend-key { flex: 1; }
.legend-val { color: var(--text); }
.legend-pct { color: var(--dim); width: 40px; text-align: right; }

.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: var(--gap); }
.card { background: var(--panel2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 14px 16px; }
.card.low { border-color: %(accent_border)s; }
.card header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.card h3 { font-size: 14px; margin: 0 0 12px; font-weight: 600; }
.chip { font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
  border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; }
.badge { font-size: 10px; letter-spacing: 0.08em; color: var(--bg); background: var(--accent);
  border-radius: 999px; padding: 2px 8px; font-weight: 700; }
.meter { height: 7px; border-radius: 999px; background: %(track)s; overflow: hidden; }
.meter span { display: block; height: 100%%; border-radius: 999px; }
.meter-row { display: flex; align-items: baseline; gap: 6px; margin-top: 7px; font-size: 12px; }
.meter-row .pct { margin-left: auto; color: var(--text); font-weight: 600; }
.card dl { margin: 12px 0 0; padding-top: 10px; border-top: 1px solid var(--border);
  display: grid; gap: 3px; }
.card dl div { display: flex; justify-content: space-between; font-size: 12px; }
.card dt { color: var(--muted); margin: 0; }
.card dd { margin: 0; }

table { width: 100%%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.06em; padding: 0 10px 8px 0;
  border-bottom: 1px solid var(--border); }
td { padding: 8px 10px 8px 0; border-bottom: 1px solid %(rowline)s; }
tbody tr:last-child td { border-bottom: none; }
td.num, th:nth-child(6), th:nth-child(7), th:nth-child(8) { text-align: right; }
td.num { text-align: right; }
td.strong { color: var(--text); font-weight: 600; }
.pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); }
.pill.ok { color: var(--good); border-color: %(good_border)s; }
.pill.bad { color: var(--accent); border-color: %(accent_border)s; }
.pill.meh { color: var(--dim); }

.filter { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.filter input { background: var(--bg); border: 1px solid var(--border); color: var(--text);
  border-radius: 8px; padding: 7px 10px; font-size: 13px; width: 240px; font-family: inherit; }
.filter input:focus { outline: none; border-color: var(--accent); }
.filter .count { color: var(--dim); font-size: 12px; }

.muted { color: var(--muted); }
footer.bottom { color: var(--dim); font-size: 12px; text-align: center; margin-top: 28px;
  padding-top: 18px; border-top: 1px solid var(--border); }
"""

_JS = """
(function () {
  var input = document.getElementById('job-filter');
  var table = document.getElementById('jobs-table');
  if (!input || !table) { return; }
  var rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr'));
  var count = document.getElementById('job-count');
  function apply() {
    var needle = input.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var hit = needle === '' || row.textContent.toLowerCase().indexOf(needle) !== -1;
      row.style.display = hit ? '' : 'none';
      if (hit) { shown += 1; }
    });
    if (count) { count.textContent = shown + ' of ' + rows.length + ' jobs'; }
  }
  input.addEventListener('input', apply);
  apply();
})();
"""


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def render_dashboard(
    report: CostReport,
    spools: Sequence[Spool],
    *,
    title: str = "spool",
    low_stock_pct: float = LOW_STOCK_PCT,
    generated_at: Optional[str] = None,
    jobs_limit: int = 50,
) -> str:
    """Build the complete HTML document as a string."""
    currency = report.currency
    generated = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")

    # Whoever calls this decides which spools to show; the CLI passes archived
    # ones only when asked with --all. The headline value and the low-stock
    # count still only consider live spools, because a retired spool is not
    # inventory you can print with.
    active = [s for s in spools if not s.archived]
    inventory_value = sum(s.remaining_g * s.price_per_gram for s in active)
    low_count = sum(1 for s in active if s.remaining_pct < low_stock_pct)

    months = report.rollup("month")
    materials = report.rollup("material")

    money = report.as_money()
    strip = "".join(
        [
            _stat("Total spent", "%s %.2f" % (currency, money["total_cost"]), "%d jobs" % report.total_jobs),
            _stat("Filament printed", "%.0f g" % report.total_grams, "%.2f kg" % (report.total_grams / 1000.0)),
            _stat("Printer time", format_duration(report.total_hours * 3600.0), "%.1f hours" % report.total_hours),
            _stat(
                "Failure rate",
                "%.0f%%" % (report.failure_rate * 100.0),
                "%s %.2f wasted" % (currency, money["wasted_cost"]),
                accent=report.failure_rate > 0,
            ),
            _stat(
                "Inventory value",
                format_money(inventory_value, currency),
                "%d spool(s), %d low" % (len(active), low_count),
                accent=low_count > 0,
            ),
        ]
    )

    cards = "".join(_spool_card(s, low_stock_pct) for s in spools) or (
        '<p class="muted">No spools yet. Add one with: spool add --material PLA --price 24.99</p>'
    )

    css = _CSS % {
        "bg": BG,
        "panel": PANEL,
        "panel2": PANEL_2,
        "border": BORDER,
        "text": TEXT,
        "muted": MUTED,
        "dim": DIM,
        "accent": ACCENT,
        "good": GOOD,
        "accent_border": _mix(ACCENT, PANEL, 0.55),
        "good_border": _mix(GOOD, PANEL, 0.6),
        "track": _mix(BORDER, BG, 0.35),
        "rowline": _mix(BORDER, PANEL, 0.45),
    }

    month_range = ""
    if months:
        month_range = "%s to %s" % (months[0].key, months[-1].key) if len(months) > 1 else months[0].key

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>%(title)s</title>
<style>%(css)s</style>
</head>
<body>
<div class="wrap">

  <header class="top">
    <h1><span class="mark">/</span> %(title)s</h1>
    <span class="tagline">filament inventory and true print cost</span>
    <span class="generated">generated %(generated)s</span>
  </header>

  <div class="strip">%(strip)s</div>

  <div class="split">
    <section class="panel">
      <h2>Cost per month</h2>
      <p class="sub">Total spend per month in %(currency)s. The brighter cap is spend lost to failed or cancelled prints. %(month_range)s</p>
      %(bars)s
    </section>
    <section class="panel">
      <h2>By material</h2>
      <p class="sub">Share of all-in cost.</p>
      <div class="donut-wrap">%(donut)s%(legend)s</div>
    </section>
  </div>

  <section class="panel">
    <h2>Inventory</h2>
    <p class="sub">Remaining filament by spool. Anything under %(low).0f%% is flagged.</p>
    <div class="cards">
%(cards)s    </div>
  </section>

  <section class="panel">
    <h2>Recent jobs</h2>
    <p class="sub">Newest first, with all-in cost per job.</p>
    <div class="filter">
      <input id="job-filter" type="text" placeholder="Filter jobs" aria-label="Filter jobs" />
      <span class="count" id="job-count"></span>
    </div>
    %(jobs)s
  </section>

  <footer class="bottom">
    Built by spool. Local only: this file contains no external references and makes no network requests.<br />
    Tariff %(tariff).4f %(currency)s per kWh, default draw %(watts).0f W, machine %(machine).4f %(currency)s per hour.
  </footer>

</div>
<script>%(js)s</script>
</body>
</html>
""" % {
        "title": esc(title),
        "css": css,
        "js": _JS,
        "generated": esc(generated),
        "strip": strip,
        "currency": esc(currency),
        "month_range": esc(month_range),
        "bars": bar_chart_svg(months, currency=currency),
        "donut": donut_svg(materials, currency=currency),
        "legend": _legend(materials, currency),
        "low": low_stock_pct,
        "cards": cards,
        "jobs": _jobs_table(report, jobs_limit),
        "tariff": report.inputs.tariff_per_kwh,
        "watts": report.inputs.default_watts,
        "machine": report.inputs.default_machine_cost_per_hour,
    }


def write_dashboard(
    path: str | Path,
    report: CostReport,
    spools: Sequence[Spool],
    **kwargs: object,
) -> Path:
    """Render and write the dashboard, returning the path written."""
    path = Path(path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dashboard(report, spools, **kwargs), encoding="utf-8")  # type: ignore[arg-type]
    return path
