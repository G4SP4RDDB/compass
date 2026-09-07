"""Runs under THIS directory's dedicated Python 3.9 venv (see README.md) —
not compass_test's own .venv, which can't install fast_stark_crypto (no
Python 3.14 wheel, and its pinned PyO3 refuses to build for 3.14 at all).

Reads one JSON object from stdin with the withdrawal's signing inputs,
computes the StarkEx withdrawal message hash and signs it with the account's
STARK private key, writes `{"r": "0x...", "s": "0x..."}` to stdout. The
private key travels only on stdin (never argv, never logged) and this
process never writes it anywhere else.

Mirrors x10/signing/withdrawal_object.py::create_withdrawal_object from
Extended's own SDK (github.com/x10xchange/python_sdk, starknet branch) call
for call — same function, same argument order — rather than a
re-derivation, since getting a STARK message hash wrong is a silent
wrong-signature failure, not a loud one.
"""

import json
import sys

from fast_stark_crypto import get_withdrawal_msg_hash, sign


def main() -> None:
    request = json.loads(sys.stdin.read())
    msg_hash = get_withdrawal_msg_hash(
        recipient_hex=request["recipient_hex"],
        position_id=request["position_id"],
        collateral_id=int(request["collateral_id_hex"], base=16),
        amount=request["amount"],
        expiration=request["expiration"],
        salt=request["salt"],
        user_public_key=int(request["public_key_hex"], base=16),
        domain_name=request["domain_name"],
        domain_version=request["domain_version"],
        domain_chain_id=request["domain_chain_id"],
        domain_revision=request["domain_revision"],
    )
    r, s = sign(private_key=int(request["private_key_hex"], base=16), msg_hash=msg_hash)
    json.dump({"r": hex(r), "s": hex(s)}, sys.stdout)


if __name__ == "__main__":
    main()
