# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import io
import os
import sys
import tarfile

import keystoneauth1.exceptions
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), os.pardir, 'library'))

import upload_artifact_swift as uas  # noqa: E402

BULK_PATH = 'box/svc/1?extract-archive=tar.gz'
OBJECT_PREFIX = 'box/svc/1'
HEADERS = {'Accept': 'application/json'}


def make_tarball(tmp_path, files):
    src = tmp_path / 'docs.tar.gz'
    with tarfile.open(str(src), 'w:gz') as tar:
        root = tarfile.TarInfo('./')
        root.type = tarfile.DIRTYPE  # directories must be skipped
        tar.addfile(root)
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return str(src)


def names_in(payload):
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as tar:
        return [m.name for m in tar if m.isfile()]


class FakeResponse:
    status_code = 201

    def __init__(self, body):
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeStore:
    """Swift behind the nginx hop: archives the proxy cannot extract before
    its first heartbeat (more than `stall_above` files) lose their tail."""

    def __init__(self, stall_above=10 ** 6, mode='short', bad_names=(),
                 stall_status='499 Client Disconnect'):
        self.stall_above = stall_above
        self.mode = mode
        self.stall_status = stall_status
        self.bad_names = set(bad_names)
        self.calls = []
        self.sizes = []
        self.stored = set()

    def put(self, path, headers=None, data=None, **kwargs):
        self.calls.append(
            {'path': path, 'kwargs': kwargs, 'headers': headers})
        if 'extract-archive' not in path:
            self.stored.add(path)
            return FakeResponse({})
        names = names_in(data)
        self.sizes.append(names)
        stalled = (len(names) > self.stall_above
                   or self.bad_names.intersection(names))
        if not stalled:
            self.stored.update(names)
            return FakeResponse({
                'Response Status': '201 Created',
                'Number Files Created': len(names), 'Errors': []})
        if self.mode == 'timeout':
            raise keystoneauth1.exceptions.ConnectTimeout('timed out')
        if self.mode == 'timeout_after_header':
            # What keystoneauth raises when the read times out once the
            # heartbeat has already delivered the response header.
            raise keystoneauth1.exceptions.ConnectFailure('Read timed out.')
        if self.mode == 'rejected':
            good = [n for n in names if n not in self.bad_names]
            self.stored.update(good)
            return FakeResponse({
                'Response Status': '400 Bad Request',
                'Number Files Created': len(good),
                'Errors': [[n, '412 Precondition Failed']
                           for n in names if n in self.bad_names]})
        if self.mode == 'list':
            return FakeResponse([])
        if self.mode == 'garbage':
            return FakeResponse(ValueError('truncated body'))
        landed = names[:len(names) // 2]
        self.stored.update(landed)
        return FakeResponse({
            'Response Status': '201 Created',
            'Number Files Created': len(landed),
            'Errors': [[names[len(landed)], self.stall_status]]})


class FakeCloud:
    def __init__(self, store):
        self.object_store = store


def members(count, size=10):
    return [(tarfile.TarInfo('f%03d.html' % i), b'x' * size)
            for i in range(count)]


def sized(pairs):
    for info, data in pairs:
        info.size = len(data)
    return pairs


def upload(store, pairs, budget=8, timeout=60, limit=100):
    return uas.upload_members(
        FakeCloud(store), BULK_PATH, OBJECT_PREFIX, HEADERS,
        sized(pairs), timeout, budget, limit)


def test_iter_chunks_caps_file_count(tmp_path):
    src = make_tarball(
        tmp_path, [('f%d.html' % i, b'x') for i in range(25)])
    chunks = list(uas.iter_chunks(src, max_bytes=10 ** 9, max_files=10))
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert chunks[0][0][0].name == 'f0.html'
    assert chunks[0][0][1] == b'x'


def test_iter_chunks_caps_bytes(tmp_path):
    src = make_tarball(
        tmp_path, [('f%d.bin' % i, b'x' * 400) for i in range(5)])
    chunks = list(uas.iter_chunks(src, max_bytes=1000, max_files=100))
    assert [len(c) for c in chunks] == [3, 2]


def test_build_archive_round_trips():
    payload = uas.build_archive(sized(members(3)))
    assert names_in(payload) == ['f000.html', 'f001.html', 'f002.html']


def test_clean_chunk_is_one_bounded_request():
    store = FakeStore()
    outcome = upload(store, members(20), timeout=45)
    assert (outcome.created, outcome.failures) == (20, [])
    assert outcome.budget == 8
    assert len(store.calls) == 1
    # The read timeout turns a 600 s server-side stall into a fast failure,
    # and keystoneauth must not silently re-send the stalled request.
    assert store.calls[0]['kwargs'] == {'timeout': 45, 'connect_retries': 0}


@pytest.mark.parametrize(
    'mode', ['short', 'timeout', 'timeout_after_header', 'garbage'])
def test_stalled_chunk_is_split_until_it_extracts(mode):
    store = FakeStore(stall_above=5, mode=mode)
    outcome = upload(store, members(20))
    assert outcome.failures == []
    assert outcome.created == 20
    assert len(store.stored) == 20
    # 20 stalls, both 10s stall, then 4 x 5 extract.
    assert len(store.calls) == 7
    assert outcome.budget == 8 - 3
    assert outcome.limit == uas.MIN_LIMIT


def test_later_archives_are_not_tried_at_the_size_that_stalled():
    store = FakeStore(stall_above=30, mode='timeout')
    outcome = upload(store, members(100))
    assert (outcome.created, outcome.failures) == (100, [])
    # 100 stalls, 50 stalls, the other 50 is cut to 25s without asking.
    assert [len(c) for c in store.sizes] == [100, 50, 25, 25, 25, 25]
    assert (outcome.budget, outcome.limit) == (8 - 2, 25)


@pytest.mark.parametrize('status', ['408 Request Timeout', '503 Busy'])
def test_two_file_stall_reported_without_499_is_still_resent(status):
    store = FakeStore(stall_above=1, stall_status=status)
    outcome = upload(store, members(2))
    assert (outcome.created, outcome.failures) == (2, [])


def test_lone_stalled_file_gets_its_plain_put_on_the_last_budget():
    store = FakeStore(stall_above=0, mode='timeout')
    outcome = upload(store, members(1), budget=1)
    assert (outcome.created, outcome.failures) == (1, [])
    assert store.calls[-1]['path'] == 'box/svc/1/f000.html'


def test_plain_put_names_and_headers():
    store = FakeStore(stall_above=0, mode='timeout')
    pairs = [(tarfile.TarInfo('./umn/a b#1.html'), b'x')]
    headers = dict(HEADERS, **{'Content-Type': 'application/gzip',
                               'X-Delete-After': '60'})
    outcome = uas.upload_members(
        FakeCloud(store), BULK_PATH, OBJECT_PREFIX, headers, sized(pairs),
        60, 8, 100)
    assert outcome.created == 1
    assert store.calls[-1]['path'] == 'box/svc/1/umn/a%20b%231.html'
    assert store.calls[-1]['headers'] == {'X-Delete-After': '60'}


def test_plain_put_with_empty_prefix_has_no_double_slash():
    store = FakeStore(stall_above=0, mode='timeout')
    outcome = uas.upload_members(
        FakeCloud(store), 'box/?extract-archive=tar.gz', 'box/', HEADERS,
        sized(members(1)), 60, 8, 100)
    assert outcome.created == 1
    assert store.calls[-1]['path'] == 'box/f000.html'


@pytest.mark.parametrize('limit', [0, -5])
def test_nonsense_limit_does_not_recurse_forever(limit):
    store = FakeStore()
    outcome = upload(store, members(3), limit=limit)
    assert (outcome.created, outcome.failures) == (3, [])


def test_non_object_json_counts_as_a_stall():
    store = FakeStore(stall_above=1, mode='list')
    outcome = upload(store, members(2))
    assert (outcome.created, outcome.failures) == (2, [])


def test_following_chunks_start_at_the_remembered_limit():
    store = FakeStore(stall_above=5, mode='timeout')
    outcome = upload(store, members(20), limit=5)
    assert (outcome.created, outcome.failures) == (20, [])
    assert len(store.calls) == 4
    assert outcome.budget == 8


def test_lone_file_falls_back_to_plain_put():
    store = FakeStore(bad_names={'f001.html'}, mode='timeout')
    outcome = upload(store, members(2))
    assert outcome.failures == []
    assert outcome.created == 2
    plain = [c['path'] for c in store.calls
             if 'extract-archive' not in c['path']]
    assert plain == ['box/svc/1/f001.html']


def test_gives_up_once_the_budget_is_spent():
    store = FakeStore(stall_above=0, mode='timeout')
    outcome = upload(store, members(64), budget=3)
    assert outcome.budget == 0
    assert outcome.created < 64
    assert outcome.failures
    assert 'not uploaded' in outcome.failures[-1]['error']
    # 3 failed requests and not a single one more.
    assert len(store.calls) == 3


def test_files_swift_refuses_are_reported_not_resent():
    store = FakeStore(bad_names={'f003.html'}, mode='rejected')
    outcome = upload(store, members(20))
    assert outcome.created == 19
    assert outcome.failures == [
        {'file': 'f003.html', 'error': '412 Precondition Failed'}]
    assert len(store.calls) == 1
    assert (outcome.budget, outcome.limit) == (8, 100)
