from graph.structures.DEXes import Chain, Stable


class Bridge:
    """Capacité de faire transiter `stable` depuis/vers `chain` — miroir de
    BridgeNode, dont la construction (Graph._addSourcesBridges) consomme
    cette liste. Les routes (quelle chain vers quelle autre) restent gérées
    par Graph._linkBridges, pas ici."""

    def __init__(self, chain: Chain, stable: Stable):
        self.chain = chain
        self.stable = stable


def buildBridgeList() -> list[Bridge]:
    """Un bridge par (chain, stable) : pour l'instant toutes les chains, pour
    USDC et USDT (les deux seuls stables modélisés par l'enum Stable)."""
    return [Bridge(chain, stable) for chain in Chain for stable in Stable]
