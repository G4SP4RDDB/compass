from graph.structures.DEXes import Chain, Stable

from .exceptions import UnsupportedChainError

# 6 décimales pour USDC et USDT sur toutes les chains listées ici.
STABLE_DECIMALS = 6

# Adresses vérifiées (CoinGecko contract data) ; None = pas encore vérifié,
# ne pas deviner une adresse de contrat, ça manipule de l'argent réel.
STABLE_TOKEN_ADDRESSES: dict[tuple[Chain, Stable], str] = {
    (Chain.ETHEREUM, Stable.USDC): "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    (Chain.ETHEREUM, Stable.USDT): "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    (Chain.ARBITRUM, Stable.USDC): "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    (Chain.BASE, Stable.USDC): "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}


def get_stable_token_address(chain: Chain, stable: Stable) -> str:
    address = STABLE_TOKEN_ADDRESSES.get((chain, stable))
    if address is None:
        raise UnsupportedChainError(f"stable_tokens[{stable.name}]", chain)
    return address
