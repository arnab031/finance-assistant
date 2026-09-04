"""
Relative-date resolution.

Deliberately NOT the model's job. The baseline probe showed qwen2.5:7b happily
returning an Indian fiscal year for "FY2026", and date arithmetic is the kind of
thing a 7B gets subtly wrong in ways that are hard to spot in a demo. So Python
resolves the common relative phrases and injects the concrete windows into the
prompt; the model picks one rather than computing it.

Windows are half-open [start, end), matching Period and the SQL compiler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from api.config import settings


def today() -> date:
    """Wall clock, unless pinned. Pinning keeps a demo reproducible and lets the
    eval set assert fixed answers regardless of when it runs."""
    return settings.reference_date or date.today()


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


@dataclass(frozen=True)
class Window:
    start: date
    end: date          # exclusive
    label: str

    def as_period(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "label": self.label}


def _label_month(d: date) -> str:
    return d.strftime("%B %Y")


def resolved_windows(ref: date | None = None) -> dict[str, Window]:
    """The relative phrases worth pre-computing, keyed by the phrase itself."""
    t = ref or today()
    this_month = _month_start(t)
    last_month = _add_months(this_month, -1)
    this_q = _quarter_start(t)
    last_q = _add_months(this_q, -3)

    return {
        "this month": Window(this_month, _add_months(this_month, 1), _label_month(this_month)),
        "last month": Window(last_month, this_month, _label_month(last_month)),
        "this quarter": Window(this_q, _add_months(this_q, 3),
                               f"Q{(this_q.month - 1) // 3 + 1} {this_q.year}"),
        "last quarter": Window(last_q, this_q,
                               f"Q{(last_q.month - 1) // 3 + 1} {last_q.year}"),
        "last 3 months": Window(_add_months(this_month, -3), this_month, "last 3 months"),
        "last 6 months": Window(_add_months(this_month, -6), this_month, "last 6 months"),
        "last 12 months": Window(_add_months(this_month, -12), this_month, "last 12 months"),
        "year to date": Window(date(t.year, 1, 1), t + timedelta(days=1), f"{t.year} to date"),
        "this year": Window(date(t.year, 1, 1), date(t.year + 1, 1, 1), str(t.year)),
        "last year": Window(date(t.year - 1, 1, 1), date(t.year, 1, 1), str(t.year - 1)),
    }


def prompt_block(ref: date | None = None) -> str:
    """Injected into LLM call #1 so the model selects a window instead of
    deriving one."""
    t = ref or today()
    lines = [f"Today is {t.isoformat()} ({t.strftime('%A, %d %B %Y')}).",
             "Resolved date windows - use these EXACT values when the question "
             "uses the phrase. All are half-open [start, end):"]
    for phrase, w in resolved_windows(t).items():
        lines.append(f'  "{phrase}" -> start {w.start}, end {w.end}  (label "{w.label}")')
    return "\n".join(lines)
