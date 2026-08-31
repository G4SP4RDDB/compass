import statistics

import requests

from graph.structures.DEXes import Chain

from .alchemy import AlchemyConnector
from .chain_metadata import get_metadata
from .config import SOLANA_RPC_URL
from .exceptions import ConnectorAPIError
from .models import GasCost

# Protocol constant (lamports charged per signature), not worth a live RPC
# round-trip to "confirm" — see connectors/alchemy.py for the equivalent
# live gas price lookup on EVM chains, where the price genuinely varies.
LAMPORTS_PER_SIGNATURE = 5000
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_COMPUTE_UNITS = 200_000


class SolanaRPCConnector:
    def __init__(self, alchemy: AlchemyConnector | None = None) -> None:
        # Solana n'a pas de network RPC Alchemy dans ce projet (voir
        # ALCHEMY_NETWORK_SLUG_BY_CHAIN) : seule la Prices API (endpoint
        # global, indépendant de la network) est utilisée ici.
        self._alchemy = alchemy or AlchemyConnector()

    def _rpc(self, method: str, params: list | None = None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        response = requests.post(SOLANA_RPC_URL, json=payload, timeout=10)
        if not response.ok:
            raise ConnectorAPIError("SolanaRPCConnector", SOLANA_RPC_URL, response.text)

        data = response.json()
        if "error" in data:
            raise ConnectorAPIError("SolanaRPCConnector", SOLANA_RPC_URL, str(data["error"]))
        return data["result"]

    def get_base_fee_lamports(self) -> int:
        return LAMPORTS_PER_SIGNATURE

    def get_recent_prioritization_fee_micro_lamports(self) -> float:
        result = self._rpc("getRecentPrioritizationFees")
        fees = [entry["prioritizationFee"] for entry in result]
        if not fees:
            return 0.0
        return statistics.mean(fees)

    def get_transaction_cost(
        self, signatures: int = 1, compute_units: int = DEFAULT_COMPUTE_UNITS
    ) -> GasCost:
        base_lamports = signatures * self.get_base_fee_lamports()
        priority_micro_lamports_per_cu = self.get_recent_prioritization_fee_micro_lamports()
        priority_lamports = (priority_micro_lamports_per_cu * compute_units) / 1_000_000

        total_lamports = base_lamports + priority_lamports
        native_amount = total_lamports / LAMPORTS_PER_SOL

        metadata = get_metadata(Chain.SOLANA)
        try:
            usd_price = self._alchemy.get_usd_price(Chain.SOLANA)
            usd_amount = native_amount * usd_price
        except ConnectorAPIError:
            usd_amount = None

        return GasCost(
            chain=Chain.SOLANA,
            native_amount=native_amount,
            native_symbol=metadata.native_symbol,
            usd_amount=usd_amount,
        )

    def get_slot_time_ms(self, slot_lookback: int = 100) -> float:
        current_slot = self._rpc("getSlot")
        recent_slot = current_slot - slot_lookback
        older_slot = current_slot - (2 * slot_lookback)

        recent_time = self._rpc("getBlockTime", [recent_slot])
        older_time = self._rpc("getBlockTime", [older_slot])

        if recent_time is None or older_time is None:
            raise ConnectorAPIError(
                "SolanaRPCConnector", SOLANA_RPC_URL, "getBlockTime returned null for a queried slot"
            )

        seconds_elapsed = recent_time - older_time
        return (seconds_elapsed / slot_lookback) * 1000
