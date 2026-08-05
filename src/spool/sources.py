"""Adapters that turn a printer's job history into :class:`~spool.models.Job`.

Three sources ship: Moonraker (Klipper), OctoPrint, and a local JSON fixture so
the whole pipeline can be demonstrated and tested with no printer switched on.

Design rules these adapters follow, and the reasons:

* **No URL is ever hardcoded.** ``base_url`` is always supplied by the caller.
  A tool that ships with someone's home IP baked in is a tool that leaks
  someone's home network topology into a public repository.
* **API keys live in the environment, never in argv.** The CLI takes
  ``--api-key-env VAR`` and reads the variable itself. Command lines end up in
  shell history, in ``ps`` output visible to every user on the box, and in CI
  logs.
* **Keys never reach a log, a repr, or an exception.** Error text is passed
  through a redactor as defence in depth, so even a future change that puts a
  key somewhere it should not be cannot print it.
* **Every request has an explicit timeout.** A printer that has gone away
  should fail the sync in seconds, not hang until someone notices.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable

from .models import Job

#: Requests give up after this many seconds unless told otherwise.
DEFAULT_TIMEOUT = 10.0

#: Upper bound on jobs pulled in one sync, so a five-year-old printer with a
#: huge history does not produce an unbounded response.
DEFAULT_LIMIT = 200

#: Replacement text wherever a secret might otherwise appear.
REDACTED = "***"


class SourceError(Exception):
    """A source could not be read: network, HTTP status, or malformed payload.

    Always safe to show to a user; the message is redacted before it is raised.
    """


@runtime_checkable
class JobSource(Protocol):
    """The whole contract a source has to satisfy."""

    name: str

    def list_jobs(self) -> list[Job]:
        """Return normalised jobs, newest last."""
        ...


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _redact(text: str, *secrets: Optional[str]) -> str:
    """Blank out any secret that appears in ``text``."""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in out:
            out = out.replace(secret, REDACTED)
    return out


def _normalize_base_url(base_url: str) -> str:
    """Validate and tidy a base URL.

    Only http and https are accepted. Allowing arbitrary schemes here would
    turn a config string into a file reader.
    """
    if not base_url or not str(base_url).strip():
        raise SourceError("base_url is required")
    base_url = str(base_url).strip().rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise SourceError(
            "base_url must start with http:// or https:// (got %r)" % parsed.scheme
        )
    if not parsed.netloc:
        raise SourceError("base_url is missing a host")
    return base_url


def _iso_from_epoch(value: Any) -> Optional[str]:
    """Convert an epoch seconds value to a UTC ISO-8601 string."""
    if value in (None, "", 0):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        # Some builds hand back an already formatted timestamp.
        text = str(value).strip()
        return text or None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    """Tolerant float conversion for values from a foreign API."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(mapping: dict, *keys: str, default: Any = None) -> Any:
    """First present, non-None value among ``keys``.

    Printer firmware and plugins rename fields between versions; trying a few
    spellings is cheaper than pinning a version.
    """
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def http_get_json(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Optional[Callable[..., Any]] = None,
    secret: Optional[str] = None,
) -> Any:
    """GET a URL and decode JSON, raising :class:`SourceError` on any problem.

    ``opener`` exists so tests can inject a canned response instead of touching
    the network; it defaults to :func:`urllib.request.urlopen`.
    """
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(
            _redact("HTTP %s from %s" % (exc.code, url), secret)
        ) from None
    except (urllib.error.URLError, OSError) as exc:
        # URLError and TimeoutError are both OSError; a printer that is off,
        # a bad hostname and a timeout all land here.
        reason = getattr(exc, "reason", exc)
        raise SourceError(
            _redact("cannot reach %s: %s" % (url, reason), secret)
        ) from None

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError(_redact("response from %s was not UTF-8" % url, secret)) from None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SourceError(
            _redact("invalid JSON from %s: %s" % (url, exc), secret)
        ) from None


# --------------------------------------------------------------------------
# Moonraker (Klipper)
# --------------------------------------------------------------------------


@dataclass(init=False)
class MoonrakerSource:
    """Reads ``GET {base_url}/server/history/list`` from a Moonraker instance.

    Moonraker reports ``filament_used`` in millimetres. When the slicer wrote
    a weight into the file metadata, Moonraker passes it through as
    ``metadata.filament_weight_total`` in grams and that is preferred, because
    it already accounts for the actual filament profile.
    """

    name = "moonraker"

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        limit: int = DEFAULT_LIMIT,
        printer_name: str = "",
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self._api_key = api_key or None
        self.timeout = float(timeout)
        self.limit = int(limit)
        self.printer_name = printer_name or "klipper"
        self._opener = opener

    def __repr__(self) -> str:
        """Deliberately omits the API key. Never add it here."""
        return "MoonrakerSource(base_url=%r, timeout=%r, limit=%r)" % (
            self.base_url,
            self.timeout,
            self.limit,
        )

    __str__ = __repr__

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        return headers

    def fetch(self) -> Any:
        """Raw decoded JSON from the history endpoint."""
        url = "%s/server/history/list?limit=%d&order=asc" % (self.base_url, self.limit)
        return http_get_json(
            url,
            headers=self._headers(),
            timeout=self.timeout,
            opener=self._opener,
            secret=self._api_key,
        )

    def list_jobs(self) -> list[Job]:
        """Fetch and normalise the printer's job history."""
        payload = self.fetch()
        entries = _moonraker_entries(payload)
        return [self._to_job(e) for e in entries if isinstance(e, dict)]

    def _to_job(self, entry: dict) -> Job:
        metadata = entry.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        raw_status = str(_first(entry, "status", default="") or "").lower()
        status = _moonraker_status(raw_status)

        filename = str(_first(entry, "filename", "job_name", default="") or "")
        name = Path(filename).name or filename or "moonraker job"

        filament_mm = _as_float(_first(entry, "filament_used", default=0.0))
        if not filament_mm:
            filament_mm = _as_float(_first(metadata, "filament_total", default=0.0))
        grams = _as_float(_first(metadata, "filament_weight_total", default=0.0))

        duration = _as_float(_first(entry, "print_duration", "total_duration", default=0.0))
        estimated = _as_float(_first(metadata, "estimated_time", default=0.0))

        fraction = None
        if status != "success" and estimated > 0 and duration > 0:
            # How far it got before it stopped, capped at 1.0. This is the
            # honest basis for waste: an eight-hour print that died after one
            # hour did not consume eight hours of filament.
            fraction = min(1.0, duration / estimated)
            # The recorded filament and duration are what actually happened, so
            # scale the estimate up to a notional full run and let the fraction
            # bring it back down. That keeps Job.filament_g meaning "what a
            # complete run costs" for every source.
        job = Job(
            name=name,
            printer=self.printer_name,
            filament_g=grams,
            filament_mm=filament_mm or None,
            duration_s=int(round(duration)),
            status=status,
            started=_iso_from_epoch(_first(entry, "start_time", default=None)),
            failed_at_fraction=None,
            source=self.name,
            source_job_id=str(_first(entry, "job_id", "uid", default="") or "") or None,
        )
        if fraction is not None and fraction < 1.0:
            # Job.filament_g is the full-run figure; the fraction reconstructs
            # what was really used. Moonraker reports actuals, so convert.
            if fraction > 0:
                job.filament_g = grams / fraction if grams else 0.0
                if job.filament_mm:
                    job.filament_mm = job.filament_mm / fraction
                job.duration_s = int(round(estimated))
            job.failed_at_fraction = fraction
        return job


def _moonraker_entries(payload: Any) -> list[Any]:
    """Dig the job list out of a Moonraker response."""
    if isinstance(payload, dict):
        result = payload.get("result", payload)
        if isinstance(result, dict):
            jobs = result.get("jobs")
            if isinstance(jobs, list):
                return jobs
        if isinstance(result, list):
            return result
    if isinstance(payload, list):
        return payload
    raise SourceError("unexpected Moonraker response shape (no jobs list)")


def _moonraker_status(raw: str) -> str:
    """Map a Moonraker status string onto our three outcomes."""
    if raw in ("completed", "complete", "finished", "done"):
        return "success"
    if raw in ("cancelled", "canceled"):
        return "cancelled"
    # error, klippy_shutdown, klippy_disconnect, server_exit, interrupted, ...
    return "failed"


# --------------------------------------------------------------------------
# OctoPrint
# --------------------------------------------------------------------------


@dataclass(init=False)
class OctoPrintSource:
    """Reads ``GET {base_url}/api/history`` with an ``X-Api-Key`` header.

    That endpoint is provided by the Print History plugin, which is what
    actually keeps a durable job log on an OctoPrint box. Lengths come back in
    millimetres and volumes in cubic centimetres.
    """

    name = "octoprint"

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        limit: int = DEFAULT_LIMIT,
        printer_name: str = "",
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self._api_key = api_key or None
        self.timeout = float(timeout)
        self.limit = int(limit)
        self.printer_name = printer_name or "octoprint"
        self._opener = opener

    def __repr__(self) -> str:
        """Deliberately omits the API key. Never add it here."""
        return "OctoPrintSource(base_url=%r, timeout=%r, limit=%r)" % (
            self.base_url,
            self.timeout,
            self.limit,
        )

    __str__ = __repr__

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        return headers

    def fetch(self) -> Any:
        """Raw decoded JSON from the history endpoint."""
        url = "%s/api/history" % self.base_url
        return http_get_json(
            url,
            headers=self._headers(),
            timeout=self.timeout,
            opener=self._opener,
            secret=self._api_key,
        )

    def list_jobs(self) -> list[Job]:
        """Fetch and normalise the printer's job history."""
        payload = self.fetch()
        entries = _octoprint_entries(payload)
        jobs = [self._to_job(e) for e in entries if isinstance(e, dict)]
        return jobs[-self.limit :] if self.limit and len(jobs) > self.limit else jobs

    def _to_job(self, entry: dict) -> Job:
        filename = str(_first(entry, "fileName", "file", "name", default="") or "")
        name = Path(filename).name or filename or "octoprint job"

        status = _octoprint_status(entry)
        filament_mm = _as_float(_first(entry, "filamentLength", "filament_length", default=0.0))
        volume_cm3 = _as_float(_first(entry, "filamentVolume", "filament_volume", default=0.0))
        duration = _as_float(_first(entry, "printTime", "print_time", default=0.0))

        job = Job(
            name=name,
            printer=str(_first(entry, "printerProfile", "printer", default="") or self.printer_name),
            filament_g=0.0,
            filament_mm=filament_mm or None,
            duration_s=int(round(duration)),
            status=status,
            started=_iso_from_epoch(_first(entry, "timestamp", "startTime", default=None)),
            source=self.name,
            source_job_id=str(_first(entry, "id", "uuid", default="") or "") or None,
            notes="volume_cm3=%.4f" % volume_cm3 if volume_cm3 else "",
        )
        return job


def _octoprint_entries(payload: Any) -> list[Any]:
    """Dig the history list out of an OctoPrint response."""
    if isinstance(payload, dict):
        for key in ("history", "jobs", "files"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    if isinstance(payload, list):
        return payload
    raise SourceError("unexpected OctoPrint response shape (no history list)")


def _octoprint_status(entry: dict) -> str:
    """Map an OctoPrint history entry onto our three outcomes."""
    raw = _first(entry, "success", "result", "status", default=None)
    if isinstance(raw, bool):
        return "success" if raw else "failed"
    if isinstance(raw, (int, float)):
        return "success" if int(raw) == 1 else "failed"
    text = str(raw or "").strip().lower()
    if text in ("true", "success", "done", "completed", "finished"):
        return "success"
    if text in ("cancelled", "canceled"):
        return "cancelled"
    return "failed"


# --------------------------------------------------------------------------
# Offline fixture
# --------------------------------------------------------------------------


@dataclass(init=False)
class FixtureSource:
    """Reads jobs from a local JSON file.

    This is not a testing crutch bolted on the side; it is how the project
    demonstrates itself. Somebody evaluating this repository can run the whole
    pipeline against ``examples/jobs.json`` without owning a 3D printer, and
    the test suite exercises the same normalisation path the real adapters use.

    Accepts either a bare list of job objects or ``{"jobs": [...]}``. Field
    names follow the :class:`~spool.models.Job` dataclass, with a couple of
    friendly aliases.
    """

    name = "fixture"

    def __init__(self, path: str | Path, *, printer_name: str = "") -> None:
        self.path = Path(path)
        self.printer_name = printer_name or ""

    def __repr__(self) -> str:
        return "FixtureSource(path=%r)" % str(self.path)

    def list_jobs(self) -> list[Job]:
        """Read and normalise the fixture file."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SourceError("cannot read fixture %s: %s" % (self.path, exc)) from None
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise SourceError("invalid JSON in fixture %s: %s" % (self.path, exc)) from None

        if isinstance(payload, dict):
            entries = payload.get("jobs")
        else:
            entries = payload
        if not isinstance(entries, list):
            raise SourceError("fixture %s must hold a list of jobs" % self.path)
        return [self._to_job(e) for e in entries if isinstance(e, dict)]

    def _to_job(self, entry: dict) -> Job:
        started = _first(entry, "started", "start_time", "timestamp", default=None)
        if isinstance(started, (int, float)):
            started = _iso_from_epoch(started)
        fraction = _first(entry, "failed_at_fraction", "failed_at", default=None)
        return Job(
            name=str(_first(entry, "name", "filename", "fileName", default="job") or "job"),
            printer=str(_first(entry, "printer", default=self.printer_name) or ""),
            spool_id=_maybe_int(_first(entry, "spool_id", default=None)),
            filament_g=_as_float(_first(entry, "filament_g", "grams", default=0.0)),
            filament_mm=(
                _as_float(_first(entry, "filament_mm", "length_mm", default=0.0)) or None
            ),
            duration_s=int(_as_float(_first(entry, "duration_s", "duration", default=0.0))),
            status=str(_first(entry, "status", default="success") or "success"),
            started=str(started) if started else None,
            failed_at_fraction=None if fraction is None else _as_float(fraction),
            source=self.name,
            source_job_id=str(_first(entry, "id", "job_id", default="") or "") or None,
            notes=str(_first(entry, "notes", default="") or ""),
        )


def _maybe_int(value: Any) -> Optional[int]:
    """Int conversion that returns None instead of raising."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_grams(jobs: Iterable[Job], diameter_mm: float, density_g_cm3: float) -> int:
    """Fill in ``filament_g`` from ``filament_mm`` where the source gave length only.

    Returns the number of jobs updated. Moonraker and OctoPrint both report
    length; the mass depends on the filament actually loaded, which only the
    local inventory knows.
    """
    from .models import length_to_mass_g  # local import keeps module import cheap

    updated = 0
    for job in jobs:
        if job.filament_g and job.filament_g > 0:
            continue
        if not job.filament_mm:
            continue
        job.filament_g = length_to_mass_g(job.filament_mm, diameter_mm, density_g_cm3)
        updated += 1
    return updated
