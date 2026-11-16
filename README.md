<img src="assets/icon.svg" width="64" height="64" alt="">

# enji-guard-radar

Polls [Enji Guard](https://enji.ai) project data, flags problems with
deterministic rules, has Claude triage them, and alerts a Telegram group.

Status: design stage — see `docs/design/v0.md`.

## Develop

```
make install   # uv sync
make verify    # lint + format + mypy + markers + tests
```

Developed with code-factory. Agents: start at `AGENTS.md`.
