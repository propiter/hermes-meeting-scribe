# Contributing

Thanks for helping! Keep changes small and focused.

1. **Read [docs/DESIGN.md](docs/DESIGN.md) first.** It records the architecture (hexagonal:
   `domain/` imports nothing from third parties or Hermes) and the decisions behind it.
2. **Test first.** Write a failing test, make it pass, then refactor. Every bug fix needs a
   regression test.
3. **Run the checks** before opening a pull request:

   ```bash
   .venv/bin/python -m pytest -q            # unit tests
   scripts/test-integration.sh -q           # against a Hermes checkout (HERMES_SRC=...)
   hermes plugins validate .
   ```

4. **Settings:** edit `meeting_scribe/config.py`, then run `.venv/bin/python scripts/gen_manifest.py`
   and update the configuration tables in both `README.md` and `README.es.md`. Tests fail if any of
   them drift.
5. **User-facing strings** go in `meeting_scribe/i18n/en.json` **and** `es.json`.
6. **Commits:** use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
   `docs:`, `test:`, `refactor:`, `ci:`). One reviewable unit per commit.
7. **Dependencies:** add as few as possible, and every one needs an upper bound
   (`>=floor,<next_major`).

Bug reports are most useful with the output of `hermes meeting-scribe doctor --json` and
`hermes meeting-scribe status --json`. Remove ids and paths you don't want to share first.
