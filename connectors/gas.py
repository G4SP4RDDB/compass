import time
from enum import Enum, auto

from graph.structures.bridges import BridgeProtocol
from graph.structures.DEXes import Chain

from .alchemy import AlchemyConnector
from .exceptions import ConnectorAPIError
from .solana_rpc import SolanaRPCConnector

CACHE_TTL_SECONDS = 30


class GasOperation(Enum):
    TRANSFER = auto()
    BRIDGE_SEND = auto()
    SWAP = auto()


# Valeurs de départ approximatives, pas mesurées (voir GasFeeService.get_bridge_gas_cost_usd,
# qui n'a pas d'équivalent "vrai depositForBurn" à simuler pour le bridge
# interne d'Aden — à affiner avec de vraies données de coût une fois
# disponibles).
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
    """Façade unifiant AlchemyConnector (EVM) et SolanaRPCConnector derrière
    une seule méthode, avec un cache par (chain, operation) pour éviter un
    appel API par edge du graphe."""

    def __init__(
        self,
        alchemy: AlchemyConnector | None = None,
        solana: SolanaRPCConnector | None = None,
    ) -> None:
        self._alchemy = alchemy or AlchemyConnector()
        # Partage l'AlchemyConnector (donc son cache de prix USD) avec Solana :
        # évite de dupliquer les appels Prices API pour le même symbole.
        self._solana = solana or SolanaRPCConnector(alchemy=self._alchemy)
        self._cache: dict[tuple[Chain, GasOperation], tuple[float, float]] = {}

    def _cached(self, cache: dict, key: tuple, fetch) -> float:
        cached = cache.get(key)
        if cached is not None:
            usd_amount, fetched_at = cached
            if time.monotonic() - fetched_at < CACHE_TTL_SECONDS:
                return usd_amount

        usd_amount = fetch()
        cache[key] = (usd_amount, time.monotonic())
        return usd_amount

    def get_gas_cost_usd(self, chain: Chain, operation: GasOperation) -> float:
        return self._cached(self._cache, (chain, operation), lambda: self._fetch_gas_cost_usd(chain, operation))

    def get_bridge_gas_cost_usd(self, source_chain: Chain, destination_chain: Chain, protocol: BridgeProtocol) -> float:
        """Coût de la tx d'initiation du bridge (payée par l'utilisateur sur
        source_chain). Un seul protocole existe aujourd'hui (ADEN_INTERNAL,
        voir graph.structures.bridges) et pas de simulation réelle
        équivalente au depositForBurn de CCTP pour lui : gas limit forfaitaire
        BRIDGE_SEND en attendant de vraies données de coût sur ce bridge."""
        del destination_chain, protocol  # pas encore de coût différencié par route/protocole
        return self.get_gas_cost_usd(source_chain, GasOperation.BRIDGE_SEND)

    def _fetch_gas_cost_usd(self, chain: Chain, operation: GasOperation) -> float:
        if chain == Chain.SOLANA:
            compute_units = SOLANA_COMPUTE_UNITS_BY_OPERATION[operation]
            gas_cost = self._solana.get_transaction_cost(compute_units=compute_units)
        else:
            gas_limit = EVM_GAS_LIMIT_BY_OPERATION[operation]
            gas_cost = self._alchemy.get_gas_cost(chain, gas_limit)

        if gas_cost.usd_amount is None:
            raise ConnectorAPIError(
                "GasFeeService",
                f"chain={chain.name}",
                "USD price unavailable for gas cost conversion",
            )
        return gas_cost.usd_amount
