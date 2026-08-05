"""The cost engine.

What a print actually costs is filament plus electricity plus a share of the
machine, and then, over a month, plus everything you threw in the bin. Most
"cost calculators" stop at the first term and quietly assume every print
succeeded. This one does not.

Money handling rule: every intermediate value is a full-precision float, and
rounding to cents happens once, at the boundary where a number is shown to a
person or summed into a printed total. Rounding inside the loop is how you get
a report whose lines do not add up to its total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Optional

from .models import Job, Printer, Settings, Spool

#: Grouping keys accepted by :func:`build_report`.
ROLLUP_KEYS: tuple[str, ...] = ("material", "printer", "month", "spool", "status")

_CENTS = Decimal("0.01")


def round_money(value: float) -> float:
    """Round to cents, half away from zero.

    ``round()`` uses banker's rounding on top of binary floats, so
    ``round(0.005, 2)`` is ``0.0``. For money that is surprising and, over a
    long enough report, wrong. Decimal with ROUND_HALF_UP is what an accountant
    expects and what an invoice shows.
    """
    if value is None:
        return 0.0
    return float(Decimal(str(float(value))).quantize(_CENTS, rounding=ROUND_HALF_UP))


def format_money(value: float, currency: str = "USD") -> str:
    """Render an amount as ``USD 12.34``.

    Currency codes rather than symbols: the code is unambiguous, it is ASCII,
    and it does not silently imply a locale the user never chose.
    """
    return "%s %.2f" % (currency, round_money(value))


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CostInputs:
    """Everything the cost model needs that does not live on a job.

    Kept separate from :class:`~spool.models.Settings` so a report can be run
    with a what-if tariff without touching the database.
    """

    tariff_per_kwh: float = 0.0
    currency: str = "USD"
    default_watts: float = 0.0
    default_machine_cost_per_hour: float = 0.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "CostInputs":
        """Build inputs from persisted settings."""
        return cls(
            tariff_per_kwh=settings.tariff_per_kwh,
            currency=settings.currency,
            default_watts=settings.default_watts,
            default_machine_cost_per_hour=settings.default_machine_cost_per_hour,
        )

    def replace(self, **kwargs: object) -> "CostInputs":
        """Return a copy with the given non-None overrides applied."""
        data = {
            "tariff_per_kwh": self.tariff_per_kwh,
            "currency": self.currency,
            "default_watts": self.default_watts,
            "default_machine_cost_per_hour": self.default_machine_cost_per_hour,
        }
        for key, value in kwargs.items():
            if value is not None and key in data:
                data[key] = value  # type: ignore[assignment]
        return CostInputs(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Per-job cost
# --------------------------------------------------------------------------


@dataclass
class JobCost:
    """The cost breakdown for a single job, in full precision.

    Call :meth:`as_money` to get the same numbers rounded to cents for display.
    """

    job_id: Optional[int]
    name: str
    status: str
    printer: str
    material: str
    month: str
    spool_id: Optional[int]
    grams_used: float
    hours_used: float
    filament_cost: float
    electricity_cost: float
    machine_cost: float
    currency: str = "USD"

    @property
    def total_cost(self) -> float:
        """Filament plus electricity plus machine share."""
        return self.filament_cost + self.electricity_cost + self.machine_cost

    @property
    def wasted_cost(self) -> float:
        """The whole cost of this job when nothing usable came out of it.

        A failed print does not waste "some" of its cost. Every gram and every
        watt-hour that went into it is gone, which is exactly why partial
        failures still need their real fraction recorded rather than a guess.
        """
        return self.total_cost if self.status != "success" else 0.0

    @property
    def cost_per_gram(self) -> float:
        """All-in cost per gram actually extruded, or 0.0 for a zero-gram job."""
        if self.grams_used <= 0:
            return 0.0
        return self.total_cost / self.grams_used

    def as_money(self) -> dict[str, float]:
        """Every monetary field rounded to cents, for display.

        The displayed total is the sum of the displayed lines, not the rounded
        exact total. Those can differ by a cent, and when they do, the version
        that adds up on screen is the one to show: a cost report whose columns
        do not sum to its total is a cost report nobody trusts. The exact value
        is still available on :attr:`total_cost` for further arithmetic.
        """
        filament = round_money(self.filament_cost)
        power = round_money(self.electricity_cost)
        machine = round_money(self.machine_cost)
        total = round_money(filament + power + machine)
        return {
            "filament_cost": filament,
            "electricity_cost": power,
            "machine_cost": machine,
            "total_cost": total,
            "wasted_cost": total if self.status != "success" else 0.0,
        }


def electricity_cost(duration_s: float, watts: float, tariff_per_kwh: float) -> float:
    """Cost of running a machine at ``watts`` for ``duration_s`` seconds.

    ``kWh = watts / 1000 * hours``. A zero-length or zero-draw print costs
    nothing, and negative inputs are treated as zero rather than credited.
    """
    if duration_s <= 0 or watts <= 0 or tariff_per_kwh <= 0:
        return 0.0
    hours = duration_s / 3600.0
    kwh = (watts / 1000.0) * hours
    return kwh * tariff_per_kwh


def compute_job_cost(
    job: Job,
    spool: Optional[Spool] = None,
    printer: Optional[Printer] = None,
    inputs: Optional[CostInputs] = None,
) -> JobCost:
    """Cost one job.

    ``spool`` supplies the price per gram and the material label; without it
    the filament line is zero because we genuinely do not know what the
    plastic cost. ``printer`` supplies watts and amortisation, falling back to
    the defaults in ``inputs`` when the printer is unknown or unconfigured.
    """
    inputs = inputs or CostInputs()

    grams = job.grams_used()
    seconds = job.seconds_used()
    hours = seconds / 3600.0

    price_per_gram = spool.price_per_gram if spool is not None else 0.0
    filament = grams * price_per_gram

    watts = inputs.default_watts
    if printer is not None and printer.watts > 0:
        watts = printer.watts
    power = electricity_cost(seconds, watts, inputs.tariff_per_kwh)

    per_hour = inputs.default_machine_cost_per_hour
    if printer is not None and printer.machine_cost_per_hour() > 0:
        per_hour = printer.machine_cost_per_hour()
    machine = hours * per_hour if per_hour > 0 else 0.0

    currency = spool.currency if spool is not None and spool.currency else inputs.currency

    return JobCost(
        job_id=job.id,
        name=job.name,
        status=job.status,
        printer=job.printer or "",
        material=spool.material if spool is not None else "unknown",
        month=job.month,
        spool_id=job.spool_id,
        grams_used=grams,
        hours_used=hours,
        filament_cost=filament,
        electricity_cost=power,
        machine_cost=machine,
        currency=currency,
    )


# --------------------------------------------------------------------------
# Rollups
# --------------------------------------------------------------------------


@dataclass
class Rollup:
    """Aggregated costs for one group (a material, a printer, a month...)."""

    key: str
    jobs: int = 0
    failed_jobs: int = 0
    cancelled_jobs: int = 0
    grams: float = 0.0
    hours: float = 0.0
    filament_cost: float = 0.0
    electricity_cost: float = 0.0
    machine_cost: float = 0.0
    # Waste is tracked line by line rather than as one number so that a group
    # made up entirely of failures reports the same waste as its total, down
    # to the cent, instead of drifting by a rounding step.
    wasted_filament_cost: float = 0.0
    wasted_electricity_cost: float = 0.0
    wasted_machine_cost: float = 0.0
    currency: str = "USD"

    @property
    def total_cost(self) -> float:
        """All three cost lines added together."""
        return self.filament_cost + self.electricity_cost + self.machine_cost

    @property
    def wasted_cost(self) -> float:
        """Cost of every job in this group that produced nothing."""
        return (
            self.wasted_filament_cost
            + self.wasted_electricity_cost
            + self.wasted_machine_cost
        )

    @property
    def failure_rate(self) -> float:
        """Failed jobs over all jobs, as a 0.0 - 1.0 fraction.

        Cancelled jobs are deliberately excluded: stopping a print because you
        changed your mind is not a machine failure, even though it does waste
        filament and therefore still shows up in ``wasted_cost``.
        """
        if self.jobs <= 0:
            return 0.0
        return self.failed_jobs / self.jobs

    def add(self, cost: JobCost) -> None:
        """Fold one job's cost into this group."""
        self.jobs += 1
        if cost.status == "failed":
            self.failed_jobs += 1
        elif cost.status == "cancelled":
            self.cancelled_jobs += 1
        self.grams += cost.grams_used
        self.hours += cost.hours_used
        self.filament_cost += cost.filament_cost
        self.electricity_cost += cost.electricity_cost
        self.machine_cost += cost.machine_cost
        if cost.status != "success":
            self.wasted_filament_cost += cost.filament_cost
            self.wasted_electricity_cost += cost.electricity_cost
            self.wasted_machine_cost += cost.machine_cost
        if cost.currency:
            self.currency = cost.currency

    def as_money(self) -> dict[str, float]:
        """Every monetary field rounded to cents, with a total that adds up.

        Same rule as :meth:`JobCost.as_money`: the total shown is the sum of
        the lines shown, and waste is rounded line by line for the same reason.
        """
        filament = round_money(self.filament_cost)
        power = round_money(self.electricity_cost)
        machine = round_money(self.machine_cost)
        wasted = (
            round_money(self.wasted_filament_cost)
            + round_money(self.wasted_electricity_cost)
            + round_money(self.wasted_machine_cost)
        )
        return {
            "filament_cost": filament,
            "electricity_cost": power,
            "machine_cost": machine,
            "total_cost": round_money(filament + power + machine),
            "wasted_cost": round_money(wasted),
        }


@dataclass
class CostReport:
    """Per-job costs plus every rollup, ready for text or HTML rendering."""

    job_costs: list[JobCost] = field(default_factory=list)
    rollups: dict[str, dict[str, Rollup]] = field(default_factory=dict)
    currency: str = "USD"
    inputs: CostInputs = field(default_factory=CostInputs)

    # -- summary ---------------------------------------------------------

    @property
    def total_jobs(self) -> int:
        """Number of jobs in the report."""
        return len(self.job_costs)

    @property
    def failed_jobs(self) -> int:
        """Jobs whose status is "failed"."""
        return sum(1 for c in self.job_costs if c.status == "failed")

    @property
    def cancelled_jobs(self) -> int:
        """Jobs a human stopped."""
        return sum(1 for c in self.job_costs if c.status == "cancelled")

    @property
    def successful_jobs(self) -> int:
        """Jobs that produced a part."""
        return sum(1 for c in self.job_costs if c.status == "success")

    @property
    def failure_rate(self) -> float:
        """Failed jobs over all jobs, 0.0 - 1.0. Excludes cancellations."""
        if not self.job_costs:
            return 0.0
        return self.failed_jobs / len(self.job_costs)

    @property
    def total_grams(self) -> float:
        """Filament actually extruded across every job."""
        return sum(c.grams_used for c in self.job_costs)

    @property
    def total_hours(self) -> float:
        """Machine hours actually spent."""
        return sum(c.hours_used for c in self.job_costs)

    @property
    def total_filament_cost(self) -> float:
        """Filament line across every job."""
        return sum(c.filament_cost for c in self.job_costs)

    @property
    def total_electricity_cost(self) -> float:
        """Electricity line across every job."""
        return sum(c.electricity_cost for c in self.job_costs)

    @property
    def total_machine_cost(self) -> float:
        """Machine amortisation line across every job."""
        return sum(c.machine_cost for c in self.job_costs)

    @property
    def total_cost(self) -> float:
        """Everything, before rounding."""
        return sum(c.total_cost for c in self.job_costs)

    @property
    def total_wasted_cost(self) -> float:
        """The bin total: full cost of every job that produced nothing."""
        return sum(c.wasted_cost for c in self.job_costs)

    @property
    def wasted_filament_cost(self) -> float:
        """Filament that went straight in the bin."""
        return sum(c.filament_cost for c in self.job_costs if c.status != "success")

    @property
    def wasted_electricity_cost(self) -> float:
        """Electricity spent heating a print that produced nothing."""
        return sum(c.electricity_cost for c in self.job_costs if c.status != "success")

    @property
    def wasted_machine_cost(self) -> float:
        """Machine wear spent on prints that produced nothing."""
        return sum(c.machine_cost for c in self.job_costs if c.status != "success")

    @property
    def wasted_grams(self) -> float:
        """Filament consumed by jobs that produced nothing."""
        return sum(c.grams_used for c in self.job_costs if c.status != "success")

    @property
    def average_cost_per_gram(self) -> float:
        """All-in cost divided by grams extruded, or 0.0 with no grams."""
        grams = self.total_grams
        if grams <= 0:
            return 0.0
        return self.total_cost / grams

    def as_money(self) -> dict[str, float]:
        """Report-level totals rounded to cents, with a total that adds up.

        Same rule as the other ``as_money`` methods, applied one level up.
        """
        filament = round_money(self.total_filament_cost)
        power = round_money(self.total_electricity_cost)
        machine = round_money(self.total_machine_cost)
        wasted = (
            round_money(self.wasted_filament_cost)
            + round_money(self.wasted_electricity_cost)
            + round_money(self.wasted_machine_cost)
        )
        return {
            "filament_cost": filament,
            "electricity_cost": power,
            "machine_cost": machine,
            "total_cost": round_money(filament + power + machine),
            "wasted_cost": round_money(wasted),
        }

    def rollup(self, key: str) -> list[Rollup]:
        """Sorted rollups for one grouping key.

        Months sort chronologically; everything else sorts by cost, largest
        first, because that is the order a person reads a spend report in.

        An unrecognised key raises rather than returning an empty list: a typo
        in a grouping name should not look like a month with no spending.
        """
        if key not in ROLLUP_KEYS:
            raise ValueError(
                "unknown rollup key %r (expected one of: %s)" % (key, ", ".join(ROLLUP_KEYS))
            )
        groups = list(self.rollups.get(key, {}).values())
        if key == "month":
            return sorted(groups, key=lambda r: r.key)
        return sorted(groups, key=lambda r: (-r.total_cost, r.key))


def _group_key(kind: str, cost: JobCost) -> str:
    if kind == "material":
        return cost.material or "unknown"
    if kind == "printer":
        return cost.printer or "unknown"
    if kind == "month":
        return cost.month or "unknown"
    if kind == "status":
        return cost.status
    if kind == "spool":
        return "spool %s" % cost.spool_id if cost.spool_id is not None else "no spool"
    raise ValueError("unknown rollup key: %s" % kind)


def build_report(
    jobs: Iterable[Job],
    spools: Mapping[int, Spool],
    printers: Optional[Mapping[str, Printer]] = None,
    inputs: Optional[CostInputs] = None,
) -> CostReport:
    """Cost every job and build all the rollups in one pass.

    ``spools`` is keyed by spool id and ``printers`` by printer name; both are
    passed in whole so this function stays pure and trivially testable without
    a database.
    """
    inputs = inputs or CostInputs()
    printers = printers or {}
    report = CostReport(currency=inputs.currency, inputs=inputs)
    report.rollups = {k: {} for k in ROLLUP_KEYS}

    for job in jobs:
        spool = spools.get(job.spool_id) if job.spool_id is not None else None
        printer = printers.get(job.printer) if job.printer else None
        cost = compute_job_cost(job, spool, printer, inputs)
        report.job_costs.append(cost)
        for kind in ROLLUP_KEYS:
            key = _group_key(kind, cost)
            bucket = report.rollups[kind]
            if key not in bucket:
                bucket[key] = Rollup(key=key, currency=cost.currency)
            bucket[key].add(cost)

    if report.job_costs:
        report.currency = report.job_costs[0].currency
    return report
