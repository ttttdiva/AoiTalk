from src.llm.manager import _ensure_openai_agents_chat_stream_compat


def test_openrouter_chat_stream_delta_accepts_missing_logprobs():
    _ensure_openai_agents_chat_stream_compat()

    from agents.models import chatcmpl_stream_handler

    event = chatcmpl_stream_handler.ResponseTextDeltaEvent(
        content_index=0,
        delta="hello",
        item_id="fake-response",
        output_index=0,
        type="response.output_text.delta",
        sequence_number=1,
    )

    assert event.delta == "hello"
    if hasattr(event, "logprobs"):
        assert event.logprobs == []
