from dataclasses import dataclass

from graph.structures.DEXes import Chain


@dataclass(frozen=True)
class ChainMetadata:
    native_symbol: str
    chain_id: int | None = None
    cowswap_slug: str | None = None


CHAIN_METADATA: dict[Chain, ChainMetadata] = {
    Chain.ETHEREUM: ChainMetadata(
        native_symbol="ETH",
        chain_id=1,
        cowswap_slug="mainnet",
    ),
    Chain.ARBITRUM: ChainMetadata(
        native_symbol="ETH",
        chain_id=42161,
        cowswap_slug="arbitrum_one",
    ),
    Chain.BASE: ChainMetadata(
        native_symbol="ETH",
        chain_id=8453,
        cowswap_slug="base",
    ),
    Chain.OPTIMISM: ChainMetadata(
        native_symbol="ETH",
        chain_id=10,
        cowswap_slug=None,
    ),
    Chain.BSC: ChainMetadata(
        native_symbol="BNB",
        chain_id=56,
        cowswap_slug="bnb",
    ),
    Chain.POLYGON: ChainMetadata(
        native_symbol="MATIC",
        chain_id=137,
        cowswap_slug="polygon",
    ),
    Chain.SOLANA: ChainMetadata(
        native_symbol="SOL",
        chain_id=None,
        cowswap_slug=None,
    ),
    Chain.AVALANCHE: ChainMetadata(
        native_symbol="AVAX",
        chain_id=43114,
        cowswap_slug=None,
    ),
}


def get_metadata(chain: Chain) -> ChainMetadata:
    return CHAIN_METADATA[chain]
