"""Loopback lifecycle coordinator; importing this package never loads AoiTalk's DB."""
import hashlib

PROTOCOL_VERSION = 1
DEFAULT_URL = "http://127.0.0.1:47641"
MAX_BODY_BYTES = 65536
LEASE_TTL_SECONDS = 45


def acquisition_digest(broker_id, client_id, request_id):
    """Opaque, broker-incarnation-scoped identity of one acquisition.

    ``/v1/status`` is readable by any local process, so it must never publish the plain
    client or request ids (together with a PID they would let another process cancel the
    acquisition). A party that already knows its own ids can compute this digest and
    recognise its acquisition; everyone else only sees an unforgeable-by-guessing token."""
    return hashlib.sha256(f"{broker_id}\0{client_id}\0{request_id}".encode("utf-8")).hexdigest()
