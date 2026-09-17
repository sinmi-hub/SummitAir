"""Existing Summit Air tool handlers, independent of transport."""
from __future__ import annotations

def _t_lookup(args: dict) -> dict:
    from app.integrations.crm import SheetsLeadStore

    phone = args.get("phone", "")
    lead = SheetsLeadStore().get_lead_by_phone(phone)
    if not lead:
        return {"found": False}
    return {
        "found": True,
        "name": lead.name,
        "company": lead.company,
        "status": lead.status,
        "notes": lead.notes,
    }


def _t_availability(args: dict) -> dict:
    from app.integrations.gcal import GoogleCalendar

    days = int(args.get("days_ahead", 5))
    slots = GoogleCalendar().find_open_slots(days_ahead=days)
    return {"slots": [{"label": s.label(), "iso": s.iso()} for s in slots]}


def _t_book(args: dict) -> dict:
    # TODO: crm.py has no "create a new row" method — it assumes the caller
    # already exists in the sheet (true for the old sales-lead use case, not
    # true for a fresh HVAC caller). Wire this up once the sheet's real field
    # layout is decided; for now this only works for a phone number that
    # already has a row.
    from app.integrations.crm import SheetsLeadStore
    from app.integrations.gcal import GoogleCalendar

    phone = args.get("phone", "")
    slot_iso = args.get("slot_iso", "")
    store = SheetsLeadStore()
    lead = store.get_lead_by_phone(phone)
    if not lead:
        return {"booked": False, "error": "no existing record for this phone number"}
    event_id = GoogleCalendar().book_meeting(
        slot_iso, args.get("summary", f"Service call — {lead.name or phone}"),
        send_updates="none",
    )
    store.set_meeting_ref(lead, slot_iso, event_id)
    return {"booked": True, "slot_iso": slot_iso}


HANDLERS = {"lookup": _t_lookup, "availability": _t_availability, "book": _t_book}
