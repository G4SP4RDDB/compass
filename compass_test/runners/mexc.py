"""MEXC — a plain CEX, standard Binance-style REST + HMAC-SHA256 auth.
Verified against MEXC's public API docs (mexcdevelop.github.io/apidocs/spot_v3_en,
checked 2026-09-04):

  POST /api/v3/capital/withdraw           withdraw
  GET  /api/v3/capital/config/getall      per-coin network names (used
                                           instead of hardcoding MEXC's exact
                                           network-string spelling, which
                                           this doc pass did not pin down
                                           precisely — resolved live instead)
  GET  /api/v3/account                    balances, for poll_balance_usd

Header: X-MEXC-APIKEY. Signature: HMAC-SHA256 over the urlencoded query
string (or body), secret as key, hex digest lowercase.

Deposit address is NOT fetched live (GET /api/v3/capital/deposit/address
returned `[]` for this account/USDT/BSC even with the address confirmed to
exist, 2026-09-05) — see build_deposit_tx, which reads it from
COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_MEXC_<CHAIN> in .env instead, set one
(dex, chain) pair at a time as each is confirmed on MEXC's own site.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import requests
from web3 import Web3

from graph.structures.DEXes import Chain, Stable

from .. import chain_ops, config
from .base import DexConnector, WithdrawResult

_BASE_URL = "https://api.mexc.com"

# dex_registry.py only wires MEXC with USDT today (BSC + ARBITRUM) — see
# src/graph/structures/dex_registry.py._DEX_SPECS.
_COIN_BY_STABLE = {Stable.USDT: "USDT", Stable.USDC: "USDC"}

# Substring MEXC uses in its network name for each chain (checked against a
# live /api/v3/capital/config/getall response, not guessed) — kept as a
# fallback matcher rather than the exact literal string, since exchanges
# occasionally rename these.
_NETWORK_HINT_BY_CHAIN = {Chain.BSC: ("BEP20", "BSC"), Chain.ARBITRUM: ("ARBITRUM",)}


class MexcConnector(DexConnector):
    name = "MEXC"
    supported_chains = frozenset({Chain.BSC, Chain.ARBITRUM})
    supported_stables = frozenset({Stable.USDT})

    def __init__(self):
        self._api_key = config.require_env("MEXC_API_KEY")
        self._api_secret = config.require_env("MEXC_API_SECRET")
        self._session = requests.Session()
        self._network_cache: dict[str, list[dict]] = {}

    def _signed_request(self, method: str, path: str, params: dict) -> dict | list:
        signed = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(signed)
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        signed["signature"] = signature
        headers = {"X-MEXC-APIKEY": self._api_key}
        response = self._session.request(method, _BASE_URL + path, params=signed, headers=headers, timeout=15)
        if not response.ok:
            raise RuntimeError(f"MEXC {method} {path} -> {response.status_code}: {response.text}")
        return response.json()

    def _resolve_network(self, coin: str, chain: Chain) -> str:
        if coin not in self._network_cache:
            data = self._signed_request("GET", "/api/v3/capital/config/getall", {})
            entry = next((c for c in data if c.get("coin") == coin), None)
            if entry is None:
                raise RuntimeError(f"MEXC: coin {coin!r} not found in capital/config/getall")
            self._network_cache[coin] = entry.get("networkList", [])

        hints = _NETWORK_HINT_BY_CHAIN[chain]
        for net in self._network_cache[coin]:
            name = net.get("network", "") + " " + net.get("netWork", "")
            if any(hint in name.upper() for hint in hints):
                # `network` is the human display name ("Arbitrum One(ARB)");
                # `netWork` is the short CODE ("ARB") that deposit/address
                # and withdraw actually expect — confirmed live 2026-09-04:
                # passing the display name here got a 700002 "Signature for
                # this request is not valid" from MEXC (a generic-sounding
                # error that was actually about the network VALUE, not the
                # HMAC signature itself — verified by fixing this and seeing
                # the exact same signing code start working).
                return net.get("netWork") or net.get("network")
        raise RuntimeError(
            f"MEXC: no network matching {chain.name} found for {coin} in capital/config/getall "
            f"(got {self._network_cache[coin]!r}) — verify network naming before a live run"
        )

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        coin = _COIN_BY_STABLE[stable]
        network = self._resolve_network(coin, chain)
        requestedAt = time.time()
        data = self._signed_request(
            "POST",
            "/api/v3/capital/withdraw",
            # `netWork` (capital W), NOT `network` — confirmed live
            # 2026-09-06: MEXC's withdraw endpoint answers 700004 "Mandatory
            # parameter 'netWork' was not sent" for the more natural-looking
            # `network` key. Same field-naming quirk as _resolve_network's
            # `network`(display name)/`netWork`(short code) VALUE mixup, but
            # this is about the request's parameter KEY, a separate bug.
            {"coin": coin, "netWork": network, "address": to_address, "amount": f"{amount_usd:.6f}"},
        )
        return WithdrawResult(
            externalId=str(data["id"]),
            requestedAt=requestedAt,
            acceptedAt=time.time(),
            amountRequestedUsd=amount_usd,
            quotedFeeUsd=None,
        )

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        # GET /api/v3/capital/deposit/address deliberately NOT used here
        # anymore: confirmed live 2026-09-05 that it returns `[]` for this
        # account/USDT/BSC even after the address existed and was pasted to
        # us directly — MEXC's dashboard apparently doesn't always back-fill
        # this endpoint the moment a deposit address is generated in the UI
        # (or it's tied to a different sub-account than this API key). Rather
        # than keep depending on an endpoint that's demonstrably unreliable
        # here, the address is hardcoded per (DEX, chain) instead — set one
        # by one as each is confirmed, see config.expected_deposit_address
        # and COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_* in .env.
        coin = _COIN_BY_STABLE[stable]
        deposit_address = config.expected_deposit_address(self.name, chain.name)
        if deposit_address is None:
            raise RuntimeError(
                f"MEXC: no deposit address configured for {coin}/{chain.name} — set "
                f"COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_{self.name.upper()}_{chain.name} in .env "
                "(copy it from MEXC's own Deposit screen, not the API — see build_deposit_tx)."
            )
        return chain_ops.build_erc20_transfer_tx(w3, chain, stable, from_address, deposit_address, amount_usd)

    def poll_balance_usd(self, stable: Stable) -> float:
        coin = _COIN_BY_STABLE[stable]
        data = self._signed_request("GET", "/api/v3/account", {})
        entry = next((b for b in data.get("balances", []) if b.get("asset") == coin), None)
        return float(entry["free"]) if entry else 0.0
