import httpx

from scripts.telnyx_recordings import parse_since, sync

RECORDINGS = {"data": [
    {"id": "rec1", "created_at": "2026-10-03T14:11:05.123Z", "download_urls": {"mp3": "https://files/rec1.mp3"}},
    {"id": "rec2", "created_at": "2026-10-03T15:00:00Z", "download_urls": {}},
]}


def clients():
    def api(request):
        assert "filter[created_at][gte]" in request.url.params
        return httpx.Response(200, json=RECORDINGS)
    plain = lambda request: httpx.Response(200, content=b"audio")
    return (httpx.Client(transport=httpx.MockTransport(api)),
            httpx.Client(transport=httpx.MockTransport(plain)))


def test_sync_saves_new_mp3s_once(tmp_path):
    api, plain = clients()
    since = parse_since("2d")
    saved = sync(api, plain, since, tmp_path)
    assert [p.name for p in saved] == ["20261003-141105_rec1.mp3"]
    assert saved[0].read_bytes() == b"audio"
    assert sync(api, plain, since, tmp_path) == []  # already downloaded


def test_dry_run_writes_nothing(tmp_path):
    api, plain = clients()
    assert len(sync(api, plain, parse_since("6h"), tmp_path, dry_run=True)) == 1
    assert list(tmp_path.iterdir()) == []
