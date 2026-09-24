"""NEXUS-7 AI decision layer (Phase 7).

AI decides opportunity quality only (TRADE / ABSTAIN / direction). It never
overrides deterministic validation, risk, loss budget, liquidation safety,
execution authority, halts or the Stage-C LIVE gate (see authority_chain).
"""
