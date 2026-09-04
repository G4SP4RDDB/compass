from __future__ import annotations

from typing import Sequence
from enum import Enum,auto

from graph.structures.positions import Position

# Valeurs de départ placeholder tant qu'aucune donnée réelle par DEX n'est
# saisie (via le panel "Config" du frontend ou connectors/dex_operational_params.json) :
# retrait facturé, dépôt gratuit, quelques minutes de traitement de chaque
# côté. À affiner DEX par DEX, ce ne sont pas des valeurs mesurées.
DEFAULT_WITHDRAW_FEE_USD = 1.0
DEFAULT_WITHDRAW_DELAY_SECONDS = 300.0
DEFAULT_DEPOSIT_FEE_USD = 0.0
DEFAULT_DEPOSIT_DELAY_SECONDS = 60.0


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
        # Frais et délais opérationnels de dépôt/retrait CEX propres à ce DEX
        # (ex: frais de retrait fixe facturé par la plateforme, délai de
        # traitement d'un retrait avant que les fonds soient utilisables
        # ailleurs). Valeurs de départ = placeholders (DEFAULT_* ci-dessus),
        # à affiner à la main via le panel "Config" du frontend (voir
        # visualization/web/graph_template.html) et rechargées par
        # connectors.dex_operational_params. Consommés par
        # costing.computeCost/computeDelay sur les edges Withdraw->Wallet et
        # Wallet/Deposit->SourceNode (voir Graph._linkWithdrawalsAndDeposits).
        self.withdrawFeeUsd: float = DEFAULT_WITHDRAW_FEE_USD
        self.withdrawDelaySeconds: float = DEFAULT_WITHDRAW_DELAY_SECONDS
        self.depositFeeUsd: float = DEFAULT_DEPOSIT_FEE_USD
        self.depositDelaySeconds: float = DEFAULT_DEPOSIT_DELAY_SECONDS

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





    

    
    


        

