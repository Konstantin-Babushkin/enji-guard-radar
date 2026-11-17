"""Replays recorded Guard responses from a directory. Reads files only: no network, no writes."""

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from enji_guard_radar.ports import GuardCallError

logger = logging.getLogger(__name__)

MANIFEST_FILE = "calls.json"


class FixtureCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    args: dict[str, str] = Field(default_factory=dict)
    response: str | None = None
    error: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Self:
        if (self.response is None) == (self.error is None):
            message = "exactly one of response or error is required"
            raise ValueError(message)
        return self


class FixtureManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: tuple[FixtureCall, ...]


class FixtureGuardTools:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> FixtureManifest:
        path = self._directory / MANIFEST_FILE
        try:
            return FixtureManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            logger.error("fixture manifest %s cannot be read", path)  # noqa: TRY400
            message = f"fixture manifest {MANIFEST_FILE} is missing or unreadable"
            raise GuardCallError(message) from None
        except ValidationError:
            logger.error("fixture manifest %s is invalid", path)  # noqa: TRY400
            message = f"fixture manifest {MANIFEST_FILE} is invalid"
            raise GuardCallError(message) from None

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        wanted = dict(args)
        for entry in self._manifest.calls:
            if entry.tool == tool and entry.args == wanted:
                if entry.error is not None:
                    raise GuardCallError(entry.error)
                if entry.response is None:  # unreachable: the validator guarantees one of the two
                    message = f"no recorded response for {tool}"
                    raise GuardCallError(message)
                return self._read_response(entry.response)
        message = f"no recorded response for {tool}"
        raise GuardCallError(message)

    def _read_response(self, name: str) -> Mapping[str, object]:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            message = "fixture response path escapes the fixture directory"
            raise GuardCallError(message)
        path = self._directory / relative
        if not path.resolve().is_relative_to(self._directory.resolve()):
            message = "fixture response path escapes the fixture directory"
            raise GuardCallError(message)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            message = f"fixture response {name} cannot be read"
            raise GuardCallError(message) from None
        except json.JSONDecodeError:
            message = f"fixture response {name} is not valid JSON"
            raise GuardCallError(message) from None
        if not isinstance(payload, dict):
            message = f"fixture response {name} is not a JSON object"
            raise GuardCallError(message)
        return payload
