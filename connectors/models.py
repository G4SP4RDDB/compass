from dataclasses import dataclass

from src.DEXes import Chain


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
