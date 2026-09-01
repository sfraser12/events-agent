from datetime import date

from events_agent.annual_anchors import AnnualAnchor, due_reminders, load_annual_anchors


def test_missing_file_returns_empty_list(tmp_path):
    assert load_annual_anchors(tmp_path / "does-not-exist.yaml") == []


def test_loads_real_yaml(tmp_path):
    path = tmp_path / "annual-anchors.yaml"
    path.write_text(
        "- name: Celtic Connections\n"
        "  typical_month: january\n"
        "  programme_announced: october\n"
        "  watch_url: \"\"\n"
    )
    anchors = load_annual_anchors(path)
    assert anchors == [
        AnnualAnchor(name="Celtic Connections", typical_month="january", programme_announced="october", watch_url="")
    ]


def test_reminder_due_the_month_before_announcement():
    anchors = [AnnualAnchor(name="Celtic Connections", typical_month="january", programme_announced="october")]
    assert due_reminders(anchors, date(2026, 9, 15)) == anchors
    assert due_reminders(anchors, date(2026, 10, 1)) == []
    assert due_reminders(anchors, date(2026, 8, 31)) == []


def test_reminder_wraps_around_year_boundary():
    # programme_announced: january -> due through all of December.
    anchors = [AnnualAnchor(name="Glasgow Film Festival", typical_month="february", programme_announced="january")]
    assert due_reminders(anchors, date(2026, 12, 5)) == anchors
    assert due_reminders(anchors, date(2027, 1, 5)) == []


def test_unrecognised_month_name_is_skipped_not_raised():
    anchors = [AnnualAnchor(name="Typo Festival", typical_month="june", programme_announced="mayy")]
    assert due_reminders(anchors, date(2026, 5, 1)) == []


def test_fixed_date_reminder_due_within_window_before_next_occurrence():
    anchors = [AnnualAnchor(name="Stonehaven Fireballs", fixed_date="12-31", remind_days_before=21)]
    # 15 days out: due.
    due = due_reminders(anchors, date(2026, 12, 16))
    assert len(due) == 1
    assert due[0].next_occurrence == date(2026, 12, 31)
    # 22 days out: not yet due.
    assert due_reminders(anchors, date(2026, 12, 9)) == []
    # The day of, still due; the day after, the next occurrence is 364 days
    # off (this same fixed_date, later in the new year) so no longer due.
    assert due_reminders(anchors, date(2026, 12, 31))[0].next_occurrence == date(2026, 12, 31)
    assert due_reminders(anchors, date(2027, 1, 1)) == []


def test_fixed_date_malformed_value_is_skipped_not_raised():
    anchors = [AnnualAnchor(name="Broken", fixed_date="not-a-date")]
    assert due_reminders(anchors, date(2026, 1, 1)) == []
