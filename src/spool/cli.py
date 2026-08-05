"""Command line interface.

Exit codes are part of the contract, because this tool is meant to be run from
a cron job on the machine next to the printer:

* ``0`` the command did what was asked
* ``1`` an error the user needs to fix (bad input, unreachable printer, ...)
* ``2`` nothing to report (no spools, no jobs in range, no metadata in a file)

Two is separated from zero deliberately. ``spool cost --since 2026-01`` finding
no jobs is not a failure, but a script that pipes the report somewhere wants to
know the difference between "here is your report" and "there was nothing".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .cost import CostInputs, build_report, format_money, round_money
from .db import (
    DBError,
    add_job,
    add_printer,
    add_spool,
    connect,
    find_job_by_source,
    get_spool,
    list_jobs,
    list_printers,
    list_spools,
    load_settings,
    open_db,
    printer_map,
    restock_spool,
    save_settings,
    schema_version,
    set_spool_archived,
)
from .dashboard import write_dashboard
from .gcode import format_duration, parse_duration, parse_file
from .models import (
    COMMON_DIAMETERS,
    JOB_STATUSES,
    Job,
    Printer,
    Settings,
    Spool,
    density_for,
    is_known_material,
    length_to_mass_g,
    normalize_material,
)
from .report import (
    render_cost_report,
    render_gcode_preview,
    render_inventory,
    render_table,
)
from .sources import (
    DEFAULT_LIMIT,
    DEFAULT_TIMEOUT,
    FixtureSource,
    MoonrakerSource,
    OctoPrintSource,
    SourceError,
    resolve_grams,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_EMPTY = 2

#: Where the database lives when neither --db nor SPOOL_DB says otherwise.
DEFAULT_DB = "spool.db"

#: Rollup keys the cost report accepts.
BY_CHOICES = ("material", "printer", "month", "spool", "status")


class CLIError(Exception):
    """A user-fixable problem. Printed without a traceback, exits 1."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _err(message: str) -> None:
    print("error: %s" % message, file=sys.stderr)


def _db_path(args: argparse.Namespace) -> str:
    return getattr(args, "db", None) or os.environ.get("SPOOL_DB") or DEFAULT_DB


def _now_iso() -> str:
    """Current time as a UTC ISO-8601 string, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_fraction(value: Optional[str]) -> Optional[float]:
    """Parse ``--failed-at``. Accepts ``0.4``, ``.4``, ``40`` or ``40%``.

    Anything above 1 is read as a percentage, because "failed at 40" plainly
    means 40 percent and rejecting it would be pedantry.
    """
    if value is None:
        return None
    text = str(value).strip().rstrip("%")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        raise CLIError("--failed-at must be a number, got %r" % value) from None
    if number < 0:
        raise CLIError("--failed-at cannot be negative")
    if number > 1.0:
        number = number / 100.0
    if number > 1.0:
        raise CLIError("--failed-at cannot exceed 100%")
    return number


def _parse_duration_arg(value: Optional[str]) -> int:
    """Parse ``--duration``. Accepts ``5400``, ``90m``, ``1h30m``, ``2d 4h``."""
    if value is None:
        return 0
    seconds = parse_duration(str(value))
    if seconds is None:
        raise CLIError("cannot read a duration from %r (try 3h30m or 12600)" % value)
    return max(0, seconds)


def _normalize_since(value: Optional[str]) -> Optional[str]:
    """Normalise a date filter. ISO strings compare correctly as text."""
    if not value:
        return None
    return str(value).strip()


def _require_spool(conn, spool_id: Optional[int]) -> Spool:
    if spool_id is None:
        raise CLIError("--spool is required (see: spool list)")
    found = get_spool(conn, int(spool_id))
    if found is None:
        raise CLIError("no spool with id %s (see: spool list)" % spool_id)
    return found


def _cost_inputs(conn, args: argparse.Namespace) -> CostInputs:
    """Settings from the database, with any command line overrides on top."""
    inputs = CostInputs.from_settings(load_settings(conn))
    return inputs.replace(
        tariff_per_kwh=getattr(args, "tariff", None),
        currency=getattr(args, "currency", None),
        default_watts=getattr(args, "watts", None),
        default_machine_cost_per_hour=getattr(args, "machine_cost_per_hour", None),
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Create or upgrade the database."""
    path = _db_path(args)
    conn = connect(path)
    version = schema_version(conn)
    spools = len(list_spools(conn, include_archived=True))
    conn.close()
    print("Database ready at %s (schema version %d)." % (Path(path).resolve(), version))
    if spools:
        print("Existing data preserved: %d spool(s)." % spools)
    else:
        print("Next: spool add --material PLA --brand Prusament --price 24.99")
    return EXIT_OK


def cmd_add(args: argparse.Namespace) -> int:
    """Add a spool to the inventory."""
    material = normalize_material(args.material)
    density = args.density if args.density else density_for(material)
    if not args.density and not is_known_material(material):
        print(
            "note: no density default for %r, assuming %.2f g/cm3. "
            "Pass --density for an accurate figure." % (material, density),
            file=sys.stderr,
        )
    if args.diameter not in COMMON_DIAMETERS:
        print(
            "note: %.2f mm is an unusual diameter (expected %s)."
            % (args.diameter, " or ".join("%.2f" % d for d in COMMON_DIAMETERS)),
            file=sys.stderr,
        )
    if args.price < 0:
        raise CLIError("--price cannot be negative")
    if args.weight <= 0:
        raise CLIError("--weight must be greater than zero")

    spool = Spool(
        material=material,
        brand=args.brand or "",
        color=args.color or "",
        diameter_mm=float(args.diameter),
        spool_weight_g=float(args.weight),
        density_g_cm3=float(density),
        price=float(args.price),
        currency=args.currency or "USD",
        purchased=args.purchased or datetime.now(timezone.utc).date().isoformat(),
        remaining_g=float(args.remaining) if args.remaining is not None else float(args.weight),
        notes=args.notes or "",
    )
    with open_db(_db_path(args)) as conn:
        spool = add_spool(conn, spool)
    print(
        "Added spool #%d: %s, %.0f g at %s (%.4f per gram), %.0f g remaining."
        % (
            spool.id,
            spool.label,
            spool.spool_weight_g,
            format_money(spool.price, spool.currency),
            spool.price_per_gram,
            spool.remaining_g,
        )
    )
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    """Show the spool inventory."""
    with open_db(_db_path(args)) as conn:
        settings = load_settings(conn)
        spools = list_spools(conn, include_archived=args.all)
    if not spools:
        print(render_inventory(spools, low_stock_pct=settings.low_stock_pct))
        return EXIT_EMPTY
    print(render_inventory(spools, low_stock_pct=settings.low_stock_pct))
    return EXIT_OK


def cmd_use(args: argparse.Namespace) -> int:
    """Record a print job by hand."""
    if args.grams is None and args.length_mm is None:
        raise CLIError("give either --grams or --length-mm")
    if args.grams is not None and args.length_mm is not None:
        raise CLIError("give --grams or --length-mm, not both")

    fraction = _parse_fraction(args.failed_at)
    duration = _parse_duration_arg(args.duration)

    with open_db(_db_path(args)) as conn:
        spool = _require_spool(conn, args.spool)

        if args.grams is not None:
            grams = float(args.grams)
            length_mm = None
        else:
            length_mm = float(args.length_mm)
            grams = length_to_mass_g(length_mm, spool.diameter_mm, spool.density_g_cm3)

        if grams < 0:
            raise CLIError("filament used cannot be negative")

        job = Job(
            name=args.name,
            printer=args.printer or "",
            spool_id=spool.id,
            filament_g=grams,
            filament_mm=length_mm,
            duration_s=duration,
            status=args.status,
            started=args.started or _now_iso(),
            failed_at_fraction=fraction,
            source="manual",
            notes=args.notes or "",
        )
        job, shortfall = add_job(conn, job)
        after = get_spool(conn, spool.id)

    used = job.grams_used()
    print(
        "Recorded job #%d %r: %s, %.2f g used, %s."
        % (job.id, job.name, job.status, used, format_duration(job.seconds_used()))
    )
    if fraction is not None and job.status != "success":
        print(
            "  Stopped at %.0f%%, so %.2f g of a %.2f g print was wasted."
            % (fraction * 100.0, used, job.filament_g)
        )
    if after is not None:
        print("  Spool #%d now has %.0f g left (%.0f%%)." % (after.id, after.remaining_g, after.remaining_pct))
    if shortfall > 0:
        print(
            "warning: spool #%d was short by %.2f g. It is now at zero; "
            "weigh it and run: spool restock %d --grams N"
            % (spool.id, shortfall, spool.id),
            file=sys.stderr,
        )
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    """Import a sliced .gcode file as a job."""
    path = Path(args.path)
    if not path.exists():
        raise CLIError("no such file: %s" % path)

    info = parse_file(path)

    with open_db(_db_path(args)) as conn:
        spool = _require_spool(conn, args.spool)

        if args.grams is not None:
            grams: Optional[float] = float(args.grams)
        else:
            grams = info.resolve_mass_g(
                diameter_mm=spool.diameter_mm,
                density_g_cm3=spool.density_g_cm3,
            )

        print(render_gcode_preview(info, mass_g=grams))

        if grams is None:
            _err(
                "no filament figure in %s and no --grams given. "
                "Re-slice with estimates enabled or pass --grams." % path
            )
            return EXIT_EMPTY

        fraction = _parse_fraction(args.failed_at)
        duration = (
            _parse_duration_arg(args.duration)
            if args.duration
            else int(info.duration_s or 0)
        )

        job = Job(
            name=args.name or info.name or path.stem,
            printer=args.printer or "",
            spool_id=spool.id,
            filament_g=float(grams),
            filament_mm=info.filament_mm,
            duration_s=duration,
            status=args.status,
            started=args.started or _now_iso(),
            failed_at_fraction=fraction,
            source="gcode",
            source_job_id=None,
            notes="slicer=%s" % info.slicer,
        )
        if args.dry_run:
            print("Dry run: nothing written.")
            return EXIT_OK

        job, shortfall = add_job(conn, job)
        after = get_spool(conn, spool.id)

    print(
        "Recorded job #%d %r from gcode: %.2f g, %s, %s."
        % (job.id, job.name, job.grams_used(), format_duration(job.seconds_used()), job.status)
    )
    if after is not None:
        print("  Spool #%d now has %.0f g left (%.0f%%)." % (after.id, after.remaining_g, after.remaining_pct))
    if shortfall > 0:
        print(
            "warning: spool #%d was short by %.2f g and is now at zero." % (spool.id, shortfall),
            file=sys.stderr,
        )
    return EXIT_OK


#: A POSIX environment variable name. Anything else given to --api-key-env is
#: almost certainly the key itself, typed into the wrong flag.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _read_api_key(var_name: str) -> str:
    """Read an API key out of the named environment variable.

    The name is validated before it is used or echoed. Someone who types
    ``--api-key-env hunter2-actual-key`` has just handed us their secret, and
    the last thing that should happen is for it to be quoted back into a
    terminal, a log file or a CI transcript.
    """
    if not _ENV_NAME.match(var_name or ""):
        raise CLIError(
            "--api-key-env takes the NAME of an environment variable, not the key "
            "itself. The value given does not look like a variable name, so it has "
            "not been echoed. Try: export MY_PRINTER_KEY=... then --api-key-env "
            "MY_PRINTER_KEY"
        )
    key = os.environ.get(var_name)
    if not key:
        raise CLIError(
            "environment variable %s is not set or is empty. "
            "Export the key there; spool never accepts a key as a flag value."
            % var_name
        )
    return key


def _build_source(args: argparse.Namespace):
    """Pick and construct the sync source from mutually exclusive flags."""
    chosen = [bool(args.moonraker), bool(args.octoprint), bool(args.fixture)]
    if sum(chosen) != 1:
        raise CLIError("give exactly one of --moonraker URL, --octoprint URL or --fixture PATH")

    api_key = None
    if args.api_key_env:
        api_key = _read_api_key(args.api_key_env)

    if args.moonraker:
        return MoonrakerSource(
            args.moonraker,
            api_key,
            timeout=args.timeout,
            limit=args.limit,
            printer_name=args.printer or "",
        )
    if args.octoprint:
        if not api_key:
            raise CLIError("--octoprint needs --api-key-env VAR naming the variable holding the key")
        return OctoPrintSource(
            args.octoprint,
            api_key,
            timeout=args.timeout,
            limit=args.limit,
            printer_name=args.printer or "",
        )
    return FixtureSource(args.fixture, printer_name=args.printer or "")


def cmd_sync(args: argparse.Namespace) -> int:
    """Pull job history from a printer or a fixture file.

    Idempotent: a job already stored with the same source and source job id is
    skipped, so running this on a timer never double-counts a print.
    """
    source = _build_source(args)
    jobs = source.list_jobs()
    if not jobs:
        print("No jobs returned by %s." % source.name)
        return EXIT_EMPTY

    with open_db(_db_path(args)) as conn:
        spool = None
        if args.spool is not None:
            spool = _require_spool(conn, args.spool)
            resolved = resolve_grams(jobs, spool.diameter_mm, spool.density_g_cm3)
            if resolved:
                print(
                    "Derived a weight for %d job(s) from length using spool #%d "
                    "(%.2f mm, %.2f g/cm3)." % (resolved, spool.id, spool.diameter_mm, spool.density_g_cm3)
                )

        added = 0
        skipped = 0
        unweighed = 0
        for job in jobs:
            if job.source_job_id and find_job_by_source(conn, job.source, job.source_job_id):
                skipped += 1
                continue
            if spool is not None and job.spool_id is None:
                job.spool_id = spool.id
            if not job.filament_g:
                unweighed += 1
            if args.dry_run:
                added += 1
                continue
            add_job(conn, job)
            added += 1

    verb = "Would add" if args.dry_run else "Added"
    print(
        "%s %d job(s) from %s, skipped %d already present."
        % (verb, added, source.name, skipped)
    )
    if unweighed:
        print(
            "note: %d job(s) have no filament weight. Re-run with --spool ID so "
            "lengths can be converted to grams." % unweighed,
            file=sys.stderr,
        )
    if added == 0:
        return EXIT_EMPTY
    return EXIT_OK


def _load_report(conn, args: argparse.Namespace):
    jobs = list_jobs(
        conn,
        since=_normalize_since(getattr(args, "since", None)),
        until=_normalize_since(getattr(args, "until", None)),
    )
    spools = {s.id: s for s in list_spools(conn, include_archived=True) if s.id is not None}
    printers = printer_map(conn)
    return build_report(jobs, spools, printers, _cost_inputs(conn, args)), spools


def cmd_cost(args: argparse.Namespace) -> int:
    """Print the cost report."""
    with open_db(_db_path(args)) as conn:
        report, _ = _load_report(conn, args)

    if not report.job_costs:
        print("No jobs in range. Record one with: spool use NAME --spool 1 --grams 20 --duration 1h")
        return EXIT_EMPTY

    if args.json:
        print(json.dumps(_report_to_dict(report, args.by), indent=2, sort_keys=True))
        return EXIT_OK

    print(render_cost_report(report, by=args.by, jobs_limit=args.limit))
    return EXIT_OK


def _report_to_dict(report, by: str) -> dict:
    """Machine-readable version of the cost report, rounded to cents."""
    return {
        "currency": report.currency,
        "inputs": {
            "tariff_per_kwh": report.inputs.tariff_per_kwh,
            "default_watts": report.inputs.default_watts,
            "default_machine_cost_per_hour": report.inputs.default_machine_cost_per_hour,
        },
        "summary": {
            "jobs": report.total_jobs,
            "successful": report.successful_jobs,
            "failed": report.failed_jobs,
            "cancelled": report.cancelled_jobs,
            "failure_rate": round(report.failure_rate, 4),
            "grams": round(report.total_grams, 2),
            "hours": round(report.total_hours, 3),
            "filament_cost": round_money(report.total_filament_cost),
            "electricity_cost": round_money(report.total_electricity_cost),
            "machine_cost": round_money(report.total_machine_cost),
            "total_cost": round_money(report.total_cost),
            "wasted_cost": round_money(report.total_wasted_cost),
        },
        "by": by,
        "rollups": [
            {
                "key": r.key,
                "jobs": r.jobs,
                "grams": round(r.grams, 2),
                "hours": round(r.hours, 3),
                "failure_rate": round(r.failure_rate, 4),
                **r.as_money(),
            }
            for r in report.rollup(by)
        ],
        "jobs": [
            {
                "id": c.job_id,
                "name": c.name,
                "printer": c.printer,
                "material": c.material,
                "status": c.status,
                "grams": round(c.grams_used, 2),
                "hours": round(c.hours_used, 3),
                **c.as_money(),
            }
            for c in report.job_costs
        ],
    }


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Write the self-contained HTML dashboard."""
    with open_db(_db_path(args)) as conn:
        settings = load_settings(conn)
        report, _ = _load_report(conn, args)
        spools = list_spools(conn, include_archived=args.all)

    if not report.job_costs and not spools:
        print("Nothing to show yet: no spools and no jobs.")
        return EXIT_EMPTY

    path = write_dashboard(
        args.out,
        report,
        spools,
        title=args.title,
        low_stock_pct=settings.low_stock_pct,
    )
    size_kb = path.stat().st_size / 1024.0
    print(
        "Wrote %s (%.1f KB, %d spool(s), %d job(s), no external references)."
        % (path.resolve(), size_kb, len(spools), report.total_jobs)
    )
    return EXIT_OK


def cmd_archive(args: argparse.Namespace) -> int:
    """Hide a spool from the default inventory listing."""
    with open_db(_db_path(args)) as conn:
        if not set_spool_archived(conn, args.id, True):
            raise CLIError("no spool with id %s" % args.id)
        spool = get_spool(conn, args.id)
    print("Archived spool #%d (%s). It stays in the cost history." % (args.id, spool.label if spool else ""))
    return EXIT_OK


def cmd_restock(args: argparse.Namespace) -> int:
    """Refill a spool and bring it back into the inventory."""
    with open_db(_db_path(args)) as conn:
        spool = restock_spool(conn, args.id, args.grams)
        if spool is None:
            raise CLIError("no spool with id %s" % args.id)
    print(
        "Spool #%d (%s) restocked to %.0f g (%.0f%%)."
        % (spool.id, spool.label, spool.remaining_g, spool.remaining_pct)
    )
    return EXIT_OK


def cmd_printer_add(args: argparse.Namespace) -> int:
    """Register or update a printer."""
    printer = Printer(
        name=args.name,
        watts=float(args.watts or 0.0),
        price=float(args.price or 0.0),
        life_hours=float(args.life_hours or 0.0),
        notes=args.notes or "",
    )
    with open_db(_db_path(args)) as conn:
        printer = add_printer(conn, printer)
    print(
        "Printer %r saved: %.0f W, machine cost %.4f per hour."
        % (printer.name, printer.watts, printer.machine_cost_per_hour())
    )
    return EXIT_OK


def cmd_printer_list(args: argparse.Namespace) -> int:
    """List registered printers."""
    with open_db(_db_path(args)) as conn:
        printers = list_printers(conn)
    if not printers:
        print("No printers registered. Add one with: spool printer add NAME --watts 120")
        return EXIT_EMPTY
    rows = [
        [p.name, "%.0f" % p.watts, "%.2f" % p.price, "%.0f" % p.life_hours, "%.4f" % p.machine_cost_per_hour()]
        for p in printers
    ]
    print(render_table(["NAME", "WATTS", "PRICE", "LIFE h", "PER HOUR"], rows, align=["l", "r", "r", "r", "r"]))
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """Show or change the persisted cost model settings."""
    with open_db(_db_path(args)) as conn:
        settings = load_settings(conn)
        if args.set:
            for item in args.set:
                if "=" not in item:
                    raise CLIError("--set expects key=value, got %r" % item)
                key, _, value = item.partition("=")
                key = key.strip()
                if key not in Settings.FIELD_TYPES:
                    raise CLIError(
                        "unknown setting %r. Known: %s"
                        % (key, ", ".join(sorted(Settings.FIELD_TYPES)))
                    )
                caster = Settings.FIELD_TYPES[key]
                try:
                    setattr(settings, key, caster(value.strip()))
                except (TypeError, ValueError):
                    raise CLIError("cannot read %r as a value for %s" % (value, key)) from None
            save_settings(conn, settings)
            print("Saved %d setting(s)." % len(args.set))

    rows = [[k, getattr(settings, k)] for k in sorted(Settings.FIELD_TYPES)]
    print(render_table(["SETTING", "VALUE"], rows, align=["l", "r"]))
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Attach shared options to a subparser without clobbering global values.

    argparse would otherwise let the subparser's default overwrite a value the
    user gave before the subcommand name; SUPPRESS keeps the earlier one.
    """
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="database file (default: $SPOOL_DB or ./spool.db)",
    )


def _add_cost_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tariff", type=float, default=None, metavar="RATE",
                        help="electricity price per kWh, overriding the stored setting")
    parser.add_argument("--watts", type=float, default=None, metavar="W",
                        help="average printer draw in watts, for printers with no registered profile")
    parser.add_argument("--machine-cost-per-hour", type=float, default=None, metavar="RATE",
                        help="amortised machine cost per hour")
    parser.add_argument("--currency", default=None, metavar="CODE",
                        help="currency code used for display")


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="spool",
        description=(
            "Local-first filament inventory and true print cost calculator. "
            "No accounts, no telemetry, no network except the printer URL you pass in."
        ),
        epilog="Run 'spool COMMAND --help' for the options of a single command.",
        # Prefix abbreviation is off on purpose. With it on, argparse accepts
        # `--api-key SECRET` as an abbreviation of `--api-key-env`, which is
        # exactly the mistake that puts a key on a command line. An unknown
        # flag should be an error, not a guess.
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version="spool %s" % __version__)
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="database file (default: $SPOOL_DB or ./spool.db)")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    class sub:  # noqa: N801 - a tiny shim, not a real class
        """Adds a subparser with the same no-abbreviation rule as the parent."""

        @staticmethod
        def add_parser(name: str, **kwargs: object) -> argparse.ArgumentParser:
            kwargs.setdefault("allow_abbrev", False)
            return subparsers.add_parser(name, **kwargs)  # type: ignore[arg-type]

    # init ---------------------------------------------------------------
    p = sub.add_parser("init", help="create the database")
    _add_common(p)
    p.set_defaults(func=cmd_init)

    # add ----------------------------------------------------------------
    p = sub.add_parser("add", help="add a spool to the inventory")
    _add_common(p)
    p.add_argument("--material", required=True,
                   help="PLA, PETG, ABS, ASA, TPU or any label you use")
    p.add_argument("--price", required=True, type=float, help="what you paid for the spool")
    p.add_argument("--brand", default="", help="manufacturer, e.g. Prusament")
    p.add_argument("--color", default="", help="colour name")
    p.add_argument("--diameter", type=float, default=1.75, help="filament diameter in mm (default 1.75)")
    p.add_argument("--weight", type=float, default=1000.0,
                   help="net filament weight when new, in grams (default 1000)")
    p.add_argument("--density", type=float, default=None,
                   help="g/cm3, overriding the nominal default for the material")
    p.add_argument("--currency", default="USD", help="currency code (default USD)")
    p.add_argument("--purchased", default=None, metavar="YYYY-MM-DD", help="purchase date")
    p.add_argument("--remaining", type=float, default=None,
                   help="grams left now, if the spool is already part used")
    p.add_argument("--notes", default="", help="free text")
    p.set_defaults(func=cmd_add)

    # list ---------------------------------------------------------------
    p = sub.add_parser("list", help="show the spool inventory")
    _add_common(p)
    p.add_argument("--all", action="store_true", help="include archived spools")
    p.set_defaults(func=cmd_list)

    # use ----------------------------------------------------------------
    p = sub.add_parser("use", help="record a print job by hand")
    _add_common(p)
    p.add_argument("name", help="job name")
    p.add_argument("--spool", type=int, required=True, help="spool id the filament came off")
    p.add_argument("--grams", type=float, default=None, help="filament used, in grams")
    p.add_argument("--length-mm", type=float, default=None,
                   help="filament used, in millimetres (converted using the spool's diameter and density)")
    p.add_argument("--duration", default="0", help="print time, e.g. 3h30m or 12600")
    p.add_argument("--status", default="success", choices=list(JOB_STATUSES), help="job outcome")
    p.add_argument("--failed-at", default=None, metavar="FRACTION",
                   help="how far a failed print got, e.g. 0.4 or 40%%")
    p.add_argument("--printer", default="", help="printer name")
    p.add_argument("--started", default=None, metavar="ISO", help="start timestamp (default: now)")
    p.add_argument("--notes", default="", help="free text")
    p.set_defaults(func=cmd_use)

    # import -------------------------------------------------------------
    p = sub.add_parser("import", help="import a sliced .gcode file as a job")
    _add_common(p)
    p.add_argument("path", help="path to a .gcode file")
    p.add_argument("--spool", type=int, required=True, help="spool id the filament came off")
    p.add_argument("--name", default=None, help="job name (default: the file name)")
    p.add_argument("--printer", default="", help="printer name")
    p.add_argument("--grams", type=float, default=None,
                   help="override the weight from the file")
    p.add_argument("--duration", default=None, help="override the estimate from the file")
    p.add_argument("--status", default="success", choices=list(JOB_STATUSES), help="job outcome")
    p.add_argument("--failed-at", default=None, metavar="FRACTION",
                   help="how far a failed print got, e.g. 0.4 or 40%%")
    p.add_argument("--started", default=None, metavar="ISO", help="start timestamp (default: now)")
    p.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    p.set_defaults(func=cmd_import)

    # sync ---------------------------------------------------------------
    p = sub.add_parser("sync", help="pull job history from a printer or a fixture")
    _add_common(p)
    p.add_argument("--moonraker", default=None, metavar="URL",
                   help="Moonraker base URL, e.g. http://printer.local:7125")
    p.add_argument("--octoprint", default=None, metavar="URL",
                   help="OctoPrint base URL")
    p.add_argument("--fixture", default=None, metavar="PATH",
                   help="local JSON file of jobs, for demos and offline use")
    p.add_argument("--api-key-env", default=None, metavar="VAR",
                   help="name of the environment variable holding the API key "
                        "(the key itself is never accepted as a flag value)")
    p.add_argument("--spool", type=int, default=None,
                   help="spool id to attribute these jobs to, and to convert lengths with")
    p.add_argument("--printer", default="", help="printer name to record on the jobs")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="maximum jobs to pull")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="request timeout in seconds")
    p.add_argument("--dry-run", action="store_true", help="report what would be added, write nothing")
    p.set_defaults(func=cmd_sync)

    # cost ---------------------------------------------------------------
    p = sub.add_parser("cost", help="the cost report")
    _add_common(p)
    p.add_argument("--since", default=None, metavar="DATE", help="only jobs on or after this ISO date")
    p.add_argument("--until", default=None, metavar="DATE", help="only jobs on or before this ISO date")
    p.add_argument("--by", default="material", choices=list(BY_CHOICES), help="how to group the rollup")
    p.add_argument("--limit", type=int, default=20, help="how many recent jobs to list")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    _add_cost_overrides(p)
    p.set_defaults(func=cmd_cost)

    # dashboard ----------------------------------------------------------
    p = sub.add_parser("dashboard", help="write the self-contained HTML dashboard")
    _add_common(p)
    p.add_argument("--out", default="spool-dashboard.html", metavar="PATH", help="output file")
    p.add_argument("--title", default="spool", help="document title")
    p.add_argument("--since", default=None, metavar="DATE", help="only jobs on or after this ISO date")
    p.add_argument("--until", default=None, metavar="DATE", help="only jobs on or before this ISO date")
    p.add_argument("--all", action="store_true", help="include archived spools")
    _add_cost_overrides(p)
    p.set_defaults(func=cmd_dashboard)

    # archive / restock --------------------------------------------------
    p = sub.add_parser("archive", help="hide a used-up spool from the inventory")
    _add_common(p)
    p.add_argument("id", type=int, help="spool id")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("restock", help="refill a spool and bring it back")
    _add_common(p)
    p.add_argument("id", type=int, help="spool id")
    p.add_argument("--grams", type=float, default=None,
                   help="grams to set it to (default: the as-new weight)")
    p.set_defaults(func=cmd_restock)

    # printer ------------------------------------------------------------
    p = sub.add_parser("printer", help="register printers for per-machine power and wear")
    _add_common(p)
    psub = p.add_subparsers(dest="printer_command", metavar="SUBCOMMAND")
    pa = psub.add_parser("add", help="register or update a printer", allow_abbrev=False)
    _add_common(pa)
    pa.add_argument("name", help="printer name, matching what jobs record")
    pa.add_argument("--watts", type=float, default=0.0, help="average draw over a print, in watts")
    pa.add_argument("--price", type=float, default=0.0, help="what the machine cost")
    pa.add_argument("--life-hours", type=float, default=0.0, help="expected printing hours before replacement")
    pa.add_argument("--notes", default="", help="free text")
    pa.set_defaults(func=cmd_printer_add)
    pl = psub.add_parser("list", help="list registered printers", allow_abbrev=False)
    _add_common(pl)
    pl.set_defaults(func=cmd_printer_list)
    p.set_defaults(func=lambda a: cmd_printer_list(a) if not a.printer_command else EXIT_OK)

    # config -------------------------------------------------------------
    p = sub.add_parser("config", help="show or change the cost model settings")
    _add_common(p)
    p.add_argument("--set", action="append", metavar="KEY=VALUE", default=None,
                   help="set a value, e.g. --set tariff_per_kwh=0.28")
    p.set_defaults(func=cmd_config)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns an exit code rather than calling sys.exit."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK

    try:
        return int(args.func(args))
    except CLIError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except SourceError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except DBError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except sqlite3.Error as exc:
        _err("database error: %s" % exc)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover - depends on the consumer
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        _err("interrupted")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
