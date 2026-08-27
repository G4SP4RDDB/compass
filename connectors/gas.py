import time
from enum import Enum, auto

from graph.structures.DEXes import Chain

from .etherscan import EtherscanConnector
from .exceptions import ConnectorAPIError
from .solana_rpc import SolanaRPCConnector

CACHE_TTL_SECONDS = 30


class GasOperation(Enum):
    TRANSFER = auto()
    BRIDGE_SEND = auto()
    SWAP = auto()


# Valeurs de départ approximatives (pas de données réelles par protocole de bridge
# pour l'instant) — à affiner une fois les connecteurs de bridge branchés.
EVM_GAS_LIMIT_BY_OPERATION: dict[GasOperation, int] = {
    GasOperation.TRANSFER: 65_000,
    GasOperation.BRIDGE_SEND: 150_000,
    GasOperation.SWAP: 180_000,
}

SOLANA_COMPUTE_UNITS_BY_OPERATION: dict[GasOperation, int] = {
    GasOperation.TRANSFER: 200_000,
    GasOperation.BRIDGE_SEND: 300_000,
    GasOperation.SWAP: 250_000,
}


class GasFeeService:
    """Façade unifiant EtherscanConnector (EVM) et SolanaRPCConnector derrière
    une seule méthode, avec un cache par (chain, operation) pour éviter un
    appel API par edge du graphe."""

    def __init__(
        self,
        etherscan: EtherscanConnector | None = None,
        solana: SolanaRPCConnector | None = None,
    ) -> None:
        self._etherscan = etherscan or EtherscanConnector()
        self._solana = solana or SolanaRPCConnector()
        self._cache: dict[tuple[Chain, GasOperation], tuple[float, float]] = {}

    def get_gas_cost_usd(self, chain: Chain, operation: GasOperation) -> float:
        cached = self._cache.get((chain, operation))
        if cached is not None:
            usd_amount, fetched_at = cached
            if time.monotonic() - fetched_at < CACHE_TTL_SECONDS:
                return usd_amount

        usd_amount = self._fetch_gas_cost_usd(chain, operation)
        self._cache[(chain, operation)] = (usd_amount, time.monotonic())
        return usd_amount

    def _fetch_gas_cost_usd(self, chain: Chain, operation: GasOperation) -> float:
        if chain == Chain.SOLANA:
            compute_units = SOLANA_COMPUTE_UNITS_BY_OPERATION[operation]
            gas_cost = self._solana.get_transaction_cost(compute_units=compute_units)
        else:
            gas_limit = EVM_GAS_LIMIT_BY_OPERATION[operation]
            gas_cost = self._etherscan.get_gas_cost(chain, gas_limit)

        if gas_cost.usd_amount is None:
            raise ConnectorAPIError(
                "GasFeeService",
                f"chain={chain.name}",
                "USD price unavailable for gas cost conversion",
            )
        return gas_cost.usd_amount
