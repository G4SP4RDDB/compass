"""Aster's "V3 Pro API-Key" per-request signature — shared by
runners/aster.py (withdraw/deposit execution) and balances.py (read-only
balance). One place, because getting this wrong twice, slightly differently,
is exactly how a signing bug hides.

Source: asterdex/api-docs, V3(Recommended)/EN/aster-finance-futures-api-v3.md
(the doc docs.asterdex.com/for-developers/aster-api links to), Python sample
`send_by_url`, re-fetched 2026-09-07. It is a genuine EIP-712 typed-data
signature, NOT the keccak/abi.encode/EIP-191 scheme this module used to
implement (ported from sentinelBackend's `_query_signature` and claimed
"confirmed live 2026-09-04" — re-tested 2026-09-07 against the same
GET /fapi/v3/account it cited, it fails with -1000 "Signature check failed").

    msg       = urlencode(<business params> + nonce + user + signer)
                — the EXACT query string sent, `&signature=` then appended
    nonce     = microseconds (seconds * 1_000_000), valid ±60s of server time
    domain    = {name: "AsterSignTransaction", version: "1", chainId: 1666,
                 verifyingContract: 0x0}
    types     = {Message: [{name: "msg", type: "string"}]}
    signed by = the agent (`signer`) private key, not the master wallet

There is no `timestamp`/`recvWindow` here — those are the V1 HMAC API-key
scheme's parameters, not V3's. Insertion order matters: the backend verifies
the string it received, so what is signed must be byte-identical to what is
sent, business params first, then nonce/user/signer, in that order.

`ASTER_USER`/`ASTER_SIGNER`/`ASTER_PRIVATE_KEY` are an already-registered
agent keypair (registered once by piggybank-arb's trading bot via
`POST /fapi/v3/registerAndApproveAgent`); that handshake is assumed done.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

from eth_account import Account
from eth_account.messages import encode_typed_data

_DOMAIN_CHAIN_ID = 1666
_VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000000"


def v3_signed_query(account: str, signer_address: str, signer_key: str, extra: dict | None = None) -> str:
    """The exact query string to send on the wire, signature included.
    `extra` is the request's own business params, if any (empty for a plain
    balance read)."""
    params = {k: str(v) for k, v in (extra or {}).items() if v is not None}
    params["nonce"] = str(int(time.time() * 1_000_000))
    params["user"] = account
    params["signer"] = signer_address
    query = urlencode(params)

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [{"name": "msg", "type": "string"}],
        },
        "primaryType": "Message",
        "domain": {
            "name": "AsterSignTransaction",
            "version": "1",
            "chainId": _DOMAIN_CHAIN_ID,
            "verifyingContract": _VERIFYING_CONTRACT,
        },
        "message": {"msg": query},
    }
    signable = encode_typed_data(full_message=typed_data)
    signature = Account.sign_message(signable, private_key=signer_key).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return f"{query}&signature={signature}"
