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
    from app.integrations.crm import SheetsLeadStore
    from app.integrations.gcal import GoogleCalendar

    phone = args.get("phone", "")
    slot_iso = args.get("slot_iso", "")
    name = args.get("name", "").strip()
    store = SheetsLeadStore()
    lead = store.get_lead_by_phone(phone) or store.create_lead(phone, name)
    # Title is built here, not left to the model, so it's always human-readable
    # in the calendar even if the model omits "name" on an existing lead: the
    # matched lead's own name is the fallback, then the phone number.
    title = f"{name or lead.name or phone} — HVAC Service Call"
    event_id = GoogleCalendar().book_meeting(
        slot_iso, title, description=args.get("notes", ""), send_updates="none",
    )
    store.set_meeting_ref(lead, slot_iso, event_id)
    return {"booked": True, "slot_iso": slot_iso}


def _t_reschedule(args: dict) -> dict:
    from app.integrations.crm import SheetsLeadStore
    from app.integrations.gcal import GoogleCalendar

    phone = args.get("phone", "")
    slot_iso = args.get("slot_iso", "")
    store = SheetsLeadStore()
    lead = store.get_lead_by_phone(phone)
    if not lead or not lead.meeting_event_id:
        return {"rescheduled": False,
                "error": "No existing appointment found for this caller. Use book for a new appointment."}
    GoogleCalendar().reschedule_meeting(lead.meeting_event_id, slot_iso)
    store.set_meeting_ref(lead, slot_iso, lead.meeting_event_id)
    return {"rescheduled": True, "slot_iso": slot_iso}


HANDLERS = {"lookup": _t_lookup, "availability": _t_availability, "book": _t_book,
            "reschedule": _t_reschedule}
