"""Core domain objects for spool.

Three dataclasses carry the whole domain: Spool (what you own), Job (what you
printed) and Printer (what you printed it on). Everything else in the package
reads and writes these.

Units are stated explicitly in every field name because filament data arrives
from slicers and printer APIs in a mix of millimetres, metres, cubic
centimetres and grams, and silently mixing them is the single easiest way to
produce a plausible-looking wrong answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import ClassVar, Optional

# --------------------------------------------------------------------------
# Materials and densities
# --------------------------------------------------------------------------

#: Nominal filament densities in g/cm3.
#:
#: These are typical published values, not measurements of the specific spool
#: in your hand. Real filament varies by brand, by pigment load and by batch;
#: filled filaments (wood, carbon, glow) can be far off. Any spool can override
#: the default with an explicit density.
DENSITY_DEFAULTS: dict[str, float] = {
    "PLA": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
}

#: Density used when the material is not one we know about.
FALLBACK_DENSITY = 1.24

#: Materials with a first-class density default. "other" is always allowed.
KNOWN_MATERIALS: tuple[str, ...] = ("PLA", "PETG", "ABS", "ASA", "TPU", "other")

#: Filament diameters the hobby world actually uses.
COMMON_DIAMETERS: tuple[float, ...] = (1.75, 2.85)

#: Job outcomes. "failed" means the printer stopped on an error or the print
#: detached; "cancelled" means a human stopped it. Both waste filament, only
#: "failed" counts toward the failure rate.
JOB_STATUSES: tuple[str, ...] = ("success", "failed", "cancelled")


def normalize_material(material: str) -> str:
    """Return a canonical material name.

    Matching is case insensitive against the known list. Anything unrecognised
    is returned stripped but otherwise untouched, so a user who types
    "PLA-CF" keeps their label instead of having it flattened to "other".
    """
    cleaned = (material or "").strip()
    if not cleaned:
        return "other"
    for known in KNOWN_MATERIALS:
        if cleaned.lower() == known.lower():
            return known
    return cleaned


def density_for(material: str) -> float:
    """Nominal density in g/cm3 for a material name.

    Unknown materials fall back to the PLA figure. Callers that care about
    accuracy should pass an explicit density instead of trusting this.
    """
    return DENSITY_DEFAULTS.get(normalize_material(material).upper(), FALLBACK_DENSITY)


def is_known_material(material: str) -> bool:
    """True when the material has a published density default in this module."""
    return normalize_material(material).upper() in DENSITY_DEFAULTS


# --------------------------------------------------------------------------
# Length / mass conversion
# --------------------------------------------------------------------------


def length_to_mass_g(length_mm: float, diameter_mm: float, density_g_cm3: float) -> float:
    """Convert a length of filament to a mass in grams.

    The filament is a cylinder::

        volume_mm3 = length_mm * pi * (diameter_mm / 2) ** 2

    A cubic centimetre is 1000 cubic millimetres, and density is quoted in
    g/cm3, so::

        mass_g = volume_mm3 / 1000 * density_g_cm3

    which is the ``/ 1000`` in the expression below. Worked example: one metre
    of 1.75 mm PLA at 1.24 g/cm3 is 2.9826 g, which matches the roughly 3 g per
    metre rule of thumb that every 3D printing forum quotes.
    """
    if length_mm <= 0 or diameter_mm <= 0 or density_g_cm3 <= 0:
        return 0.0
    radius_mm = diameter_mm / 2.0
    volume_mm3 = length_mm * math.pi * radius_mm * radius_mm
    return volume_mm3 * density_g_cm3 / 1000.0


def mass_to_length_mm(mass_g: float, diameter_mm: float, density_g_cm3: float) -> float:
    """Inverse of :func:`length_to_mass_g`. Returns millimetres of filament."""
    if mass_g <= 0 or diameter_mm <= 0 or density_g_cm3 <= 0:
        return 0.0
    radius_mm = diameter_mm / 2.0
    volume_mm3 = mass_g * 1000.0 / density_g_cm3
    return volume_mm3 / (math.pi * radius_mm * radius_mm)


def volume_cm3_to_mass_g(volume_cm3: float, density_g_cm3: float) -> float:
    """Convert a slicer-reported extrusion volume to grams."""
    if volume_cm3 <= 0 or density_g_cm3 <= 0:
        return 0.0
    return volume_cm3 * density_g_cm3


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass
class Spool:
    """One physical spool of filament.

    ``spool_weight_g`` is the net filament weight when new, not the gross
    weight including the plastic reel. ``remaining_g`` is what is left now and
    is what jobs decrement.
    """

    material: str
    brand: str = ""
    color: str = ""
    diameter_mm: float = 1.75
    spool_weight_g: float = 1000.0
    density_g_cm3: float = 0.0
    price: float = 0.0
    currency: str = "USD"
    purchased: Optional[str] = None
    #: Negative means "not specified", which is read as a brand new spool. A
    #: plain 0.0 default would make it impossible to record an empty spool.
    remaining_g: float = -1.0
    notes: str = ""
    archived: bool = False
    id: Optional[int] = None

    def __post_init__(self) -> None:
        self.material = normalize_material(self.material)
        if not self.density_g_cm3:
            self.density_g_cm3 = density_for(self.material)
        if self.remaining_g < 0:
            self.remaining_g = float(self.spool_weight_g)

    @property
    def price_per_gram(self) -> float:
        """Cost of one gram of this filament, or 0.0 when unknown.

        Based on the net weight when new, which is the only honest denominator:
        you paid the price for the whole spool, not for what is left today.
        """
        if self.spool_weight_g <= 0:
            return 0.0
        return self.price / self.spool_weight_g

    @property
    def remaining_fraction(self) -> float:
        """Remaining filament as a 0.0 - 1.0 fraction of the new weight."""
        if self.spool_weight_g <= 0:
            return 0.0
        return max(0.0, min(1.0, self.remaining_g / self.spool_weight_g))

    @property
    def remaining_pct(self) -> float:
        """Remaining filament as a 0 - 100 percentage."""
        return self.remaining_fraction * 100.0

    @property
    def label(self) -> str:
        """Short human label, e.g. "Prusament PLA Galaxy Black"."""
        parts = [p for p in (self.brand, self.material, self.color) if p]
        return " ".join(parts) or self.material

    def remaining_length_mm(self) -> float:
        """How many millimetres of filament are left on this spool."""
        return mass_to_length_mm(self.remaining_g, self.diameter_mm, self.density_g_cm3)


@dataclass
class Job:
    """One print job, finished or abandoned.

    ``filament_g`` is what the job would consume if it ran to completion. For a
    job that stopped early, the filament actually burned is
    ``filament_g * failed_at_fraction`` - see :mod:`spool.cost`. Storing the
    full figure plus the fraction keeps both numbers recoverable.
    """

    name: str
    printer: str = ""
    spool_id: Optional[int] = None
    filament_g: float = 0.0
    duration_s: int = 0
    status: str = "success"
    started: Optional[str] = None
    failed_at_fraction: Optional[float] = None
    filament_mm: Optional[float] = None
    source: str = "manual"
    source_job_id: Optional[str] = None
    notes: str = ""
    id: Optional[int] = None

    def __post_init__(self) -> None:
        status = (self.status or "success").strip().lower()
        if status not in JOB_STATUSES:
            status = "failed"
        self.status = status
        self.duration_s = int(self.duration_s or 0)
        if self.failed_at_fraction is not None:
            self.failed_at_fraction = max(0.0, min(1.0, float(self.failed_at_fraction)))

    @property
    def completed_fraction(self) -> float:
        """Fraction of the job that actually ran, in 0.0 - 1.0.

        A successful job is 1.0 by definition. A job that stopped early uses
        its recorded fraction, and falls back to 1.0 when no fraction was
        recorded, because assuming a failure wasted everything is the
        conservative reading when we genuinely do not know.
        """
        if self.status == "success":
            return 1.0
        if self.failed_at_fraction is None:
            return 1.0
        return self.failed_at_fraction

    @property
    def is_waste(self) -> bool:
        """True when nothing usable came out of this job."""
        return self.status != "success"

    @property
    def month(self) -> str:
        """The ``YYYY-MM`` bucket this job belongs to, or "unknown"."""
        if not self.started or len(self.started) < 7:
            return "unknown"
        return self.started[:7]

    def grams_used(self) -> float:
        """Filament actually consumed, accounting for an early stop."""
        return max(0.0, self.filament_g * self.completed_fraction)

    def seconds_used(self) -> float:
        """Printer time actually spent, accounting for an early stop."""
        return max(0.0, self.duration_s * self.completed_fraction)

    def hours_used(self) -> float:
        """Printer time actually spent, in hours."""
        return self.seconds_used() / 3600.0


@dataclass
class Printer:
    """A machine, so electricity and amortisation can differ per printer.

    ``watts`` is average draw over a print, not the nameplate maximum of the
    power supply. A bed-heating i3 style printer averages far less than its
    peak. ``price`` and ``life_hours`` together give an amortised cost per
    hour; leave them at zero to ignore machine wear entirely.
    """

    name: str
    watts: float = 0.0
    price: float = 0.0
    life_hours: float = 0.0
    notes: str = ""
    id: Optional[int] = None

    def machine_cost_per_hour(self) -> float:
        """Straight-line amortisation of the machine over its expected life."""
        if self.price <= 0 or self.life_hours <= 0:
            return 0.0
        return self.price / self.life_hours


@dataclass
class Settings:
    """Persisted defaults used by the cost engine.

    Kept as a dataclass rather than loose kwargs so that adding an input to the
    cost model is a single visible change here and in the settings table.
    """

    tariff_per_kwh: float = 0.0
    currency: str = "USD"
    default_watts: float = 0.0
    default_machine_cost_per_hour: float = 0.0
    low_stock_pct: float = 15.0

    #: Type coercion for values loaded out of the key/value settings table.
    FIELD_TYPES: ClassVar[dict[str, type]] = {
        "tariff_per_kwh": float,
        "currency": str,
        "default_watts": float,
        "default_machine_cost_per_hour": float,
        "low_stock_pct": float,
    }
