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
        # à la construction (voir Graph._addSourceAndDepositNodes), comme
        # inbalance/target.
        self.withdrawBalances: dict[Stable, float] = {}
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
        # costing.computeCost/computeDelay sur les edges Withdraw->Deposit et
        # Deposit->SourceNode (voir Graph._addSourceAndDepositNodes).
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





    

    
    


        

