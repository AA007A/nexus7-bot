from pathlib import Path


def test_sitecustomize_is_orchestration_only_for_extracted_overlays():
    text = Path("sitecustomize.py").read_text()
    assert "_runtime_overlays.install(" in text
    assert "def _validate_with_history" not in text
    assert "def _analyze_mtf_with_shadow" not in text
    assert "_np_io_lock" not in text


def test_runtime_overlays_contains_behavior_preserving_guards():
    text = Path("bot/runtime_overlays.py").read_text()
    assert '_single_conn_serialized' in text
    assert '_mtf_shadow_patched' in text
    assert '_nexus_persistence_patched' in text
    assert 'return dec' in text
    assert 'return result' in text
    assert 'execution_effect=NONE' in text
