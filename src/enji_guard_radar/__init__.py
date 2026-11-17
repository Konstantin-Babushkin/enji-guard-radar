"""Enji Guard Radar: poll Guard, triage with Claude, alert Telegram."""

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    # Imported here so that importing the package stays light and free of shell code.
    from enji_guard_radar.cli import main as cli_main

    return cli_main(argv)
