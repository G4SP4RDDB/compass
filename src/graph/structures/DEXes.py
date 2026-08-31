from typing import Sequence
from enum import Enum,auto




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





    

    
    


        

