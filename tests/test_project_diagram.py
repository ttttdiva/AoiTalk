"""project_diagram のMermaid生成テスト。"""

from src.services.project_diagram import (
    build_record_table_diagram,
    build_system_diagram,
    build_wbs_diagram,
    mermaid_escape,
    table_kind,
)


def _field(key, label, field_type="text", is_title=False):
    return {"key": key, "label": label, "field_type": field_type, "is_title": is_title}


class TestMermaidEscape:
    def test_quotes_and_brackets(self):
        assert mermaid_escape('A "B" [C]') == "A 'B' (C)"

    def test_empty(self):
        assert mermaid_escape(None) == ""

    def test_long_label_truncated(self):
        result = mermaid_escape("x" * 100)
        assert len(result) == 60
        assert result.endswith("…")


class TestTableKind:
    def test_connection(self):
        assert table_kind("接続一覧") == "connection"

    def test_device(self):
        assert table_kind("機器一覧") == "device"

    def test_other(self):
        assert table_kind("課題管理表") == ""


class TestWbsDiagram:
    def test_empty_tasks(self):
        assert build_wbs_diagram("P", []) is None

    def test_hierarchy_and_status(self):
        tasks = [
            {"id": "a", "title": "親", "status": "in_progress", "parent_task_id": None},
            {"id": "b", "title": "子", "status": "done", "parent_task_id": "a"},
            {"id": "c", "title": "孤児", "status": "todo", "parent_task_id": "zzz"},
        ]
        mermaid = build_wbs_diagram("案件X", tasks)
        assert mermaid.startswith("graph TD")
        assert 'root(["案件X"])' in mermaid
        assert 't0["親"]' in mermaid
        assert "t0 --> t1" in mermaid
        assert "root --> t0" in mermaid
        assert "root --> t2" in mermaid
        assert "class t1 done" in mermaid
        assert "class t0 wip" in mermaid

    def test_max_nodes_truncation(self):
        tasks = [
            {"id": str(i), "title": f"T{i}", "status": "todo", "parent_task_id": None}
            for i in range(10)
        ]
        mermaid = build_wbs_diagram("P", tasks, max_nodes=3)
        assert "先頭 3 件のみ表示" in mermaid
        assert 't3["T3"]' not in mermaid


class TestRecordTableDiagram:
    def test_empty_rows(self):
        assert build_record_table_diagram("接続一覧", [], []) is None

    def test_connection_edges(self):
        fields = [
            _field("src", "接続元"),
            _field("dst", "接続先"),
            _field("usage", "用途"),
        ]
        rows = [
            {"title": "", "values": {"src": "FW01", "dst": "SW01", "usage": "管理"}},
            {"title": "", "values": {"src": "SW01", "dst": "SV01", "usage": ""}},
            {"title": "", "values": {"src": "", "dst": "SV02"}},
        ]
        mermaid = build_record_table_diagram("接続一覧", fields, rows)
        assert mermaid.startswith("graph LR")
        assert 'n0["FW01"]' in mermaid
        assert "n0 -->|管理| n1" in mermaid
        assert "n1 --> n2" in mermaid

    def test_grouped_fallback(self):
        fields = [
            _field("name", "機器名", is_title=True),
            _field("loc", "拠点", field_type="select"),
        ]
        rows = [
            {"title": "FW01", "values": {"name": "FW01", "loc": "本社"}},
            {"title": "SW01", "values": {"name": "SW01", "loc": "本社"}},
            {"title": "SV01", "values": {"name": "SV01", "loc": "DC"}},
        ]
        mermaid = build_record_table_diagram("機器一覧", fields, rows)
        assert mermaid.startswith("graph TB")
        assert 'subgraph g0["本社"]' in mermaid
        assert 'subgraph g1["DC"]' in mermaid
        assert '"SV01"' in mermaid

    def test_row_label_falls_back_to_first_value(self):
        fields = [_field("memo", "メモ")]
        rows = [{"title": "", "values": {"memo": "値A"}}]
        mermaid = build_record_table_diagram("一覧", fields, rows)
        assert '"値A"' in mermaid


class TestSystemDiagram:
    def test_no_relevant_tables(self):
        tables = [{"name": "課題管理表", "fields": [], "rows": [{"title": "x", "values": {}}]}]
        assert build_system_diagram("P", tables) is None

    def test_combined_device_and_connection(self):
        tables = [
            {
                "name": "機器一覧",
                "fields": [
                    _field("name", "機器名", is_title=True),
                    _field("loc", "拠点", field_type="select"),
                ],
                "rows": [
                    {"title": "FW01", "values": {"name": "FW01", "loc": "本社"}},
                    {"title": "SW01", "values": {"name": "SW01", "loc": "本社"}},
                ],
            },
            {
                "name": "接続一覧",
                "fields": [_field("src", "接続元"), _field("dst", "接続先")],
                "rows": [
                    {"title": "", "values": {"src": "FW01", "dst": "SW01"}},
                    {"title": "", "values": {"src": "SW01", "dst": "SV01"}},
                ],
            },
        ]
        mermaid = build_system_diagram("案件X", tables)
        assert mermaid.startswith("graph LR")
        assert "%% 案件X 構成図" in mermaid
        # 機器一覧で定義済みノードは再定義されず、subgraph内の定義が使われる
        assert mermaid.count('["FW01"]') == 1
        assert 'subgraph d0_0["本社"]' in mermaid
        # 接続一覧だけに出るノードはトップレベルで定義される
        assert '["SV01"]' in mermaid
        assert "-->" in mermaid

    def test_connection_only(self):
        tables = [
            {
                "name": "connection list",
                "fields": [_field("from", "From"), _field("to", "To")],
                "rows": [{"title": "", "values": {"from": "A", "to": "B"}}],
            }
        ]
        mermaid = build_system_diagram("P", tables)
        assert "n0 --> n1" in mermaid
