"""SigV4 signing for pmproxy behind an AWS_IAM Function URL.

The Function URL rejects unsigned requests account-wide (AuthType=NONE is
blocked), so every proxied CLOB call must carry a SigV4 signature computed
with the local AWS credentials. Signature and Cognito Bearer both claim the
Authorization header — the Lambda runs with PMPROXY_AUTH_ENABLED=false and
IAM is the sole auth layer.

We patch py_clob_client_v2's `request` helper: pre-merge query params and
pre-serialize the JSON body so the signed bytes are exactly the sent bytes.
Only headers botocore signs (host, x-amz-date, x-amz-content-sha256) matter
for verification; the client's own headers ride along unsigned.
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlencode

from . import hosts


def _install_sigv4_patch_once() -> None:
    """Idempotent; no-op when PMPROXY_URL is unset (direct mode)."""
    import py_clob_client_v2.http_helpers.helpers as helpers

    if getattr(helpers, "_pmt_sigv4_patched", False):
        return

    proxy = hosts.proxy_url()
    if not proxy:
        helpers._pmt_sigv4_patched = True
        return

    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    region = os.environ.get("PMPROXY_AWS_REGION", "eu-west-1")
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials required to sign pmproxy requests")

    original = helpers.request

    def signed_request(endpoint, method, headers=None, data=None, params=None):
        if not endpoint.startswith(proxy):
            return original(endpoint, method, headers, data, params)

        url = endpoint
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True, safe="")

        if data is None:
            body = b""
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            # Serialize here so signature bytes == wire bytes; downstream
            # sends strings via `content=`, not httpx's own json encoder.
            data = json.dumps(data)
            body = data.encode("utf-8")

        req = AWSRequest(method=method, url=url, data=body)
        # Function URLs require the payload hash as a header, not just in
        # the signature — without it AWS returns a bare 403 Forbidden.
        req.headers["X-Amz-Content-SHA256"] = hashlib.sha256(body).hexdigest()
        SigV4Auth(credentials.get_frozen_credentials(), "lambda", region).add_auth(req)

        merged = dict(headers or {})
        merged.update(dict(req.headers))
        return original(url, method, merged, data, None)

    helpers.request = signed_request
    helpers._pmt_sigv4_patched = True
