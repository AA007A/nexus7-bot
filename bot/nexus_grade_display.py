"""Display-only clarification for NEXUS setup grades.

The score-band taxonomy historically labels every score below 65 as
``NO_TRADE``. Runtime execution, however, is controlled by the configured
NEXUS threshold and can legitimately approve a score between that threshold
and 65. This module fixes only the human-facing Telegram wording; it does not
change the decision object, thresholds, risk, or execution gates.
"""


def install(notifier, log):
    if getattr(notifier, "_nexus_grade_display_patched", False):
        return

    original = notifier.nexus_approved_msg

    async def nexus_approved_msg_with_clear_band(d):
        view = dict(d or {})
        if (
            view.get("setup_grade") == "NO_TRADE"
            and view.get("execution_allowed") is True
        ):
            view["setup_grade"] = "BELOW_C"
            reasoning = list(view.get("reasoning") or [])
            reasoning.append(
                "Faixa <65; aprovação determinada pelo threshold operacional configurado"
            )
            view["reasoning"] = reasoning
        return await original(view)

    notifier.nexus_approved_msg = nexus_approved_msg_with_clear_band
    notifier._nexus_grade_display_patched = True
    log.info(
        "[NEXUS_GRADE_DISPLAY] below-C approved setups are labeled BELOW_C in Telegram; decision logic unchanged"
    )
