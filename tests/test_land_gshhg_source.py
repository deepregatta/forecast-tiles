"""The shoreline archive's download, verified to a root the trust store carries.

The University of Hawaii mirror's chain ends at emSign Root TLS CA - G1, which
the Mozilla store (certifi) does not carry, so a cold runner's `requests`
refused it and the land-index run for `med` died before compiling anything
(2026-11-08). emSign Root CA - G1, which the store does carry, cross-signs that
root, and the mirror's own AIA hands out the cross certificate. The download is
verified against the standard bundle plus that one public certificate: TLS
stays on, and the recorded digest still decides what is compiled.
"""

import base64
import hashlib
import io
import zipfile
from pathlib import Path

import certifi

from ingest.land import gshhg

#: SHA-256 of the cross certificate's DER, as `openssl x509 -fingerprint`.
CROSS_FINGERPRINT = "9b913476eab476e21e0887dbcde44345d1359d28116b83b7cd3e87bd807ebf68"


def _der(pem: str) -> bytes:
    body = pem.split("-----BEGIN CERTIFICATE-----")[1].split("-----END CERTIFICATE-----")[0]
    return base64.b64decode("".join(body.split()))


def _archive(member_bytes: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(gshhg.MEMBER, member_bytes)
    return buffer.getvalue()


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body


class _Recording:
    def __init__(self, body: bytes):
        self.body = body
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return _Response(self.body)


def test_the_committed_cross_certificate_is_the_one_recorded():
    pem = gshhg.EXTRA_CA.read_text()
    assert pem.count("BEGIN CERTIFICATE") == 1
    assert hashlib.sha256(_der(pem)).hexdigest() == CROSS_FINGERPRINT


def test_the_archive_is_verified_against_the_store_and_the_cross_certificate(tmp_path, monkeypatch):
    member = b"shoreline"
    archive = _archive(member)
    monkeypatch.setattr(gshhg, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(gshhg, "MEMBER_SHA256", hashlib.sha256(member).hexdigest())
    session = _Recording(archive)

    path = gshhg.ensure_source(tmp_path, session=session)

    assert path.read_bytes() == member
    (call,) = session.calls
    bundle = Path(call["verify"]).read_text()
    assert Path(certifi.where()).read_text() in bundle
    assert gshhg.EXTRA_CA.read_text() in bundle
