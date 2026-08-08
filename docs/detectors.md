# Detector math — writeups

Placeholder. Populated in Phase 2 (build plan §2.3–§2.7) with the derivation,
edge-case behaviour, and decision thresholds for each detector:

- **CategoryInsiderDetector** — binomial p-value vs category base rate
- **EventInsiderDetector** — logistic combo of nonce + size + niche + contrarian
- **FreshWalletDetector** — softer fresh signal; composite-only (never solo-flags)
- **CoordinationDetector** — co-occurrence graph, 24h rolling window (G4)
- **TimingDetector** — late-window concentration, per-category base-rate weighted (G5)

See `polysim-spec.md` §7.3 for the spec-level definition.
