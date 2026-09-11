"""Deterministic, negation-aware headline semantics for active RSS scoring.

This module deliberately avoids a bag-of-substrings decision for high-impact
phrases. It is installed after the external-context freshness layer so the
public RSS reader uses this classifier at runtime.
"""
from __future__ import annotations

import re


_NEGATORS = {
    "not", "no", "never", "without", "false", "fake", "unconfirmed",
    "denied", "rejected", "rejects", "denies",
}


def _tokens(text: str) -> list[str]:
    return re.findall("[a-z]+(?:n't)?", str(text or "").lower().replace("’", "'"))


def _negated(tokens: list[str], index: int, window: int = 4) -> bool:
    before = tokens[max(0, index - window):index]
    return any(token in _NEGATORS or token.endswith("n't") for token in before)


def _stem_match(token: str, keyword: str) -> bool:
    """Conservative inflection support for the small legacy lexicon."""
    if token == keyword:
        return True
    variants = {
        "rally": ("rally", "rallies", "rallied", "rallying"),
        "surge": ("surge", "surges", "surged", "surging"),
        "approve": ("approve", "approves", "approved", "approving"),
        "reject": ("reject", "rejects", "rejected", "rejecting"),
        "deny": ("deny", "denies", "denied", "denying"),
        "hack": ("hack", "hacks", "hacked", "hacking"),
        "collapse": ("collapse", "collapses", "collapsed", "collapsing"),
    }
    return token in variants.get(keyword, ())


def _keyword_hits(tokens: list[str], keywords, *, skip: set[str] | None = None) -> float:
    ignored = skip or set()
    total = 0.0
    for raw_keyword in keywords:
        keyword = str(raw_keyword or "").strip().lower()
        if not keyword or keyword in ignored or " " in keyword:
            continue
        for index, token in enumerate(tokens):
            if _stem_match(token, keyword) and not _negated(tokens, index):
                total += 1.0
    return total


def classify_headline(text: str, scoring) -> tuple[str, float, bool]:
    """Return (classification, confidence, macro_event_flag).

    High-impact approval/rejection semantics are evaluated before generic
    vocabulary. `ETF`, `approval`, and `SEC` are intentionally neutral without
    context. Negated terms such as `not hacked` do not create bearish evidence.
    """
    lowered = str(text or "").lower().replace("’", "'")
    tokens = _tokens(lowered)

    macro_keywords = getattr(scoring, "_MACRO_KEYWORDS", ())
    bullish_keywords = getattr(scoring, "_BULLISH_KW", ())
    bearish_keywords = getattr(scoring, "_BEARISH_KW", ())
    is_macro = any(str(keyword).lower() in lowered for keyword in macro_keywords)

    has_etf_context = "etf" in tokens
    has_application_context = any(token in {"application", "proposal"} for token in tokens)
    approval_context = has_etf_context or has_application_context

    rejection_phrases = (
        "not approved", "approval denied", "approval rejected",
        "application denied", "application rejected",
        "proposal denied", "proposal rejected",
        "etf denied", "etf rejected", "etf delayed",
    )
    approval_phrases = (
        "etf approved", "approves spot", "approved spot",
        "approval granted", "application approved", "proposal approved",
    )

    explicit_bear = approval_context and any(phrase in lowered for phrase in rejection_phrases)
    explicit_bull = (
        approval_context
        and not explicit_bear
        and any(phrase in lowered for phrase in approval_phrases)
    )

    # Generic lexical evidence keeps the previous vocabulary but removes terms
    # whose polarity is context-dependent.
    bull = _keyword_hits(
        tokens,
        bullish_keywords,
        skip={"etf", "approval"},
    )
    bear = _keyword_hits(
        tokens,
        bearish_keywords,
        skip={"sec"},
    )

    # Common headline morphology absent from the legacy exact-keyword list.
    if any(token in {"rally", "rallies", "rallied", "rallying"} for token in tokens):
        bull += 1.0

    if explicit_bull:
        bull += 3.0
    if explicit_bear:
        bear += 3.0

    diff = bull - bear
    total = bull + bear
    if diff > 0.5:
        confidence = min(0.95, 0.55 + min(0.30, abs(diff) * 0.10) + min(0.10, total * 0.02))
        return "BULLISH", round(confidence, 3), is_macro
    if diff < -0.5:
        confidence = min(0.95, 0.55 + min(0.30, abs(diff) * 0.10) + min(0.10, total * 0.02))
        return "BEARISH", round(confidence, 3), is_macro
    return "NEUTRO", 0.3, is_macro


def install(scoring, derivatives_hardening, log) -> None:
    if getattr(scoring, "_news_semantic_hardening_installed", False):
        return

    def hardened(text: str):
        return classify_headline(text, scoring)

    scoring._classify_news = hardened
    # Keep the public helper in the earlier freshness module consistent with
    # the actually installed runtime semantics and its regression tests.
    derivatives_hardening.classify_headline = classify_headline
    scoring._news_semantic_hardening_installed = True
    log.warning(
        "[NEWS_SEMANTICS] installed negation_aware=true "
        "etf_approval_context_required=true standalone_sec_directional=false "
        "execution_permissions_unchanged=true"
    )
