from src.service_manager import (
    _extract_port_from_netstat_address,
    _is_port_open,
    _is_selected_local_model_running,
    _listening_pid_for_netstat_line,
    _service_log_dir,
)


def test_extract_port_from_windows_netstat_address():
    assert _extract_port_from_netstat_address("0.0.0.0:3000") == 3000
    assert _extract_port_from_netstat_address("[::]:6002") == 6002
    assert _extract_port_from_netstat_address("127.0.0.1:59407") == 59407
    assert _extract_port_from_netstat_address("invalid") is None


def test_windows_netstat_port_match_is_exact():
    assert (
        _listening_pid_for_netstat_line(
            "  TCP    0.0.0.0:3000           0.0.0.0:0              LISTENING       1111",
            3000,
        )
        == "1111"
    )
    assert (
        _listening_pid_for_netstat_line(
            "  TCP    0.0.0.0:30000          0.0.0.0:0              LISTENING       2222",
            3000,
        )
        is None
    )
    assert (
        _listening_pid_for_netstat_line(
            "  TCP    [::]:6002              [::]:0                 LISTENING       3333",
            6002,
        )
        == "3333"
    )
    assert (
        _listening_pid_for_netstat_line(
            "  TCP    127.0.0.1:3000         127.0.0.1:59407        ESTABLISHED     4444",
            3000,
        )
        is None
    )


def test_is_port_open_returns_false_for_unused_port():
    assert _is_port_open("127.0.0.1", 9, timeout_seconds=0.1) is False


def test_selected_local_model_running_requires_expected_model_id():
    assert _is_selected_local_model_running({"qwopus3.6-35b-a3b"}, {"qwopus3.6-35b-a3b"}) is True
    assert _is_selected_local_model_running({"luce-dflash"}, {"qwopus3.6-35b-a3b"}) is False


def test_service_log_dir_is_under_logs_services(tmp_path):
    log_dir = _service_log_dir(tmp_path)

    assert log_dir == tmp_path / "logs" / "services"
    assert log_dir.is_dir()
