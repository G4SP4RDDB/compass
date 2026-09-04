from enum import Enum, auto

from graph.structures.DEXes import Chain, Stable

# Seule route bridgée du système (voir Graph.BRIDGES_ENABLED) : Aden opère son
# propre bridge interne entre BSC et Arbitrum, les deux seules chains encore
# supportées par le registre (voir graph.structures.dex_registry). Pas de
# CCTP/GENERIC : plus besoin de bridging multi-chain généraliste maintenant
# que chaque DEX ne vit plus que sur BSC et/ou Arbitrum.
_ADEN_BRIDGE_CHAINS = frozenset({Chain.BSC, Chain.ARBITRUM})

# Frais forfaitaire facturé par Aden pour son bridge interne, PAR SENS (pas
# symétrique : moins cher Arbitrum->BSC que BSC->Arbitrum) — distinct du gas
# de la tx elle-même (voir GasFeeService.get_bridge_gas_cost_usd), comme
# withdrawFeeUsd/depositFeeUsd sont distincts du gas ailleurs dans le modèle.
# Dénommé en USDT par Aden, traité comme USD directement (peg 1:1, même
# convention que les autres frais forfaitaires du modèle).
_ADEN_BRIDGE_FEE_USD_BY_DIRECTION: dict[tuple[Chain, Chain], float] = {
    (Chain.BSC, Chain.ARBITRUM): 0.5,
    (Chain.ARBITRUM, Chain.BSC): 0.2,
}


class BridgeProtocol(Enum):
    ADEN_INTERNAL = auto()


def adenBridgeFeeUsd(sourceChain: Chain, destinationChain: Chain) -> float:
    """Frais forfaitaire du bridge interne d'Aden pour cette route dirigée
    (voir _ADEN_BRIDGE_FEE_USD_BY_DIRECTION) — appelable seulement pour une
    route où availableBridgeProtocols a effectivement renvoyé ADEN_INTERNAL,
    cette fonction fait confiance à l'appelant et ne revalide pas."""
    return _ADEN_BRIDGE_FEE_USD_BY_DIRECTION[(sourceChain, destinationChain)]


def availableBridgeProtocols(sourceChain: Chain, destinationChain: Chain, stable: Stable) -> list[BridgeProtocol]:
    """Protocoles de bridge réels disponibles pour cette route dirigée
    (utilisé par Graph._linkBridges pour créer une edge directe par
    protocole entre les deux WalletNode de `stable` sur ces deux chains).
    ADEN_INTERNAL est la SEULE route bridgée modélisée : BSC<->ARBITRUM,
    n'importe quelle stable (le bridge Aden n'est pas restreint à l'USDT
    d'Aden lui-même — c'est une route ouverte à tout le graphe, comme
    l'était GENERIC avant lui). Toute autre paire de chains -> aucun
    protocole -> pas d'edge de bridge du tout entre elles."""
    if sourceChain in _ADEN_BRIDGE_CHAINS and destinationChain in _ADEN_BRIDGE_CHAINS and sourceChain != destinationChain:
        return [BridgeProtocol.ADEN_INTERNAL]
    return []
