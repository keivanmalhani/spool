"""Pull filament and time estimates out of a sliced .gcode file.

Slicers write their estimates as comments. Every slicer writes them
differently, some at the top of the file and some at the bottom, so the parser
walks the file once from start to finish and keeps whatever it recognises.

Files are read line by line and never loaded whole. A 200 MB gcode file for a
multi-day print is entirely normal, and reading it into a string to run a
regex over would be a needless several hundred megabytes of resident memory on
a Raspberry Pi that has one gigabyte in total.

Supported flavours:

* PrusaSlicer and SuperSlicer  ``; filament used [g] = 12.34``
* OrcaSlicer and Bambu Studio  same key/value comment style as PrusaSlicer
* Cura                         ``;Filament used: 1.234m`` and ``;TIME:3723``

Anything unrecognised parses to an empty result rather than an exception. A
file with no metadata is a normal thing to hand this parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .models import length_to_mass_g, volume_cm3_to_mass_g

#: Slicer flavours this module can name.
FLAVOR_PRUSA = "prusaslicer"
FLAVOR_SUPER = "superslicer"
FLAVOR_ORCA = "orcaslicer"
FLAVOR_BAMBU = "bambustudio"
FLAVOR_CURA = "cura"
FLAVOR_UNKNOWN = "unknown"

#: Emitted by the generator banner most slicers put on line 1.
_GENERATORS: tuple[tuple[str, str], ...] = (
    ("prusaslicer", FLAVOR_PRUSA),
    ("superslicer", FLAVOR_SUPER),
    ("orcaslicer", FLAVOR_ORCA),
    ("bambustudio", FLAVOR_BAMBU),
    ("bambu studio", FLAVOR_BAMBU),
    ("cura_steamengine", FLAVOR_CURA),
    ("cura", FLAVOR_CURA),
)

# PrusaSlicer family. Values may be a comma separated list on a multi-material
# machine, e.g. "; filament used [g] = 12.34, 5.67", which we sum.
_RE_PRUSA_MM = re.compile(r"^;\s*(?:total\s+)?filament\s+used\s*\[mm\]\s*=\s*(.+)$", re.I)
_RE_PRUSA_CM3 = re.compile(r"^;\s*(?:total\s+)?filament\s+used\s*\[cm3\]\s*=\s*(.+)$", re.I)
_RE_PRUSA_G = re.compile(r"^;\s*(?:total\s+)?filament\s+used\s*\[g\]\s*=\s*(.+)$", re.I)
_RE_PRUSA_TIME = re.compile(
    r"^;\s*estimated\s+printing\s+time\s*(?:\(normal\s+mode\))?\s*=\s*(.+)$", re.I
)
# Orca and Bambu put two time fields on one line:
#   "; model printing time: 3h 12m 40s; total estimated time: 3h 20m 5s"
# so this is scanned with finditer and the value is bounded by the semicolon.
# Capturing to end of line instead would hand parse_duration both durations,
# which it would happily add together into a nonsense total.
_RE_ORCA_TIME = re.compile(
    r"(?:total\s+estimated\s+time|model\s+printing\s+time)\s*[:=]\s*([^;]+)", re.I
)
_RE_FIL_TYPE = re.compile(r"^;\s*filament_type\s*=\s*(.+)$", re.I)
_RE_FIL_DIAM = re.compile(r"^;\s*filament_diameter\s*=\s*(.+)$", re.I)
_RE_FIL_DENSITY = re.compile(r"^;\s*filament_density\s*=\s*(.+)$", re.I)

# Cura. "Filament used" is in METRES, which is the classic unit trap here.
_RE_CURA_FIL = re.compile(r"^;\s*Filament\s+used\s*:\s*(.+)$", re.I)
_RE_CURA_TIME = re.compile(r"^;\s*TIME\s*:\s*([0-9.]+)\s*$", re.I)

_RE_GENERATOR = re.compile(r"^;\s*(?:generated\s+with|generated\s+by)\s+(.+)$", re.I)

#: "1d 2h 3m 4s" and any subset of it.
_RE_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.I)

_SECONDS_PER = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}


def parse_duration(text: str) -> Optional[int]:
    """Parse a slicer duration string into whole seconds.

    Handles ``"1h 2m 3s"``, ``"2d 3h"``, ``"45m"`` and a bare number of
    seconds. Returns None when nothing numeric is present.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None

    # Trailing prose such as Orca's "; total estimated time: 1h 2m 3s" is fine;
    # anything after the last unit is ignored by the finditer below.
    parts = _RE_DURATION_PART.findall(text)
    if parts:
        total = 0.0
        for value, unit in parts:
            total += float(value) * _SECONDS_PER[unit.lower()]
        return int(round(total))

    # A bare number is seconds (Cura's ;TIME: form after the prefix is stripped).
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def _sum_numbers(text: str) -> Optional[float]:
    """Sum the numbers in a possibly comma separated slicer value.

    Multi-extruder files report one figure per tool. The total is what we want,
    because a job consumes all of it off (in the simple case) one spool.
    """
    if text is None:
        return None
    total = 0.0
    found = False
    for chunk in re.split(r"[,\s]+", text.strip()):
        if not chunk:
            continue
        try:
            total += float(chunk)
        except ValueError:
            continue
        found = True
    return total if found else None


def _first_number(text: str) -> Optional[float]:
    """First number in a string, for single-valued fields like diameter."""
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else None


def _parse_cura_filament(text: str) -> Optional[float]:
    """Cura's ``;Filament used: 1.234m, 0.5m`` in millimetres.

    Cura quotes metres. Getting this wrong by a factor of a thousand produces a
    print that appears to use three grams instead of three kilograms, which is
    exactly the kind of error that looks plausible on a dashboard.
    """
    total_m = 0.0
    found = False
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        value = _first_number(chunk)
        if value is None:
            continue
        # A unit-less value is still metres; Cura has always written "m".
        total_m += value
        found = True
    if not found:
        return None
    return total_m * 1000.0


@dataclass
class GcodeInfo:
    """Whatever the parser could find in one gcode file.

    Every numeric field is optional. Callers must handle None rather than
    assume a slicer wrote the comment they wanted.
    """

    path: str = ""
    slicer: str = FLAVOR_UNKNOWN
    filament_mm: Optional[float] = None
    filament_cm3: Optional[float] = None
    filament_g: Optional[float] = None
    duration_s: Optional[int] = None
    filament_type: Optional[str] = None
    diameter_mm: Optional[float] = None
    density_g_cm3: Optional[float] = None
    lines_scanned: int = 0
    fields_found: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """The file's stem, a reasonable default job name."""
        return Path(self.path).stem if self.path else ""

    def found_anything(self) -> bool:
        """True when at least one recognised field was present."""
        return bool(self.fields_found)

    def resolve_mass_g(
        self,
        diameter_mm: Optional[float] = None,
        density_g_cm3: Optional[float] = None,
    ) -> Optional[float]:
        """Best available mass in grams, or None.

        Preference order, most to least direct:

        1. grams stated by the slicer
        2. extrusion volume times density
        3. length times cross-section times density

        Explicit arguments win over anything the file said, because the spool
        in the machine is the ground truth for diameter and density; the file
        only records what the slicer profile claimed.
        """
        if self.filament_g is not None:
            return self.filament_g

        density = density_g_cm3 or self.density_g_cm3
        if self.filament_cm3 is not None and density:
            return volume_cm3_to_mass_g(self.filament_cm3, density)

        diameter = diameter_mm or self.diameter_mm
        if self.filament_mm is not None and diameter and density:
            return length_to_mass_g(self.filament_mm, diameter, density)
        return None

    def summary(self) -> str:
        """One-line human description, for CLI output."""
        bits = ["slicer=%s" % self.slicer]
        if self.filament_g is not None:
            bits.append("filament=%.2f g" % self.filament_g)
        elif self.filament_mm is not None:
            bits.append("filament=%.1f mm" % self.filament_mm)
        if self.duration_s is not None:
            bits.append("time=%s" % format_duration(self.duration_s))
        if self.filament_type:
            bits.append("type=%s" % self.filament_type)
        return ", ".join(bits)


def format_duration(seconds: float) -> str:
    """Render seconds as ``2d 3h 04m`` / ``3h 04m`` / ``12m 30s``."""
    seconds = int(max(0, round(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return "%dd %dh %02dm" % (days, hours, minutes)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm %02ds" % (minutes, secs)
    return "%ds" % secs


def _detect_flavor(text: str) -> Optional[str]:
    lowered = text.lower()
    for needle, flavor in _GENERATORS:
        if needle in lowered:
            return flavor
    return None


def parse_lines(lines: Iterable[str], path: str = "") -> GcodeInfo:
    """Parse an iterable of gcode lines. The file-based entry point wraps this.

    Split out from :func:`parse_file` so tests (and callers holding gcode from
    somewhere other than disk) can feed it any iterator of strings.
    """
    info = GcodeInfo(path=path)
    found: set[str] = set()

    for raw in lines:
        info.lines_scanned += 1

        # The overwhelming majority of lines in a gcode file are movement
        # commands. Rejecting them with one startswith is what keeps a
        # multi-hundred-megabyte scan tolerable.
        line = raw.lstrip()
        if not line or line[0] != ";":
            continue
        line = line.rstrip("\r\n")

        match = _RE_GENERATOR.match(line)
        if match:
            flavor = _detect_flavor(match.group(1))
            if flavor:
                info.slicer = flavor
                found.add("slicer")
            continue

        # Some Cura builds write ";Generated with Cura_SteamEngine" but Orca
        # and Bambu identify themselves in a plain banner comment instead.
        if info.slicer == FLAVOR_UNKNOWN:
            flavor = _detect_flavor(line)
            if flavor:
                info.slicer = flavor
                found.add("slicer")

        match = _RE_PRUSA_G.match(line)
        if match:
            value = _sum_numbers(match.group(1))
            if value is not None:
                info.filament_g = value
                found.add("filament_g")
            continue

        match = _RE_PRUSA_MM.match(line)
        if match:
            value = _sum_numbers(match.group(1))
            if value is not None:
                info.filament_mm = value
                found.add("filament_mm")
            continue

        match = _RE_PRUSA_CM3.match(line)
        if match:
            value = _sum_numbers(match.group(1))
            if value is not None:
                info.filament_cm3 = value
                found.add("filament_cm3")
            continue

        match = _RE_PRUSA_TIME.match(line)
        if match:
            value = parse_duration(match.group(1))
            if value is not None:
                info.duration_s = value
                found.add("duration_s")
            continue

        match = _RE_CURA_TIME.match(line)
        if match:
            value = parse_duration(match.group(1))
            if value is not None:
                info.duration_s = value
                found.add("duration_s")
                if info.slicer == FLAVOR_UNKNOWN:
                    info.slicer = FLAVOR_CURA
            continue

        match = _RE_CURA_FIL.match(line)
        if match:
            value = _parse_cura_filament(match.group(1))
            if value is not None:
                info.filament_mm = value
                found.add("filament_mm")
                if info.slicer == FLAVOR_UNKNOWN:
                    info.slicer = FLAVOR_CURA
            continue

        orca_times = [parse_duration(m.group(1)) for m in _RE_ORCA_TIME.finditer(line)]
        orca_times = [t for t in orca_times if t is not None]
        if orca_times:
            # Model time excludes tool changes and heat-up; the total is what
            # the machine is actually busy for, so the larger one wins.
            best = max(orca_times)
            if info.duration_s is None or best > info.duration_s:
                info.duration_s = best
                found.add("duration_s")
            continue

        match = _RE_FIL_TYPE.match(line)
        if match:
            value = match.group(1).strip().split(";")[0].strip()
            first = value.split(",")[0].strip()
            if first:
                info.filament_type = first
                found.add("filament_type")
            continue

        match = _RE_FIL_DIAM.match(line)
        if match:
            value = _first_number(match.group(1))
            if value and value > 0:
                info.diameter_mm = value
                found.add("diameter_mm")
            continue

        match = _RE_FIL_DENSITY.match(line)
        if match:
            value = _first_number(match.group(1))
            if value and value > 0:
                info.density_g_cm3 = value
                found.add("density_g_cm3")
            continue

    # A file with PrusaSlicer-style keys but no banner is still that family.
    if info.slicer == FLAVOR_UNKNOWN and {"filament_g", "filament_cm3"} & found:
        info.slicer = FLAVOR_PRUSA

    info.fields_found = sorted(found)
    return info


def parse_file(path: str | Path, *, encoding: str = "utf-8") -> GcodeInfo:
    """Parse a gcode file from disk, streaming it one line at a time.

    Decoding errors are replaced rather than raised: gcode files sometimes
    carry a stray byte in a thumbnail block or a profile name, and that is no
    reason to refuse to read the estimates.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("no such gcode file: %s" % path)
    with path.open("r", encoding=encoding, errors="replace") as handle:
        # A file object is already a lazy iterator of lines, so this hands
        # parse_lines one line at a time without materialising the file.
        return parse_lines(handle, path=str(path))
