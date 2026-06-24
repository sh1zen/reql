# Contributing to REQL

Thanks for wanting to help. REQL is a graph-native, storage-agnostic Python
memory engine for deterministic project and knowledge retrieval. Contributions
are welcome when they keep the core local, predictable, dependency-light, and
useful without mandatory LLM calls.

## Ground Rules

- Keep changes focused. One pull request should address one fix, feature, or
  documentation improvement.
- Preserve the deterministic core. LLMs may be optional adapters, but REQL must
  remain usable without API keys, network calls, accounts, telemetry, or hosted
  services.
- Avoid new runtime dependencies unless they are clearly justified. Prefer the
  standard library and existing project modules.
- Optional integrations must degrade gracefully when their dependencies are not
  installed.
- Do not leave mocks, toy implementations, placeholder modules, or TODO-only
  features in mergeable code.
- Preserve public APIs unless the change is necessary. If behavior changes,
  update docs and examples in the same PR.
- Add type hints where they clarify contracts.
- Add tests for important behavior, especially storage, query semantics,
  compilation, parsing, CLI behavior, and security boundaries.
- Never commit secrets, credentials, private datasets, local graph stores, build
  artifacts, or generated caches.

## Development Setup

From a checkout:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

Run the test suite before opening a PR:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

On Windows, if tests cannot create temporary lock files under the default temp
directory, point `TEMP` and `TMP` at a writable local directory before running
the suite.

## Pull Requests

1. Fork the repository and create a branch from `main`.
2. Make the smallest coherent change that solves the issue.
3. Add or update tests for the behavior you changed.
4. Update relevant docs in `README.md` or `docs/` when commands, configuration,
   output formats, or public APIs change.
5. Run the full unittest command above.
6. Open a pull request against `main` with a concise description of what changed,
   why it changed, and how you verified it.

Good pull requests are easy to review: clear scope, passing tests, no unrelated
formatting churn, and no generated files unless they are intentional release or
documentation artifacts.

## Good First Contributions

- Bug fixes with a regression test.
- Documentation clarifications.
- CLI help or error-message improvements.
- Parser coverage for existing supported behavior.
- Small query, reporting, or retrieval quality improvements.
- Graceful handling for missing optional dependencies.

For larger work such as new storage backends, query-language extensions,
significant parser changes, or optional semantic extraction adapters, open an
issue first so the design can be discussed before implementation.

## Reporting Bugs

Open an issue at https://github.com/sh1zen/reql/issues and include:

- What you expected to happen.
- What actually happened.
- Steps to reproduce the problem.
- Your Python version and operating system.
- The exact REQL command or API call involved.
- Relevant traceback, log output, or minimal input files when available.

Please avoid attaching private graph stores or confidential project files. If
the issue depends on sensitive data, reduce it to a minimal public reproduction.

## Requesting Features

Feature requests are most useful when they describe the workflow, not only the
implementation idea. Include:

- The problem you are trying to solve.
- How you use REQL today.
- The command, API, or integration point where the feature would fit.
- Any constraints around determinism, dependencies, storage, or offline use.

## Release Expectations

Release tags use semantic version tags in the form `vMAJOR.MINOR.PATCH`, for
example `v0.3.1`. Publishing workflows are configured to reject other tag
formats.

## Communication

Use GitHub issues for bugs, proposals, and questions about whether a larger
change is worth building. Small focused PRs are welcome directly; larger design
changes should start with an issue.
