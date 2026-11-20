"""The MPP wire format (``calotype/mpp.py``), checked against the published examples.

The fixed strings below are copied from the specifications, not produced by
the code under test:

- ``SPEC_*``: https://paymentauth.org/draft-stripe-charge-00 , appendix B.1
- ``MPPDEV_*``: https://mpp.dev/protocol/challenges and /protocol/credentials

The last tests run only when ``pympp``, the protocol's official Python SDK, is
installed (it is not a dependency). They let that SDK read what this module
writes and write what this module reads.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from calotype import mpp

SECRET = "test-challenge-secret"

SPEC_REQUEST = "eyJhbW91bnQiOiI1MDAwIiwiY3VycmVuY3kiOiJ1c2QiLCJkZXNjcmlwdGlvbiI6IkFJIGdlbmVyYXRpb24ifQ"
SPEC_CREDENTIAL = (
    "eyJjaGFsbGVuZ2UiOnsiaWQiOiJjaF8xYTJiM2M0ZDVlIiwicmVhbG0iOiJhcGkuZXhhbXBsZS5jb20iLCJtZXRob2QiOiJzdHJpcGUi"
    "LCJpbnRlbnQiOiJjaGFyZ2UiLCJyZXF1ZXN0IjoiZXlKaGJXOTFiblFpT2lJMU1EQXdJaXdpWTNWeWNtVnVZM2tpT2lKMWMyUWlMQ0pr"
    "WlhOamNtbHdkR2x2YmlJNklrRkpJR2RsYm1WeVlYUnBiMjRpZlEiLCJleHBpcmVzIjoiMjAyNS0wMS0xNVQxMjowNTowMFoifSwicGF5"
    "bG9hZCI6eyJzcHQiOiJzcHRfMU40WnYzMmVadktZbG8yQ1BoVlBrSmxXIn19"
)
SPEC_RECEIPT = (
    "eyJtZXRob2QiOiJzdHJpcGUiLCJyZWZlcmVuY2UiOiJwaV8xTjRadjMyZVp2S1lsbzJDUGhWUGtKbFciLCJzdGF0dXMiOiJzdWNjZXNz"
    "IiwidGltZXN0YW1wIjoiMjAyNS0wMS0xNVQxMjowNDozMloifQ"
)
MPPDEV_REQUEST = (
    "eyJhbW91bnQiOiIxMDAwIiwiY3VycmVuY3kiOiIweDIwYzAwMDAwMDAwMDAwMDAwMDAwMDAwMDZhMzdEQTVDOTk2ODc0QkUiLCJyZWNp"
    "cGllbnQiOiIweGYzOUZkNmU1MWFhZDg4RjZGNGNlNmFCODgyNzI3OWNmZkZiOTIyNjYifQ"
)
MPPDEV_OPAQUE = "eyJyb3V0ZSI6Ii92MS9zZWFyY2gifQ"
# The credential mpp.dev shows for a challenge that says header="Payment-Authorization".
MPPDEV_CREDENTIAL = (
    "eyJjaGFsbGVuZ2UiOnsiZXhwaXJlcyI6IjIwMjctMDEtMTVUMTI6MDU6MDBaIiwiaGVhZGVyIjoiUGF5bWVudC1BdXRob3JpemF0aW9u"
    "IiwiaWQiOiJxQjN3RXJUeVU3aU9wQXNEOWZHaEprIiwiaW50ZW50IjoiY2hhcmdlIiwibWV0aG9kIjoidGVtcG8iLCJvcGFxdWUiOiJl"
    "eUp5YjNWMFpTSTZJaTkyTVM5elpXRnlZMmdpZlEiLCJyZWFsbSI6Im1wcC5kZXYiLCJyZXF1ZXN0IjoiZXlKaGJXOTFiblFpT2lJeE1E"
    "QXdJaXdpWTNWeWNtVnVZM2tpT2lJd2VESXdZekF3TURBd01EQXdNREF3TURBd01EQXdNREF3TURaaE16ZEVRVFZET1RrMk9EYzBRa1Vp"
    "TENKeVpXTnBjR2xsYm5RaU9pSXdlR1l6T1Vaa05tVTFNV0ZoWkRnNFJqWkdOR05sTm1GQ09EZ3lOekkzT1dObVprWmlPVEl5TmpZaWZR"
    "In0sInBheWxvYWQiOnsiaGFzaCI6IjB4YWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFi"
    "YWJhYmFiYWJhYiIsInR5cGUiOiJoYXNoIn0sInNvdXJjZSI6ImRpZDpwa2g6ZWlwMTU1OjQyMTc6MHgxMjM0NTY3ODkwYWJjZGVmMTIz"
    "NDU2Nzg5MGFiY2RlZjEyMzQ1Njc4In0"
)

REQUEST = {
    "amount": "1900",
    "currency": "usd",
    "methodDetails": {"networkId": "profile_test_1", "paymentMethodTypes": ["card", "link"]},
}


def issue(**overrides) -> mpp.Challenge:
    args = dict(realm="calotype.test", method="stripe", intent="charge", request=REQUEST, opaque={"key": "7"})
    args.update(overrides)
    return mpp.issue_challenge(SECRET, **args)


def credential_for(challenge: mpp.Challenge, payload: dict, **changes) -> str:
    """What a client sends back for ``challenge``: its parameters echoed unchanged, plus the payload."""
    echo = {
        "id": challenge.id,
        "realm": challenge.realm,
        "method": challenge.method,
        "intent": challenge.intent,
        "request": challenge.request,
        "expires": challenge.expires,
        "opaque": challenge.opaque,
    }
    if challenge.header:
        echo["header"] = challenge.header
    echo.update(changes)
    return "Payment " + mpp.b64url(json.dumps({"challenge": echo, "payload": payload}).encode())


# --- canonical JSON and base64url ----------------------------------------------------------------


def test_request_encoding_matches_the_specifications_examples():
    assert mpp.encode_object({"currency": "usd", "amount": "5000", "description": "AI generation"}) == SPEC_REQUEST
    assert mpp.encode_object(
        {
            "recipient": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
            "amount": "1000",
            "currency": "0x20c0000000000000000000006a37DA5C996874BE",
        }
    ) == MPPDEV_REQUEST
    assert mpp.encode_object({"route": "/v1/search"}) == MPPDEV_OPAQUE
    assert mpp.decode_object(SPEC_REQUEST) == {"amount": "5000", "currency": "usd", "description": "AI generation"}


def test_canonical_json_is_sorted_compact_utf8():
    assert mpp.canonical_json({"b": [1, True, None], "a": {"z": "é", "y": "/"}}) == (
        '{"a":{"y":"/","z":"é"},"b":[1,true,null]}'.encode("utf-8")
    )
    assert "=" not in mpp.encode_object({"a": "b"})  # no padding


@pytest.mark.parametrize("value", [{"a": 1.5}, {"a": float("nan")}, {"a": 2**60}, {1: "a"}, {"é": "a"}, {"a": b"x"}])
def test_canonical_json_refuses_what_it_cannot_write_exactly(value):
    with pytest.raises(ValueError):
        mpp.canonical_json(value)


@pytest.mark.parametrize("text", ["", "not base64!", "e30=", mpp.b64url(b"[1]"), mpp.b64url(b"{"), "a" * 20_000])
def test_decode_object_refuses_anything_but_an_encoded_object(text):
    with pytest.raises(ValueError):
        mpp.decode_object(text)


# --- challenges ----------------------------------------------------------------------------------


def test_challenge_id_is_the_documented_hmac():
    """realm|method|intent|request|expires|digest|opaque, keyed with the server's secret."""
    c = issue(expires="2027-01-15T12:05:00Z")
    text = "|".join(["calotype.test", "stripe", "charge", c.request, "2027-01-15T12:05:00Z", "", c.opaque])
    expected = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), text.encode(), hashlib.sha256).digest())
    assert c.id == expected.decode().rstrip("=")


def test_challenge_id_binds_the_credential_header_before_opaque():
    c = issue(expires="2027-01-15T12:05:00Z", header="Payment-Authorization")
    text = "|".join(
        ["calotype.test", "stripe", "charge", c.request, "2027-01-15T12:05:00Z", "", "Payment-Authorization", c.opaque]
    )
    expected = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), text.encode(), hashlib.sha256).digest())
    assert c.id == expected.decode().rstrip("=")
    assert c.id != issue(expires="2027-01-15T12:05:00Z").id


def test_www_authenticate_has_every_parameter_in_order():
    c = issue(description="Calotype Starter pack: 5000 prepaid screenshots", header="Payment-Authorization")
    value = c.www_authenticate()
    assert value.startswith(f'Payment id="{c.id}", realm="calotype.test", method="stripe", intent="charge", ')
    names = [part.split("=", 1)[0] for part in value[len("Payment ") :].split(", ")]
    assert names == ["id", "realm", "method", "intent", "request", "expires", "description", "header", "opaque"]
    assert f'request="{c.request}"' in value and 'header="Payment-Authorization"' in value
    assert mpp.decode_object(c.request) == REQUEST
    assert "\n" not in value and "\r" not in value


def test_challenge_without_a_header_names_none():
    assert "header=" not in issue().www_authenticate()


def test_each_challenge_is_unique_and_carries_the_servers_data():
    a, b = issue(), issue()
    assert a.id != b.id
    opaque = mpp.decode_object(a.opaque)
    assert opaque["key"] == "7" and len(opaque["nonce"]) == 32


def test_challenge_expires_in_five_minutes_by_default():
    expires = mpp.parse_time(issue().expires)
    assert timedelta(minutes=4) < expires - datetime.now(UTC) <= timedelta(minutes=5)
    assert issue().expires.endswith("Z")


def test_only_payment_authorization_may_be_selected():
    with pytest.raises(ValueError):
        issue(header="X-Payment")
    with pytest.raises(ValueError):
        issue(description="line\nbreak").www_authenticate()


# --- credentials ---------------------------------------------------------------------------------


def test_parses_the_specifications_stripe_credential():
    c = mpp.parse_credential("Payment " + SPEC_CREDENTIAL)
    assert (c.id, c.realm, c.method, c.intent) == ("ch_1a2b3c4d5e", "api.example.com", "stripe", "charge")
    assert c.request == SPEC_REQUEST and c.expires == "2025-01-15T12:05:00Z"
    assert c.payload == {"spt": "spt_1N4Zv32eZvKYlo2CPhVPkJlW"}
    assert (c.header, c.opaque, c.digest, c.source) == ("", "", "", "")


def test_parses_mpp_devs_credential_with_a_named_header():
    c = mpp.parse_credential("Payment " + MPPDEV_CREDENTIAL)
    assert c.header == "Payment-Authorization" and c.opaque == MPPDEV_OPAQUE and c.request == MPPDEV_REQUEST
    assert c.payload["type"] == "hash" and c.source.startswith("did:pkh:eip155:")


def test_a_credential_for_our_own_challenge_checks_out():
    challenge = issue(header="Payment-Authorization")
    c = mpp.parse_credential(credential_for(challenge, {"spt": "spt_1"}))
    assert c.issued_with(SECRET)
    assert not c.issued_with("another-secret")


@pytest.mark.parametrize(
    "field,value",
    [
        ("request", mpp.encode_object({**REQUEST, "amount": "1"})),
        ("realm", "other.test"),
        ("method", "tempo"),
        ("intent", "session"),
        ("expires", "2099-01-01T00:00:00Z"),
        ("opaque", mpp.encode_object({"key": "8"})),
        ("digest", "sha-256=:abc:"),
        ("header", "Payment-Authorization"),
        ("id", "x" * 43),
    ],
)
def test_changing_any_echoed_parameter_breaks_the_id(field, value):
    c = mpp.parse_credential(credential_for(issue(), {"spt": "spt_1"}, **{field: value}))
    assert not c.issued_with(SECRET)


@pytest.mark.parametrize(
    "header",
    [
        "Bearer abc",
        "Payment",
        "Payment not-base64!",
        "Payment " + mpp.b64url(b"[]"),
        "Payment " + mpp.b64url(b'{"payload": {}}'),
        "Payment " + mpp.b64url(b'{"challenge": {"id": "a"}, "payload": {}}'),
        "Payment " + mpp.b64url(b'{"challenge": "x", "payload": {}}'),
        "Payment "
        + mpp.b64url(
            json.dumps(
                {
                    "challenge": {"id": "a", "realm": "r", "method": "stripe", "intent": "charge", "request": 5},
                    "payload": {},
                }
            ).encode()
        ),
        "Payment "
        + mpp.b64url(
            json.dumps(
                {
                    "challenge": {
                        "id": "a", "realm": "r", "method": "stripe", "intent": "charge", "request": "e30",
                        "header": "Authorization",
                    },
                    "payload": {},
                }
            ).encode()
        ),
        "Payment " + "A" * 70_000,
    ],
)
def test_malformed_credentials_are_refused(header):
    with pytest.raises(mpp.MalformedCredential):
        mpp.parse_credential(header)


def test_payment_credential_tells_payment_from_other_schemes():
    assert mpp.payment_credential("Payment abc") == "abc"
    assert mpp.payment_credential("payment   abc ") == "abc"
    assert mpp.payment_credential("Bearer cal_x") is None
    assert mpp.payment_credential("PaymentX abc") is None
    assert mpp.payment_credential("") is None and mpp.payment_credential(None) is None


# --- receipts and problems -----------------------------------------------------------------------


def test_receipt_matches_the_specifications_example():
    when = datetime(2025, 1, 15, 12, 4, 32, tzinfo=UTC)
    assert mpp.receipt("stripe", "pi_1N4Zv32eZvKYlo2CPhVPkJlW", timestamp=when) == SPEC_RECEIPT
    assert mpp.decode_object(mpp.receipt("stripe", "pi_1", external_id="order_9"))["externalId"] == "order_9"


def test_problem_details_use_the_protocols_types():
    body = mpp.problem("payment-required", "Payment is required.", challengeId="abc")
    assert body == {
        "type": "https://paymentauth.org/problems/payment-required",
        "title": "Payment Required",
        "status": 402,
        "detail": "Payment is required.",
        "challengeId": "abc",
    }


def test_parse_time():
    assert mpp.parse_time("2027-01-15T12:05:00Z") == datetime(2027, 1, 15, 12, 5, tzinfo=UTC)
    assert mpp.parse_time("2027-01-15T12:05:00.250Z") is not None
    assert mpp.parse_time("2027-01-15T12:05:00") is None  # no zone
    assert mpp.parse_time("soon") is None and mpp.parse_time("") is None


# --- against the official Python SDK, when it is installed ---------------------------------------


def test_the_official_sdk_reads_our_challenge_and_we_read_its_credential():
    sdk = pytest.importorskip("mpp", reason="pympp, the official MPP SDK, is not installed")
    challenge = issue(description="Calotype Starter pack: 5000 prepaid screenshots", header="Payment-Authorization")

    theirs = sdk.Challenge.from_www_authenticate(challenge.www_authenticate())
    assert (theirs.id, theirs.realm, theirs.method, theirs.intent) == (challenge.id, "calotype.test", "stripe", "charge")
    assert theirs.request == REQUEST and theirs.expires == challenge.expires
    assert theirs.header == "Payment-Authorization" and theirs.opaque["key"] == "7"
    # The SDK recomputes the id its own way, from the decoded values, and agrees.
    assert theirs.verify(SECRET, "calotype.test") is True
    assert theirs.verify("another-secret", "calotype.test") is False

    header = sdk.Credential(challenge=theirs.to_echo(), payload={"spt": "spt_from_sdk"}).to_authorization()
    ours = mpp.parse_credential(header)
    assert ours.issued_with(SECRET) and ours.payload == {"spt": "spt_from_sdk"}
    assert (ours.request, ours.opaque, ours.header) == (challenge.request, challenge.opaque, "Payment-Authorization")


def test_the_official_sdk_agrees_on_the_default_header_and_the_receipt():
    sdk = pytest.importorskip("mpp", reason="pympp, the official MPP SDK, is not installed")
    challenge = issue()
    theirs = sdk.Challenge.from_www_authenticate(challenge.www_authenticate())
    assert theirs.header is None and theirs.verify(SECRET, "calotype.test") is True
    assert sdk.generate_challenge_id(
        secret_key=SECRET,
        realm="calotype.test",
        method="stripe",
        intent="charge",
        request=REQUEST,
        expires=challenge.expires,
        opaque=mpp.decode_object(challenge.opaque),
    ) == challenge.id

    receipt = sdk.Receipt.from_payment_receipt(mpp.receipt("stripe", "pi_123"))
    assert (receipt.method, receipt.reference, receipt.status) == ("stripe", "pi_123", "success")
