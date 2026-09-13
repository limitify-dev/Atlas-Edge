"""Pure enrollment diff/sync logic — no device, no network, no I/O.

Given what Atlas says the card ↔ student list *should* be, and what the F18
currently has enrolled, work out the minimal set of ``set_user`` writes:

* card on Atlas but not on the device        -> ADD  (new device slot / uid)
* card on both, but the name differs          -> UPDATE (same uid)
* card on both, same name                      -> unchanged (no write)
* card on the device but not on Atlas          -> reported as an "orphan"
  (we never delete — Atlas is the source of additions/changes only)

Malformed Atlas rows (missing card number or name, duplicate card number) are
collected as ``skipped`` with a reason rather than aborting the whole run.
Running the same plan twice is a no-op: the second run reports everything as
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class AtlasAssignment:
    student_id: str
    name: str
    card_number: str


@dataclass(frozen=True)
class DeviceUser:
    uid: int          # the F18's internal slot number, required by set_user()
    card_number: str  # we keep the device user_id == card_number
    name: str


@dataclass(frozen=True)
class PlannedWrite:
    uid: int
    card_number: str
    name: str
    action: str  # "add" | "update"


@dataclass(frozen=True)
class SkippedRow:
    row: dict
    reason: str


@dataclass
class SyncPlan:
    to_write: list[PlannedWrite] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)      # card numbers
    skipped: list[SkippedRow] = field(default_factory=list)
    orphan_card_numbers: list[str] = field(default_factory=list)

    @property
    def adds(self) -> int:
        return sum(1 for w in self.to_write if w.action == "add")

    @property
    def updates(self) -> int:
        return sum(1 for w in self.to_write if w.action == "update")

    def summary(self) -> dict:
        return {
            "to_add": self.adds,
            "to_update": self.updates,
            "unchanged": len(self.unchanged),
            "skipped": len(self.skipped),
            "orphans": len(self.orphan_card_numbers),
        }


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def normalize_assignments(
    raw_rows: Iterable[object],
) -> tuple[list[AtlasAssignment], list[SkippedRow]]:
    """Coerce/validate rows coming from Atlas. Order is preserved; on a duplicate
    card number the *last* row wins and the earlier one is skipped."""
    kept: dict[str, AtlasAssignment] = {}
    order: list[str] = []
    skipped: list[SkippedRow] = []

    for raw in raw_rows:
        row = _as_dict(raw)
        card = _clean(row.get("card_number"))
        name = _clean(row.get("name"))
        student_id = _clean(row.get("student_id"))

        if not card:
            skipped.append(SkippedRow(row, "missing card_number"))
            continue
        if not name:
            skipped.append(SkippedRow(row, "missing name"))
            continue
        if card in kept:
            skipped.append(
                SkippedRow(kept[card].__dict__.copy(), f"duplicate card_number {card}")
            )
        else:
            order.append(card)
        kept[card] = AtlasAssignment(
            student_id=student_id, name=name, card_number=card
        )

    return [kept[c] for c in order], skipped


def next_free_uid(used: Iterable[int], start: int = 1) -> int:
    used_set = {int(u) for u in used}
    uid = start
    while uid in used_set:
        uid += 1
    return uid


def compute_sync_plan(
    device_users: Iterable[DeviceUser],
    assignments: Iterable[AtlasAssignment],
    *,
    uid_start: int = 1,
) -> SyncPlan:
    by_card: dict[str, DeviceUser] = {u.card_number: u for u in device_users}
    used_uids = {u.uid for u in by_card.values()}
    plan = SyncPlan()
    seen_cards: set[str] = set()

    for a in assignments:
        seen_cards.add(a.card_number)
        existing = by_card.get(a.card_number)
        if existing is None:
            uid = next_free_uid(used_uids, uid_start)
            used_uids.add(uid)
            plan.to_write.append(
                PlannedWrite(uid=uid, card_number=a.card_number, name=a.name, action="add")
            )
        elif existing.name != a.name:
            plan.to_write.append(
                PlannedWrite(
                    uid=existing.uid,
                    card_number=a.card_number,
                    name=a.name,
                    action="update",
                )
            )
        else:
            plan.unchanged.append(a.card_number)

    plan.orphan_card_numbers = sorted(set(by_card) - seen_cards)
    return plan


def _as_dict(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    # Accept AtlasClient.CardAssignment or any object with the right attrs.
    out = {}
    for key in ("student_id", "name", "card_number"):
        if hasattr(raw, key):
            out[key] = getattr(raw, key)
    return out


def to_assignment_list(raw_rows: Iterable[object]) -> list[AtlasAssignment]:
    """Convenience for callers that don't care about the skip reasons."""
    kept, _ = normalize_assignments(raw_rows)
    return kept


_HEADER_TOKENS = {"card", "card_number", "cardnumber", "number", "name", "student", "label"}


def parse_card_rows(text: str) -> list[dict]:
    """Parse pasted / uploaded text into ``{card_number, name}`` rows.

    One record per line; the card number is the first field, the name is the
    rest of the line. Comma, tab or semicolon separate them::

        1001,Aline Uwase
        1002<TAB>Bosco Habimana
        1003        (name optional)

    Blank lines and an obvious header row are ignored. Validation (missing
    fields, duplicate cards) is left to :func:`normalize_assignments`.
    """
    rows: list[dict] = []
    for i, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip().lstrip("﻿")
        if not line:
            continue
        for sep in (",", "\t", ";"):
            if sep in line:
                card, _, name = line.partition(sep)
                break
        else:
            card, name = line, ""
        card = card.strip()
        name = name.strip().strip('"').strip()
        # Skip a header line like "card_number,name".
        if i == 0 and card.lower() in _HEADER_TOKENS and name.lower() in _HEADER_TOKENS | {""}:
            continue
        rows.append({"card_number": card, "name": name})
    return rows
