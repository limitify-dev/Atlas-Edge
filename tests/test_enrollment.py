"""Enrollment diff/sync logic — the part most prone to edge cases:
partial failures, re-runs, malformed rows from Atlas."""

from atlas_edge.enrollment import (
    AtlasAssignment,
    DeviceUser,
    compute_sync_plan,
    next_free_uid,
    normalize_assignments,
    parse_card_rows,
)


# ── normalize_assignments ────────────────────────────────────────────────
def test_normalize_keeps_good_rows_and_coerces_card_to_str():
    rows = [
        {"student_id": "s1", "name": "Alice", "card_number": 1001},
        {"student_id": "s2", "name": " Bob ", "card_number": " 1002 "},
    ]
    kept, skipped = normalize_assignments(rows)
    assert skipped == []
    assert kept == [
        AtlasAssignment("s1", "Alice", "1001"),
        AtlasAssignment("s2", "Bob", "1002"),
    ]


def test_normalize_skips_missing_name_or_card_with_reasons():
    rows = [
        {"student_id": "s1", "name": "", "card_number": "1001"},
        {"student_id": "s2", "name": "Bob", "card_number": None},
        {"student_id": "s3", "name": "  ", "card_number": "  "},
    ]
    kept, skipped = normalize_assignments(rows)
    assert kept == []
    reasons = sorted(s.reason for s in skipped)
    assert reasons == ["missing card_number", "missing card_number", "missing name"]


def test_normalize_dedupes_card_number_last_wins():
    rows = [
        {"student_id": "s1", "name": "Old", "card_number": "1001"},
        {"student_id": "s1b", "name": "New", "card_number": "1001"},
        {"student_id": "s2", "name": "Bob", "card_number": "1002"},
    ]
    kept, skipped = normalize_assignments(rows)
    assert [(a.card_number, a.name) for a in kept] == [("1001", "New"), ("1002", "Bob")]
    assert len(skipped) == 1 and "duplicate card_number 1001" in skipped[0].reason


def test_normalize_accepts_objects_not_just_dicts():
    class Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    kept, skipped = normalize_assignments(
        [Row(student_id="s1", name="Ada", card_number="42")]
    )
    assert kept == [AtlasAssignment("s1", "Ada", "42")]
    assert skipped == []


# ── next_free_uid ───────────────────────────────────────────────────────
def test_next_free_uid_finds_first_gap():
    assert next_free_uid([], start=1) == 1
    assert next_free_uid([1, 2, 4], start=1) == 3
    assert next_free_uid([1, 2, 3], start=1) == 4


# ── compute_sync_plan ──────────────────────────────────────────────────
def test_plan_adds_updates_and_leaves_unchanged():
    device = [
        DeviceUser(uid=1, card_number="1001", name="Alice"),
        DeviceUser(uid=2, card_number="1002", name="Bob OLD NAME"),
    ]
    atlas = [
        AtlasAssignment("s1", "Alice", "1001"),          # unchanged
        AtlasAssignment("s2", "Bob New", "1002"),        # update, keep uid 2
        AtlasAssignment("s3", "Carol", "1003"),          # add, next free uid = 3
    ]
    plan = compute_sync_plan(device, atlas)

    assert plan.unchanged == ["1001"]
    assert plan.summary() == {
        "to_add": 1,
        "to_update": 1,
        "unchanged": 1,
        "skipped": 0,
        "orphans": 0,
    }
    by_card = {w.card_number: w for w in plan.to_write}
    assert by_card["1002"].action == "update" and by_card["1002"].uid == 2
    assert by_card["1003"].action == "add" and by_card["1003"].uid == 3


def test_plan_assigns_distinct_uids_to_multiple_adds():
    device = [DeviceUser(uid=1, card_number="1001", name="A")]
    atlas = [
        AtlasAssignment("s1", "A", "1001"),
        AtlasAssignment("s2", "B", "2002"),
        AtlasAssignment("s3", "C", "3003"),
    ]
    plan = compute_sync_plan(device, atlas)
    add_uids = sorted(w.uid for w in plan.to_write if w.action == "add")
    assert add_uids == [2, 3]


def test_plan_is_idempotent_second_run_is_a_noop():
    device = [DeviceUser(uid=1, card_number="1001", name="Alice")]
    atlas = [AtlasAssignment("s1", "Alice", "1001")]
    plan = compute_sync_plan(device, atlas)
    assert plan.to_write == []
    assert plan.unchanged == ["1001"]


def test_plan_reports_orphans_but_never_plans_to_delete():
    device = [
        DeviceUser(uid=1, card_number="1001", name="Alice"),
        DeviceUser(uid=2, card_number="9999", name="Left the school"),
    ]
    atlas = [AtlasAssignment("s1", "Alice", "1001")]
    plan = compute_sync_plan(device, atlas)
    assert plan.orphan_card_numbers == ["9999"]
    assert plan.to_write == []  # we never remove device users


def test_full_flow_from_raw_atlas_rows_with_some_bad_ones():
    raw = [
        {"student_id": "s1", "name": "Alice", "card_number": "1001"},
        {"student_id": "s2", "name": "", "card_number": "1002"},          # skipped
        {"student_id": "s3", "name": "Carol", "card_number": 1003},       # add
        {"student_id": "s3b", "name": "Carol Dup", "card_number": 1003},  # dedupe
    ]
    assignments, skipped = normalize_assignments(raw)
    device = [DeviceUser(uid=5, card_number="1001", name="Alice")]
    plan = compute_sync_plan(device, assignments)

    assert len(skipped) == 2  # empty name + duplicate
    assert plan.unchanged == ["1001"]
    assert [w.card_number for w in plan.to_write] == ["1003"]
    assert plan.to_write[0].name == "Carol Dup"  # last-wins survived


# ── parse_card_rows (bulk enrol input) ─────────────────────────────────
def test_parse_rows_comma_tab_semicolon_and_name_optional():
    text = "1001,Aline Uwase\n1002\tBosco Habimana\n1003; Chantal\n1004"
    assert parse_card_rows(text) == [
        {"card_number": "1001", "name": "Aline Uwase"},
        {"card_number": "1002", "name": "Bosco Habimana"},
        {"card_number": "1003", "name": "Chantal"},
        {"card_number": "1004", "name": ""},
    ]


def test_parse_rows_skips_blanks_header_and_bom_and_quotes():
    text = '﻿card_number,name\n\n 1001 , "Alice" \n\n1002,Bob\n'
    assert parse_card_rows(text) == [
        {"card_number": "1001", "name": "Alice"},
        {"card_number": "1002", "name": "Bob"},
    ]


def test_parse_rows_feeds_normalize_assignments():
    rows = parse_card_rows("1001,Alice\n1002\n1001,Alice Again")
    kept, skipped = normalize_assignments(rows)
    assert [(a.card_number, a.name) for a in kept] == [("1001", "Alice Again")]
    reasons = sorted(s.reason for s in skipped)
    assert reasons == ["duplicate card_number 1001", "missing name"]
