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

import io
import logging
import tarfile
import time
import traceback

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


def iter_chunks(src, max_bytes):
    """Yield (payload, file_count) tar.gz blobs of at most max_bytes of content.

    A whole docs tree in one extract-archive request is answered with a
    truncated body once the archive gets large (~48 MB reproduced a 12-byte
    reply), which silently loses the tail of the tree.
    """
    with tarfile.open(src, 'r:gz') as source:
        buf = io.BytesIO()
        out = tarfile.open(fileobj=buf, mode='w:gz')
        size = 0
        count = 0
        for member in source:
            if not member.isfile():
                continue
            extracted = source.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            out.addfile(member, io.BytesIO(data))
            size += len(data)
            count += 1
            if size >= max_bytes:
                out.close()
                yield buf.getvalue(), count
                buf = io.BytesIO()
                out = tarfile.open(fileobj=buf, mode='w:gz')
                size = 0
                count = 0
        out.close()
        if count:
            yield buf.getvalue(), count


def put_chunk(cloud, path, headers, payload, expected, retries):
    """PUT one chunk, retrying while the extraction comes back short.

    Observed in production: identically sized chunks mostly extract in
    seconds, but one occasionally stalls for minutes and Swift reports
    e.g. "created 465 of 553" plus a 499 Client Disconnect. Re-sending the
    same archive overwrites whatever landed, so a retry is safe and
    recovers the tail instead of failing the whole publish.
    """
    result = {}
    created = 0
    errors = []
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(2 ** (attempt - 1))
        response = cloud.object_store.put(path, headers=headers, data=payload)
        result = response.json()
        created = result.get('Number Files Created', 0)
        errors = result.get('Errors') or []
        if created == expected and not errors:
            break
    return result, created, errors


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
            chunk_retries=dict(type='int', default=2),
        )
    )

    p = module.params
    cloud = get_cloud(p.get('cloud'))
    failures = []
    extract_status = ''
    files_created = 0
    try:
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

        path = "{}/{}?extract-archive=tar.gz".format(
            p['container'],
            p['prefix'],
        )
        for payload, expected in iter_chunks(p['src'], p['chunk_bytes']):
            result, created, errors = put_chunk(
                cloud, path, headers, payload, expected, p['chunk_retries'])
            # Swift reports per-file extraction problems in the body, not
            # via the HTTP status, so a 200 here can still mean a partial
            # upload.
            extract_status = result.get('Response Status', '')
            files_created += created
            if created != expected:
                failures.append({
                    "file": "<chunk>",
                    "error": "created %s of %s files after %s attempts"
                             % (created, expected, p['chunk_retries'] + 1)})
            for error in errors:
                failures.append({
                    "file": error[0],
                    "error": error[1]})

    except (keystoneauth1.exceptions.http.HttpError,
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
