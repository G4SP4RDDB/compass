from enum import Enum, auto

from graph.structures.DEXes import Chain, Stable

# Aden opère son propre bridge interne entre BSC et Arbitrum, les deux seules
# chains encore supportées par le registre DEX (voir graph.structures.dex_registry).
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

# CCTP (Circle's Cross-Chain Transfer Protocol, burn-and-mint) : seule route
# encore utile pour le withdraw pipeline vers le vault Solana -- ARBITRUM<->SOLANA,
# USDC uniquement (CCTP ne transporte QUE de l'USDC natif, jamais l'USDT).
# BSC n'a jamais eu de domaine CCTP (Circle n'a jamais couvert BNB Chain) :
# un solde BSC doit d'abord passer par Aden vers Arbitrum (voir _linkBridges,
# qui compose librement Bridge/Swap edges -- le solveur choisit l'ordre le
# moins cher, aucun cas particulier à coder ici). Modélisé en V1 (Standard
# Transfer) seulement : WalletDeficitNode n'a pas d'urgence (σ = +inf, voir
# graph.urgency.computeSinkUrgencySigma), donc le solveur préfère de toute
# façon V1 (frais protocole nul) à un V2 Fast Transfer payant -- V2 pourrait
# s'ajouter plus tard comme un second protocole parallèle sur cette même
# route, exactement comme ADEN_INTERNAL et CCTP coexistent déjà ici.
_CCTP_CHAINS = frozenset({Chain.ARBITRUM, Chain.SOLANA})


class BridgeProtocol(Enum):
    ADEN_INTERNAL = auto()
    CCTP = auto()


# Which real DEX (graph.structures.DEXes.DEX.name, matching dex_registry.py)
# actually implements a protocol's bridge, if any — ADEN_INTERNAL is
# literally "deposit into Aden on one chain, withdraw from Aden on the
# other" (see Graph._linkBridges, costing.computeBridgeDelay), so its
# delay/fee should be able to read Aden's OWN configured per-chain
# withdraw/deposit figures rather than a hand-picked constant. CCTP has no
# entry: it isn't any DEX's own ledger, it's Circle's protocol directly
# (see connectors/cctp.py) — its delay/fee stay the flat estimates in
# costing.py/this module.
BRIDGE_PROTOCOL_DEX_NAME: dict[BridgeProtocol, str] = {
    BridgeProtocol.ADEN_INTERNAL: "Aden",
}


def adenBridgeFeeUsd(sourceChain: Chain, destinationChain: Chain) -> float:
    """Frais forfaitaire du bridge interne d'Aden pour cette route dirigée
    (voir _ADEN_BRIDGE_FEE_USD_BY_DIRECTION) — appelable seulement pour une
    route où availableBridgeProtocols a effectivement renvoyé ADEN_INTERNAL,
    cette fonction fait confiance à l'appelant et ne revalide pas."""
    return _ADEN_BRIDGE_FEE_USD_BY_DIRECTION[(sourceChain, destinationChain)]


def bridgeFeeUsd(protocol: "BridgeProtocol", sourceChain: Chain, destinationChain: Chain) -> float:
    """Dispatcher par protocole, appelé par costing.computeCost -- le point
    unique où un nouveau protocole de bridge doit brancher son propre frais,
    plutôt que costing.py n'importe une fonction spécifique à Aden."""
    if protocol == BridgeProtocol.ADEN_INTERNAL:
        return adenBridgeFeeUsd(sourceChain, destinationChain)
    if protocol == BridgeProtocol.CCTP:
        # V1 (Standard Transfer) : aucun frais de protocole, seulement le gas
        # de la tx de burn (voir GasFeeService.get_bridge_gas_cost_usd) --
        # contrairement à Aden, Circle ne facture rien pour ce chemin.
        return 0.0
    raise ValueError(f"no fee function for {protocol}")


def availableBridgeProtocols(sourceChain: Chain, destinationChain: Chain, stable: Stable) -> list[BridgeProtocol]:
    """Protocoles de bridge réels disponibles pour cette route dirigée
    (utilisé par Graph._linkBridges pour créer une edge directe par
    protocole entre les deux WalletNode de `stable` sur ces deux chains) :
    une edge PAR PROTOCOLE retourné, le solveur arbitrant entre elles par
    coût/délai comme n'importe quelle paire d'edges parallèles. ADEN_INTERNAL
    couvre BSC<->ARBITRUM pour l'USDT uniquement (le ledger interne d'Aden
    ne gère pas l'USDC — voir compass_test/runners/aden.py::AdenConnector.
    supported_stables, la même restriction que plan_loader.py::
    _bridge_hop_supported et web_view.py::_testableHopInfo appliquaient déjà
    côté exécution ; avant ce garde-fou, le solveur pouvait choisir une
    edge Bridge/USDC via Aden qui n'existe simplement pas dans la réalité —
    un déséquilibre USDC entre BSC et Arbitrum doit maintenant composer
    Swap USDC->USDT (CoW Swap) + ce bridge + Swap USDT->USDC si besoin,
    exactement comme le docstring de _CCTP_CHAINS plus haut décrit déjà pour
    BSC->SOLANA via Arbitrum) ; CCTP couvre ARBITRUM<->SOLANA pour l'USDC
    uniquement (voir _CCTP_CHAINS). Toute autre combinaison -> liste vide ->
    pas d'edge de bridge du tout entre ces deux chains pour cette stable."""
    protocols: list[BridgeProtocol] = []
    if (
        stable == Stable.USDT
        and sourceChain in _ADEN_BRIDGE_CHAINS
        and destinationChain in _ADEN_BRIDGE_CHAINS
        and sourceChain != destinationChain
    ):
        protocols.append(BridgeProtocol.ADEN_INTERNAL)
    if (
        stable == Stable.USDC
        and sourceChain in _CCTP_CHAINS
        and destinationChain in _CCTP_CHAINS
        and sourceChain != destinationChain
    ):
        protocols.append(BridgeProtocol.CCTP)
    return protocols
