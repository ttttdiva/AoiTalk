from src.tools.basic.time_tools import get_current_time_impl
from src.tools.basic.calculation_tools import calculate_impl


def test_get_current_time_returns_string():
    result = get_current_time_impl()
    assert isinstance(result, str)
    assert any(char.isdigit() for char in result)


def test_calculate_impl_simple_expression():
    result = calculate_impl("2 + 3 * 4")
    assert "=" in result
    value = float(result.split("=")[-1].strip())
    assert value == 14
