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
