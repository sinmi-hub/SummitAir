"""Offline carrier lifecycle regressions: real signatures, no live calls."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock, Mock

import httpx
from nacl.signing import SigningKey
import pytest
from starlette.testclient import TestClient

from app.realtime import Call
from app.server import create_app
from outbound import persona, signature, state, telnyx, trigger
from outbound.webhook import cleanup
from test_sip import config, signed


@pytest.fixture
def setup(tmp_path, monkeypatch):
    key = SigningKey.generate()
    cfg = config(tmp_path, human_transfer_number='', telnyx_api_key='offline',
                 telnyx_public_key=base64.b64encode(bytes(key.verify_key)).decode(),
                 outbound_from_number='+12025550101',
                 openai_sip_uri='sip:proj_test@sip.api.openai.com:5061;transport=tls')
    managers = []
    def factory(cfg, **kwargs):
        m = SimpleNamespace(incoming=Mock(return_value=True), recover=AsyncMock(), close=AsyncMock(),
                            options=kwargs)
        managers.append(m)
        return m
    transfer = AsyncMock()
    hangup = AsyncMock()
    monkeypatch.setattr(telnyx, 'transfer_to_openai', transfer)
    monkeypatch.setattr(telnyx, 'hangup', hangup)
    app = create_app(cfg, factory)
    with TestClient(app) as client:
        yield SimpleNamespace(config=cfg, key=key, app=app, client=client, managers=managers,
                              store=app.state.outbound_state, transfer=transfer, hangup=hangup)


def carrier_body(token, leg, call_id, kind='call.answered'):
    return json.dumps({'data': {'id': 'evt-'+call_id, 'event_type': kind, 'payload': {
        'call_control_id': call_id, 'client_state': state.client_state(token, leg)}}})


def carrier_headers(key, body):
    ts = str(int(time.time()))
    sig = key.sign(ts.encode() + b'|' + body.encode()).signature
    return {'telnyx-timestamp': ts, 'telnyx-signature-ed25519': base64.b64encode(sig).decode()}


def carrier(s, token, leg, call_id, kind='call.answered'):
    body = carrier_body(token, leg, call_id, kind)
    return s.client.post('/webhooks/telnyx-outbound', content=body, headers=carrier_headers(s.key, body))


def incoming_body(token=None, call_id='rtc-test'):
    headers = [{'name': 'From', 'value': 'sip:+12025550101@carrier.example'}]
    if token is not None:
        headers.append({'name': state.HEADER.lower(), 'value': token})
    return json.dumps({'type': 'realtime.call.incoming', 'data': {'call_id': call_id, 'sip_headers': headers}})


def incoming(s, token=None, call_id='rtc-test'):
    body = incoming_body(token, call_id)
    return s.client.post('/webhooks/openai', content=body, headers=signed(body))


def test_early_webhooks_and_retries_keep_domain(setup, monkeypatch):
    s = setup
    async def transfer(call_id, token, **kwargs):
        # This event arrives before transfer HTTP completes.
        body = incoming_body(token)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(s.app), base_url='http://test') as client:
            assert (await client.post('/webhooks/openai', content=body, headers=signed(body))).status_code == 200
    s.transfer.side_effect = transfer
    async def dial(to, webhook_url, token, **kwargs):
        # This event arrives before dial HTTP returns the carrier ID.
        body = carrier_body(token, 'pstn', 'original')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(s.app), base_url='http://test') as client:
            assert (await client.post('/webhooks/telnyx-outbound', content=body,
                                      headers=carrier_headers(s.key, body))).status_code == 200
        return 'original'
    monkeypatch.setattr(telnyx, 'dial', dial)
    asyncio.run(trigger.main('+12025550102', 'https://test/webhooks/telnyx-outbound', s.config))
    with s.store.transaction() as db:
        token = db.execute('SELECT token FROM attempts').fetchone()[0]
    assert carrier(s, token, 'pstn', 'original').status_code == 200
    assert carrier(s, token, 'openai', 'target').status_code == 200
    s.transfer.assert_awaited_once()
    s.managers[1].incoming.return_value = False
    assert incoming(s, token).status_code == 503
    s.managers[1].incoming.return_value = True
    assert incoming(s, token).status_code == 200
    assert incoming(s).status_code == 200  # same call ID remains pinned without metadata
    s.managers[0].incoming.assert_not_called()
    assert incoming(s, call_id='ordinary').status_code == 200
    s.managers[0].incoming.assert_called_once_with('ordinary')
    assert state.Store(s.config.call_state_path).route('rtc-test') == 'outbound'
    s.hangup.assert_not_awaited()


def test_transfer_failure_event_cleans_original_and_target(setup):
    s = setup
    token = s.store.create()
    carrier(s, token, 'pstn', 'original')
    carrier(s, token, 'openai', 'target', 'call.hangup')
    assert {c.args[0] for c in s.hangup.await_args_list} == {'original', 'target'}
    count = s.hangup.await_count
    carrier(s, token, 'openai', 'target', 'call.hangup')
    assert s.hangup.await_count == count
    assert incoming(s, token).status_code == 403


def test_request_failure_cleanup_is_retried(setup):
    s = setup
    token = s.store.create()
    s.transfer.side_effect = httpx.ReadTimeout('offline')
    s.hangup.side_effect = httpx.ReadTimeout('offline')
    assert carrier(s, token, 'pstn', 'original').status_code == 200
    assert len(s.store.cleanup_due()) == 1
    s.hangup.side_effect = None
    s.client.portal.call(cleanup, s.app)
    assert s.hangup.await_count == 2
    assert s.store.cleanup_due() == []
    carrier(s, token, 'pstn', 'original')
    s.transfer.assert_awaited_once()


def test_transfer_timeout_without_any_openai_event(setup):
    s = setup
    token = s.store.create()
    carrier(s, token, 'pstn', 'original')
    with s.store.transaction() as db:
        db.execute('UPDATE attempts SET deadline=0 WHERE token=?', (token,))
    s.client.portal.call(cleanup, s.app)
    s.hangup.assert_awaited_once_with('original', config=s.config)
    assert incoming(s, token).status_code == 403


def test_hangup_before_answer_never_transfers(setup):
    s = setup
    token = s.store.create()
    carrier(s, token, 'pstn', 'original', 'call.hangup')
    carrier(s, token, 'pstn', 'original')
    s.transfer.assert_not_awaited()


def test_ambiguous_dial_late_webhook_is_cleaned(setup, monkeypatch):
    s = setup
    dial = AsyncMock(side_effect=httpx.ReadTimeout('offline'))
    monkeypatch.setattr(telnyx, 'dial', dial)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(trigger.main('+12025550102', 'https://test/webhooks/telnyx-outbound', s.config))
    token = dial.call_args.args[2]
    carrier(s, token, 'pstn', 'late-original')
    s.transfer.assert_not_awaited()
    s.hangup.assert_awaited_once_with('late-original', config=s.config)


def test_recovery_keeps_route_and_cleans_all_known_legs(setup):
    s = setup
    token = s.store.create()
    carrier(s, token, 'pstn', 'original')
    incoming(s, token)
    carrier(s, token, 'openai', 'target')
    reopened = state.Store(s.config.call_state_path)
    reopened.recover()
    s.client.portal.call(cleanup, s.app)
    assert {c.args[0] for c in s.hangup.await_args_list} == {'original', 'target'}
    assert reopened.route('rtc-test') == 'outbound'


def test_atomic_transfer_claim_across_connections(tmp_path):
    path = str(tmp_path / 'calls')
    store = state.Store(path)
    token = store.create()
    def claim(_):
        return state.Store(path).observe(token, 'pstn', 'original', 'call.answered')
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(8))) == 1


def test_unknown_expired_reused_tokens_and_leg_ids(setup):
    s = setup
    assert incoming(s, 'unknown').status_code == 403
    carrier(s, 'unknown', 'pstn', 'stranger')
    token = s.store.create()
    assert incoming(s, token).status_code == 403  # no transfer has been claimed
    carrier(s, token, 'pstn', 'original')
    carrier(s, token, 'pstn', 'different-original')
    assert incoming(s, token).status_code == 200
    assert incoming(s, token, 'different-openai').status_code == 403
    s.transfer.assert_awaited_once()
    token2 = s.store.create()
    carrier(s, token2, 'pstn', 'original-2')
    with s.store.transaction() as db:
        db.execute('UPDATE attempts SET deadline=0 WHERE token=?', (token2,))
    assert incoming(s, token2, 'expired').status_code == 403


def test_authentication_and_bad_payloads(setup):
    s = setup
    body = carrier_body(s.store.create(), 'pstn', 'original')
    assert s.client.post('/webhooks/telnyx-outbound', content=body).status_code == 401
    assert s.client.post('/webhooks/telnyx-outbound', content=body+' ',
                         headers=carrier_headers(s.key, body)).status_code == 401
    assert not signature.verify(body.encode(), '', '', '')
    original = s.app.state.config
    s.app.state.config = replace(original, telnyx_public_key='')
    assert s.client.post('/webhooks/telnyx-outbound', content=body).status_code == 503
    s.app.state.config = original
    for bad in ['[]', '{"data":1}', '{"data":{"payload":1}}', '{']:
        assert s.client.post('/webhooks/telnyx-outbound', content=bad,
                             headers=carrier_headers(s.key, bad)).status_code == 400
    s.transfer.assert_not_awaited()


def test_outbound_goodbye_is_recognized(setup):
    options = setup.managers[1].options
    call = Call(SimpleNamespace(), 'test', None, closing_goodbye=options['closing_goodbye'],
                closing_phrases=options['closing_phrases'])
    call.last_agent_text = persona.CLOSING_GOODBYE
    assert call._closing_said()


async def test_telnyx_requests_carry_correlation_and_stable_commands(tmp_path, monkeypatch):
    cfg = config(tmp_path, telnyx_api_key='offline', outbound_from_number='+12025550101',
                 openai_sip_uri='sip:proj_test@sip.api.openai.com:5061;transport=tls')
    requests = []
    real_client = httpx.AsyncClient
    def handle(req):
        requests.append(req)
        if req.method == 'GET':
            return httpx.Response(200, json={'data':[{'application_name':cfg.call_control_app_name,'id':'app'}]})
        return httpx.Response(200, json={'data':{'call_control_id':'original'}})
    monkeypatch.setattr(telnyx.httpx, 'AsyncClient',
                        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    token = 'offline-attempt'
    assert await telnyx.dial('+12025550102', 'https://test/hook', token, config=cfg) == 'original'
    dial = json.loads(requests[-1].content)
    assert state.decode_client_state(dial['client_state']) == (token, 'pstn')
    await telnyx.transfer_to_openai('original', token, config=cfg)
    transfer = json.loads(requests[-1].content)
    assert state.decode_client_state(transfer['target_leg_client_state']) == (token, 'openai')
    assert transfer['custom_headers'] == [{'name':state.HEADER,'value':token}]
    assert transfer['from'] == cfg.outbound_from_number
    assert transfer['media_encryption'] == 'SRTP' and transfer['sip_transport_protocol'] == 'TLS'
    await telnyx.transfer_to_openai('original', token, config=cfg)
    assert json.loads(requests[-1].content)['command_id'] == transfer['command_id']
    assert transfer['command_id'] != dial['command_id']


@pytest.mark.parametrize('status,errors,raises', [
    (200, [], False), (422, [{'code':'90018'}], False),
    (422, [{'code':'other'}], True), (500, [], True),
])
async def test_hangup_only_tolerates_already_ended(tmp_path, monkeypatch, status, errors, raises):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(telnyx.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _:httpx.Response(status,json={'errors':errors})), **kw))
    if raises:
        with pytest.raises(httpx.HTTPStatusError):
            await telnyx.hangup('test', config=config(tmp_path))
    else:
        await telnyx.hangup('test', config=config(tmp_path))
