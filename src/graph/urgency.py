import math
from dataclasses import dataclass

from graph.structures.DEXes import DEX


@dataclass(frozen=True)
class TimeWeightParams:
    """Paramètres de calibration de λ(σ_d), configurables (pas de valeurs en
    dur dans le code du solveur).

    lambda_min : poids du temps en régime normal (σ_d -> ∞).
    lambda_max : poids du temps en régime d'urgence maximale (σ_d -> 0+).
    k : raideur de la transition ; zone critique typique σ ∈ [1, 3].
    epsilon : garde-fou plancher appliqué à σ_d (σ_d_safe = max(σ_d, epsilon)).
    """

    lambda_min: float
    lambda_max: float
    k: float
    epsilon: float = 0.1

    def __post_init__(self) -> None:
        if self.epsilon <= 0:
            raise ValueError("epsilon doit être strictement positif")
        if self.lambda_max < self.lambda_min:
            raise ValueError("lambda_max doit être >= lambda_min")
        if self.k <= 0:
            raise ValueError("k doit être strictement positif")


def computeDexUrgencySigma(dex: DEX) -> float:
    """σ_d : urgence de liquidation du DEX destination d.

    σ_d = min_i(σ_i) sur toutes les positions ouvertes i du DEX d.
    +inf si le DEX n'a aucune position ouverte (pas d'urgence -> régime normal).
    """
    if not dex.positions:
        return math.inf
    return min(position.sigma for position in dex.positions)


def safeUrgencySigma(sigmaD: float, epsilon: float) -> float:
    """Garde-fou σ_d_safe = max(σ_d, ε), évite une division par ~0 dans λ."""
    if epsilon <= 0:
        raise ValueError("epsilon doit être strictement positif")
    return max(sigmaD, epsilon)


def computeTimeWeight(sigmaD: float, params: TimeWeightParams) -> float:
    """λ(σ_d), poids borné du temps dans le coût d'arête w(e, d).

    λ(σ_d) = λ_min + (λ_max - λ_min) · (1 - exp(-k / σ_d_safe))

    σ_d -> 0+ : λ -> λ_max (urgence max, la vitesse prime)
    σ_d -> ∞  : λ -> λ_min (régime normal, le coût prime)
    """
    sigmaSafe = safeUrgencySigma(sigmaD, params.epsilon)
    return params.lambda_min + (params.lambda_max - params.lambda_min) * (
        1 - math.exp(-params.k / sigmaSafe)
    )
