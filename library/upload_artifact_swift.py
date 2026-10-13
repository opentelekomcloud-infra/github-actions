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

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type


"""
Utility to upload files to swift
"""

import collections
import io
import logging
import tarfile
import time
import traceback
import urllib.parse

import openstack
import requests
import requests.exceptions
import requestsexceptions
import keystoneauth1.exceptions

from ansible.module_utils.basic import AnsibleModule


def get_cloud(cloud):
    if isinstance(cloud, dict):
        config = openstack.config.loader.OpenStackConfig().get_one(**cloud)
        return openstack.connection.Connection(config=config)
    else:
        return openstack.connect(cloud=cloud)


# What one upload_members() call achieved: files that landed, per-file
# failures, the last extract status seen, the failed requests still allowed
# and the largest archive (in files) still worth attempting.
Outcome = collections.namedtuple(
    'Outcome', ['created', 'failures', 'status', 'budget', 'limit'])


def iter_chunks(src: str, max_bytes: int, max_files: int):
    """Yield lists of (TarInfo, data) holding at most max_bytes / max_files.

    A whole docs tree in one extract-archive request is answered with a
    truncated body once the archive gets large (~48 MB reproduced a 12-byte
    reply), which silently loses the tail of the tree.

    The file cap is what keeps a chunk fast: extraction time is driven by
    the number of objects, not their size, and a chunk has to be fully
    extracted before Swift sends its first heartbeat (see upload_members).
    """
    with tarfile.open(src, 'r:gz') as source:
        chunk = []
        size = 0
        for member in source:
            if not member.isfile():
                continue
            extracted = source.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            chunk.append((member, data))
            size += len(data)
            if size >= max_bytes or len(chunk) >= max(max_files, 1):
                yield chunk
                chunk = []
                size = 0
        if chunk:
            yield chunk


def build_archive(members: list) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as out:
        for info, data in members:
            out.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def authenticate(cloud, retries, timeout):
    """Force authentication up front, bounded and retried.

    IAM intermittently blackholes the connect (~4% of jobs). No timeout is
    configured anywhere -- the otc profile sets none -- so the SDK blocks for
    about six minutes and then kills the job. Bound only this phase: the
    session timeout is restored afterwards so large chunk uploads, which
    legitimately run for minutes, stay unbounded.
    """
    session = cloud.session
    previous = session.timeout
    session.timeout = timeout
    try:
        for attempt in range(retries + 1):
            try:
                session.get_token()
                return
            except keystoneauth1.exceptions.ConnectionError:
                if attempt == retries:
                    raise
                time.sleep(2 ** attempt)
    finally:
        session.timeout = previous


# How a silent server shows up on the client. A read timeout that hits
# after the early response header is raised as ConnectFailure, not as
# ConnectTimeout, so only the common base class catches both.
STALLED = (keystoneauth1.exceptions.ConnectionError,
           requests.exceptions.RequestException)


# A stall never shrinks later archives below this many files: one slow
# request must not turn the rest of the tree into single-file uploads.
MIN_LIMIT = 10


def is_rejection(error) -> bool:
    """True for a per-file status that re-sending cannot change.

    408 and 499 are how the proxy reports a body that stopped arriving, and
    5xx may be transient, so only the remaining 4xx count.
    """
    code = str(error[1])[:3]
    return code.startswith('4') and code not in ('408', '499')


def object_name(member_name: str) -> str:
    """The object name Swift's bulk middleware derives from a tar member."""
    name = member_name[2:] if member_name.startswith('./') else member_name
    return name.lstrip('/')


def extract_archive(cloud, path: str, headers: dict, members: list,
                    timeout: int) -> tuple:
    """PUT one archive; return (created, errors, status, verdict).

    The verdict is 'complete', 'stalled' (the tail of the archive never
    reached Swift: worth re-sending in smaller pieces) or 'rejected' (Swift
    read every file and refused some: re-sending cannot help).
    """
    payload = build_archive(members)
    try:
        # connect_retries=0: the SDK would otherwise re-send a timed out
        # request once by itself and double the wait.
        response = cloud.object_store.put(
            path, headers=headers, data=payload,
            timeout=timeout, connect_retries=0)
        result = response.json()
    except STALLED + (ValueError,):
        return 0, [], '', 'stalled'
    if not isinstance(result, dict):
        return 0, [], '', 'stalled'
    created = result.get('Number Files Created') or 0
    errors = result.get('Errors') or []
    status = str(result.get('Response Status') or '')
    if created == len(members) and not errors and status.startswith('20'):
        return created, errors, status, 'complete'
    accounted = created + len(errors) == len(members)
    if errors and accounted and all(is_rejection(e) for e in errors):
        return created, errors, status, 'rejected'
    return created, errors, status, 'stalled'


def put_object(cloud, object_prefix: str, headers: dict, member: tuple,
               timeout: int, budget: int, limit: int) -> Outcome:
    """Plain object PUT: no bulk middleware involved, so it cannot stall."""
    info, data = member
    path = '%s/%s' % (
        object_prefix, urllib.parse.quote(object_name(info.name)))
    plain = {key: value for key, value in headers.items()
             if key.lower() not in ('content-type', 'accept')}
    try:
        response = cloud.object_store.put(
            path, headers=plain, data=data,
            timeout=timeout, connect_retries=0)
        status = response.status_code
    except STALLED as exc:
        status = type(exc).__name__
    if status in (200, 201):
        return Outcome(1, [], '201 Created', budget, limit)
    return Outcome(
        0, [{"file": path, "error": "plain PUT failed: %s" % status}],
        '', max(budget - 1, 0), limit)


def upload_members(cloud, bulk_path: str, object_prefix: str, headers: dict,
                   members: list, timeout: int, budget: int,
                   limit: int) -> Outcome:
    """Extract one chunk; halve and re-send it while it comes back short.

    Root cause, reproduced against nginx 1.27: Swift's bulk middleware
    commits the response header with its first heartbeat (yield_frequency,
    10 s) while it is still reading the archive. nginx stops forwarding a
    request body the moment the upstream starts answering, so any archive
    the proxy has not fully read by then loses its tail. The proxy waits
    client_timeout (600 s) for bytes that never come, fails the file it was
    reading with "499 Client Disconnect" and reports e.g. "created 387 of
    472". Re-sending the same archive stalls the same way, which is why a
    plain retry cost 3 x 10 minutes and still failed.

    So: bound the wait (the stall is silent, a read timeout catches it in
    `timeout` seconds instead of 600), then make the request cheaper rather
    than repeat it. Halving ends at a single file sent as a plain object
    PUT, which involves no bulk middleware. Re-sending files that already
    landed overwrites them, so every step is idempotent. `budget` caps the
    failed requests overall: a broken backend fails the job in minutes.
    `limit` remembers the size that stalled, so the chunks that follow are
    cut down up front instead of each paying for the same lesson.
    """
    limit = max(limit, 1)
    if budget <= 0:
        return Outcome(0, [{
            "file": "<chunk>",
            "error": "%d files not uploaded: too many stalled requests"
                     % len(members)}], '', 0, limit)
    if len(members) <= limit:
        created, errors, status, verdict = extract_archive(
            cloud, bulk_path, headers, members, timeout)
        if verdict == 'complete':
            return Outcome(created, [], status, budget, limit)
        if verdict == 'rejected':
            return Outcome(
                created,
                [{"file": error[0], "error": error[1]} for error in errors],
                status, budget, limit)
        budget -= 1
        if len(members) == 1:
            # Always worth one plain PUT, even on the last of the budget.
            return put_object(cloud, object_prefix, headers, members[0],
                              timeout, budget, limit)
        limit = min(limit, max(len(members) // 2, MIN_LIMIT))
    middle = (len(members) + 1) // 2
    first = upload_members(cloud, bulk_path, object_prefix, headers,
                           members[:middle], timeout, budget, limit)
    second = upload_members(cloud, bulk_path, object_prefix, headers,
                            members[middle:], timeout, first.budget,
                            first.limit)
    return Outcome(
        first.created + second.created,
        first.failures + second.failures,
        second.status or first.status,
        second.budget,
        second.limit)


def main():
    module = AnsibleModule(
        argument_spec=dict(
            cloud=dict(required=True, type='raw'),
            container=dict(required=True, type='str'),
            prefix=dict(type='str', default=''),
            src=dict(required=True, type='str'),
            public=dict(type='bool', default=True),
            read_acl=dict(type='str'),
            delete_after=dict(type='int', default=0),
            chunk_bytes=dict(type='int', default=4194304),
            chunk_files=dict(type='int', default=100),
            chunk_timeout=dict(type='int', default=60),
            max_failed_requests=dict(type='int', default=8),
            auth_timeout=dict(type='int', default=30),
            auth_retries=dict(type='int', default=3),
        )
    )

    p = module.params
    cloud = get_cloud(p.get('cloud'))
    failures = []
    extract_status = ''
    files_created = 0
    try:
        authenticate(cloud, p['auth_retries'], p['auth_timeout'])
        container = cloud.get_container(p['container'])
        if not container:
            cloud.create_container(name=p['container'])
        read_acl = ''
        if not p['read_acl']:
            read_acl = '.r:*,.rlistings' if p['public'] else ''
        else:
            read_acl = p['read_acl']
        cloud.update_container(
            p['container'], {
                'x-container-read': read_acl,
                'X-Container-Meta-Web-Index': 'index.html',
                'X-Container-Meta-Access-Control-Allow-Origin': '*'
            })

        headers = {
            "X-Detect-Content-Type": "true",
            "Content-Type": "application/gzip",
            "Accept": "application/json"
        }
        if p["delete_after"] > 0:
            headers["X-Delete-After"] = str(p["delete_after"])

        object_prefix = "{}/{}".format(p['container'], p['prefix'])
        path = object_prefix + "?extract-archive=tar.gz"
        budget = p['max_failed_requests']
        limit = p['chunk_files']
        for members in iter_chunks(
                p['src'], p['chunk_bytes'], p['chunk_files']):
            # Swift reports per-file extraction problems in the body, not
            # via the HTTP status, so a 200 here can still mean a partial
            # upload.
            outcome = upload_members(
                cloud, path, object_prefix, headers, members,
                p['chunk_timeout'], budget, limit)
            budget = outcome.budget
            limit = outcome.limit
            extract_status = outcome.status or extract_status
            files_created += outcome.created
            failures.extend(outcome.failures)

    except (keystoneauth1.exceptions.http.HttpError,
            keystoneauth1.exceptions.ConnectionError,
            requests.exceptions.RequestException):
        s = "Error uploading to %s.%s" % (cloud.name, cloud.config.region_name)
        logging.exception(s)
        s += "\n" + traceback.format_exc()
        module.fail_json(
            changed=False,
            msg=s,
            cloud=cloud.name,
            region_name=cloud.config.region_name)

    if failures or not extract_status.startswith('20'):
        module.fail_json(
            changed=bool(files_created),
            msg="Swift archive extraction incomplete: status=%r, "
                "files created=%s, failures=%d. The published tree is "
                "partial." % (extract_status, files_created, len(failures)),
            upload_failures=failures,
            files_created=files_created,
            extract_status=extract_status,
        )

    module.exit_json(
        changed=True,
        files_created=files_created,
        upload_failures=failures,
    )


if __name__ == '__main__':
    # Avoid unactionable warnings
    requestsexceptions.squelch_warnings(
        requestsexceptions.InsecureRequestWarning)

    main()
