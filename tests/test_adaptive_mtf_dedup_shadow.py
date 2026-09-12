from bot.adaptive_mtf_dedup_shadow import assess_failures


def test_shadow_removes_only_score_tf_overlap_checks():
    result = assess_failures([
        "SCORE_4H", "SCORE_COMBINED", "ALIGN_15M", "VOLUME", "ADX",
        "ENTRY_TYPE", "EXTENSION",
    ])
    assert result["removed_duplicate_checks"] == ["ALIGN_15M", "VOLUME", "ADX"]
    assert result["remaining_failures"] == [
        "SCORE_4H", "SCORE_COMBINED", "ENTRY_TYPE", "EXTENSION",
    ]
    assert result["would_reach_nexus"] is False


def test_shadow_can_mark_candidate_as_would_reach_nexus_when_only_duplicates_failed():
    result = assess_failures(["ALIGN_15M", "VOLUME", "ADX", "RSI_LONG"])
    assert result["dedup_failure_count"] == 0
    assert result["would_reach_nexus"] is True


def test_shadow_preserves_score_thresholds_and_independent_guards():
    result = assess_failures([
        "SCORE_15M", "ENTRY_TYPE", "EXTENSION", "HTF_OPPOSITION",
    ])
    assert result["removed_duplicate_checks"] == []
    assert result["remaining_failures"] == [
        "SCORE_15M", "ENTRY_TYPE", "EXTENSION", "HTF_OPPOSITION",
    ]
    assert result["would_reach_nexus"] is False


def test_shadow_does_not_mutate_input():
    failures = ["VOLUME", "SCORE_1H"]
    original = list(failures)
    assess_failures(failures)
    assert failures == original
