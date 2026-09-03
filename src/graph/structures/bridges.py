from enum import Enum, auto

from connectors import cctp
from connectors.stable_tokens import is_stable_supported
from graph.structures.DEXes import Chain, Stable


class BridgeProtocol(Enum):
    # Coût forfaitaire (voir GasFeeService.get_gas_cost_usd) : fallback
    # toujours disponible, jamais retiré même quand un protocole spécifique
    # existe aussi, pour ne jamais casser la connectivité d'une route.
    GENERIC = auto()
    CCTP_V1 = auto()
    CCTP_V2 = auto()


class Bridge:
    """Capacité de faire transiter `stable` depuis/vers `chain` — miroir de
    BridgeNode, dont la construction (Graph._addSourcesBridges) consomme
    cette liste. Les routes (quelle chain vers quelle autre) et les
    protocoles qui les desservent restent gérés par Graph._linkBridges via
    availableBridgeProtocols, pas ici."""

    def __init__(self, chain: Chain, stable: Stable):
        self.chain = chain
        self.stable = stable


def buildBridgeList() -> list[Bridge]:
    """Un bridge par (chain, stable) : pour l'instant toutes les chains, pour
    USDC et USDT (les deux seuls stables modélisés par l'enum Stable)."""
    return [Bridge(chain, stable) for chain in Chain for stable in Stable]


def availableBridgeProtocols(sourceChain: Chain, destinationChain: Chain, stable: Stable) -> list[BridgeProtocol]:
    """Protocoles de bridge réels disponibles pour cette route dirigée
    (utilisé par Graph._linkBridges pour créer une edge par protocole entre
    deux BridgeNode). GENERIC est toujours inclus ; les protocoles CCTP
    s'ajoutent en plus quand la route les supporte (USDC, chains couvertes
    par cctp.py ET par stable_tokens.py — voir GasFeeService pour le calcul
    de coût réel de chaque protocole)."""
    protocols = [BridgeProtocol.GENERIC]
    if stable != Stable.USDC or not is_stable_supported(sourceChain, stable):
        return protocols

    if cctp.is_v1_supported(sourceChain) and cctp.is_v1_supported(destinationChain):
        protocols.append(BridgeProtocol.CCTP_V1)
    if cctp.is_v2_supported(sourceChain) and cctp.is_v2_supported(destinationChain):
        protocols.append(BridgeProtocol.CCTP_V2)
    return protocols
