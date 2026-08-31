from dataclasses import dataclass

from graph.structures.DEXes import Chain


@dataclass
class GasCost:
    chain: Chain
    native_amount: float
    native_symbol: str
    usd_amount: float | None


@dataclass
class SwapQuote:
    chain: Chain
    sell_token: str
    buy_token: str
    buy_amount: int
    fee_amount: int
    valid_to: int


@dataclass
class UniswapQuote:
    chain: Chain
    sell_token: str
    buy_token: str
    buy_amount: int
    gas_estimate: int
