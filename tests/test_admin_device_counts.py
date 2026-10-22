"""Discovered devices and active polling workers are separate admin counts."""
from types import SimpleNamespace

from test_platform import web, login, bound  # noqa: F401


def test_dashboard_counts_only_present_devices_and_live_eligible_workers(web):
    client, app = web
    profile = login(client)
    store, runtime = app.state.store, app.state.runtime
    uid = profile['user_id']
    bound(store, uid)
    store.sync_devices(uid, [
        {'id': 'speaker-a', 'name': 'Play', 'hardware': 'L05B', 'supported': True, 'reason': ''},
        {'id': 'mini', 'name': 'Mini', 'hardware': 'LX01', 'supported': True, 'reason': ''},
        {'id': 'native', 'name': 'Native', 'hardware': 'OH2', 'supported': True, 'reason': ''},
    ])
    with store.db() as db:
        db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?,?)",
                   (uid, 'removed', 'Removed', 'LX01', 1, '', 0, 0))
    running, finished = SimpleNamespace(done=lambda: False), SimpleNamespace(done=lambda: True)
    runtime.workers = {(uid, 'speaker-a'): running, (uid, 'mini'): running,
                       (uid, 'native'): running, ('other-user', 'other'): running}
    try:
        def counts():
            result = client.get('/api/admin')
            assert result.status_code == 200
            row = next(u for u in result.json()['users'] if u['id'] == uid)
            return row['listening_count'], row['device_count']
        assert counts() == (1, 3)
        runtime.workers[(uid, 'speaker-a')] = finished
        assert counts() == (0, 3)
        runtime.workers[(uid, 'speaker-a')] = running
        store.set_account_state(uid, 'needs_verification')
        assert counts() == (0, 3)
    finally:
        runtime.workers.clear()
