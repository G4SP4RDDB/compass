"""Délai de confirmation on-chain par chain : combine un temps de bloc mesuré
en live (RPC) avec un nombre de confirmations "sûres" par chain (statique,
voir CONFIRMATIONS_BY_CHAIN) pour estimer Time(e) sur toute edge qui reste sur
une seule et même chain (Deposit<->Deposit, Deposit<->Bridge, Bridge<->Swap).
Avant ce module, costing.computeDelay renvoyait 0.0 pour toutes ces edges
(voir les TODO qu'il remplace) : un virement même chain n'est pourtant jamais
instantané, il attend au moins une confirmation de bloc.

Contrairement au coût de gas (mesurable exactement par simulation RPC à
l'instant T, voir GasFeeService), le "bon" nombre de confirmations est une
question de tolérance au risque de reorg, pas une quantité mesurable en live :
CONFIRMATIONS_BY_CHAIN est un ordre de grandeur usuel (recommandations
Alchemy/Infura/docs de chaque chain, 2026-09-02), à ajuster si le projet a une
tolérance au risque différente. Le temps de bloc lui-même, en revanche, EST
mesuré en live (fetch_all_block_delays) plutôt que hardcodé, puis persisté
dans un fichier (voir save_block_delays / load_block_delays) — même pattern
que connectors/dex_operational_params.py, pour ne pas refaire un aller-retour
RPC par edge à chaque calcul du graphe.
"""

import json
from pathlib import Path

from graph.structures.DEXes import Chain

from .alchemy import AlchemyConnector
from .solana_rpc import SolanaRPCConnector

# - L1 PoW/PoS classique (Ethereum) : 12 blocks, recommandation standard
#   Alchemy/Infura/Etherscan depuis The Merge.
# - L2 optimistic (Arbitrum/Optimism/Base) : 1 block suffit en pratique, le
#   séquenceur unique ne reorg quasiment jamais son propre ordering ; la
#   finalité L1 complète (~15-20 min) est un phénomène différent, déjà
#   modélisé séparément pour CCTP V1 (voir connectors/cctp.py).
# - Avalanche (consensus Snowman) : finalité sub-seconde, 1 block suffit.
# - Polygon PoS : historiquement sujet à des reorgs profonds, 128 blocks
#   reste la recommandation standard prudente (checkpoints L1 espacés).
# - BSC (BNB Smart Chain, PoSA) : 15 blocks, recommandation standard des
#   exchanges/fournisseurs de nodes.
# - Solana : 32 slots ("finalized", supermajority root) — "confirmed"
#   (~1-2 slots) est plus rapide mais moins sûr contre un fork.
CONFIRMATIONS_BY_CHAIN: dict[Chain, int] = {
    Chain.ETHEREUM: 12,
    Chain.ARBITRUM: 1,
    Chain.OPTIMISM: 1,
    Chain.BASE: 1,
    Chain.AVALANCHE: 1,
    Chain.POLYGON: 128,
    Chain.BSC: 15,
    Chain.SOLANA: 32,
}

BLOCK_LOOKBACK = 100

DEFAULT_BLOCK_DELAYS_PATH = Path("connectors/chain_block_delays.json")

# Repli si une chain manque du fichier persisté (pas encore fetchée, ou API
# indisponible) : même logique que costing.GENERIC_BRIDGE_DELAY_SECONDS, un
# ordre de grandeur prudent plutôt qu'un 0.0 qui biaiserait le solveur en
# faveur d'une chain qu'on n'a pas mesurée.
DEFAULT_BLOCK_DELAY_SECONDS = 5 * 60


def fetch_block_delay_seconds(chain: Chain, alchemy: AlchemyConnector, solana: SolanaRPCConnector) -> float:
    confirmations = CONFIRMATIONS_BY_CHAIN[chain]
    if chain == Chain.SOLANA:
        block_time_seconds = solana.get_slot_time_ms() / 1000
    else:
        block_time_seconds = alchemy.get_block_time_seconds(chain, block_lookback=BLOCK_LOOKBACK)
    return block_time_seconds * confirmations


def fetch_all_block_delays(
    alchemy: AlchemyConnector | None = None, solana: SolanaRPCConnector | None = None
) -> dict[Chain, float]:
    alchemy = alchemy or AlchemyConnector()
    solana = solana or SolanaRPCConnector(alchemy=alchemy)
    return {chain: fetch_block_delay_seconds(chain, alchemy, solana) for chain in CONFIRMATIONS_BY_CHAIN}


def save_block_delays(delays: dict[Chain, float], path: Path | str = DEFAULT_BLOCK_DELAYS_PATH) -> None:
    filePath = Path(path)
    filePath.write_text(
        json.dumps({chain.name: seconds for chain, seconds in delays.items()}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_block_delays(path: Path | str = DEFAULT_BLOCK_DELAYS_PATH) -> dict[Chain, float]:
    filePath = Path(path)
    if not filePath.exists():
        return {}
    raw = json.loads(filePath.read_text(encoding="utf-8"))
    return {Chain[name]: seconds for name, seconds in raw.items()}


_cached_delays: dict[Chain, float] | None = None


def get_block_delay_seconds(chain: Chain) -> float:
    """Lit le fichier persisté (voir save_block_delays), caché en mémoire pour
    le process (une seule lecture disque, même graphe reconstruit plusieurs
    fois). Repli sur DEFAULT_BLOCK_DELAY_SECONDS pour une chain absente du
    fichier plutôt que de lever — costing.computeDelay ne doit jamais planter
    faute de mesure, seulement rester prudent."""
    global _cached_delays
    if _cached_delays is None:
        _cached_delays = load_block_delays()
    return _cached_delays.get(chain, DEFAULT_BLOCK_DELAY_SECONDS)


if __name__ == "__main__":
    delays = fetch_all_block_delays()
    save_block_delays(delays)
    for chain, seconds in sorted(delays.items(), key=lambda item: item[0].name):
        print(f"{chain.name}: {seconds:.1f}s ({CONFIRMATIONS_BY_CHAIN[chain]} confirmations)")
    print(f"\nÉcrit dans {DEFAULT_BLOCK_DELAYS_PATH}")
