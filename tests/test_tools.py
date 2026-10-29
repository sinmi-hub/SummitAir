"""Direct tests of app/tools.py's HANDLERS: lookup, availability, book, reschedule.

test_sip.py exercises tools through Call.execute (dedup, offered-slot
checks, transport). These tests isolate each handler's own argument
handling and its contract with the CRM/calendar adapters, independent of
the call machinery.
"""
from types import SimpleNamespace
from unittest.mock import patch

from app.tools import HANDLERS, _t_availability, _t_book, _t_lookup, _t_reschedule


# --- lookup -------------------------------------------------------------

def test_lookup_returns_found_false_for_unknown_phone():
    with patch("app.integrations.crm.SheetsLeadStore") as store:
        store.return_value.get_lead_by_phone.return_value = None
        result = _t_lookup({"phone": "+15559990000"})
    assert result == {"found": False}
    store.return_value.get_lead_by_phone.assert_called_once_with("+15559990000")


def test_lookup_returns_existing_lead_fields_only():
    lead = SimpleNamespace(name="Jordan Lee", company="Lee Residence",
                           status="active", notes="prefers afternoons")
    with patch("app.integrations.crm.SheetsLeadStore") as store:
        store.return_value.get_lead_by_phone.return_value = lead
        result = _t_lookup({"phone": "+15551234567"})
    assert result == {
        "found": True, "name": "Jordan Lee", "company": "Lee Residence",
        "status": "active", "notes": "prefers afternoons",
    }


def test_lookup_defaults_to_empty_phone_rather_than_raising():
    with patch("app.integrations.crm.SheetsLeadStore") as store:
        store.return_value.get_lead_by_phone.return_value = None
        result = _t_lookup({})
    assert result == {"found": False}
    store.return_value.get_lead_by_phone.assert_called_once_with("")


# --- availability ---------------------------------------------------------

def test_availability_passes_through_requested_days_ahead():
    with patch("app.integrations.gcal.GoogleCalendar") as calendar:
        calendar.return_value.find_open_slots.return_value = []
        _t_availability({"days_ahead": 3})
    calendar.return_value.find_open_slots.assert_called_once_with(days_ahead=3)


def test_availability_defaults_days_ahead_to_five():
    with patch("app.integrations.gcal.GoogleCalendar") as calendar:
        calendar.return_value.find_open_slots.return_value = []
        _t_availability({})
    calendar.return_value.find_open_slots.assert_called_once_with(days_ahead=5)


def test_availability_shapes_slots_as_label_and_iso():
    slot = SimpleNamespace(label=lambda: "Tuesday, June 3 at 2:00 PM",
                           iso=lambda: "2026-06-03T14:00:00-05:00")
    with patch("app.integrations.gcal.GoogleCalendar") as calendar:
        calendar.return_value.find_open_slots.return_value = [slot]
        result = _t_availability({"days_ahead": 5})
    assert result == {"slots": [
        {"label": "Tuesday, June 3 at 2:00 PM", "iso": "2026-06-03T14:00:00-05:00"},
    ]}


# --- book -------------------------------------------------------------

def test_book_builds_calendar_title_from_the_supplied_name():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        store.return_value.get_lead_by_phone.return_value = SimpleNamespace(name="")
        calendar.return_value.book_meeting.return_value = "evt_1"
        _t_book({"phone": "+15551234567", "slot_iso": "2026-09-18T10:00:00-05:00",
                "name": "Jordan Lee", "notes": "No heat"})
    assert calendar.return_value.book_meeting.call_args.args[1] == "Jordan Lee — HVAC Service Call"


def test_book_falls_back_to_the_matched_leads_name_when_name_is_omitted():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        store.return_value.get_lead_by_phone.return_value = SimpleNamespace(name="Jordan Lee")
        calendar.return_value.book_meeting.return_value = "evt_1"
        _t_book({"phone": "+15551234567", "slot_iso": "2026-09-18T10:00:00-05:00"})
    assert calendar.return_value.book_meeting.call_args.args[1] == "Jordan Lee — HVAC Service Call"


def test_book_passes_notes_as_the_calendar_description_not_the_title():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        store.return_value.get_lead_by_phone.return_value = SimpleNamespace(name="Jordan Lee")
        calendar.return_value.book_meeting.return_value = "evt_1"
        _t_book({"phone": "+15551234567", "slot_iso": "2026-09-18T10:00:00-05:00",
                "notes": "No heat, elderly resident, urgent"})
    assert calendar.return_value.book_meeting.call_args.kwargs["description"] == \
        "No heat, elderly resident, urgent"
    assert "No heat" not in calendar.return_value.book_meeting.call_args.args[1]


def test_book_never_sends_calendar_invites():
    """Booking always passes send_updates="none"; no one gets emailed."""
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        store.return_value.get_lead_by_phone.return_value = SimpleNamespace(name="Jordan Lee")
        calendar.return_value.book_meeting.return_value = "evt_1"
        _t_book({"phone": "+15551234567", "slot_iso": "2026-09-18T10:00:00-05:00"})
    assert calendar.return_value.book_meeting.call_args.kwargs["send_updates"] == "none"


def test_book_records_meeting_ref_against_the_matched_lead():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        lead = SimpleNamespace(name="Jordan Lee")
        store.return_value.get_lead_by_phone.return_value = lead
        calendar.return_value.book_meeting.return_value = "evt_1"
        slot = "2026-09-18T10:00:00-05:00"
        result = _t_book({"phone": "+15551234567", "slot_iso": slot})
    assert result == {"booked": True, "slot_iso": slot}
    store.return_value.set_meeting_ref.assert_called_once_with(lead, slot, "evt_1")


# --- reschedule -----------------------------------------------------------

def test_reschedule_errors_when_caller_has_no_existing_appointment():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        store.return_value.get_lead_by_phone.return_value = None
        result = _t_reschedule({"phone": "+15551234567", "slot_iso": "2026-09-18T10:00:00-05:00"})
    assert result == {"rescheduled": False,
                       "error": "No existing appointment found for this caller. Use book for a new appointment."}
    calendar.return_value.reschedule_meeting.assert_not_called()


def test_reschedule_errors_when_lead_exists_but_has_no_booked_meeting():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        store.return_value.get_lead_by_phone.return_value = SimpleNamespace(
            name="Jordan Lee", meeting_event_id="")
        result = _t_reschedule({"phone": "+15551234567", "slot_iso": "2026-09-18T10:00:00-05:00"})
    assert result["rescheduled"] is False
    calendar.return_value.reschedule_meeting.assert_not_called()


def test_reschedule_moves_the_existing_calendar_event_and_updates_the_sheet():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        lead = SimpleNamespace(name="Jordan Lee", meeting_event_id="evt_1")
        store.return_value.get_lead_by_phone.return_value = lead
        new_slot = "2026-09-19T10:00:00-05:00"
        result = _t_reschedule({"phone": "+15551234567", "slot_iso": new_slot})
    assert result == {"rescheduled": True, "slot_iso": new_slot}
    calendar.return_value.reschedule_meeting.assert_called_once_with("evt_1", new_slot)
    store.return_value.set_meeting_ref.assert_called_once_with(lead, new_slot, "evt_1")


def test_handlers_registry_exposes_exactly_the_four_tool_backed_actions():
    assert set(HANDLERS) == {"lookup", "availability", "book", "reschedule"}
