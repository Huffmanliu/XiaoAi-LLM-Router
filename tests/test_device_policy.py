"""Native AI models cannot be opted into takeover, even with stale flags."""
from copy import deepcopy

import pytest

from platform_app.device_policy import NATIVE_AI_REASON
from platform_app.runtime import Runtime
from platform_app.store import Rejected, Store
from test_platform import store, CFG, PASSWORD, bound  # noqa: F401


@pytest.mark.parametrize('hardware', ['OH2', 'oh2p', 'xiaomi.wifispeaker.oh2', 'L17A', 'X10A'])
def test_native_ai_discovery_and_store_guard(store, hardware):
    uid = store.create_user('alice', PASSWORD)
    runtime = Runtime(store, deepcopy(CFG))
    rows = runtime.normalize_devices([{'deviceID': 'native', 'hardware': hardware, 'name': '旧音箱'},
                                      {'deviceID': 'old', 'hardware': 'L05B', 'name': '超级小爱 AI Pro'}])
    assert not rows[0]['supported'] and rows[0]['reason'] == NATIVE_AI_REASON
    assert rows[1]['supported']  # A nickname cannot change eligibility.
    rows[0]['supported'] = True
    store.sync_devices(uid, rows)
    assert not next(d for d in store.devices(uid) if d['id'] == 'native')['supported']
    with pytest.raises(Rejected):
        store.select_devices(uid, ['native'])
    store.select_devices(uid, ['old'])


def test_stale_flags_cannot_restart_native_ai_worker_and_are_cleared_on_start(store):
    uid = store.create_user('alice', PASSWORD)
    bound(store, uid)
    with store.db() as db:
        db.execute("UPDATE devices SET hardware='OH2P',supported=1,enabled=1 WHERE user_id=?", (uid,))
    assert not Runtime(store, deepcopy(CFG)).enabled(uid, 'speaker-a')
    with pytest.raises(Rejected):
        store.select_devices(uid, ['speaker-a'])
    reopened = Store(store.root, store.vault)
    device = reopened.devices(uid)[0]
    assert not device['enabled'] and not device['supported']
    assert device['reason'] == NATIVE_AI_REASON
