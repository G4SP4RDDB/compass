from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence
from enum import Enum,auto

from graph.structures.positions import Position


@dataclass
class MeasuredDelay:
    """Délai RÉELLEMENT observé pour un (DEX, chain, withdraw|deposit),
    agrégé sur les derniers runs live réussis de compass_test (voir
    compass_test/calibration.py, qui produit connectors/dex_measured_delays.json,
    et connectors.dex_measured_delays qui le recharge sur le DEX). Une
    entrée n'existe que si au moins UN run live a réussi : absent ->
    costing.computeDelay retombe sur la valeur configurée/DEFAULT_*.

    samplesSeconds : les délais individuels retenus (au plus
    connectors.dex_measured_delays.MAX_SAMPLES, les plus récents), du plus
    ancien au plus récent. meanSeconds en est la moyenne arithmétique --
    c'est LA valeur consommée par le solveur. Les autres champs sont
    informatifs (UI, CLI), jamais lus par costing."""

    meanSeconds: float
    samplesSeconds: list[float] = field(default_factory=list)
    sampleRunIds: list[str] = field(default_factory=list)
    lastMeasuredAt: float | None = None

    @property
    def n(self) -> int:
        return len(self.samplesSeconds)

    @property
    def stdSeconds(self) -> float:
        if self.n < 2:
            return 0.0
        mean = sum(self.samplesSeconds) / self.n
        return (sum((x - mean) ** 2 for x in self.samplesSeconds) / (self.n - 1)) ** 0.5

    @property
    def minSeconds(self) -> float:
        return min(self.samplesSeconds) if self.samplesSeconds else self.meanSeconds

    @property
    def maxSeconds(self) -> float:
        return max(self.samplesSeconds) if self.samplesSeconds else self.meanSeconds

# Valeurs de départ placeholder tant qu'aucune donnée réelle par DEX n'est
# saisie (via le panel "Config" du frontend ou connectors/dex_operational_params.json) :
# retrait facturé, dépôt gratuit, quelques minutes de traitement de chaque
# côté. À affiner DEX par DEX, ce ne sont pas des valeurs mesurées.
DEFAULT_WITHDRAW_FEE_USD = 1.0
DEFAULT_WITHDRAW_DELAY_SECONDS = 300.0
DEFAULT_DEPOSIT_FEE_USD = 0.0
DEFAULT_DEPOSIT_DELAY_SECONDS = 60.0
# Smallest amount this DEX will actually let a withdrawal go through for, PER
# CHAIN (real exchanges reject a withdraw request below this — e.g. MEXC's
# live capital/config/getall reports $0.50 USDT min on BSC, $1 on Arbitrum,
# fetched 2026-09-04). Distinct from withdrawFeeUsd (a cost paid ON a
# withdrawal, not a floor on its size). Also doubles as the amount
# compass_test uses for a dry/live test of this DEX's Withdraw edge (see
# compass_test/plan_loader.py) — a realistic exchange-account-sized amount,
# not the solver's flow on a deficit DEX's edge, which is still an
# whatever the user typed in the Imbalance form (connectors.dex_imbalances)
# and can be far larger than any real balance.
DEFAULT_MIN_WITHDRAW_USD = 5.0
# Mirror of DEFAULT_MIN_WITHDRAW_USD for the crediting side: smallest amount
# this DEX will actually credit a deposit for, PER CHAIN. Defaults to 0.0
# (no known floor) rather than a guessed nonzero placeholder — unlike
# withdrawals, a deposit's minimum isn't a universal exchange concept (MEXC's
# live capital/config/getall exposes withdrawMin but no deposit equivalent,
# checked 2026-09-04); fill in a real number per DEX/chain via the Config tab
# once you have one. compass_test tests a journey at
# max(minDepositUsd of the destination, minWithdrawUsd of the source) — see
# compass_test/plan_loader.py — so this only matters once it's actually set
# above the source's withdraw floor for a given route.
DEFAULT_MIN_DEPOSIT_USD = 0.0


#Ou est ce que l'on recoit les targets ? => target reçue envoyées via Armand

class DEX:
    def __init__(self,supportedChains: Sequence[Chain],supportedStables: Sequence[Stable],name: str = ""):
        self.name = name
        self.chains = supportedChains
        self.stables = supportedStables
        self.margin = 0
        self.target = 0
        self.inbalance = 0
        # Cash réellement retirable, par stable (toujours >= 0). Fongible entre
        # chains (n'importe laquelle des chains supportées peut recevoir le
        # retrait), mais jamais entre stables (déposé en USDC -> retiré en
        # USDC, jamais converti implicitement). Doit être rempli AVANT de
        # construire un Graph : WithdrawNode lit cette valeur une seule fois
        # à la construction (voir Graph._addSourceAndWithdrawNodes), comme
        # inbalance/target.
        self.withdrawBalances: dict[Stable, float] = {}
        # Certains DEX (ex: Aster) lient le retrait à la chain de dépôt :
        # un solde crédité via Arbitrum ne peut être retiré que vers Arbitrum,
        # jamais fongible entre chains comme c'est le cas par défaut. Deux
        # champs séparés :
        #   - requiresSameChainWithdraw : LA RÈGLE (ce DEX applique cette
        #     contrainte ou non) — voir graph.structures.dex_registry pour la
        #     liste des DEX concernés.
        #   - withdrawChainByStable : LA DONNÉE (sur quelle chain le solde de
        #     CETTE stable est actuellement crédité), n'a de sens que si la
        #     règle ci-dessus est active. Absent/vide -> aucune restriction
        #     (comportement fongible historique), même si la règle est active
        #     mais qu'aucun solde n'a encore été assigné à une chain.
        # Lu par Graph._linkWithdrawalsAndDeposits pour restreindre les edges
        # WithdrawNode -> WalletNode à la seule chain autorisée.
        self.requiresSameChainWithdraw: bool = False
        self.withdrawChainByStable: dict[Stable, Chain] = {}
        # Tous les DEX du registre aujourd'hui (y compris MEXC, qui crédite
        # automatiquement dès réception sur son adresse de dépôt) créditent
        # le dépôt dans LA MÊME transaction que le virement — un seul appel
        # de contrat/évènement, pas d'adresse de dépôt distincte à surveiller
        # (voir Graph._linkWithdrawalsAndDeposits, WalletNode -> SourceNode
        # direct). Un vrai CEX dont le dépôt serait réellement reconnu/crédité
        # en deux étapes séparées fonctionnerait différemment : on envoie à
        # une adresse de dépôt dédiée, puis la plateforme reconnaît/crédite
        # séparément après ses propres délais — DEUX actions distinctes
        # (WalletNode -> DepositNode -> SourceNode). Faux par défaut (dépôt
        # direct) ; voir graph.structures.dex_registry pour la liste (vide
        # aujourd'hui) des DEX qui le mettent à True.
        self.requiresDepositAddress: bool = False
        # Positions de marge ouvertes sur ce DEX, utilisées pour calculer son
        # urgence de liquidation (voir graph.urgency.computeDexUrgencySigma).
        # Vide par défaut = pas de position ouverte = pas d'urgence.
        self.positions: list[Position] = []
        # Frais et délais opérationnels de dépôt/retrait CEX, PAR CHAIN (ex:
        # MEXC peut être plus lent/cher à créditer sur une chain que sur une
        # autre) — un dict par champ, une entrée par chain supportée par ce
        # DEX, initialisée aux placeholders DEFAULT_* ci-dessus. Éditable à la
        # main via le panel "Config" du frontend (voir
        # visualization/web/graph_template.html, une ligne par (DEX, chain))
        # et rechargé par connectors.dex_operational_params. Consommés par
        # costing.computeCost/computeDelay sur les edges Withdraw->Wallet
        # (chain = celle du WalletNode destination) et Wallet/Deposit->SourceNode
        # (chain = celle du WalletNode/DepositNode source), voir
        # Graph._linkWithdrawalsAndDeposits.
        self.withdrawFeeUsdByChain: dict[Chain, float] = {chain: DEFAULT_WITHDRAW_FEE_USD for chain in supportedChains}
        self.withdrawDelaySecondsByChain: dict[Chain, float] = {
            chain: DEFAULT_WITHDRAW_DELAY_SECONDS for chain in supportedChains
        }
        self.depositFeeUsdByChain: dict[Chain, float] = {chain: DEFAULT_DEPOSIT_FEE_USD for chain in supportedChains}
        self.depositDelaySecondsByChain: dict[Chain, float] = {
            chain: DEFAULT_DEPOSIT_DELAY_SECONDS for chain in supportedChains
        }
        self.minWithdrawUsdByChain: dict[Chain, float] = {chain: DEFAULT_MIN_WITHDRAW_USD for chain in supportedChains}
        self.minDepositUsdByChain: dict[Chain, float] = {chain: DEFAULT_MIN_DEPOSIT_USD for chain in supportedChains}
        # Délais MESURÉS (voir MeasuredDelay ci-dessus) : seulement les chains
        # pour lesquelles au moins un run live de compass_test a réussi --
        # contrairement aux dicts *DelaySecondsByChain ci-dessus, PAS une
        # entrée par chain supportée. Quand une entrée existe, elle PRIME
        # sur withdrawDelaySecondsByChain / depositDelaySecondsByChain dans
        # costing.computeDelay ; la valeur configurée reste intacte (toujours
        # éditable dans le panel Config, affichée à côté de la mesure) et
        # redevient effective dès que la mesure disparaît. Rempli par
        # connectors.dex_measured_delays.apply_measured_delays.
        self.measuredWithdrawDelayByChain: dict[Chain, MeasuredDelay] = {}
        self.measuredDepositDelayByChain: dict[Chain, MeasuredDelay] = {}

    def update_target(self,newTarget: float) -> None:
        self.target = newTarget
    def getBalance(self) ->float:
        return self.inbalance
    
    def getChains(self) -> Sequence[Chain]:
        return self.chains
    def getStables(self) -> Sequence[Stable]:
        return self.stables


class Chain(Enum):
    ETHEREUM = auto()
    ARBITRUM = auto()
    SOLANA = auto()
    BSC = auto()
    POLYGON = auto()
    BASE = auto()
    OPTIMISM = auto()
    AVALANCHE = auto()



class Stable(Enum):
    USDC = auto()
    USDT = auto()





    

    
    


        

