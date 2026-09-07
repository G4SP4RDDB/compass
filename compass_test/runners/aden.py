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
actually drives). **Confirmed live 2026-09-07**: a real deposit through this
plain-transfer path landed and was credited (`poll_balance_usd` showed it) —
so despite going around the vault-call/LayerZero machinery entirely, Aden's
backend still picks it up (almost certainly via on-chain sweep watching that
address, independent of the perps.aden.io endpoints).

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

**The login POST alone is gated by risk control** (`407001
P_FOMOX_IN_RISK_ERROR`) — confirmed 2026-09-07 across three escalating
attempts: plain Python, a browser-executed fetch() with a placeholder device
id, and a browser-executed fetch() with the REAL device fingerprint from a
logged-in tab's localStorage — all three rejected identically. The actual
fix, found by diffing a real successful browser login's raw request headers:
a separate `risk-verify-token` header (same value as `x-perp-const-id`, both
= `localStorage['aden_web3_device_finger_key']`, ADEN_DEVICE_FINGERPRINT
below) that this connector was never sending, plus `Content-Type:
text/plain;charset=UTF-8` on the login POST specifically (not
application/json). With both, plain Python `requests` logs in headlessly —
no browser, no Selenium, confirmed live. ADEN_DEVICE_FINGERPRINT itself is a
stable per-installation id, not a short-lived session credential, so this
isn't a manual-refresh workflow like the token-copying approach it replaced.

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

**WITHDRAW: CONFIRMED LIVE 2026-09-07** — a real $10.50 BSC withdraw,
`amountReceivedUsd: $10.30` ($0.20 fee, exactly matching the configured
`withdrawFeeUsd`), external id a real UUID, on-chain balance increase
confirmed by `executor.py`'s own before/after poll. Getting there took
real ground truth, not more guessing: every attempt before this 500'd with
a generic `50001201 P_FOMOX_IN_INTERNAL_ERROR "Invalid request"`, and nine
different live-tested guesses (address casing, amount string formatting,
an explicit `withdraw_nonce` field, `Origin`/`Referer` headers, `json=` vs
`data=`+`text/plain` content-type) all failed to fix it or even change the
error. What actually broke the deadlock was a real browser HAR capture of
one successful UI withdrawal (`sign_data` + `submit`, both 200) — diffing
its exact headers/body against what this connector sent found TWO real,
independent bugs, confirmed by testing each in isolation:

1. `x-perp-broker-main-uid` was sent as `perp_evm_uid` (55409231) instead
   of `perp_main_uid` (41047073) — two DIFFERENT fields in the login
   response this connector had conflated into one. Wrong value alone
   reproduces a clean 401 P_FOMOX_IN_UNAUTHORIZED — a NEW error the old
   guessing never surfaced, because bug #2 below was masking it.
2. An earlier fix pass (reasoning from the HAR alone, which doesn't show
   browser-managed session cookies) removed the `Authorization: Bearer
   <general access_token>` header entirely, since the captured request
   didn't show it explicitly. That header genuinely IS required —
   removing it alone (even with bug #1 fixed) reproduces the same 401.
   Both bugs had to be fixed together; neither fix alone was sufficient,
   which is exactly why blind single-field guessing never found either
   one — changing one masked symptom of the other.

`x-perp-app-version` ("0.1.0", not "1.0.0"), the missing `x-perp-device-
name`/`x-perp-device-version` headers, and dropping `risk-verify-token`
outside of login all came from that same HAR diff and are still in
`_headers()` unchanged. The original amount-below-minimum bug
(`_check_min_withdraw`'s $10.2 BSC / $10.5 Arbitrum floor) was real too
and stays fixed independent of all this.
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


def _trim_amount(amount_usd: float) -> str:
    """`10.2`, not `10.200000` — matches the real UI's own request bodies
    (see withdraw's body comment for why this turned out to matter)."""
    return f"{amount_usd:.6f}".rstrip("0").rstrip(".")


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
        self._general_token_value: str | None = None
        self._broker_uid_value: int | None = None
        self._broker_main_uid_value: int | None = None
        self._address_value: str | None = None

    def _unauth_headers(self) -> dict:
        # ADEN_DEVICE_FINGERPRINT is `localStorage['aden_web3_device_finger_key']`
        # from a real logged-in browser session — a stable per-installation
        # id, not a short-lived credential (unlike everything else this
        # connector deals with). Sent under BOTH header names: `x-perp-const-id`
        # is what every authenticated call also carries, but the ACTUAL fix
        # for login's 407001 P_FOMOX_IN_RISK_ERROR risk-control rejection was
        # the separate `risk-verify-token` header (same value) — confirmed
        # 2026-09-07 by diffing a real browser login's request headers
        # against what this connector was sending; `x-perp-const-id` alone
        # (even the real value, even replayed from the real tab) was NOT
        # enough. See module docstring.
        fingerprint = config.require_env("ADEN_DEVICE_FINGERPRINT")
        return {"x-perp-const-id": fingerprint, "risk-verify-token": fingerprint, "Content-Type": "application/json"}

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

        body = {
            "chain": "evm",
            "address": address,
            "sign": signed.signature.hex(),
            "source_type": _LOGIN_SOURCE_TYPE,
            "timestamp": challenge["timestamp"],
        }
        response = self._session.post(
            f"{_PERPS_BASE_URL}/api/account/user/plug_in_auth",
            # `data=` + an explicit text/plain content-type, NOT `json=` —
            # confirmed 2026-09-07 against a real browser login's actual
            # request headers (`content-type: text/plain;charset=UTF-8`).
            # Untested whether application/json would also have worked; not
            # worth another live login attempt to find out once this one did.
            data=json.dumps(body),
            headers={**self._unauth_headers(), "Content-Type": "text/plain;charset=UTF-8"},
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(f"Aden login -> {response.status_code}: {response.text}")
        data = response.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"Aden login rejected: {data}")

        # `perp_evm_uid` and `perp_main_uid` are TWO SEPARATE numbers in the
        # login response (confirmed live 2026-09-07: 55409231 vs 41047073
        # for this account) — an earlier version of this connector conflated
        # them, using `perp_evm_uid` for BOTH the withdraw body's
        # `"broker_uid"` field (correct — matches a real captured browser
        # request) AND the `x-perp-broker-main-uid` HEADER (wrong — a real
        # captured browser request sends `perp_main_uid` there instead).
        # That mismatch is the real cause of a 401 P_FOMOX_IN_UNAUTHORIZED
        # on withdraw/sign_data once the OTHER bug (a spurious `Authorization`
        # header, see `_headers()`) stopped masking it behind a generic 500.
        self._general_token_value = data["data"]["access_token"]
        self._token_value = data["data"]["perp_evm_access_token"]
        self._broker_uid_value = data["data"]["perp_evm_uid"]
        self._broker_main_uid_value = data["data"]["perp_main_uid"]
        self._address_value = address

    def _load_session_auth(self) -> None:
        if self._token_value is None:
            self._login()

    @property
    def _token(self) -> str:
        self._load_session_auth()
        return self._token_value

    @property
    def _general_token(self) -> str:
        self._load_session_auth()
        return self._general_token_value

    @property
    def _broker_uid(self) -> int:
        self._load_session_auth()
        return self._broker_uid_value

    @property
    def _broker_main_uid(self) -> int:
        self._load_session_auth()
        return self._broker_main_uid_value

    @property
    def _address(self) -> str:
        self._load_session_auth()
        return self._address_value

    def _headers(self) -> dict:
        # Rebuilt from a real captured browser withdraw (HAR export,
        # sign_data + submit, both 200) plus live isolation testing
        # 2026-09-07 — the PRIOR version of this method was a guess that
        # never actually worked (every live withdraw attempt 500'd with a
        # generic P_FOMOX_IN_INTERNAL_ERROR "Invalid request").
        #
        # TWO real, independent bugs, both confirmed by testing each one in
        # isolation against a live sign_data call:
        #
        # 1. `x-perp-broker-main-uid` was sent as `perp_evm_uid` (55409231),
        #    but it must be `perp_main_uid` (41047073) — two DIFFERENT
        #    fields in the login response this connector previously
        #    conflated. (`perp_evm_uid` is still correct for the withdraw
        #    body's own `"broker_uid"` field — that one matches the real
        #    capture.) Sending the wrong number here alone reproduces a
        #    clean 401 P_FOMOX_IN_UNAUTHORIZED, confirmed live.
        #
        # 2. The `Authorization: Bearer <general access_token>` header
        #    genuinely IS required — the real captured browser request
        #    doesn't show it, but a browser also sends session cookies HAR
        #    export can omit/redact; the general access_token most likely
        #    travels that way in a real session instead. Removing it here
        #    (as an earlier version of this fix did, reasoning from the HAR
        #    alone) reproduces the same 401 even with bug #1 fixed —
        #    confirmed live by re-adding it in isolation and getting 200.
        #    Both bugs had to be fixed together; neither alone was enough.
        #
        # Everything else below (`x-perp-app-version: "0.1.0"` not
        # "1.0.0", no `risk-verify-token` outside login, the two
        # previously-missing `x-perp-device-*` headers) still matches the
        # real capture and is unchanged from that pass.
        return {
            "Authorization": f"Bearer {self._general_token}",
            "x-perp-const-id": config.require_env("ADEN_DEVICE_FINGERPRINT"),
            "x-perp-authorization": self._token,
            "x-perp-broker-main-uid": str(self._broker_main_uid),
            "x-perp-device-type": "3",
            "x-perp-device-name": "Chrome on macOS 10.15",
            "x-perp-device-version": "150.0",
            "x-perp-app-version": "0.1.0",
            "x-perp-language": "en",
        }

    def _post(self, path: str, body: dict) -> dict:
        # `data=` + compact-JSON `Content-Type: text/plain;charset=UTF-8` —
        # NOT `json=` (which sends `application/json` and python's default
        # `", "`/`": "` separators). Both confirmed to differ from the real
        # capture 2026-09-07: real request bodies have NO whitespace at all
        # (`{"address":"0x...",...}`), and content-type is text/plain, same
        # pattern already known to matter for `_login`'s own POST.
        encoded = json.dumps(body, separators=(",", ":"))
        headers = {**self._headers(), "Content-Type": "text/plain;charset=UTF-8"}
        response = self._session.post(f"{_PERPS_BASE_URL}{path}", data=encoded, headers=headers, timeout=15)
        if not response.ok:
            raise RuntimeError(f"Aden POST {path} -> {response.status_code}: {response.text}")
        data = response.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"Aden POST {path} rejected: {data}")
        return data["data"]

    def _check_min_withdraw(self, chain_name: str, amount_usd: float) -> None:
        # An amount below this floor is ONE confirmed way to get
        # `withdraw/submit`'s generic 50001201 P_FOMOX_IN_INTERNAL_ERROR
        # "Invalid request" — but NOT the only way: a live $12 BSC withdraw
        # (above the $10.2 floor) 500'd identically 2026-09-07, so a
        # request clearing this check can still fail for a DIFFERENT,
        # still-unidentified reason (see the long "STILL UNRESOLVED" note in
        # the module docstring — casing, amount formatting, an explicit
        # withdraw_nonce field, and Origin/Referer headers were all tried
        # live and ruled out). Kept anyway: checking this live (not trusting
        # `connectors/dex_operational_params.json`, a user-editable
        # placeholder that can drift from the venue's actual floor) is cheap
        # and turns AT LEAST this one failure mode into a readable error
        # instead of the opaque 500 — necessary, evidently not sufficient.
        response = self._session.get(
            "https://brokerapi.gateperps.com/apiw/v2/perp-dex/withdraw-min-limit", headers=self._headers(), timeout=15
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Aden withdraw-min-limit -> {data}")
        floor = next((float(row["min_limit"]) for row in data["data"]["list"] if row["chain"] == chain_name.upper()), None)
        if floor is not None and amount_usd < floor:
            raise RuntimeError(
                f"Aden: ${amount_usd} is below the live minimum withdrawal of ${floor} on {chain_name.upper()} — "
                "refusing rather than let it 500 with an unreadable P_FOMOX_IN_INTERNAL_ERROR."
            )

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        from connectors.stable_tokens import get_stable_token_address

        chain_name = _CHAIN_NAME[chain]
        self._check_min_withdraw(chain_name, amount_usd)
        token_address = Web3.to_checksum_address(get_stable_token_address(chain, stable))
        body = {
            # Lowercase, not checksummed — matches the real captured request
            # exactly ("address":"0x477899d0e04e8b510ada7ecd1db22d1bdf3f1650",
            # all lowercase). EIP-712 signing itself doesn't care about
            # string case (addresses are encoded as 20 raw bytes either way),
            # but the submit body's own server-side validation apparently
            # does — confirmed live 2026-09-07: a checksummed (mixed-case)
            # body 500'd with P_FOMOX_IN_INTERNAL_ERROR "Invalid request".
            "address": self._address.lower(),
            "chain": chain_name,
            "to_address": Web3.to_checksum_address(to_address).lower(),
            "token_address": token_address.lower(),
            # Trimmed, not a fixed 6-decimal string — matches the real
            # capture exactly ("token_amount":"10.2", not "10.200000"),
            # confirmed live 2026-09-07 to matter (see address casing note).
            "token_amount": _trim_amount(amount_usd),
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
