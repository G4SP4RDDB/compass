from dataclasses import dataclass

from graph.structures.DEXes import Chain


@dataclass(frozen=True)
class ChainMetadata:
    native_symbol: str
    coingecko_id: str
    etherscan_chainid: int | None = None
    cowswap_slug: str | None = None


CHAIN_METADATA: dict[Chain, ChainMetadata] = {
    Chain.ETHEREUM: ChainMetadata(
        native_symbol="ETH",
        coingecko_id="ethereum",
        etherscan_chainid=1,
        cowswap_slug="mainnet",
    ),
    Chain.ARBITRUM: ChainMetadata(
        native_symbol="ETH",
        coingecko_id="ethereum",
        etherscan_chainid=42161,
        cowswap_slug="arbitrum_one",
    ),
    Chain.BASE: ChainMetadata(
        native_symbol="ETH",
        coingecko_id="ethereum",
        etherscan_chainid=8453,
        cowswap_slug="base",
    ),
    Chain.OPTIMISM: ChainMetadata(
        native_symbol="ETH",
        coingecko_id="ethereum",
        etherscan_chainid=10,
        cowswap_slug=None,
    ),
    Chain.BSC: ChainMetadata(
        native_symbol="BNB",
        coingecko_id="binancecoin",
        etherscan_chainid=56,
        cowswap_slug="bnb",
    ),
    Chain.POLYGON: ChainMetadata(
        native_symbol="MATIC",
        coingecko_id="matic-network",
        etherscan_chainid=137,
        cowswap_slug="polygon",
    ),
    Chain.SOLANA: ChainMetadata(
        native_symbol="SOL",
        coingecko_id="solana",
        etherscan_chainid=None,
        cowswap_slug=None,
    ),
}


def get_metadata(chain: Chain) -> ChainMetadata:
    return CHAIN_METADATA[chain]
