"""Aster's REAL "V3 Pro API-Key" per-request signature — shared by
runners/aster.py (withdraw/deposit execution) and balances.py (read-only
balance). Extracted to one place because getting this wrong twice, slightly
differently, is exactly how a signing bug hides.

This is NOT what Aster's public docs describe (an EIP-712 "Message" struct
wrapping the raw query string — the first thing built here, which failed
closed with a signature error). The real scheme, ported from
sentinelBackend's AsterHttpConnector._query_signature
(src/connectors/aster/http.py), confirmed live 2026-09-04 via a real
`GET /fapi/v3/account` call:

    sorted(params) -> compact JSON -> abi.encode(
        ["string", "address", "address", "uint256"],
        [json_str, user, signer, nonce],
    ) -> keccak256 -> EIP-191 personal_sign (NOT EIP-712) with the signer's
    key.

`ASTER_USER`/`ASTER_SIGNER`/`ASTER_PRIVATE_KEY` (piggybank-arb/.env) are used
as an already-registered agent keypair — sentinel's equivalent
(`_ensure_signer`) only self-registers a FRESH agent via
`POST /fapi/v3/registerAndApproveAgent` when no persisted signer exists;
piggybank-arb's own trading bot already registered this one to place real
trades, so that one-time handshake is assumed done here, not repeated.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak


def v3_signed_query(account: str, signer_address: str, signer_key: str, extra: dict | None = None) -> str:
    """The exact query string to send on the wire (signature included) —
    built and signed as one string, matching sentinel's `_signed_query`
    byte-for-byte. `extra` is the request's own business params, if any
    (empty for a plain balance read)."""
    params = dict(extra or {})
    params.setdefault("timestamp", int(time.time() * 1000))
    params.setdefault("recvWindow", 5000)
    nonce = int(time.time() * 1_000_000)

    trimmed = {k: str(v) for k, v in params.items() if v is not None}
    sorted_obj = {k: trimmed[k] for k in sorted(trimmed.keys())}
    json_str = json.dumps(sorted_obj, separators=(",", ":"))
    encoded = abi_encode(["string", "address", "address", "uint256"], [json_str, account, signer_address, nonce])
    digest = keccak(encoded)
    signature = Account.sign_message(encode_defunct(digest), private_key=signer_key).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    params["nonce"] = nonce
    params["user"] = account
    params["signer"] = signer_address
    params["signature"] = signature
    return urlencode(params)
