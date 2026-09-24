// Replaces the original's four independent variables (selectedName /
// selectedEdgeKey / selectedWalletChain / selectedSwap, which could in
// principle disagree with each other) with one discriminated union — see
// state/SelectionController.ts.

export type Selection =
  | { kind: "none" }
  | { kind: "dex"; name: string }
  | { kind: "swap" }
  | { kind: "wallet"; chain: string }
  | { kind: "edge"; key: string };
