"""Ondo Perps — deposit is "provision a deposit address, then plain ERC-20
transfer to it" (COMPASS's requiresDepositAddress model); withdraw is a
single signed REST call. Both are real, current endpoints under Ondo's
"API Reference" (docs.ondoperps.xyz/api-reference/wallet/*) — NOT the
"Builder Integration Guide" page this project looked at first and gave up
on (see git history on runners/unsupported.py): that page only documents a
Bearer-JWT-via-SIWE flow for third-party app builders, a different,
more-involved flow from what this project's already-provisioned
ONDO_API_KEY/ONDO_API_SECRET pair actually needs. The real wallet/* pages
(get-account, provision-deposit-address, withdraw, get-withdrawal-limits,
list-deposit-addresses) all document a second option: `X-API-KEY-ID` header
+ an unspecified signature. That header NAME was never exercised — this
project already discovered (balances.py::_ondo) that the header the
ALREADY-WORKING production key pair actually signs with is
ONDO-KEY-ID/ONDO-TIMESTAMP/ONDO-SIGN, HMAC-SHA256 over
`timestamp + METHOD + path + body` (`body` = "" for a GET). Re-verified
LIVE here 2026-09-07 against three endpoints the docs never covered before
this file existed — GET /v1/account (200), POST /v1/provision_address
(200), POST /v1/wallet/deposit_address/list (200) — so this is the scheme
used below for every call, not the docs' bare `X-API-KEY-ID` name.

Deposit network: funding-ondo-perps.md documents "USDC is supported on
Ethereum and Arbitrum", but every api-reference page's own `network` enum
only ever lists `ethereum`/`avalanche`/`solana` — Arbitrum is never in that
enum. `POST /v1/provision_address` with `network: "arbitrum"` returns 200
(not `invalid_network`) with a real address; `POST /v1/wallet/
deposit_address/list` shows that SAME address registered under
`"network": "arbitrum"` in Ondo's own records (and, interestingly, under
`"ethereum"` and `"avalanche"` too — this account's deposit addresses are
shared across all three EVM networks Ondo watches, not per-network vaults;
the `"chain": "avax-c-chain"` field the provision_address response itself
returns is therefore not a routing signal, just a static/default label — it
came back identical for ethereum/avalanche/arbitrum requests alike).
**CONFIRMED LIVE 2026-09-07 end to end**: a real `$1.00` Arbitrum-USDC
transfer to a freshly provisioned address, `poll_balance_usd` showing the
credit (`GET /v1/perps/balance` went from $2.12 to $3.12), tx
`0x822f9ece4759e02655f9cd5e3a21fa524d13e0d491e5f980f185d544aa668b9e`.

Withdraw: `POST /v1/withdraw`, same auth as everything else in this file.
`from.wallet` is `"margin"` — the only wallet provision_address's own
example ever uses, and the one `GET /v1/perps/balance`'s `marginBalance`
field (balances.py::_ondo) already reads successfully. `customer_withdrawal_id`
is a fresh uuid4 per call (the API rejects a REUSED one outright —
`withdrawal_duplicate_customer_withdrawal_id`). `withdrawalFeeUSD` comes
back as a STRING on GET /v1/account (e.g. "1" for this account) — reused as
the same "refuse rather than send it and lose the difference" guard every
other connector here applies before Extended's.

**Real gap hit and fixed live 2026-09-07**: the first live attempt 400'd
`withdrawal_address_not_found` for this account's OWN operating-wallet
address, even though `GET /v1/wallet/address_book` already listed it —
because that entry is stored lowercase and `wallet.address` is EIP-55
checksummed (mixed case); Ondo's lookup doesn't normalize case before
comparing. Fixed by `_address_book_entry`: look the destination up
case-insensitively against the address book and send back whatever string
is actually stored there, never the checksummed form passed in. A truly
unregistered address still fails loudly (new addresses need a live SIWE
wallet signature via the separate address-book challenge endpoints — not
implemented here, same "this connector can't point elsewhere" situation as
runners/extended.py's withdraw recipient).

**CONFIRMED LIVE 2026-09-07 end to end** after that fix: a real $1.50
withdraw, `_run_withdraw`'s own before/after on-chain balance poll (not
just the REST "pending" response) confirmed the wallet's real USDC balance
increased by the full $1.50 within 3 seconds — `actualCostUsd: 0.0`, i.e.
NO fee was actually deducted from this withdrawal despite `withdrawalFeeUSD`
reading "$1" on the account — worth re-checking on a larger real withdrawal
before assuming $1 never applies; this could be a promo/waiver on small
amounts rather than the field being simply wrong.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import requests
from web3 import Web3

from graph.structures.DEXes import Chain, Stable

from .. import chain_ops, config
from .base import DexConnector, WithdrawResult

_BASE_URL = "https://api.ondoperps.xyz"

# See module docstring — Arbitrum accepted and self-consistent live
# 2026-09-07 even though no api-reference page's `network` enum lists it.
_NETWORK_CODE = {Chain.ARBITRUM: "arbitrum"}

_WALLET = "margin"


class OndoConnector(DexConnector):
    name = "Ondo Perps"
    supported_chains = frozenset({Chain.ARBITRUM})
    supported_stables = frozenset({Stable.USDC})

    def __init__(self):
        self._api_key = config.require_env("ONDO_API_KEY")
        self._api_secret = config.require_env("ONDO_API_SECRET")
        self._session = requests.Session()
        self._account_cache: dict | None = None

    def _headers(self, method: str, path: str, body: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method}{path}{body}"
        signature = hmac.new(self._api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "ONDO-KEY-ID": self._api_key,
            "ONDO-TIMESTAMP": timestamp,
            "ONDO-SIGN": signature,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _unwrap(response: requests.Response, method: str, path: str) -> dict:
        # NEVER response.raise_for_status() first — Ondo's error responses
        # are structured JSON (`{"success": false, "error", "error_code"}`,
        # e.g. bad_withdrawal_address/insufficient_funds/invalid_network/
        # withdrawal_amount_too_small per docs.ondoperps.xyz/api-reference/
        # wallet/withdraw), and raise_for_status() would discard that body
        # in favor of a bare "400 Client Error" with no error_code at all —
        # the one piece of information actually needed to tell "your address
        # is malformed" from "your amount is too small" apart. Parse first,
        # raise with whatever's actually in the body either way.
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError(f"Ondo {method} {path} -> {response.status_code}, non-JSON body: {response.text!r}")
        if not data.get("success"):
            raise RuntimeError(f"Ondo {method} {path} -> {response.status_code}: {data}")
        return data["result"]

    def _get(self, path: str) -> dict:
        response = self._session.get(f"{_BASE_URL}{path}", headers=self._headers("GET", path, ""), timeout=15)
        return self._unwrap(response, "GET", path)

    def _post(self, path: str, body_obj: dict) -> dict:
        # Compact separators — the signed message includes this exact string,
        # so whatever is sent over the wire must byte-for-byte match what was
        # signed (json.dumps' default separators embed spaces requests.post's
        # own json= would reproduce differently across urllib3 versions —
        # sent explicitly via `data=` instead of `json=` for that reason).
        body = json.dumps(body_obj, separators=(",", ":"))
        response = self._session.post(
            f"{_BASE_URL}{path}", data=body, headers=self._headers("POST", path, body), timeout=15
        )
        return self._unwrap(response, "POST", path)

    def _account(self) -> dict:
        if self._account_cache is None:
            self._account_cache = self._get("/v1/account")
        return self._account_cache

    def _address_book_entry(self, to_address: str) -> str:
        # Withdrawals only go to a PRE-REGISTERED address (GET /v1/wallet/
        # address_book) — confirmed live 2026-09-07 the hard way, a real
        # `POST /v1/withdraw` 400'd `withdrawal_address_not_found` for this
        # account's own operating-wallet address even though it WAS already
        # in the address book, because that entry is stored lowercase
        # (`0x477899d0...`) while `wallet.address` is EIP-55 checksummed
        # (mixed case, `0x477899D0...`) — Ondo's lookup doesn't normalize
        # case before comparing. Fix: look the address up case-insensitively
        # here and send back whatever string the address book itself has,
        # never the checksummed form. Registering a NEW address at all needs
        # a separate live wallet signature (SIWE address-book challenge,
        # api-reference/auth/{get,complete}-siwe-address-book-challenge) —
        # not implemented here, same "this connector can't point elsewhere"
        # gap as runners/extended.py's withdraw recipient.
        entries = self._get("/v1/wallet/address_book")["addressBook"]
        for entry in entries:
            if entry["withdrawalAddress"].lower() == to_address.lower():
                return entry["withdrawalAddress"]
        raise RuntimeError(
            f"Ondo: {to_address} is not in this account's withdrawal address book "
            f"({[e['withdrawalAddress'] for e in entries]}) — add it via Ondo's own UI/SIWE "
            "address-book flow first, this connector can't register a new one."
        )

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        network = _NETWORK_CODE.get(chain)
        if network is None:
            raise RuntimeError(f"Ondo: no network code known for {chain.name}")

        account = self._account()
        fee_usd = float(account.get("withdrawalFeeUSD") or 0)
        if fee_usd >= amount_usd:
            raise RuntimeError(
                f"Ondo: withdrawal fee (${fee_usd}) would consume the entire ${amount_usd} withdrawal or "
                "more — refusing rather than send it and lose the difference."
            )
        registered_address = self._address_book_entry(to_address)

        body = {
            "customer_withdrawal_id": f"compass-{uuid.uuid4()}",
            "symbol": stable.name,
            "network": network,
            "amount": str(amount_usd),
            "address": registered_address,
            "from": {"id": account["accountID"], "wallet": _WALLET},
        }
        requestedAt = time.time()
        result = self._post("/v1/withdraw", body)
        return WithdrawResult(
            externalId=result["withdrawal_id"],
            requestedAt=requestedAt,
            acceptedAt=time.time(),
            amountRequestedUsd=amount_usd,
            quotedFeeUsd=fee_usd,
        )

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        network = _NETWORK_CODE.get(chain)
        if network is None:
            raise RuntimeError(f"Ondo: no network code known for {chain.name}")

        account = self._account()
        result = self._post(
            "/v1/provision_address",
            {
                "symbol": stable.name,
                "deposit_destination": {"id": account["accountID"], "wallet": _WALLET},
                "network": network,
            },
        )
        deposit_address = result["address"]
        return chain_ops.build_erc20_transfer_tx(w3, chain, stable, from_address, deposit_address, amount_usd)

    def poll_balance_usd(self, stable: Stable) -> float:
        # Same read as balances.py::_ondo — USDC-only, marginBalance field.
        data = self._get("/v1/perps/balance")
        return float(data["marginBalance"])
