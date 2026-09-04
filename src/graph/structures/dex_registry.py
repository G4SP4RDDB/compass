from graph.structures.DEXes import DEX, Chain, Stable

# Un seul point de vérité pour "quel DEX supporte quelles chains / quels
# stables de dépôt". On ne garde que USDC/USDT (les seuls stables modélisés
# par l'enum Stable) même quand le DEX en accepte d'autres (ex: Aster USDF/
# asUSDF, Lighter USDG) : ces autres stables sont ignorés, pas convertis.
#
# Une entrée manquante signalée explicitement plutôt que devinée :
# - Variational (Omni) : stablecoin (USDC) connu, mais aucune chain fournie
#   -> pas d'entrée tant que les chains ne sont pas précisées.
#
# Chains volontairement restreintes à {BSC, ARBITRUM} : toutes les DEX ont
# désormais un endpoint sur Arbitrum, et la seule route de bridging encore
# nécessaire (BSC<->Arbitrum) est servie par le bridge interne d'Aden (voir
# graph.structures.bridges) — plus besoin de modéliser Ethereum/Solana/Base/
# Polygon/Optimism/Avalanche pour ces DEX tant que ce n'est pas le cas.
_DEX_SPECS: dict[str, tuple[list[Chain], list[Stable]]] = {
    "Aden": ([Chain.BSC, Chain.ARBITRUM], [Stable.USDT]),
    "Aster": ([Chain.BSC], [Stable.USDT]),
    "dYdX": ([Chain.ARBITRUM], [Stable.USDC]),
    "Extended": ([Chain.ARBITRUM], [Stable.USDC]),
    "Gate (Perp DEX)": ([Chain.BSC, Chain.ARBITRUM], [Stable.USDT]),
    "Hyperliquid": ([Chain.ARBITRUM], [Stable.USDC]),
    "Lighter": ([Chain.ARBITRUM], [Stable.USDC]),
    # CEX avec "dizaines de réseaux, variable par token" (non énuméré) : on
    # sous-estime volontairement à toutes les chains déjà modélisées ailleurs
    # plutôt que d'inventer une liste précise -> jamais une chain qu'on ne
    # supporte pas nous-mêmes, donc pas de risque de routage vers un rail
    # inexistant, mais à affiner si MEXC ne supporte pas l'une d'entre elles
    # pour l'USDT.
    "MEXC": ([Chain.BSC, Chain.ARBITRUM], [Stable.USDT]),
    "Ondo Perps": ([Chain.ARBITRUM], [Stable.USDC]),
}

# DEX qui lient le retrait à la chain de dépôt (voir DEX.requiresSameChainWithdraw) :
# un solde crédité via une chain donnée ne peut être retiré que vers cette
# MÊME chain, jamais fongible entre chains comme c'est le cas par défaut
# ailleurs. Aster est le seul confirmé pour l'instant.
_SAME_CHAIN_WITHDRAW_DEXES = {"Aster"}

# DEX qui fonctionnent comme un CEX classique (voir DEX.requiresDepositAddress) :
# on envoie à une adresse de dépôt dédiée, la plateforme crédite séparément
# après ses propres délais -- deux actions distinctes, pas un seul appel de
# contrat. Vide aujourd'hui : MEXC crédite automatiquement dès réception sur
# son adresse de dépôt, donc pas de délai/frais de reconnaissance distinct du
# virement lui-même -- même modèle direct (Wallet -> SourceNode, un seul hop)
# que les perp DEX on-chain où déposer EST l'appel de contrat qui crédite
# (voir Graph._linkWithdrawalsAndDeposits). Prêt à reprendre du service pour
# un futur DEX dont le dépôt serait réellement un processus séparé en deux
# étapes.
_DEPOSIT_ADDRESS_DEXES: set[str] = set()


def buildDexRegistry() -> dict[str, DEX]:
    """Construit une nouvelle instance de DEX par entrée à chaque appel (DEX
    porte un état mutable : inbalance/target/margin), pour ne jamais partager
    d'objets entre deux graphes construits séparément."""
    registry = {
        name: DEX(supportedChains=chains, supportedStables=stables, name=name)
        for name, (chains, stables) in _DEX_SPECS.items()
    }
    for name in _SAME_CHAIN_WITHDRAW_DEXES:
        registry[name].requiresSameChainWithdraw = True
    for name in _DEPOSIT_ADDRESS_DEXES:
        registry[name].requiresDepositAddress = True
    return registry
