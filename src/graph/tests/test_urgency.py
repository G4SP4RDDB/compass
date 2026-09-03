import math

import pytest

from graph.costing import computeTimeWeightedCost
from graph.structures.DEXes import Chain, DEX, Stable
from graph.structures.positions import Position
from graph.urgency import (
    TimeWeightParams,
    computeDexUrgencySigma,
    computeTimeWeight,
    safeUrgencySigma,
)


def _makeDex(sigmas: list[float]) -> DEX:
    dex = DEX(supportedChains=[Chain.ETHEREUM], supportedStables=[Stable.USDC], name="test-dex")
    dex.positions = [Position(sigma=s) for s in sigmas]
    return dex


def _params(**overrides) -> TimeWeightParams:
    defaults = dict(lambda_min=0.1, lambda_max=5.0, k=2.0, epsilon=0.1)
    defaults.update(overrides)
    return TimeWeightParams(**defaults)


class TestTimeWeightParams:
    def test_rejects_lambda_max_below_lambda_min(self):
        with pytest.raises(ValueError):
            _params(lambda_min=5.0, lambda_max=1.0)

    def test_rejects_non_positive_k(self):
        with pytest.raises(ValueError):
            _params(k=0.0)

    def test_rejects_non_positive_epsilon(self):
        with pytest.raises(ValueError):
            _params(epsilon=0.0)


class TestComputeDexUrgencySigma:
    def test_no_open_positions_is_infinite(self):
        dex = _makeDex([])
        assert computeDexUrgencySigma(dex) == math.inf

    def test_single_position(self):
        dex = _makeDex([2.5])
        assert computeDexUrgencySigma(dex) == pytest.approx(2.5)

    def test_multiple_positions_takes_the_minimum(self):
        # La position la plus proche de sa liquidation domine l'urgence du DEX,
        # peu importe combien d'autres positions plus saines il a également.
        dex = _makeDex([4.0, 0.8, 2.1])
        assert computeDexUrgencySigma(dex) == pytest.approx(0.8)

    def test_multiple_positions_order_independent(self):
        dex = _makeDex([0.8, 4.0, 2.1])
        assert computeDexUrgencySigma(dex) == pytest.approx(0.8)


class TestSafeUrgencySigma:
    def test_clamps_below_epsilon(self):
        assert safeUrgencySigma(0.01, epsilon=0.1) == pytest.approx(0.1)

    def test_clamps_negative_or_zero(self):
        assert safeUrgencySigma(0.0, epsilon=0.05) == pytest.approx(0.05)

    def test_passes_through_above_epsilon(self):
        assert safeUrgencySigma(3.0, epsilon=0.1) == pytest.approx(3.0)

    def test_rejects_non_positive_epsilon(self):
        with pytest.raises(ValueError):
            safeUrgencySigma(1.0, epsilon=0.0)


class TestComputeTimeWeight:
    def test_sigma_to_zero_saturates_at_lambda_max(self):
        params = _params()
        # sigma_d en dessous du plancher epsilon : le garde-fou le ramène à
        # epsilon, donc lambda doit déjà être très proche de lambda_max.
        weight = computeTimeWeight(1e-6, params)
        assert weight == pytest.approx(params.lambda_max, abs=1e-6)

    def test_sigma_to_infinity_saturates_at_lambda_min(self):
        params = _params()
        weight = computeTimeWeight(math.inf, params)
        assert weight == pytest.approx(params.lambda_min)

    def test_large_finite_sigma_approaches_lambda_min(self):
        params = _params()
        weight = computeTimeWeight(1e6, params)
        assert weight == pytest.approx(params.lambda_min, abs=1e-3)

    def test_weight_is_monotonically_decreasing_in_sigma(self):
        params = _params()
        sigmas = [0.05, 0.2, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 100.0]
        weights = [computeTimeWeight(s, params) for s in sigmas]
        assert weights == sorted(weights, reverse=True)

    def test_weight_stays_within_bounds(self):
        params = _params()
        for sigma in [1e-9, 0.05, 0.1, 0.5, 1.0, 2.0, 3.0, 50.0, 1e9, math.inf]:
            weight = computeTimeWeight(sigma, params)
            assert params.lambda_min - 1e-9 <= weight <= params.lambda_max + 1e-9

    def test_no_open_positions_dex_gets_lambda_min(self):
        dex = _makeDex([])
        params = _params()
        sigmaD = computeDexUrgencySigma(dex)
        assert computeTimeWeight(sigmaD, params) == pytest.approx(params.lambda_min)

    def test_near_liquidation_dex_gets_near_lambda_max(self):
        dex = _makeDex([0.001])
        params = _params()
        sigmaD = computeDexUrgencySigma(dex)
        weight = computeTimeWeight(sigmaD, params)
        assert weight == pytest.approx(params.lambda_max, abs=1e-3)

    def test_lambda_min_equals_lambda_max_is_constant(self):
        params = _params(lambda_min=1.0, lambda_max=1.0)
        assert computeTimeWeight(0.001, params) == pytest.approx(1.0)
        assert computeTimeWeight(1000.0, params) == pytest.approx(1.0)

    def test_k_controls_steepness_around_critical_zone(self):
        # Un k plus grand pousse la transition vers des sigma plus élevés :
        # à sigma fixe dans la zone critique [1, 3], un k plus grand donne un
        # poids plus proche de lambda_max.
        lowK = _params(k=0.5)
        highK = _params(k=5.0)
        sigma = 2.0
        assert computeTimeWeight(sigma, highK) > computeTimeWeight(sigma, lowK)


class TestComputeTimeWeightedCost:
    def test_matches_fee_plus_weighted_time(self):
        params = _params(lambda_min=0.1, lambda_max=5.0, k=2.0, epsilon=0.1)
        fee = 3.0
        edgeTime = 10.0
        sigmaD = 2.0
        expected = fee + computeTimeWeight(sigmaD, params) * edgeTime
        assert computeTimeWeightedCost(fee, edgeTime, sigmaD, params) == pytest.approx(expected)

    def test_zero_time_edge_reduces_to_fee(self):
        params = _params()
        assert computeTimeWeightedCost(fee=7.5, edgeTime=0.0, sigmaD=0.001, params=params) == pytest.approx(
            7.5
        )

    def test_urgent_destination_costs_more_than_normal_for_same_edge(self):
        params = _params()
        fee = 1.0
        edgeTime = 20.0
        urgentCost = computeTimeWeightedCost(fee, edgeTime, sigmaD=0.01, params=params)
        normalCost = computeTimeWeightedCost(fee, edgeTime, sigmaD=1000.0, params=params)
        assert urgentCost > normalCost
