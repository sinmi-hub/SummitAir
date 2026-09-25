import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from unittest.mock import AsyncMock, Mock, patch
import httpx
import pytest
from starlette.testclient import TestClient
from app.agent.persona import system_prompt
from app.realtime import Call, CallManager, TOOLS, acceptance
from app.server import create_app
from app.tools import _t_book
from config import Settings

SECRET = 'whsec_' + base64.b64encode(b'test-signing-secret').decode()

def config(tmp_path, **overrides):
    return Settings(openai_api_key='test-key', openai_webhook_secret=SECRET,
                    call_state_path=str(tmp_path / 'calls.db'), **overrides)

def signed(body, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    message = f'wh_test.{timestamp}.{body}'.encode()
    signature = base64.b64encode(hmac.new(b'test-signing-secret', message, hashlib.sha256).digest()).decode()
    return {'webhook-id': 'wh_test', 'webhook-timestamp': timestamp, 'webhook-signature': 'v1,' + signature}

def test_prompt_is_exact_file(tmp_path):
    expected = Path('app/agent/SYSTEM-PROMPT.md').read_text()
    assert system_prompt() == expected
    payload = acceptance(config(tmp_path))
    assert payload['instructions'] == expected
    assert payload['model'] == 'gpt-realtime-2.1'
    assert payload['tools'] == TOOLS
    assert not any('callback' == t['name'] for t in TOOLS)

def test_webhook_verification_and_limits(tmp_path):
    manager = SimpleNamespace(incoming=Mock(return_value=True), close=AsyncMock(), recover=AsyncMock())
    app = create_app(config(tmp_path), lambda *a, **k: manager)
    body = json.dumps({'type': 'realtime.call.incoming', 'data': {'call_id': 'rtc_test'}})
    with TestClient(app) as client:
        assert client.get('/health').json()['ok']
        assert client.post('/webhooks/openai', content=body).status_code == 400
        assert client.post('/webhooks/openai', content=body+' ', headers=signed(body)).status_code == 400
        assert client.post('/webhooks/openai', content=body, headers=signed(body, int(time.time())-600)).status_code == 400
        assert client.post('/webhooks/openai', content='x'*65537).status_code == 413
        manager.incoming.assert_not_called()
        assert client.post('/webhooks/openai', content=body, headers=signed(body)).status_code == 200
        manager.incoming.assert_called_once_with('rtc_test')
        manager.incoming.return_value = False
        assert client.post('/webhooks/openai', content=body, headers=signed(body)).status_code == 503
        for invalid in ['[]', '{', '{"type":"realtime.call.incoming","data":null}']:
            assert client.post('/webhooks/openai', content=invalid, headers=signed(invalid)).status_code == 400
        unrelated = '{"type":"response.completed"}'
        assert client.post('/webhooks/openai', content=unrelated, headers=signed(unrelated)).status_code == 204
        assert client.post('/tools/book', json={}).status_code == 404

def test_missing_secret_prevents_start(tmp_path):
    c = config(tmp_path)
    object.__setattr__(c, 'openai_webhook_secret', '')
    with pytest.raises(RuntimeError, match='required'):
        with TestClient(create_app(c)): pass

async def test_duplicate_webhooks_survive_restart(tmp_path):
    manager = CallManager(config(tmp_path))
    manager.run = AsyncMock()
    assert manager.incoming('rtc_test')
    assert manager.incoming('rtc_test')
    await asyncio.sleep(0)
    manager.run.assert_awaited_once_with('rtc_test')
    await manager.close()
    manager2 = CallManager(config(tmp_path))
    manager2.run = AsyncMock()
    assert manager2.incoming('rtc_test')
    await asyncio.sleep(0)
    manager2.run.assert_not_called()
    await manager2.close()

class Socket:
    def __init__(self, events=()):
        self.events = list(events)
        self.messages = []
        self.closed = False
    async def send(self, raw): self.messages.append(json.loads(raw))
    async def close(self): self.closed = True
    def __aiter__(self): return self
    async def __anext__(self):
        if not self.events: raise StopAsyncIteration
        return json.dumps(self.events.pop(0))

async def test_accept_attach_greet_and_cleanup(tmp_path):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200)
    socket = Socket()
    factory = AsyncMock(return_value=socket)
    http = httpx.AsyncClient(base_url='https://api.openai.com/v1/', transport=httpx.MockTransport(handle))
    manager = CallManager(config(tmp_path), http, factory)
    await manager.run('rtc_test')
    assert requests[0].url.path.endswith('/rtc_test/accept')
    assert json.loads(requests[0].content)['instructions'] == system_prompt()
    assert factory.call_args.args[0] == 'wss://api.openai.com/v1/realtime?call_id=rtc_test'
    assert socket.messages[0]['type'] == 'response.create'
    assert 'Summit Air' in socket.messages[0]['response']['instructions']
    assert requests[-1].url.path.endswith('/rtc_test/hangup')
    assert socket.closed
    await manager.close()

async def test_accept_failure_does_not_attach(tmp_path):
    factory = AsyncMock()
    http = httpx.AsyncClient(base_url='https://api.openai.com/v1/', transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    manager = CallManager(config(tmp_path), http, factory)
    await manager.run('rtc_failed')
    factory.assert_not_called()
    await manager.close()

@pytest.fixture
def call(tmp_path):
    manager = SimpleNamespace(settings=config(tmp_path, human_transfer_number='+15551234567'),
                              action=AsyncMock(), worker=ThreadPoolExecutor(max_workers=1))
    c = Call(manager, 'rtc_test', Socket())
    c.greeting_protected = False  # most tests aren't exercising greeting-window behavior
    yield c
    manager.worker.shutdown(wait=True, cancel_futures=True)

async def test_google_tool_does_not_block_events_and_is_deduplicated(call):
    started, release = threading.Event(), threading.Event()
    def lookup(args):
        started.set()
        release.wait(timeout=2)
        return {'found': True}
    event = {'type': 'response.function_call_arguments.done', 'name': 'lookup',
             'call_id': 'tool_1', 'arguments': '{"phone":"+15551234567"}'}
    with patch.dict('app.realtime.HANDLERS', lookup=lookup):
        await call.event({'type': 'response.created'})
        await call.event(event)
        await call.event(event)
        for _ in range(100):
            if started.is_set(): break
            await asyncio.sleep(.005)
        assert started.is_set()
        assert len(call.pending) == 1
        await call.event({'type': 'input_audio_buffer.speech_stopped'})
        assert call.speech_stopped_at is not None
        await call.event({'type': 'response.done'})
        assert not call.ws.messages
        release.set()
        await asyncio.gather(*list(call.pending))
    assert [m['type'] for m in call.ws.messages] == ['conversation.item.create', 'response.create']
    assert len(call.seen) == 1

async def test_tool_output_waits_for_active_response(call):
    call.response_active = True
    with patch.dict('app.realtime.HANDLERS', lookup=lambda _: {'found': False}):
        await call.event({'type': 'response.function_call_arguments.done', 'name': 'lookup',
                          'call_id': 'tool_1', 'arguments': '{"phone":"+15551234567"}'})
        await asyncio.gather(*list(call.pending))
    assert len(call.ws.messages) == 1
    await call.event({'type': 'response.done'})
    assert call.ws.messages[-1]['type'] == 'response.create'

async def test_acceptance_starts_with_interrupt_response_disabled(tmp_path):
    payload = acceptance(config(tmp_path))
    assert payload['audio']['input']['turn_detection']['interrupt_response'] is False
    transcription = payload['audio']['input']['transcription']
    assert transcription['languages'] == ['en'] and 'language' not in transcription
    assert payload['audio']['input']['turn_detection']['eagerness'] == 'low'

async def test_greeting_protection_restores_only_after_matching_playback(call):
    call.greeting_protected = True
    await call.request_greeting()
    tag = call.ws.messages[-1]['response']['metadata']['playback_gate']
    await call.event({'type': 'response.created', 'response': {'id': 'greeting', 'metadata': {'playback_gate': tag}}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'greeting'})
    await call.event({'type': 'response.done', 'response': {'id': 'greeting', 'status': 'completed'}})
    assert call.greeting_protected
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'unrelated'})
    assert call.greeting_protected
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'greeting'})
    assert not call.greeting_protected
    updates = [m for m in call.ws.messages if m['type'] == 'session.update']
    assert len(updates) == 1
    assert updates[0]['session']['audio']['input']['turn_detection'] == {
        'type': 'semantic_vad', 'eagerness': 'low', 'interrupt_response': True}
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'greeting'})
    assert len([m for m in call.ws.messages if m['type'] == 'session.update']) == 1

async def test_transfer_waits_for_playback_and_does_not_claim_answer(call):
    await call.event({'type': 'response.created'})
    await call.event({'type': 'output_audio_buffer.started'})
    transfer = asyncio.create_task(call.execute('transfer_to_human', {}))
    await call.event({'type': 'response.done'})
    await asyncio.sleep(0)
    call.manager.action.assert_not_called()
    await call.event({'type': 'output_audio_buffer.stopped'})
    result = await transfer
    call.manager.action.assert_awaited_once_with('rtc_test', 'refer', {'target_uri': 'tel:+15551234567'})
    assert result['answer_status'] == 'unknown'
    assert 'transferred' not in result
    assert not (await call.execute('transfer_to_human', {}))['transferred']
    assert call.manager.action.await_count == 1

async def test_unconfigured_transfer_and_argument_validation(call):
    object.__setattr__(call.manager.settings, 'human_transfer_number', '')
    assert not (await call.execute('transfer_to_human', {}))['transferred']
    for i, (name, args) in enumerate([('book', []), ('availability', {'days_ahead': 999}),
                                     ('transfer_to_human', {'number': '+15559876543'}), ('missing', {})]):
        await call.event({'type': 'response.function_call_arguments.done', 'name': name,
                          'call_id': f'tool_{i}', 'arguments': json.dumps(args)})
        await asyncio.gather(*list(call.pending))
        outputs = [m for m in call.ws.messages if m['type'] == 'conversation.item.create']
        assert 'error' in json.loads(outputs[-1]['item']['output'])
    call.manager.action.assert_not_called()

async def test_book_requires_offered_slot_and_never_retries_uncertainty(call):
    args = {'phone': '+15551234567', 'slot_iso': '2026-09-18T10:00:00-05:00'}
    assert not (await call.execute('book', args))['booked']
    call.offered_slots.add(args['slot_iso'])
    handler = Mock(side_effect=RuntimeError('write status unknown'))
    with patch.dict('app.realtime.HANDLERS', book=handler):
        with pytest.raises(RuntimeError): await call.execute('book', args)
        assert not (await call.execute('book', args))['booked']
    handler.assert_called_once()

def test_booking_handler_creates_a_lead_for_an_unknown_phone():
    with patch('app.integrations.crm.SheetsLeadStore') as store, patch('app.integrations.gcal.GoogleCalendar') as calendar:
        store.return_value.get_lead_by_phone.return_value = None
        new_lead = SimpleNamespace(name='')
        store.return_value.create_lead.return_value = new_lead
        calendar.return_value.book_meeting.return_value = 'evt_new'
        result = _t_book({'phone': '+15551234567', 'slot_iso': '2026-09-18T10:00:00-05:00'})
        assert result['booked']
        store.return_value.create_lead.assert_called_once_with('+15551234567', '')
        store.return_value.set_meeting_ref.assert_called_once_with(new_lead, result['slot_iso'], 'evt_new')

def test_existing_booking_handler_preserved():
    with patch('app.integrations.crm.SheetsLeadStore') as store, patch('app.integrations.gcal.GoogleCalendar') as calendar:
        lead = SimpleNamespace(name='Test')
        store.return_value.get_lead_by_phone.return_value = lead
        calendar.return_value.book_meeting.return_value = 'evt_test'
        result = _t_book({'phone': '+15551234567', 'slot_iso': '2026-09-18T10:00:00-05:00'})
        assert result['booked']
        store.return_value.create_lead.assert_not_called()
        store.return_value.set_meeting_ref.assert_called_once_with(lead, result['slot_iso'], 'evt_test')
        assert calendar.return_value.book_meeting.call_args.kwargs['send_updates'] == 'none'

async def test_orphan_recovery_hangs_up_without_reaccepting(tmp_path):
    manager = CallManager(config(tmp_path))
    with manager.db:
        manager.db.execute('INSERT INTO calls (id, created) VALUES (?, ?)', ('rtc_orphan', time.time()))
    manager.action = AsyncMock()
    await manager.recover()
    manager.action.assert_awaited_once_with('rtc_orphan', 'hangup')
    assert manager.db.execute('SELECT active FROM calls').fetchone() == (0,)
    await manager.close()

async def test_shutdown_cancels_call_and_requests_hangup(tmp_path):
    manager = CallManager(config(tmp_path))
    begun = asyncio.Event()
    async def action(call_id, name, payload=None):
        if name == 'accept':
            begun.set()
            await asyncio.Event().wait()
    manager.action = AsyncMock(side_effect=action)
    manager.incoming('rtc_shutdown')
    await begun.wait()
    await manager.close()
    assert manager.action.call_args_list[-1].args == ('rtc_shutdown', 'hangup')

async def test_socket_connection_failure_retries_then_hangs_up(tmp_path):
    factory = AsyncMock(side_effect=OSError('offline'))
    manager = CallManager(config(tmp_path), socket_factory=factory)
    manager.action = AsyncMock()
    with patch('app.realtime.asyncio.sleep', new=AsyncMock()):
        await manager.run('rtc_offline')
    assert factory.await_count == 3
    assert manager.action.call_args_list[-1].args == ('rtc_offline', 'hangup')
    await manager.close()

async def test_end_call_waits_until_closing_is_played(call):
    call.last_agent_response_id = 'closing'
    call.last_agent_text = 'Thank you for choosing Summit Air. Have a great day.'
    await call.event({'type': 'response.created', 'response': {'id': 'closing'}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'closing'})
    ending = asyncio.create_task(call.execute('end_call', {}))
    await call.event({'type': 'response.done'})
    await asyncio.sleep(0)
    call.manager.action.assert_not_called()
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'closing'})
    assert (await ending)['ended']
    call.manager.action.assert_awaited_once_with('rtc_test', 'hangup')
    assert call.ws.closed

async def test_end_call_says_goodbye_itself_when_the_model_skipped_it(call):
    call.last_agent_response_id = 'closing'
    call.last_agent_text = 'Alright, let me wrap this up for you.'
    await call.event({'type': 'response.created', 'response': {'id': 'closing'}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'closing'})
    ending = asyncio.create_task(call.execute('end_call', {}))
    await call.event({'type': 'response.done'})
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'closing'})
    await asyncio.sleep(0)
    call.manager.action.assert_not_called()
    goodbye = [m for m in call.ws.messages if m['type'] == 'response.create'
              and 'Thank you for choosing' in m.get('response', {}).get('instructions', '')]
    assert len(goodbye) == 1
    tag = goodbye[0]['response']['metadata']['playback_gate']
    await call.event({'type': 'response.created', 'response': {'id': 'injected', 'metadata': {'playback_gate': tag}}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'injected'})
    await call.event({'type': 'response.done'})
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'closing'})
    await asyncio.sleep(0)
    call.manager.action.assert_not_called()
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'injected'})
    assert (await ending)['ended']
    call.manager.action.assert_awaited_once_with('rtc_test', 'hangup')

async def test_end_call_skips_goodbye_injection_when_already_said(call):
    call.last_agent_response_id = 'closing'
    call.last_agent_text = 'Thank you for choosing Summit Air. Have a great day.'
    await call.event({'type': 'response.created', 'response': {'id': 'closing'}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'closing'})
    ending = asyncio.create_task(call.execute('end_call', {}))
    await call.event({'type': 'response.done'})
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'closing'})
    assert (await ending)['ended']
    assert not any(m['type'] == 'response.create' for m in call.ws.messages)
    call.manager.action.assert_awaited_once_with('rtc_test', 'hangup')

async def test_end_call_skips_goodbye_injection_during_emergency(call):
    call.emergency_declared = True
    call.last_agent_response_id = 'closing'
    call.last_agent_text = 'Please leave the building and call 911 immediately.'
    await call.event({'type': 'response.created', 'response': {'id': 'closing'}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'closing'})
    ending = asyncio.create_task(call.execute('end_call', {}))
    await call.event({'type': 'response.done'})
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'closing'})
    assert (await ending)['ended']
    assert not any(m['type'] == 'response.create' for m in call.ws.messages)
    call.manager.action.assert_awaited_once_with('rtc_test', 'hangup')

async def test_no_audio_events_are_sent_or_relayed(call):
    await call.event({'type': 'response.output_audio.delta', 'delta': 'not-audio'})
    await call.event({'type': 'input_audio_buffer.speech_started'})
    assert not call.ws.messages

async def test_interrupted_greeting_retries_and_ignores_old_stop(call):
    call.greeting_protected = True
    await call.request_greeting()
    first_tag = call.greeting_tag
    await call.event({'type': 'response.created', 'response': {
        'id': 'first', 'metadata': {'playback_gate': first_tag}}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'first'})
    await call.event({'type': 'output_audio_buffer.cleared', 'response_id': 'first'})
    assert call.greeting_protected
    assert call.greeting_tag == first_tag  # wait for generation to finish before retry
    await call.event({'type': 'response.done', 'response': {'id': 'first', 'status': 'cancelled'}})
    assert call.greeting_tag != first_tag
    assert call.greeting_protected
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'first'})
    assert call.greeting_protected
    await call.event({'type': 'response.created', 'response': {
        'id': 'second', 'metadata': {'playback_gate': call.greeting_tag}}})
    await call.event({'type': 'response.done', 'response': {'id': 'second', 'status': 'completed'}})
    assert call.greeting_protected
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'second'})
    assert not call.greeting_protected


@pytest.mark.parametrize('interruption', ['output_audio_buffer.cleared', 'input_audio_buffer.speech_started'])
async def test_interrupted_goodbye_never_hangs_up_on_later_stop(call, interruption):
    await call.event({'type': 'response.created', 'response': {'id': 'bye'}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'bye'})
    await call.event({'type': 'response.output_audio_transcript.done', 'response_id': 'bye',
                      'transcript': 'Thank you for choosing Summit Air.'})
    ending = asyncio.create_task(call.execute('end_call', {}, response_id='bye'))
    await asyncio.sleep(0)
    await call.event({'type': 'response.done', 'response': {'id': 'bye', 'status': 'completed'}})
    await call.event({'type': interruption, 'response_id': 'bye'})
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'bye'})
    assert not (await ending)['ended']
    call.manager.action.assert_not_called()
    assert not call.ws.closed


async def test_interrupted_injected_goodbye_returns_to_conversation(call):
    ending = asyncio.create_task(call.execute('end_call', {}))
    for _ in range(10):
        await asyncio.sleep(0)
        if call.ws.messages:
            break
    tag = call.ws.messages[-1]['response']['metadata']['playback_gate']
    await call.event({'type': 'response.created', 'response': {
        'id': 'injected', 'metadata': {'playback_gate': tag}}})
    await call.event({'type': 'output_audio_buffer.started', 'response_id': 'injected'})
    await call.event({'type': 'input_audio_buffer.speech_started'})
    await call.event({'type': 'output_audio_buffer.cleared', 'response_id': 'injected'})
    assert not (await ending)['ended']
    call.manager.action.assert_not_called()
    assert not call.ending


async def test_end_call_from_response_before_caller_resumed_is_rejected(call):
    await call.event({'type': 'response.created', 'response': {'id': 'old'}})
    await call.event({'type': 'response.done', 'response': {'id': 'old', 'status': 'completed'}})
    await call.event({'type': 'input_audio_buffer.speech_started'})
    await call.event({'type': 'input_audio_buffer.speech_stopped'})
    await call.tool({'name': 'end_call', 'call_id': 'tool_end', 'response_id': 'old', 'arguments': '{}'})
    outputs = [m for m in call.ws.messages if m['type'] == 'conversation.item.create']
    assert json.loads(outputs[0]['item']['output'])['ended'] is False
    call.manager.action.assert_not_called()


async def test_goodbye_timeout_does_not_hang_up(call):
    with patch.object(call, 'wait_for_goodbye', new=AsyncMock(side_effect=TimeoutError)):
        result = await call.execute('end_call', {})
    assert result['ended'] is False
    call.manager.action.assert_not_called()


async def test_generated_goodbye_without_matching_playback_does_not_end(call):
    await call.event({'type': 'response.created', 'response': {'id': 'bye'}})
    await call.event({'type': 'response.output_audio_transcript.done', 'response_id': 'bye',
                      'transcript': 'Thank you for choosing Summit Air.'})
    await call.event({'type': 'response.done', 'response': {'id': 'bye', 'status': 'completed'}})
    ending = asyncio.create_task(call.execute('end_call', {}, response_id='bye'))
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'other'})
    for _ in range(3):
        await asyncio.sleep(0)
    assert not ending.done()
    call.manager.action.assert_not_called()
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'bye'})
    assert (await ending)['ended']

async def test_greeting_retry_is_bounded(call):
    call.greeting_protected = True
    await call.request_greeting()
    for index in range(2):
        response_id = f'greeting_{index}'
        await call.event({'type': 'response.created', 'response': {
            'id': response_id, 'metadata': {'playback_gate': call.greeting_tag}}})
        event = {'type': 'response.done', 'response': {'id': response_id, 'status': 'failed'}}
        if index == 0:
            await call.event(event)
        else:
            with pytest.raises(RuntimeError, match='Greeting playback'):
                await call.event(event)
    assert call.greeting_protected
    assert call.greeting_attempts == 2
    assert not any(m['type'] == 'session.update' for m in call.ws.messages)


async def test_failed_goodbye_generation_does_not_hang_up(call):
    await call.event({'type': 'response.created', 'response': {'id': 'bye'}})
    await call.event({'type': 'response.output_audio_transcript.done', 'response_id': 'bye',
                      'transcript': 'Thank you for choosing Summit Air.'})
    await call.event({'type': 'response.done', 'response': {'id': 'bye', 'status': 'failed'}})
    await call.event({'type': 'output_audio_buffer.stopped', 'response_id': 'bye'})
    assert not (await call.execute('end_call', {}, response_id='bye'))['ended']
    call.manager.action.assert_not_called()
