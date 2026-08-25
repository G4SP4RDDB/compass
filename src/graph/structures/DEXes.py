from typing import Sequence
from enum import Enum,auto




#Ou est ce que l'on recoit les targets ? => target reçue envoyées via Armand

class DEX: 
    def __init__(self,supportedChains: Sequence[Chain],supportedStables: Sequence[Stable]):
        self.chains = supportedChains
        self.stables = supportedStables
        self.margin = 0
        self.target = 0
        self.inbalance = 0

    def update_target(self,newTarget: float) -> None:
        self.target = newTarget
    def getBalance(self) ->float:
        return self.inbalance
    
    def getChains(self) -> Sequence[Chain]:
        return self.chains
    def getStables(self) -> Sequence[Stables]:
        return self.stables


class Chain(Enum):
    ETHEREUM = auto()
    ARBITRUM = auto()
    SOLANA = auto()
    BSC = auto()
    POLYGON = auto()
    BASE = auto()
    OPTIMISM = auto()



class Stable(Enum):
    USDC = auto()
    USDT = auto()





    

    
    


        

