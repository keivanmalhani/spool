# spool

Local-first filament inventory and true print cost calculator for 3D printing.

[![CI](https://github.com/keivanmalhani/spool/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/spool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: none](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen.svg)](#no-dependencies-on-purpose)

Read this in [Spanish / en espanol](README.es.md).

![spool demo: log a failed print and watch the cost report account for it](docs/demo.gif)

`spool` tracks what filament you own and what it cost, ingests finished print
jobs from Klipper/Moonraker, OctoPrint or a `.gcode` file, decrements your
spools, and tells you what every print actually cost, including electricity,
machine wear and the prints that failed.

---

## Why

Ask a 3D printing hobbyist what a part costs and you will usually get the price
of the filament in it. That number is always too low, for three reasons:

1. **The failures are missing.** A print that peeled off the bed at 40 percent
   still burned 40 percent of the plastic, 40 percent of the electricity and
   40 percent of the hours. It produced nothing.
2. **The electricity is missing.** A bed-heated printer running for nine hours
   is not free, and the number is easy to compute once and never think about.
3. **The machine is missing.** Nozzles, belts, hotends and eventually the
   printer itself wear out over a countable number of hours.

`spool` computes all four lines and shows you the total. It also keeps the
inventory honest: every job it records comes off a specific spool, so
`spool list` answers "do I have enough grey PETG for this weekend" without
anyone having to weigh anything.

## No dependencies on purpose

**`spool` requires nothing at runtime except the Python standard library.**
`urllib.request` for HTTP, `sqlite3` for storage, `argparse` for the CLI, `json`
for everything else. `pytest` is the only development dependency.

This is a deliberate constraint, not an accident of scope. The natural place to
run this tool is the Raspberry Pi already sitting next to the printer running
Klipper: a machine with limited memory, no compiler toolchain worth the name,
and an SD card you do not want to spend twenty minutes rebuilding wheels onto.
Anything that pulls in a scientific stack for arithmetic that fits in one
function, or a charting library for a bar chart that is thirty lines of SVG, is
a dependency you will be maintaining for years. `pip install .` here takes a
second and works offline.

---

## Install

```bash
git clone https://github.com/keivanmalhani/spool.git
cd spool
pip install .
```

Or for development:

```bash
pip install -e ".[dev]"
```

Python 3.11 or newer. No other requirements.

## Quick start

You do not need a printer to try this. The repository ships an offline fixture.

```bash
spool init
spool add --material PLA  --brand Prusament --color "Galaxy Black" --price 29.99
spool add --material PETG --brand Overture  --color Grey --price 21.50 --remaining 180

spool printer add "Voron 2.4" --watts 150 --price 900 --life-hours 3000
spool config --set tariff_per_kwh=0.30 --set default_watts=120

spool sync --fixture examples/jobs.json --spool 1
spool cost --by month
spool dashboard --out dashboard.html
```

`spool list` shows the inventory:

```
ID  MATERIAL  BRAND      COLOR          DIA  LEFT g  NEW g  REMAINING                          PRICE   PER g
--  --------  ---------  ------------  ----  ------  -----  -----------------------------  ---------  ------
 1  PLA       Prusament  Galaxy Black  1.75     198   1000  [####----------------]  19.8%  USD 29.99  0.0300
 2  PETG      Overture   Grey          1.75     180   1000  [####----------------]  18.0%  USD 21.50  0.0215

2 spool(s), 378 g on hand, approx USD 9.80 of unused filament.
```

`spool cost` shows where the money went:

```
By month (USD)
  KEY      JOBS  GRAMS  HOURS  FILAMENT  POWER  MACHINE  TOTAL  WASTED  FAIL
  -------  ----  -----  -----  --------  -----  -------  -----  ------  ----
  2026-01     3    235   12.6      7.06   0.57     3.78  11.41    3.62   33%
  2026-02     3    149    8.3      4.47   0.35     1.59   6.41    0.16    0%
  2026-03     4    418   22.4     12.52   0.89     2.69  16.10    2.31   25%

Summary
  Jobs            10  (7 ok, 2 failed, 1 cancelled)
  Failure rate    20.0%
  Filament used   802 g
  Printer time    1d 19h 16m
  Filament cost   USD 24.06
  Electricity     USD 1.80
  Machine wear    USD 8.06
  TOTAL           USD 33.92
  Wasted on fails USD 6.09  (152 g)
  Cost per gram   USD 0.04
```

`spool dashboard` writes one self-contained HTML file: inventory cards with
remaining-percentage bars and low-stock highlighting, a cost-per-month bar
chart, a material breakdown donut, the recent jobs table and a summary strip.
Inline CSS, inline JavaScript, hand-drawn inline SVG. No CDN, no web font, no
external reference of any kind. It opens on a machine that has never been
online, and a test asserts the file contains no URL at all.

---

## The cost model

Every print costs four things. `spool` prices each one separately so you can
see which one is actually hurting.

| Line | Formula | Where the inputs come from |
| --- | --- | --- |
| Filament | `grams_used * (spool price / spool net weight)` | The spool the job came off |
| Electricity | `(seconds / 3600) * (watts / 1000) * tariff_per_kwh` | `spool printer add --watts`, else `default_watts`; tariff from `spool config` |
| Machine wear | `(seconds / 3600) * (machine price / expected life hours)` | `spool printer add --price --life-hours` |
| Failure waste | The full cost of any job that produced nothing | The `status` and `--failed-at` on the job |

And the inputs you set once:

| Input | Set with | Default | Notes |
| --- | --- | --- | --- |
| `tariff_per_kwh` | `spool config --set tariff_per_kwh=0.30` | `0.0` | Flat rate per kWh, in your currency |
| `default_watts` | `spool config --set default_watts=120` | `0.0` | Used for printers with no registered profile |
| `default_machine_cost_per_hour` | `spool config --set ...` | `0.0` | Fallback amortisation |
| `currency` | `spool config --set currency=EUR` | `USD` | Display only, no conversion is performed |
| `low_stock_pct` | `spool config --set low_stock_pct=15` | `15.0` | Threshold for the LOW flag |
| Per-printer watts | `spool printer add NAME --watts 150` | none | Wins over `default_watts` |
| Per-printer wear | `spool printer add NAME --price 900 --life-hours 3000` | none | Straight-line amortisation |

### How failures are costed

A job records the filament and time a **complete** run would take, plus how far
it actually got:

```bash
spool use "drawer organiser" --spool 1 --grams 212 --duration 11h30m \
    --status failed --failed-at 0.35
```

That is 35 percent of the filament, 35 percent of the electricity and 35
percent of the machine hours, all of it counted as waste because nothing usable
came out. Recording the fraction is the whole point: assuming every failure
wasted a full spool is as wrong as ignoring failures entirely.

`--failed-at` accepts `0.35`, `35` or `35%`.

**Failure rate** counts only jobs with status `failed`. Cancelling a print
because you changed your mind is not a machine failure, so `cancelled` jobs are
excluded from the rate. They still appear in the waste total, because the
plastic is still in the bin.

### How money is rounded

Every intermediate value is a full-precision float. Rounding to cents happens
once, at the point a number is shown to a person, using `ROUND_HALF_UP` rather
than Python's default banker's rounding, so `0.125` becomes `0.13` the way an
invoice would.

Where a total is displayed next to the lines that make it up, the total shown
is the sum of the lines shown. Those can differ from the rounded exact total by
a cent, and when they do, the version that adds up on screen is the one to
print. The exact value stays available on the API for further arithmetic.

## Filament densities

Length-to-mass conversion needs a density. `spool` uses these nominal values:

| Material | Density (g/cm3) |
| --- | --- |
| PLA | 1.24 |
| PETG | 1.27 |
| ABS | 1.04 |
| ASA | 1.07 |
| TPU | 1.21 |

**These are nominal published figures, not measurements of the spool in your
hand.** Real filament varies by brand, by pigment load and by batch. Filled
filaments (wood, carbon fibre, glow, metal) can be a long way off. Any spool
can override the default:

```bash
spool add --material PLA-CF --price 39.00 --density 1.30
```

An unknown material keeps the label you gave it, falls back to the PLA figure,
and warns you on stderr that it did.

The conversion itself, for the record:

```
mass_g = length_mm * pi * (diameter_mm / 2)^2 * density_g_cm3 / 1000
```

One metre of 1.75 mm PLA at 1.24 g/cm3 is 2.9825 g, which matches the rule of
thumb that a metre of PLA is about three grams. The test suite checks this
against a hand calculation at both 1.75 mm and 2.85 mm.

---

## Commands

| Command | What it does |
| --- | --- |
| `spool init` | Create or upgrade the database. Safe to run on an existing one. |
| `spool add` | Add a spool. `--material` and `--price` required. |
| `spool list [--all]` | Inventory table. `--all` includes archived spools. |
| `spool use NAME` | Record a job by hand. |
| `spool import PATH` | Import a sliced `.gcode` file as a job. |
| `spool sync` | Pull job history from Moonraker, OctoPrint or a fixture. |
| `spool cost` | The cost report. `--by material\|printer\|month\|spool\|status`. |
| `spool dashboard` | Write the self-contained HTML dashboard. |
| `spool archive ID` | Hide a used-up spool. It stays in the cost history. |
| `spool restock ID` | Refill a spool and bring it back into the inventory. |
| `spool printer add\|list` | Register printers for per-machine power and wear. |
| `spool config [--set K=V]` | Show or change the cost model settings. |

The database is `./spool.db` by default, overridable with `--db PATH` or the
`SPOOL_DB` environment variable.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command did what was asked. |
| `1` | An error you need to fix: bad input, unreachable printer, unknown spool. |
| `2` | Nothing to report: no spools, no jobs in range, no metadata in the file. |

`2` is separated from `0` on purpose. `spool cost --since 2026-01` finding no
jobs is not a failure, but a cron job piping the report somewhere wants to know
the difference between "here is your report" and "there was nothing".

### G-code support

`spool import` streams the file one line at a time and never loads it into
memory, because a 200 MB gcode file for a multi-day print is normal.

| Slicer | Filament | Time | Notes |
| --- | --- | --- | --- |
| PrusaSlicer | `; filament used [g] / [mm] / [cm3]` | `; estimated printing time (normal mode) =` | Grams given directly |
| SuperSlicer | Same as PrusaSlicer | Same, handles `1d 4h 30m 10s` | |
| OrcaSlicer | Same as PrusaSlicer | `; total estimated time:` | Two times on one line; the total wins |
| Bambu Studio | Same as PrusaSlicer | `; total estimated time:` | Multi-extruder values are summed |
| Cura | `;Filament used: 4.321m` | `;TIME:3723` | **Metres**, and no weight, so mass is derived |

A file with no metadata does not crash anything. It reports that it found
nothing and exits `2`, and you can supply `--grams` yourself.

### Syncing from a printer

```bash
export MOONRAKER_KEY="..."          # only if your instance requires one
spool sync --moonraker http://printer.local:7125 \
           --api-key-env MOONRAKER_KEY \
           --spool 1 --printer "Voron 2.4"
```

```bash
export OCTOPRINT_KEY="..."
spool sync --octoprint http://octopi.local \
           --api-key-env OCTOPRINT_KEY \
           --spool 1
```

Sync is **idempotent**. Jobs are keyed by source and source job id, so running
it on a timer never double-counts a print. `--dry-run` shows what it would add.

Moonraker and OctoPrint both report filament as a **length**. The mass depends
on the filament actually loaded, which only your inventory knows, so pass
`--spool ID` and `spool` converts using that spool's diameter and density.

OctoPrint's `/api/history` endpoint comes from the Print History plugin, which
is what keeps a durable job log on an OctoPrint box.

---

## Security

`spool` is a local tool that holds a record of what you own. It is built so
that there is very little to get wrong.

- **Local only.** One SQLite file on your disk. No server, no cloud, no
  account, no sign-up, no sync service.
- **No telemetry.** Nothing is measured, counted or reported anywhere. There is
  no analytics code in this repository, and the test suite asserts the
  dashboard contains no URL at all, so it cannot acquire a beacon by accident.
- **No network except the printer you name.** The only outbound requests are to
  the `--moonraker` or `--octoprint` base URL you pass on the command line. No
  URL pointing at a private network is hardcoded anywhere in the source, and
  only `http` and `https` are accepted, so a configuration string cannot be
  turned into a file reader.
- **Every request has an explicit timeout.** A printer that has gone away fails
  the sync in seconds rather than hanging.
- **Secrets come from the environment, never from a flag.** `--api-key-env VAR`
  takes the *name* of an environment variable and reads it itself. There is
  deliberately no `--api-key` flag.

  Why: command lines are not private. They land in your shell history file, in
  `ps` output readable by every user on the machine, in systemd unit files, in
  CI logs, and in the crash reports of anything that captures a process tree.
  An environment variable is not perfect either, but it is not written to disk
  by default and it is not visible to other users' `ps`.

  Argparse's prefix matching would happily accept `--api-key SECRET` as an
  abbreviation of `--api-key-env` and then print the secret back in the error,
  so abbreviation is turned off across the whole CLI. If you do type a key into
  `--api-key-env` by mistake, `spool` notices that it does not look like a
  variable name and refuses **without echoing it**.
- **No secret is ever written to the database or the dashboard.** API keys are
  held in memory for the duration of one request and are not persisted.
- **No secret reaches a log, an exception or a repr.** The source adapters
  override `__repr__` to exclude the key, and all error text passes through a
  redactor as defence in depth. The test suite asserts this explicitly for the
  repr, for network errors, for HTTP status errors and for JSON decode errors.
- **User data is escaped.** Spool names, printer names and job names are user
  input, and every one of them is HTML-escaped before it reaches the dashboard.

The threat model this does *not* cover: `spool` does not encrypt the database,
and anyone with read access to the file can see your inventory. If that matters
to you, put it on an encrypted volume.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

**370 tests, 628 assertions.** They cover:

- Unit conversion against hand-computed masses at 1.75 mm and 2.85 mm, and
  length to mass to length round trips.
- The cost engine against a scenario worked out by hand, including the
  40-percent-failure case, zero-gram and zero-duration jobs, and the rounding
  behaviour.
- One G-code fixture per slicer flavour, a file with no metadata at all, and a
  50,000 line file whose peak memory is asserted with `tracemalloc` to be a
  fraction of the file size, proving the parse really is streaming.
- Schema creation, migration idempotency, upgrading a version 1 database in
  place, spool decrement including the never-negative and shortfall-reporting
  behaviour, and archive visibility.
- Both HTTP adapters driven through an injected fake opener, so **no test
  touches the network**, plus explicit assertions that an API key appears in no
  repr and no error message.
- Sync idempotency, dashboard self-containment (asserted by searching the
  output for `http`), and every SVG chunk parsed as XML.
- The CLI end to end through `main([...])`, with exit codes asserted.

CI runs the suite on Python 3.11 and 3.12, then uninstalls pytest and imports
every module to prove the runtime really has no third party dependencies.

### Layout

```
src/spool/
  models.py     dataclasses, densities, length/mass conversion
  db.py         sqlite3 schema and migrations by version
  gcode.py      streaming slicer metadata parser
  cost.py       the cost engine
  sources.py    Moonraker, OctoPrint and offline fixture adapters
  report.py     plain text rendering
  dashboard.py  self-contained HTML writer
  cli.py        argparse entry point
```

---

## Limitations

Stated plainly, because a cost tool that overstates its own accuracy is worse
than no cost tool.

- **Densities are nominal.** The table above is published typical values, not
  your spool. Expect a few percent of error on unfilled filament and
  potentially much more on filled or foaming filament. Override with
  `--density` when it matters.
- **Slicer estimates are estimates.** Print time in particular is routinely off
  by 10 to 30 percent depending on how well the slicer's acceleration model
  matches your printer. Filament length is usually much closer, but it still
  assumes no purging, no priming tower and perfect flow.
- **The electricity tariff is flat rate only.** There is no support for
  time-of-use pricing, tiered rates, standing charges, demand charges or
  solar export. If you are on a variable tariff, the number is an average at
  best.
- **Printer wattage is a single average.** Real draw swings from several
  hundred watts during bed heat-up to a fraction of that during a long
  small-layer print. One average figure over a whole print is a reasonable
  approximation and nothing more.
- **Machine amortisation is straight-line.** Divide the purchase price by
  expected life hours. It ignores consumables you replace along the way
  (nozzles, belts, PTFE), resale value and repairs.
- **Multi-material prints are attributed to one spool.** Multi-extruder gcode
  values are summed and charged to the spool you name. A tool-changer user
  wanting per-tool attribution will need to split jobs by hand.
- **One currency at a time.** Spools carry a currency code, but no conversion
  is performed. Mixing currencies in one database produces a meaningless total.
- **Failure fractions are yours to supply.** `spool` cannot know how far a
  print got unless you tell it, or unless the source reports enough to infer it
  (Moonraker does; OctoPrint's history does not). Without a fraction, a failure
  is conservatively assumed to have consumed everything.
- **Sync only reads.** `spool` never writes to your printer, never uploads and
  never starts a job.

## License

MIT. See [LICENSE](LICENSE).

Copyright (c) 2026 Keivan Malhani
