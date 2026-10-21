import pytest

from xdsh.core.task_contract import acceptance_context, retrieval_context


@pytest.mark.parametrize('task', [
    '请你查下当前此刻火山引擎coding plan中是否已经上线deepseek-v4.1-flash？',  # noqa: RUF001
    '請核實目前套餐是否支持這個模型',
    'Verify whether this plan supports the model today',
])
def test_observed_lookup_gets_bounded_batch_guidance(task: str) -> None:
    text = retrieval_context(task)
    assert text is not None
    assert 'independent=true' in text
    assert 'same evidence' in text
    assert 'not a hard task limit' in text


def test_numbered_acceptance_tracks_all_requested_fields_and_evidence() -> None:
    task = "1. Parse records\n2. Save duplicate_ids\n3. Verify original files unchanged"
    text = acceptance_context(task)
    assert text is not None
    assert "R2 (item 2): Save duplicate_ids" in text
    assert "BEFORE/AFTER" in text
    assert "SAME ORIGINAL path" in text
    assert "never hand-copy digests" in text
    assert "JSON parsing alone is not verification" in text
    assert "not duplicate IDs" in text
    assert "no-tool tasks" in text


def test_simple_tasks_do_not_get_checklist_overhead() -> None:
    assert acceptance_context("Hello") is None
    assert acceptance_context("1. Answer\n2. Briefly") is None


def test_reproduction_guidance_prevents_method_and_plot_defaults_drift() -> None:
    text = acceptance_context("复现这篇论文的分析过程与图表, 不要反向拟合。")
    assert text is not None
    assert "effective arguments" in text and "same parameter object" in text
    assert "source_json" in text and "unchanged plan" in text
    assert "do not implement or run it as an optional audit" in text
    assert "maximum deviation" in text
    assert "max_abs_differences" in text and "--details" in text
    assert "recheck after artifact edits" in text
    assert "not independent scientific approval" in text


def test_skill_links_do_not_skip_official_source_verification() -> None:
    text = retrieval_context("Search the latest software release")
    assert text is not None
    assert "Skill source links are discovery leads" in text
    assert "verify ownership before using them in the official-first stage" in text


def test_large_checklist_is_bounded_and_never_claims_to_replace_original() -> None:
    text = acceptance_context("\n".join(f"{i}. " + "x" * 500 for i in range(1, 20)))
    assert text is not None and len(text) < 2400
    assert "FULL original request" in text
