# opencode-usage-v2

[![CI](https://github.com/fcomovaz/opencode-usage-v2-supp/actions/workflows/ci.yml/badge.svg)](https://github.com/fcomovaz/opencode-usage-v2-supp/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/opencode-usage-v2)](https://pypi.org/project/opencode-usage-v2/)
[![Python versions](https://img.shields.io/pypi/pyversions/opencode-usage-v2)](https://pypi.org/project/opencode-usage-v2/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Maintained fork:** `opencode-usage-v2` is a maintained fork of the deleted upstream `opencode-usage` repository (`rchardx/opencode-usage`), updated with full support for OpenCode v1, OpenCode v2, and migrated mixed SQLite schemas.

CLI tool to track and display [OpenCode](https://github.com/opencodeco/opencode) token usage statistics. Reads directly from OpenCode's local SQLite database — no API keys or external services needed for basic usage.

## Features

- **OpenCode v1 & v2 support** — reads legacy `message`/`session`, current `session_message`/`session_v2`, and migrated mixed databases without double-counting
- **Daily breakdown** — token usage and cost per day
- **Group by dimension** — model, agent, provider, or session
- **Agent × Model view** — see which model each agent uses
- **Time filtering** — last N days, relative durations (`7d`, `2w`), or ISO dates
- **Period comparison** — compare current vs previous period with `--compare`
- **JSON output** — pipe to `jq` or other tools
- **LLM-powered insights** — analyze session transcripts and generate a self-contained HTML report
- **Cross-platform** — macOS, Linux, Windows

## Installation

### From PyPI (Recommended)

Run directly without installing using `uvx`:

```bash
uvx opencode-usage-v2
uvx opencode-usage-v2 run --by model
```

Or install globally / into a virtual environment:

```bash
# With uv
uv tool install opencode-usage-v2

# With pip
pip install opencode-usage-v2
```

Installing `opencode-usage-v2` exposes both `opencode-usage-v2` and `opencode-usage` CLI commands (drop-in compatible).

> **Warning:** Do **not** install `opencode-usage-v2` into the same virtual environment as the legacy `opencode-usage` package. Both distributions provide the `opencode_usage` Python namespace and `opencode-usage` script, so co-installing them in one environment causes file conflicts. Uninstall `opencode-usage` first if upgrading in an existing environment.

### From source (local development)

```bash
# Clone repository
git clone https://github.com/fcomovaz/opencode-usage-v2-supp.git
cd opencode-usage-v2-supp

# Option 1: with standard Python venv & pip
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (Command Prompt):
.venv\Scripts\activate.bat
pip install -e .

# Option 2: with uv
uv sync
uv tool install -e .
```

## Usage

Both `opencode-usage-v2` and `opencode-usage` invoke the same CLI, which has two subcommands: `run` (default) and `insights`. You can also invoke it as `python -m opencode_usage`.

### `run` — Token usage statistics

```bash
# Default: last 7 days, daily breakdown
opencode-usage-v2

# Time filtering
opencode-usage-v2 run --days 30
opencode-usage-v2 run --since 7d
opencode-usage-v2 run --since 2025-01-01

# Group by dimension
opencode-usage-v2 run --by model
opencode-usage-v2 run --by agent          # shows model per agent
opencode-usage-v2 run --by provider
opencode-usage-v2 run --by session --limit 10

# JSON output
opencode-usage-v2 run --json
opencode-usage-v2 run --by model --json | jq '.rows[].label'

# Compare with previous period
opencode-usage-v2 run --since 7d --compare
```

### `insights` — LLM-powered analysis

Analyze your OpenCode sessions and generate an HTML report with workflow insights, friction patterns, agent performance, and actionable suggestions.

```bash
# Interactive model picker
opencode-usage-v2 insights

# Specific model
opencode-usage-v2 insights --model gpt-4o-mini

# Customize analysis
opencode-usage-v2 insights --days 30 --concurrency 4 --output report.html

# Force re-analysis (ignore cache)
opencode-usage-v2 insights --force
```

Requires an API key for the LLM provider — set via environment variable (e.g. `OPENAI_API_KEY`) or OpenCode's `auth.json`.

> **Note:** The `insights` extraction queries currently target the legacy v1 schema tables (`session`, `message`, `part`). Token usage reporting (`run`) supports both v1 and v2 schemas.

### Example output

```
╭──────────────── OpenCode Usage — Last 7 days ────────────────╮
│   Calls: 1,280  │  Tokens: 52.3M  │  Cost: $0.00             │
╰──────────────────────────────────────────────────────────────╯

               Usage by Agent (Last 7 days)
┏━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┓
┃ Agent                  ┃ Model        ┃ Calls ┃   Total ┃    Cost ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━┩
│ build                  │ deepseek-r1  │   420 │   18.2M │       - │
│                        │ qwen-3-coder │   105 │    4.7M │       - │
├────────────────────────┼──────────────┼───────┼─────────┼─────────┤
│ explore                │ gemma-3      │   310 │   12.5M │       - │
│                        │ minimax-m2.5 │   198 │    8.1M │       - │
├────────────────────────┼──────────────┼───────┼─────────┼─────────┤
│ librarian              │ llama-4      │   156 │    5.8M │       - │
├────────────────────────┼──────────────┼───────┼─────────┤
│ oracle                 │ qwen-3-coder │    91 │    3.0M │       - │
└────────────────────────┴──────────────┴───────┴─────────┴─────────┘
```

## Schema Support

`opencode-usage-v2` supports both OpenCode v1 and v2 database schemas, as well as migrated (mixed) databases:

| OpenCode Version | Session Table | Message Table | Status |
|---|---|---|---|
| **v1** (legacy) | `session` | `message` (`role = 'assistant'`) | Supported |
| **v2** (current) | `session_v2` | `session_message` (`type = 'assistant'`) | Supported |
| **Mixed / Migrated** | Both tables present | Both tables present | Supported (deduplicated by ID) |

In mixed databases migrated from v1 to v2, messages present in both tables are automatically deduplicated by message ID to prevent double-counting.

## Configuration

| Environment Variable | Description |
|---|---|
| `OPENCODE_DB` | Override database path (default: auto-detected per platform) |
| `NO_COLOR` | Disable colored output when set (see [no-color.org](https://no-color.org)) |
| `{PROVIDER}_API_KEY` | API key for insights LLM provider (e.g. `OPENAI_API_KEY`) |
| `{PROVIDER}_BASE_URL` | Base URL override for insights LLM provider |

Default database locations:

- **All platforms** (macOS, Linux, Windows): `~/.local/share/opencode/opencode.db`

## Development

```bash
git clone https://github.com/fcomovaz/opencode-usage-v2-supp.git
cd opencode-usage-v2-supp
uv sync

# Run tests
uv run pytest

# Lint & format
uvx ruff check .
uvx ruff format --check .

# Install pre-commit hooks
uvx pre-commit install
```

## License

[MIT](LICENSE)
