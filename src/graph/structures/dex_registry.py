from graph.structures.DEXes import DEX, Chain, Stable

# Un seul point de vérité pour "quel DEX supporte quelles chains / quels
# stables de dépôt". On ne garde que USDC/USDT (les seuls stables modélisés
# par l'enum Stable) même quand le DEX en accepte d'autres (ex: Aster USDF/
# asUSDF, Lighter USDG) : ces autres stables sont ignorés, pas convertis.
#
# Une entrée manquante signalée explicitement plutôt que devinée :
# - Variational (Omni) : stablecoin (USDC) connu, mais aucune chain fournie
#   -> pas d'entrée tant que les chains ne sont pas précisées.
_DEX_SPECS: dict[str, tuple[list[Chain], list[Stable]]] = {
    "Aden": ([Chain.ETHEREUM, Chain.BSC, Chain.SOLANA], [Stable.USDT]),
    "Aster": (
        [Chain.BSC, Chain.ETHEREUM, Chain.ARBITRUM, Chain.SOLANA],
        [Stable.USDT, Stable.USDC],
    ),
    "dYdX": (
        [Chain.ETHEREUM, Chain.ARBITRUM, Chain.OPTIMISM, Chain.BASE, Chain.POLYGON, Chain.AVALANCHE],
        [Stable.USDC],
    ),
    "Extended": (
        [Chain.ARBITRUM, Chain.ETHEREUM, Chain.BASE, Chain.BSC, Chain.AVALANCHE, Chain.POLYGON],
        [Stable.USDC],
    ),
    "Gate (Perp DEX)": ([Chain.SOLANA, Chain.ETHEREUM, Chain.BSC, Chain.BASE], [Stable.USDT]),
    "Hyperliquid": ([Chain.ARBITRUM], [Stable.USDC]),
    "Lighter": ([Chain.ARBITRUM, Chain.BASE, Chain.AVALANCHE], [Stable.USDC]),
    # CEX avec "dizaines de réseaux, variable par token" (non énuméré) : on
    # sous-estime volontairement à toutes les chains déjà modélisées ailleurs
    # plutôt que d'inventer une liste précise -> jamais une chain qu'on ne
    # supporte pas nous-mêmes, donc pas de risque de routage vers un rail
    # inexistant, mais à affiner si MEXC ne supporte pas l'une d'entre elles
    # pour USDT/USDC.
    "MEXC": (list(Chain), [Stable.USDT, Stable.USDC]),
    "Ondo Perps": ([Chain.ETHEREUM], [Stable.USDC]),
}


def buildDexRegistry() -> dict[str, DEX]:
    """Construit une nouvelle instance de DEX par entrée à chaque appel (DEX
    porte un état mutable : inbalance/target/margin), pour ne jamais partager
    d'objets entre deux graphes construits séparément."""
    return {
        name: DEX(supportedChains=chains, supportedStables=stables, name=name)
        for name, (chains, stables) in _DEX_SPECS.items()
    }
