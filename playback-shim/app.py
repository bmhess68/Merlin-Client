"""Playback auth shim. mediamtx's playback server (port 9996 internal) only
accepts HTTP Basic auth. Cloud Merlin sends ?user=X&pass=Y in the URL (the
same model the HLS player already uses) — this shim sits on the box's public
:9996 port, reads user/pass out of the query string, strips them, attaches
Basic auth on the upstream call to mediamtx.

Cloud Caddy block stays the same:
    handle /dvr/* { uri strip_prefix /dvr; reverse_proxy http://merlin-edge-videoclient:9996 }

Falls through to forwarded Basic auth if no query-string credentials are
present, so curl tests with -u still work.
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

import requests
from flask import Flask, Response, request

UPSTREAM = os.environ.get("UPSTREAM", "http://mediamtx:9996").rstrip("/")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9996"))
TIMEOUT = 30
CHUNK = 64 * 1024

app = Flask(__name__)


def _resolve_auth(args: dict) -> tuple[tuple[str, str] | None, dict]:
    """Pop user/pass from the query, return (auth tuple, scrubbed args)."""
    scrubbed = dict(args)
    user = scrubbed.pop("user", None)
    pwd = scrubbed.pop("pass", None)
    if user and pwd:
        return (user, pwd), scrubbed
    if request.authorization and request.authorization.username:
        return (request.authorization.username, request.authorization.password or ""), scrubbed
    return None, scrubbed


@app.route("/", defaults={"rest": ""}, methods=["GET", "HEAD"])
@app.route("/<path:rest>", methods=["GET", "HEAD"])
def proxy(rest: str):
    auth, scrubbed = _resolve_auth(request.args.to_dict(flat=True))

    url = f"{UPSTREAM}/{rest}"
    qs = urlencode(scrubbed)
    if qs:
        url = f"{url}?{qs}"

    upstream = requests.request(
        request.method,
        url,
        auth=auth,
        stream=True,
        timeout=TIMEOUT,
    )

    # Pass through interesting headers; drop hop-by-hop and any incorrect Content-Length
    # since we're streaming the body.
    drop = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in drop]

    def stream():
        for chunk in upstream.iter_content(chunk_size=CHUNK):
            if chunk:
                yield chunk

    return Response(stream(), status=upstream.status_code, headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=LISTEN_PORT, threaded=True)
