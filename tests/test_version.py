import pytest

import enji_guard_radar


def test_version_flag_prints_name_and_version_then_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        enji_guard_radar.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("enji-guard-radar ")
