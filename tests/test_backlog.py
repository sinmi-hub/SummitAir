"""Tests that pin down BACKLOG.md items to concrete, checkable behavior.

Each test below documents a real gap or a real guarantee named in the
backlog. A test that currently fails is a gap; a test that currently
passes locks in behavior we don't want to regress while the gap is open.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.exceptions import ConnectionClosedError

from app.realtime import Call, CallManager
from app.tools import _t_book

from test_sip import Socket, config


# --- "Add new-customer row creation so first-time callers can book." -------
# Fixed: an unknown phone number now gets a new sheet row instead of a refusal.

async def test_unknown_caller_gets_a_new_row_through_the_full_call_path(tmp_path):
    """A first-time caller with no existing sheet row now books successfully:
    a new row is created for them first. Exercised through Call.execute, not
    _t_book directly, so it also proves offered-slot tracking agrees.
    """
    manager = SimpleNamespace(settings=config(tmp_path, human_transfer_number="+15551234567"),
                              action=AsyncMock(), worker=None)
    from concurrent.futures import ThreadPoolExecutor
    manager.worker = ThreadPoolExecutor(max_workers=1)
    call = Call(manager, "rtc_test", Socket())
    slot = "2026-09-18T10:00:00-05:00"
    call.offered_slots.add(slot)
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        store.return_value.get_lead_by_phone.return_value = None
        new_lead = SimpleNamespace(name="Jordan Lee", phone="+15559990000", row=7)
        store.return_value.create_lead.return_value = new_lead
        calendar.return_value.book_meeting.return_value = "evt_new"
        result = await call.execute(
            "book", {"phone": "+15559990000", "slot_iso": slot, "name": "Jordan Lee"})
    assert result["booked"]
    store.return_value.create_lead.assert_called_once_with("+15559990000", "Jordan Lee")
    store.return_value.set_meeting_ref.assert_called_once_with(new_lead, slot, "evt_new")
    manager.worker.shutdown(wait=True, cancel_futures=True)


# --- "Persist intake fields ... to the sheet, not just a generic summary." -
# The sheet's notes column is exactly where free-text intake belongs (no new
# columns needed). The real gap: booking never writes to notes at all.

def test_booking_never_logs_caller_intake_to_sheet_notes():
    with patch("app.integrations.crm.SheetsLeadStore") as store, \
         patch("app.integrations.gcal.GoogleCalendar") as calendar:
        lead = SimpleNamespace(name="Test", notes="")
        store.return_value.get_lead_by_phone.return_value = lead
        calendar.return_value.book_meeting.return_value = "evt_test"
        result = _t_book({
            "phone": "+15551234567", "slot_iso": "2026-09-18T10:00:00-05:00",
            "notes": "No heat, elderly resident, urgent",
        })
        assert result["booked"]
        # Fixed: the issue/urgency now reaches the calendar event's description,
        # not its title (name/the matched lead's name builds the title instead --
        # see test_tools.py). Still-open gap: nothing calls log_call, so the
        # sheet's own notes column, the one place meant for exactly this, stays
        # untouched.
        store.return_value.log_call.assert_not_called()
        assert calendar.return_value.book_meeting.call_args.kwargs["description"] == \
            "No heat, elderly resident, urgent"
        assert calendar.return_value.book_meeting.call_args.args[1] == "Test — HVAC Service Call"


# --- "Investigate the unexplained ConnectionClosedError ... before relying
#      on the control socket holding up mid-call." --------------------------

class FlakySocket:
    """A socket that answers normally, then drops mid-call like a real
    control connection can, instead of ending cleanly.
    """
    def __init__(self, events, exc):
        self.events, self.exc = list(events), exc
        self.messages, self.closed = [], False

    async def send(self, raw):
        self.messages.append(json.loads(raw))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.events:
            return json.dumps(self.events.pop(0))
        raise self.exc


async def test_control_socket_drop_mid_call_hangs_up_without_reaccepting(tmp_path):
    import httpx

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200)

    socket = FlakySocket(events=[], exc=ConnectionClosedError(None, None))
    factory = AsyncMock(return_value=socket)
    http = httpx.AsyncClient(base_url="https://api.openai.com/v1/",
                             transport=httpx.MockTransport(handle))
    manager = CallManager(config(tmp_path), http, factory)

    await manager.run("rtc_test")

    # One accept, one connect attempt, no retry after a connected socket dies.
    assert factory.await_count == 1
    accept_and_hangup = [r.url.path.rsplit("/", 1)[-1] for r in requests]
    assert accept_and_hangup == ["accept", "hangup"]
    assert socket.closed
    await manager.close()
