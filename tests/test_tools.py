"""toolsモジュールのテスト（外部API不要）"""
import math
import pytest


class TestCalculationTools:
    def test_basic_arithmetic(self):
        from src.tools.basic.calculation_tools import calculate_impl

        assert "14" in calculate_impl("2 + 3 * 4")
        assert "5" in calculate_impl("10 / 2")
        assert "7" in calculate_impl("3 + 4")

    def test_math_functions(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("sin(pi/2)")
        assert "1" in result

        result = calculate_impl("sqrt(16)")
        assert "4" in result

        result = calculate_impl("log(e)")
        assert "1" in result

    def test_power_notation(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("2^3")
        assert "8" in result

    def test_unicode_operators(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("6×3")
        assert "18" in result

        result = calculate_impl("10÷2")
        assert "5" in result

    def test_implicit_multiplication(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("2pi")
        val = float(result.split("=")[-1].strip())
        assert abs(val - 2 * math.pi) < 0.01

    def test_zero_division(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("1/0")
        assert "ゼロで割る" in result

    def test_empty_expression(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("")
        assert "入力されていません" in result

    def test_dangerous_patterns_blocked(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("import os")
        assert "安全上の理由" in result

        result = calculate_impl("__builtins__")
        assert "安全上の理由" in result

        result = calculate_impl("exec('code')")
        assert "安全上の理由" in result

    def test_syntax_error(self):
        from src.tools.basic.calculation_tools import calculate_impl

        # Pythonでは "2 ++ 3" は有効（unary +）なので、本当の構文エラーを使う
        result = calculate_impl("2 +* 3")
        assert "エラー" in result

    def test_constants(self):
        from src.tools.basic.calculation_tools import calculate_impl

        result = calculate_impl("pi")
        val = float(result.split("=")[-1].strip())
        assert abs(val - math.pi) < 0.001

        result = calculate_impl("e")
        val = float(result.split("=")[-1].strip())
        assert abs(val - math.e) < 0.001


class TestTimeTools:
    def test_returns_string(self):
        from src.tools.basic.time_tools import get_current_time_impl

        result = get_current_time_impl()
        assert isinstance(result, str)

    def test_contains_digits(self):
        from src.tools.basic.time_tools import get_current_time_impl

        result = get_current_time_impl()
        assert any(c.isdigit() for c in result)

    def test_contains_year(self):
        from src.tools.basic.time_tools import get_current_time_impl

        result = get_current_time_impl()
        assert "年" in result or "202" in result


class TestGrokXSearchParsers:
    def test_parse_handles_basic(self):
        from src.tools.basic.grok_x_search import _parse_handles

        result = _parse_handles("@user1, @user2, user3")
        assert result == ["user1", "user2", "user3"]

    def test_parse_handles_fullwidth_at(self):
        from src.tools.basic.grok_x_search import _parse_handles

        result = _parse_handles("＠user1,＠user2")
        assert result == ["user1", "user2"]

    def test_parse_handles_empty(self):
        from src.tools.basic.grok_x_search import _parse_handles

        assert _parse_handles("") == []
        assert _parse_handles(None) == []

    def test_parse_handles_skip_empty_entries(self):
        from src.tools.basic.grok_x_search import _parse_handles

        result = _parse_handles("@a, , @b")
        assert result == ["a", "b"]

    def test_validate_iso_date_valid(self):
        from src.tools.basic.grok_x_search import _validate_iso_date

        assert _validate_iso_date("2026-02-12", "from_date") == "2026-02-12"

    def test_validate_iso_date_empty(self):
        from src.tools.basic.grok_x_search import _validate_iso_date

        assert _validate_iso_date("", "from_date") is None
        assert _validate_iso_date(None, "from_date") is None

    def test_validate_iso_date_invalid(self):
        from src.tools.basic.grok_x_search import _validate_iso_date

        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            _validate_iso_date("not-a-date", "from_date")

    def test_extract_text_from_response_basic(self):
        from src.tools.basic.grok_x_search import _extract_text_from_response

        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "text", "text": "Hello world"}],
                }
            ]
        }
        assert _extract_text_from_response(payload) == "Hello world"

    def test_extract_text_from_response_output_text(self):
        from src.tools.basic.grok_x_search import _extract_text_from_response

        payload = {
            "output": [],
            "output_text": "Fallback text",
        }
        assert _extract_text_from_response(payload) == "Fallback text"

    def test_extract_text_from_response_fallback_json(self):
        from src.tools.basic.grok_x_search import _extract_text_from_response

        payload = {"other_key": "value"}
        result = _extract_text_from_response(payload)
        assert "other_key" in result  # JSON表現

    def test_build_tool_config_basic(self):
        from src.tools.basic.grok_x_search import _build_tool_config

        cfg = _build_tool_config(
            allowed_handles=[],
            excluded_handles=[],
            from_date=None,
            to_date=None,
            enable_image_understanding=False,
            enable_video_understanding=False,
            max_results=10,
            freshness="auto",
            search_mode="auto",
            language="ja",
        )
        assert cfg["max_results"] == 10
        assert cfg["language"] == "ja"
        assert "allowed_x_handles" not in cfg
        assert "from_date" not in cfg

    def test_build_tool_config_with_options(self):
        from src.tools.basic.grok_x_search import _build_tool_config

        cfg = _build_tool_config(
            allowed_handles=["user1"],
            excluded_handles=[],
            from_date="2026-01-01",
            to_date="2026-02-01",
            enable_image_understanding=True,
            enable_video_understanding=True,
            max_results=5,
            freshness="day",
            search_mode="latest",
            language="en",
        )
        assert cfg["allowed_x_handles"] == ["user1"]
        assert cfg["from_date"] == "2026-01-01"
        assert cfg["to_date"] == "2026-02-01"
        assert cfg["enable_image_understanding"] is True
        assert cfg["enable_video_understanding"] is True


class TestFileSystem:
    def test_format_size_bytes(self):
        from src.tools.os_operations.file_system import FileSystem

        assert FileSystem._format_size(100) == "100 B"

    def test_format_size_kb(self):
        from src.tools.os_operations.file_system import FileSystem

        result = FileSystem._format_size(1536)
        assert "KB" in result

    def test_format_size_mb(self):
        from src.tools.os_operations.file_system import FileSystem

        result = FileSystem._format_size(1048576)
        assert "MB" in result

    def test_format_size_gb(self):
        from src.tools.os_operations.file_system import FileSystem

        result = FileSystem._format_size(1073741824)
        assert "GB" in result

    def test_format_size_tb(self):
        from src.tools.os_operations.file_system import FileSystem

        result = FileSystem._format_size(1099511627776)
        assert "TB" in result

    def test_format_size_zero(self):
        from src.tools.os_operations.file_system import FileSystem

        assert FileSystem._format_size(0) == "0 B"

    def test_validate_path_allowed(self, tmp_path):
        from src.tools.os_operations.file_system import FileSystem

        fs = FileSystem(allowed_paths=[str(tmp_path)])
        result = fs._validate_path(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_validate_path_denied(self, tmp_path):
        from src.tools.os_operations.file_system import FileSystem, FileSystemError

        fs = FileSystem(allowed_paths=[str(tmp_path / "subdir")])
        with pytest.raises(FileSystemError, match="outside allowed"):
            fs._validate_path(str(tmp_path / "other"))


class TestCommandExecutor:
    def test_dangerous_command_detection(self):
        from src.tools.os_operations.command_executor import CommandExecutor

        executor = CommandExecutor(enable_dangerous_check=True)
        assert executor._is_dangerous_command("rm -rf /") is True
        assert executor._is_dangerous_command("rm -rf *") is True
        assert executor._is_dangerous_command("format C:") is True
        assert executor._is_dangerous_command("ls -la") is False
        assert executor._is_dangerous_command("python script.py") is False

    def test_dangerous_check_disabled(self):
        from src.tools.os_operations.command_executor import CommandExecutor

        executor = CommandExecutor(enable_dangerous_check=False)
        assert executor._is_dangerous_command("rm -rf /") is False

    def test_command_result_dataclass(self):
        from src.tools.os_operations.command_executor import CommandResult

        result = CommandResult(success=True, stdout="output", return_code=0)
        assert result.success is True
        assert result.stdout == "output"
        assert result.timed_out is False
