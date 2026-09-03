from graph.structures.DEXes import Chain, Stable

from .exceptions import UnsupportedChainError

# 6 décimales pour USDC et USDT sur toutes les chains listées ici.
STABLE_DECIMALS = 6

# Adresses vérifiées (CoinGecko contract data) ; None = pas encore vérifié,
# ne pas deviner une adresse de contrat, ça manipule de l'argent réel.
#
# AVALANCHE/OPTIMISM/POLYGON ajoutées le 2026-09-03, vérifiées contre Circle
# (developers.circle.com/stablecoins/usdc-contract-addresses) ET recoupées
# individuellement sur leur block explorer respectif (Snowtrace/Optimistic
# Etherscan/Polygonscan, "Circle: USDC Token") — ces trois chains sont
# couvertes par CCTP (voir connectors/cctp.py:CCTP_DOMAIN_BY_CHAIN) mais
# n'avaient pas d'adresse ici, ce qui bloquait availableBridgeProtocols
# (graph.structures.bridges) AVANT même d'atteindre son check CCTP :
# is_stable_supported() renvoyait False, donc CCTP_V1/V2 n'étaient jamais
# proposés comme options au solveur pour ces chains, seul GENERIC (fallback
# 20 min) l'était — pas un choix du solveur, une route jamais offerte.
STABLE_TOKEN_ADDRESSES: dict[tuple[Chain, Stable], str] = {
    (Chain.ETHEREUM, Stable.USDC): "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    (Chain.ETHEREUM, Stable.USDT): "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    (Chain.ARBITRUM, Stable.USDC): "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    (Chain.BASE, Stable.USDC): "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    (Chain.AVALANCHE, Stable.USDC): "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    (Chain.OPTIMISM, Stable.USDC): "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    (Chain.POLYGON, Stable.USDC): "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
}


def is_stable_supported(chain: Chain, stable: Stable) -> bool:
    return (chain, stable) in STABLE_TOKEN_ADDRESSES


def get_stable_token_address(chain: Chain, stable: Stable) -> str:
    address = STABLE_TOKEN_ADDRESSES.get((chain, stable))
    if address is None:
        raise UnsupportedChainError(f"stable_tokens[{stable.name}]", chain)
    return address
