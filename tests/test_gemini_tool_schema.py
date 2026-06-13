from __future__ import annotations

from src.tools.adapters import _clean_schema_for_gemini
from src.tools.adapters import GeminiAdapter
from src.tools.core import ToolDefinition, ToolParam


def _noop(**_kwargs):
    return "ok"


def test_gemini_schema_preserves_property_named_title():
    tool_def = ToolDefinition(
        name="create_item",
        description="Create an item.",
        function=_noop,
        parameters=[
            ToolParam(name="title", type="string", required=True),
            ToolParam(name="description", type="string", required=True),
        ],
    )

    declaration = GeminiAdapter.convert(tool_def)
    parameters = declaration["parameters"]

    assert "title" in parameters["properties"]
    assert set(parameters["required"]) == {"title", "description"}


def test_gemini_schema_removes_json_schema_title_metadata():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "title": "Title"},
            "payload": {
                "type": "object",
                "title": "Payload",
                "properties": {
                    "title": {"type": "string", "title": "Nested title"},
                },
            },
        },
        "required": ["title", "payload"],
    }

    cleaned = _clean_schema_for_gemini(schema)

    assert "title" in cleaned["properties"]
    assert "title" not in cleaned["properties"]["title"]
    assert "title" in cleaned["properties"]["payload"]["properties"]
    assert "title" not in cleaned["properties"]["payload"]
