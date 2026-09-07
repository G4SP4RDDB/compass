"""Aden — perps run on Orderly Network infrastructure, not the Gate.io-clone
REST API balances.py already reads from (`api.aden.io`, HMAC KEY/SECRET —
still used here for poll_balance_usd, see below). Deposit/withdraw live on a
COMPLETELY SEPARATE host pair with wallet-session auth, undocumented
anywhere public: reverse-engineered 2026-09-07 by capturing the real
frontend's network + wallet-signing traffic during live withdraw/deposit
runs (`perps.aden.io` = Aden's own backend-for-frontend,
`brokerapi.gateperps.com` = the underlying Gate-perps broker layer).

    perps.aden.io/api/perp/withdraw_limit               GET  (day/once caps)
    brokerapi.gateperps.com/apiw/v2/perp-dex/withdraw-min-limit  GET  (per-chain floor)
    perps.aden.io/api/perp/withdraw/sign_data            POST -> hex-encoded EIP-712 payload to sign
    perps.aden.io/api/perp/withdraw/submit                POST {..., r, s, v, timestamp} -> event_id
    perps.aden.io/api/perp/withdraw/status?event_id=...   GET  (10 submitted -> 11 processing -> 20 done)

The sign_data hex payload decodes (UTF-8) to a complete, ready-to-sign
EIP-712 `{domain, types, primaryType, message}` object — Orderly Network's
public, documented "Withdraw" scheme (domain name "Orderly", primaryType
"Withdraw", fields brokerId/chainId/receiver/token/amount/withdrawNonce/
timestamp — cross-checked against orderly.network/docs). This connector
signs EXACTLY what the backend hands back rather than reconstructing the
struct by hand (unlike Aster's _withdraw_action_signature): the nonce is
server-assigned and there is no known way to derive it independently.

Deposit is a PLAIN ERC-20 transfer to a fixed address off Aden's own
"Deposit" screen (config.expected_deposit_address, same
COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_ADEN_BSC convention as MEXC's
build_deposit_tx) — by explicit instruction, in place of an earlier version
of this connector that called Aden's Vault.deposit(VaultDepositFE) contract
directly (the flow the real web UI's "connect wallet and deposit" button
actually drives, and the one this was originally reverse-engineered against
and confirmed live end-to-end: sign_data -> deposit -> status 0 -> 10 -> 20).
**That live confirmation was for the VAULT CALL, not for this plain-transfer
address** — whether a bare `transfer()` to it credits the account the same
way (immediately, via on-chain sweep, or not without some other trigger this
connector doesn't send) has not been verified live. If a deposit test lands
on-chain but poll_balance_usd never shows the credit, that's the first thing
to suspect, and the Vault.deposit path (see this file's git history) is the
fallback with an actual live confirmation behind it.

Auth for the perps.aden.io/brokerapi.gateperps.com calls is a wallet-session
JWT, NOT an API key/secret pair like every other connector in this file — but
unlike an earlier version of this connector (which required a token MANUALLY
copied out of the real site's localStorage, expiring every ~15 minutes), the
login handshake itself was captured live 2026-09-07 and is now done by this
connector directly, with no browser involved at all:

    GET  /api/account/user/plug_in_sign_message   -> {message, timestamp}
         message = "Please sign this message to login Address: {address} Timestamp: {timestamp}"
    POST /api/account/user/plug_in_auth            {chain:"evm", address, sign, source_type:5, timestamp}
         -> {perp_evm_access_token, perp_evm_refresh_token, perp_evm_uid,
             perp_main_uid, perp_evm_access_token_exp, ...}

`sign` is a plain `personal_sign` over the message text (NOT EIP-712 like the
withdraw signature) — `eth_account.messages.encode_defunct`, signed with the
SAME operating wallet key already used for withdraw/deposit, so this needs no
new credential. `_login()` runs once per process, lazily, the first time a
withdraw actually needs auth (same discipline as before — a deposit-only run
still doesn't touch any of this).

A `perp_evm_refresh_token` came back too (observed ~7-day lifetime, vs. the
access token's ~15 minutes, renewed via `POST /api/account/perp/renew`) but
is deliberately NOT used here: persisting and rotating a second credential
across process runs is real complexity for a saving that doesn't matter — a
personal_sign login is a local, instant, gasless operation, so just doing it
again next run is simpler than caching anything. broker_uid/broker_main_uid
come straight from the login response (`perp_evm_uid`/`perp_main_uid`)
instead of a separate config entry, so they can't drift out of sync with it.

Two more request headers were observed but never reverse-engineered
(`csrftoken`, `X-GATE-AUTH-TIME`) — generation logic unknown, not sent here.
If a live call 403s, these are the first suspects; capture a fresh session
with the browser hooks (see this session's chat) to check.

poll_balance_usd deliberately does NOT use this fragile session auth at
all — it reuses the SAME already-working HMAC-signed `api.aden.io` endpoint
balances.py::_aden calls (KEY/Timestamp/SIGN headers, ADEN_API_KEY/
ADEN_API_SECRET), which returns the identical `total`/`unrealised_pnl`
fields either way (confirmed live: brokerapi.gateperps.com's
/futures/usdt/accounts returns the exact same shape). No reason to depend on
a 15-minute token for a plain balance read when a stable, documented-working
path already exists.

perps.aden.io ALSO serves an incomplete TLS chain (confirmed 2026-09-07,
`openssl s_client`: "Verify return code: 21, unable to verify the first
certificate") — the same class of trap sentinelBackend's Aden README already
documents for a different Aden subdomain. A browser tolerates it via AIA-
chasing; plain `requests`/certifi does not, so every call in this file goes
through a Session whose `verify` is the combined certifi+missing-intermediate
bundle from _aden_ca_bundle_path (see `_aden_intermediate_ca.pem`) — NOT
`verify=False`. That file's header explains where the cert came from and how
to re-fetch/re-verify it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
from pathlib import Path

import certifi
import requests
from eth_account.messages import encode_defunct
from web3 import Web3

from graph.structures.DEXes import Chain, Stable

from .. import chain_ops, config
from ..wallet import OperatingWallet
from .base import DexConnector, WithdrawResult

_PERPS_BASE_URL = "https://perps.aden.io"
_LEGACY_API_HOST = "api.aden.io"
_LEGACY_API_PREFIX = "/api/v1"

_INTERMEDIATE_CA_PATH = Path(__file__).resolve().parent / "_aden_intermediate_ca.pem"


def _aden_ca_bundle_path() -> str:
    """certifi's normal root bundle + the one intermediate perps.aden.io
    forgets to send — see module docstring. Rebuilt into a temp file each
    time rather than shipping a merged file in the repo, so it stays current
    with whatever certifi version is actually installed."""
    combined = Path(tempfile.gettempdir()) / "aden_ca_bundle.pem"
    combined.write_text(Path(certifi.where()).read_text() + "\n" + _INTERMEDIATE_CA_PATH.read_text())
    return str(combined)

_CHAIN_NAME = {Chain.BSC: "bsc"}
_ASSET_BY_STABLE = {Stable.USDT: "USDT"}

# Observed literal value in the real login request's `source_type` field —
# meaning unknown (which client/platform is logging in?), sent as-is since
# there's no way to derive it and it's what a real browser login sends.
_LOGIN_SOURCE_TYPE = 5


class AdenConnector(DexConnector):
    name = "Aden"
    supported_chains = frozenset({Chain.BSC})
    supported_stables = frozenset({Stable.USDT})

    def __init__(self):
        self._operating_wallet = OperatingWallet()
        # Only used by poll_balance_usd, against the separate HMAC-keyed
        # legacy API — see module docstring.
        self._legacy_api_key = config.require_env("ADEN_API_KEY")
        self._legacy_api_secret = config.require_env("ADEN_API_SECRET")
        self._session = requests.Session()
        self._session.verify = _aden_ca_bundle_path()
        # Wallet-session credentials (withdraw only) — deliberately NOT
        # logged in here, same lazy discipline as wallet.OperatingWallet's
        # private key: a deposit-only run (build_deposit_tx,
        # poll_balance_usd) needs none of this. See _login/_load_session_auth.
        self._token_value: str | None = None
        self._broker_uid_value: int | None = None
        self._address_value: str | None = None

    def _unauth_headers(self) -> dict:
        # x-perp-const-id: an observed header whose generation logic is
        # unknown (device fingerprint) — a fixed placeholder is sent rather
        # than omitted, on the theory that a wrong-but-present value is more
        # likely to be accepted than a missing one, but this is a guess.
        return {"x-perp-const-id": "compass-test-connector", "Content-Type": "application/json"}

    def _login(self) -> None:
        """Logs in with a `personal_sign` over Aden's own login challenge —
        no browser, no manually-copied token. See module docstring for the
        full captured handshake."""
        address = Web3.to_checksum_address(config.require_env("ADEN_WALLET_ADDRESS"))
        signer = Web3.to_checksum_address(self._operating_wallet.address)
        if signer != address:
            raise RuntimeError(
                f"Aden: operating wallet {signer} is not ADEN_WALLET_ADDRESS ({address}) — logging in with a "
                "different key logs into a DIFFERENT Aden account than the one this connector's other reads "
                "(ADEN_API_KEY/SECRET, ADEN_WALLET_ADDRESS) target."
            )
        response = self._session.get(
            f"{_PERPS_BASE_URL}/api/account/user/plug_in_sign_message",
            params={"chain": "evm", "address": address},
            headers=self._unauth_headers(),
            timeout=15,
        )
        response.raise_for_status()
        challenge = response.json()["data"]

        signable = encode_defunct(text=challenge["message"])
        signed = self._operating_wallet.account.sign_message(signable)

        response = self._session.post(
            f"{_PERPS_BASE_URL}/api/account/user/plug_in_auth",
            json={
                "chain": "evm",
                "address": address,
                "sign": signed.signature.hex(),
                "source_type": _LOGIN_SOURCE_TYPE,
                "timestamp": challenge["timestamp"],
            },
            headers=self._unauth_headers(),
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(f"Aden login -> {response.status_code}: {response.text}")
        data = response.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"Aden login rejected: {data}")

        self._token_value = data["data"]["perp_evm_access_token"]
        self._broker_uid_value = data["data"]["perp_evm_uid"]
        self._address_value = address

    def _load_session_auth(self) -> None:
        if self._token_value is None:
            self._login()

    @property
    def _token(self) -> str:
        self._load_session_auth()
        return self._token_value

    @property
    def _broker_uid(self) -> int:
        self._load_session_auth()
        return self._broker_uid_value

    @property
    def _address(self) -> str:
        self._load_session_auth()
        return self._address_value

    def _headers(self) -> dict:
        # csrftoken / X-GATE-AUTH-TIME: observed on some authenticated calls,
        # generation logic unknown, not sent here — see module docstring.
        return {
            **self._unauth_headers(),
            "Authorization": f"Bearer {self._token}",
            "x-perp-authorization": self._token,
            "x-perp-broker-main-uid": str(self._broker_uid),
            "x-perp-device-type": "3",
            "x-perp-app-version": "1.0.0",
            "x-perp-language": "en",
        }

    def _post(self, path: str, body: dict) -> dict:
        response = self._session.post(f"{_PERPS_BASE_URL}{path}", json=body, headers=self._headers(), timeout=15)
        if not response.ok:
            raise RuntimeError(f"Aden POST {path} -> {response.status_code}: {response.text}")
        data = response.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"Aden POST {path} rejected: {data}")
        return data["data"]

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        from connectors.stable_tokens import get_stable_token_address

        chain_name = _CHAIN_NAME[chain]
        token_address = Web3.to_checksum_address(get_stable_token_address(chain, stable))
        body = {
            "address": self._address,
            "chain": chain_name,
            "to_address": Web3.to_checksum_address(to_address),
            "token_address": token_address,
            "token_amount": f"{amount_usd:.6f}",
            "token_symbol": _ASSET_BY_STABLE[stable],
            "broker_uid": self._broker_uid,
        }
        sign_data = self._post("/api/perp/withdraw/sign_data", body)
        # `data.data` is a hex-encoded UTF-8 JSON string: a complete, ready-
        # to-sign {domain, types, primaryType, message} EIP-712 object —
        # signed AS-IS, never reconstructed by hand (see module docstring).
        typed_data = json.loads(bytes.fromhex(sign_data["data"]).decode("utf-8"))

        requestedAt = time.time()
        signed = self._operating_wallet.sign_typed_data(typed_data)
        submit_body = {
            **body,
            # Zero-padded to a full 32 bytes: unlike runners/hyperliquid.py's
            # plain hex(signed.r)/hex(signed.s) (untested live there too),
            # r/s are 32-byte signature components and a short value
            # (leading zero byte) would otherwise serialize shorter than
            # that — safer to pad explicitly than assume the backend's JSON
            # parser treats "0x1a2b" and "0x00...1a2b" as the same bignum.
            "r": "0x" + format(signed.r, "064x"),
            "s": "0x" + format(signed.s, "064x"),
            "v": signed.v,
            "timestamp": typed_data["message"]["timestamp"],
        }
        result = self._post("/api/perp/withdraw/submit", submit_body)
        if not result.get("success"):
            raise RuntimeError(f"Aden withdraw submit did not report success: {result}")

        return WithdrawResult(
            externalId=str(result["event_id"]),
            requestedAt=requestedAt,
            acceptedAt=time.time(),
            amountRequestedUsd=amount_usd,
            quotedFeeUsd=None,
        )

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        # Plain address, MEXC-style — NOT the vault contract call this was
        # originally built and confirmed against; see module docstring for
        # why that trade-off was made and what's unverified about it.
        deposit_address = config.expected_deposit_address(self.name, chain.name)
        if deposit_address is None:
            raise RuntimeError(
                f"Aden: no deposit address configured for {stable.name}/{chain.name} — set "
                f"COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_{self.name.upper()}_{chain.name.upper()} in "
                "compass/.env (copy it from Aden's own Deposit screen)."
            )
        return chain_ops.build_erc20_transfer_tx(w3, chain, stable, from_address, deposit_address, amount_usd)

    def poll_balance_usd(self, stable: Stable) -> float:
        # The separate, already-working HMAC-keyed legacy API — see module
        # docstring for why this doesn't reuse the wallet-session auth above.
        # Ported from balances.py::_aden (same endpoint, same signing recipe,
        # same total+unrealised_pnl formula).
        path = f"{_LEGACY_API_PREFIX}/dex_futures/{stable.name.lower()}/accounts"
        timestamp = int(time.time())
        body_hash = hashlib.sha512(b"").hexdigest()
        message = f"GET\n{path}\n\n{body_hash}\n{timestamp}"
        signature = hmac.new(self._legacy_api_secret.encode(), message.encode(), hashlib.sha512).hexdigest()
        headers = {"KEY": self._legacy_api_key, "Timestamp": str(timestamp), "SIGN": signature}
        response = self._session.get(f"https://{_LEGACY_API_HOST}{path}", headers=headers, timeout=15)
        if not response.ok:
            raise RuntimeError(f"Aden legacy account read -> {response.status_code}: {response.text}")
        data = response.json()
        return float(data.get("total") or 0) + float(data.get("unrealised_pnl") or 0)
