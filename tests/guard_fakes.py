"""Test doubles for the Guard tool boundary (no network, no files)."""

from collections.abc import Mapping

from enji_guard_radar.ports import GuardCallError

Key = tuple[str, tuple[tuple[str, str], ...]]


def call_key(tool: str, **args: str) -> Key:
    """The lookup key of one call: the tool plus its arguments sorted by name."""
    return tool, tuple(sorted(args.items()))


class InMemoryGuardTools:
    """Answers from a dict and records every request, answered or not."""

    def __init__(self, responses: Mapping[Key, Mapping[str, object] | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((tool, dict(args)))
        key = call_key(tool, **args)
        if key not in self._responses:
            message = f"no recorded response for {tool}"
            raise GuardCallError(message)
        response = self._responses[key]
        if isinstance(response, Exception):
            raise response
        return response
