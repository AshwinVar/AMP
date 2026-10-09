# Transcribed HMI sheets

Figures read off machine screens and typed in by hand, for machines that cannot
yet reach AMP by wire. `hmi_sheet.py` imports them. Every row here is the
machine's own measurement; a person copied the digits, which is why these rows
live in their own id namespace (`hmi-day:` / `hmi-hour:`) and are outranked by
anything the controller writes itself.

## Where each figure came from

| File | Screen | Taken |
|---|---|---|
| `shrinidhi-2026-10-day.csv` | ARICO **MONTH** page — a day's shots and kWh per row | 9 Oct 2026 |
| `shrinidhi-2026-10-09-hour.csv` | ARICO **HOUR PROD.** page — 24 hourly buckets | 9 Oct 2026 |

## A blank kWh is not a zero

`kwh` is left empty wherever the screen did not report energy. Three different
situations all produce a blank, and none of them is "the machine drew no power":

- **No energy column at all.** The older HOUR PROD. page (IMM-04, IMM-08,
  IMM-10) prints shots and nothing else.
- **A column of 0.0.** IMM-12 prints an energy row that reads 0.0 for every
  hour of a day it made 3,658 shots. Its `ENERGY PULSE KWh` constant on the
  MASTER 1 page is `000.0`, so each meter pulse is multiplied by zero. The
  machine is counting pulses and scaling them to nothing.
- **A screen too dark to read.** Left blank rather than guessed.

Writing 0.0 for any of these would draw a bar asserting a measurement that was
never taken. `_energy_hours` in `ai/plant_board.py` already refuses to invent
those zeros; this file must not hand it any.

## These rows are meant to be thrown away

Four machines can write their own history to a USB stick from the HOUR PROD.
page's `EXPORT DATA` button — IMM-03, IMM-11, IMM-12 and IMM-13. The moment one
of those exports is imported, `arico_import` deletes the transcribed rows for
every day it covers, because the controller's own file outranks a photograph.
IMM-03's export is already in: its rows here are kept only so that re-running
the import exercises that rule against real data.

## Accuracy

Hourly figures are read from a photograph of a 7-inch screen. Where a day's
hours are summed and compared against the machine's own printed day total, the
two agree to within about 0.1% — a digit or two misread across a day of ~3,500
shots. That is immaterial to a chart and is not a reason to prefer these rows
over an export. It is a reason to get the export.
