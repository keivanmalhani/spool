"""Plain-text rendering for the terminal.

Everything here returns a string rather than printing, so the CLI owns all the
output and the renderers stay testable. Tables are fixed-width ASCII: they line
up in any terminal, they survive being pasted into an issue, and they do not
depend on the terminal's Unicode support.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .cost import CostReport, Rollup, format_money, round_money
from .gcode import format_duration
from .models import Spool

#: Width of the remaining-filament bar drawn next to each spool.
BAR_WIDTH = 20


def _fmt(value: object) -> str:
    return "" if value is None else str(value)


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    align: Optional[Sequence[str]] = None,
    indent: str = "",
) -> str:
    """Render a fixed-width ASCII table.

    ``align`` is one character per column, ``"l"`` or ``"r"``. Numbers get
    right alignment so decimal points stack, which is the whole point of a
    cost table.
    """
    if not rows:
        rows = []
    text_rows = [[_fmt(c) for c in row] for row in rows]
    columns = len(headers)
    align = list(align or ["l"] * columns)
    while len(align) < columns:
        align.append("l")

    widths = [len(h) for h in headers]
    for row in text_rows:
        for i in range(columns):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], len(cell))

    def line(cells: Sequence[str]) -> str:
        out = []
        for i in range(columns):
            cell = cells[i] if i < len(cells) else ""
            out.append(cell.rjust(widths[i]) if align[i] == "r" else cell.ljust(widths[i]))
        return indent + "  ".join(out).rstrip()

    parts = [line(list(headers)), indent + "  ".join("-" * w for w in widths)]
    parts.extend(line(row) for row in text_rows)
    return "\n".join(parts)


def bar(fraction: float, width: int = BAR_WIDTH) -> str:
    """A ``[#####-----]`` progress bar for a 0.0 - 1.0 fraction."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


def render_inventory(
    spools: Sequence[Spool],
    *,
    low_stock_pct: float = 15.0,
) -> str:
    """Render the spool inventory table.

    Spools under ``low_stock_pct`` are flagged with ``LOW`` so the answer to
    "do I need to order filament before this weekend" is one glance away.
    """
    if not spools:
        return "No spools. Add one with: spool add --material PLA --price 24.99"

    rows = []
    for s in spools:
        flags = []
        if s.remaining_pct < low_stock_pct:
            flags.append("LOW")
        if s.archived:
            flags.append("ARCHIVED")
        rows.append(
            [
                s.id,
                s.material,
                s.brand or "-",
                s.color or "-",
                "%.2f" % s.diameter_mm,
                "%.0f" % s.remaining_g,
                "%.0f" % s.spool_weight_g,
                "%s %5.1f%%" % (bar(s.remaining_fraction), s.remaining_pct),
                format_money(s.price, s.currency),
                "%.4f" % s.price_per_gram,
                " ".join(flags),
            ]
        )

    table = render_table(
        ["ID", "MATERIAL", "BRAND", "COLOR", "DIA", "LEFT g", "NEW g", "REMAINING", "PRICE", "PER g", ""],
        rows,
        align=["r", "l", "l", "l", "r", "r", "r", "l", "r", "r", "l"],
    )

    active = [s for s in spools if not s.archived]
    total_remaining = sum(s.remaining_g for s in active)
    total_value = sum(s.remaining_g * s.price_per_gram for s in active)
    currency = active[0].currency if active else (spools[0].currency if spools else "USD")
    low = [s for s in active if s.remaining_pct < low_stock_pct]

    footer = [
        "",
        "%d spool(s), %.0f g on hand, approx %s of unused filament."
        % (len(active), total_remaining, format_money(total_value, currency)),
    ]
    if low:
        footer.append(
            "Low stock (under %.0f%%): %s"
            % (low_stock_pct, ", ".join("#%s %s" % (s.id, s.label) for s in low))
        )
    return table + "\n" + "\n".join(footer)


# --------------------------------------------------------------------------
# Cost report
# --------------------------------------------------------------------------


def render_rollup(rollups: Sequence[Rollup], title: str, currency: str) -> str:
    """Render one grouping of the cost report."""
    if not rollups:
        return "%s\n  (nothing to show)" % title

    rows = []
    for r in rollups:
        money = r.as_money()
        rows.append(
            [
                r.key,
                r.jobs,
                "%.0f" % r.grams,
                "%.1f" % r.hours,
                "%.2f" % money["filament_cost"],
                "%.2f" % money["electricity_cost"],
                "%.2f" % money["machine_cost"],
                "%.2f" % money["total_cost"],
                "%.2f" % money["wasted_cost"],
                "%.0f%%" % (r.failure_rate * 100.0),
            ]
        )
    table = render_table(
        ["KEY", "JOBS", "GRAMS", "HOURS", "FILAMENT", "POWER", "MACHINE", "TOTAL", "WASTED", "FAIL"],
        rows,
        align=["l", "r", "r", "r", "r", "r", "r", "r", "r", "r"],
        indent="  ",
    )
    return "%s (%s)\n%s" % (title, currency, table)


def render_jobs(report: CostReport, limit: int = 20) -> str:
    """Render the most recent jobs with their all-in cost."""
    costs = report.job_costs[-limit:]
    if not costs:
        return "Recent jobs\n  (none)"
    rows = []
    for c in costs:
        money = c.as_money()
        rows.append(
            [
                c.job_id,
                c.name[:34],
                c.printer or "-",
                c.material,
                c.status,
                "%.1f" % c.grams_used,
                format_duration(c.hours_used * 3600.0),
                "%.2f" % money["total_cost"],
            ]
        )
    table = render_table(
        ["ID", "JOB", "PRINTER", "MATERIAL", "STATUS", "GRAMS", "TIME", "COST"],
        rows,
        align=["r", "l", "l", "l", "l", "r", "r", "r"],
        indent="  ",
    )
    return "Recent jobs (last %d)\n%s" % (len(costs), table)


def render_summary(report: CostReport) -> str:
    """The headline numbers: spend, output, time, waste."""
    currency = report.currency
    money = report.as_money()
    lines = [
        "Summary",
        "  Jobs            %d  (%d ok, %d failed, %d cancelled)"
        % (report.total_jobs, report.successful_jobs, report.failed_jobs, report.cancelled_jobs),
        "  Failure rate    %.1f%%" % (report.failure_rate * 100.0),
        "  Filament used   %.0f g" % report.total_grams,
        "  Printer time    %s" % format_duration(report.total_hours * 3600.0),
        "  Filament cost   %s %.2f" % (currency, money["filament_cost"]),
        "  Electricity     %s %.2f" % (currency, money["electricity_cost"]),
        "  Machine wear    %s %.2f" % (currency, money["machine_cost"]),
        "  TOTAL           %s %.2f" % (currency, money["total_cost"]),
        "  Wasted on fails %s %.2f  (%.0f g)"
        % (currency, money["wasted_cost"], report.wasted_grams),
        "  Cost per gram   %s" % format_money(report.average_cost_per_gram, currency),
    ]
    return "\n".join(lines)


def render_inputs(report: CostReport) -> str:
    """Echo the assumptions, so a number can always be traced to its inputs."""
    inputs = report.inputs
    return "\n".join(
        [
            "Cost model inputs",
            "  Electricity tariff       %.4f %s per kWh"
            % (inputs.tariff_per_kwh, inputs.currency),
            "  Default printer draw     %.0f W" % inputs.default_watts,
            "  Machine cost per hour    %s"
            % format_money(inputs.default_machine_cost_per_hour, inputs.currency),
        ]
    )


def render_cost_report(report: CostReport, by: str = "material", *, jobs_limit: int = 20) -> str:
    """The full text cost report: inputs, rollup, recent jobs, summary."""
    titles = {
        "material": "By material",
        "printer": "By printer",
        "month": "By month",
        "spool": "By spool",
        "status": "By status",
    }
    parts = [
        render_inputs(report),
        "",
        render_rollup(report.rollup(by), titles.get(by, "By %s" % by), report.currency),
        "",
        render_jobs(report, limit=jobs_limit),
        "",
        render_summary(report),
    ]
    return "\n".join(parts)


def render_gcode_preview(info, *, mass_g: Optional[float] = None) -> str:
    """Describe what was found in a gcode file, for ``spool import``."""
    lines = ["Parsed %s" % info.path, "  Slicer          %s" % info.slicer]
    if info.filament_g is not None:
        lines.append("  Filament (g)    %.2f" % info.filament_g)
    if info.filament_mm is not None:
        lines.append("  Filament (mm)   %.1f" % info.filament_mm)
    if info.filament_cm3 is not None:
        lines.append("  Filament (cm3)  %.3f" % info.filament_cm3)
    if info.duration_s is not None:
        lines.append("  Est. time       %s" % format_duration(info.duration_s))
    if info.filament_type:
        lines.append("  Filament type   %s" % info.filament_type)
    if mass_g is not None:
        lines.append("  Mass used       %.2f g" % round_money(mass_g))
    if not info.found_anything():
        lines.append("  (no slicer metadata found in this file)")
    return "\n".join(lines)
