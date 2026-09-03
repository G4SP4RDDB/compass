from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    """Position de marge ouverte sur un DEX.

    sigma : distance en écarts-types (stdev) entre le prix courant et le prix
    de liquidation de cette position. Plus sigma est petit, plus la
    liquidation est proche (urgence plus grande).
    """

    sigma: float
