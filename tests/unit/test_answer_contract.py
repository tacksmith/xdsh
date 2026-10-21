# ruff: noqa: RUF001
import pytest

from xdsh.core.answer_contract import format_answer


def test_explicit_character_caps_are_checked_without_truncating_answers() -> None:
    from xdsh.core.answer_contract import check_answer_length

    for request in ("总计不超过10字", "10字以内", "最多10个字符", "at most 10 characters"):
        result = check_answer_length(request, "不能发布，支付回归尚未通过。")
        assert result is not None
        assert result.limit == 10 and result.exceeded
    assert check_answer_length("最多10个单词", "long answer") is None
    assert check_answer_length('解释例子“10字内”是什么意思', "long answer") is None
    assert check_answer_length("标题10字内，正文100字内", "long answer") is None


def test_observed_json_prompt_gets_readable_lossless_layout() -> None:
    import json

    prompt = "仅输出一个合法JSON对象，不使用Markdown代码框，不添加解释"
    answer = '{"precise":1.00000000000000001,"rows":["a\\nb",false],"count":10000000000000001}'
    result = format_answer(prompt, answer)
    assert '\n  "precise": 1.00000000000000001,' in result
    assert '\n    "a\\nb",' in result
    assert json.loads(result) == json.loads(answer)
    assert not result.startswith('```')


def test_explicit_json_only_removes_one_valid_fence_without_changing_data() -> None:
    value = '{"precise":1.00000000000000001,"s":"a\\nb"}'
    result = format_answer("只输出JSON", "```json\n" + value + "\n```")
    assert '1.00000000000000001' in result and '"a\\nb"' in result
    assert result.startswith('{\n')


def test_unrequested_or_invalid_fences_are_preserved() -> None:
    for prompt, answer in (
        ("展示JSON代码示例", '```json\n{"x":1}\n```'),
        ("只输出JSON", '说明\n```json\n{"x":1}\n```'),
        ("只输出JSON", '```json\n{"x":}\n```'),
        ("只输出JSON", '```json\n{"x":1,"x":2}\n```'),
    ):
        assert format_answer(prompt, answer) == answer


def test_observed_overrun_can_drop_layout_without_dropping_claims() -> None:
    answer = ('都不能。\n\n1. 缺 key「v2.8.1」不等于没有版本记录：现有 key 是「2.8.1」，'
              '且无定义时缺失键≠无数据。\n2. published_at 无定义，键名直译不能替代定义，'
              '不能认作构建时间，只能记为未证实。')
    result = format_answer("100字内。", answer)
    assert len(result) <= 100
    assert '不能认作构建时间，只能记为未证实。' in result
    assert 'v2.8.1' in result and '2.8.1' in result


def test_never_truncates_claims_or_alters_code_for_length() -> None:
    for answer in ('不可以。条件仍未验证。', '```python\nx = 100\n```'):
        assert format_answer("5字内", answer) == answer


@pytest.mark.parametrize(
    "prompt,answer",
    [
        (
            "字段 published_at 定义为上线时间。另一个字段 build_at 没有定义，"
            "能不能认作构建时间？",
            "published_at 是上线时间；build_at 需查阅定义。",
        ),
        (
            "字段 published_at 原先没有定义；刚取得的 schema 明确定义为发布时间。"
            "是否可视为发布时间？",
            "根据补充的 schema，published_at 是发布时间。",
        ),
        (
            "JSON field published_at is undefined. Return only JSON, single line, "
            "with the observed literal value and is_valid. Do not interpret the field.",
            '{"published_at":"2026-01-12","is_valid":true}',
        ),
        (
            "JSON field published_at is undefined. Show Python checking a non-null "
            "value; do not interpret its meaning.",
            '```python\nassert payload["published_at"] is not None\n```',
        ),
        (
            "字段 published_at 没有定义。原样返回下面的错误示例供测试："
            "published_at 是发布时间。",
            "published_at 是发布时间。",
        ),
        (
            "JSON 字段 published_at 没有定义，能不能认作构建时间？",
            "不能确认；字段语义未定义，键名本身不能证明它是构建时间。",
        ),
        (
            "Only translate the field name published_at; its meaning is undefined.",
            "published_at 的直译是发布于。",
        ),
    ],
    ids=["other-field", "new-definition", "literal-json", "code", "quoted-example",
         "qualified-answer", "translation"],
)
def test_final_renderer_does_not_infer_semantics_from_request_keywords(
    prompt: str, answer: str,
) -> None:
    from xdsh.core.events import FinishEvent
    from xdsh.telegram.render import TurnRenderer

    renderer = TurnRenderer(max_answer_chars=2000, original_request=prompt)
    renderer.text_parts = [answer]
    renderer.finish = FinishEvent(session_id="synthetic", reason="completed")
    renderer.apply_output_contract()
    assert "".join(renderer.text_parts) == answer


def test_sections_get_blank_lines_and_bold_without_changing_code() -> None:
    answer = '已完成：\n- 登录\n待办：\n- 支付\n```text\n下一步：\n\\n\n```'
    assert format_answer("简述进度", answer) == (
        '**已完成**\n\n- 登录\n\n**待办**\n\n- 支付\n```text\n下一步：\n\\n\n```'
    )
    assert format_answer("原样返回，恰好6行", answer) == answer


def test_json_preserves_duplicate_keys_invalid_values_and_explicit_single_line() -> None:
    for value in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":}'):
        assert format_answer("仅输出一个合法JSON对象", value) == value
    value = '{"rows":[1,2]}'
    assert format_answer("只输出JSON，单行", value) == value


def test_final_renderer_normalizes_only_when_explicitly_applied() -> None:
    from xdsh.core.events import FinishEvent
    from xdsh.telegram.render import TurnRenderer
    from xdsh.telegram.service import TelegramService

    renderer = TurnRenderer(max_answer_chars=1000, original_request="只输出JSON")
    renderer.text_parts = ['```json\n{"my_key":false}\n```']
    renderer.finish = FinishEvent(session_id="synthetic", reason="completed")
    renderer.apply_output_contract()
    assert renderer.text_parts == ['{\n  "my_key": false\n}']
    service = object.__new__(TelegramService)
    messages = service._terminal_messages(renderer, outcome="completed", error_text=None)
    assert len(messages) == 1
    assert messages[0].kind == "text" and messages[0].text == '{\n  "my_key": false\n}'


def test_inline_sections_and_list_blocks_are_separated() -> None:
    answer = '**结论：** 尚未确认。\n**依据：** 官方列表未列出。\n- 证据一\n- 证据二'
    expected = '**结论：** 尚未确认。\n\n**依据：** 官方列表未列出。\n\n- 证据一\n- 证据二'
    assert format_answer('简要回答', answer) == expected
    assert format_answer('简要回答', expected) == expected
    assert format_answer('原样输出', answer) == answer
    code = '```text\n' + answer + '\n```'
    assert format_answer('给出代码', code) == code


def test_progress_blocks_have_blank_lines_in_each_language() -> None:
    from xdsh.telegram.render import ProgressRenderer

    for language in ('zh-CN', 'zh-TW', 'en'):
        progress = ProgressRenderer(language=language)
        progress.recent_output = ['Observed evidence.']
        text = progress.render(final=False, elapsed_seconds=10)
        assert text is not None and '\n\n💬' in text
        assert '\n• Observed evidence.' in text
