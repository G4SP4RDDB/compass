from graph.structures.DEXes import Chain, Stable

class Bridge:
    def __init__(self,chainIn: Chain,chainOut:Chain,stable:Stable):
        self.chainIn = chainIn
        self.chainOut = chainOut
        self.Stable = stable
        
        