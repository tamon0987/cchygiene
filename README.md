# cchygiene

Health check for [Claude Code](https://docs.claude.com/en/docs/claude-code) **context assets** — the `CLAUDE.md`, memory files, skills, agents, and rules that shape how Claude behaves in your projects.

Inspired by [`ccusage`](https://github.com/ryoppippi/ccusage): a single, dependency-free CLI that reads Claude Code's local logs and never touches your setup.

## What problem does it solve?

As you accumulate rules in `~/.claude/CLAUDE.md`, per-project `MEMORY.md` indexes, custom skills, and feedback memories, two failure modes appear:

1. **Important rules go unread** — you wrote "always alert before `rm -rf`" but Claude never opened the file that explains why
2. **Old rules rot** — outdated instructions sit in the directory, polluting the context surface area

cchygiene scans your Claude Code session logs (`~/.claude/projects/*/*.jsonl`) and reports which context files are actually being read, which are stale, and which look important-but-ignored.

## Install

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/tamon0987/cchygiene.git
cd cchygiene
uv venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
uv pip install -e .
```

Then use `cchygiene` from any directory (while venv is activated):
```bash
cchygiene
cchygiene -o reports/$(date +%Y-%m-%d).md
```

## Usage

```bash
# Default: markdown report to stdout, 30-day read window, 90-day stale threshold
cchygiene

# Save report
cchygiene -o reports/$(date +%Y-%m-%d).md

# JSON output for further processing
cchygiene --format json | jq '.assets[] | select(.category=="unread_rule")'

# Custom thresholds
cchygiene --window 60 --stale-days 180
```

## Categories

| Emoji | Category | Meaning |
|---|---|---|
| 🟡 | `unread_rule` | Marked important (filename / keywords / size) but **not read in window** — your rule may not be firing |
| 🔴 | `stale` | `mtime` older than `--stale-days` AND not read in window — delete candidate |
| ⚫ | `quiet` | Unread but neither important nor old — informational |
| 🟢 | `active` | Read at least once in window — healthy |
| ⚪ | `new` | Modified within 14 days — evaluation pending |

## How importance is scored (heuristic)

- `+2` filename contains `feedback`, `critical`, `must`, or `rule`
- `+1` content contains any of: `CRITICAL`, `⚠️`, `MUST`, `IMPORTANT`, `必ず`, `禁止`, `DO NOT`, `NEVER`
- `+1` file is 30+ lines (substantial)

A score of 2+ qualifies as "important" for the `unread_rule` flag.

This is intentionally crude. If you want better, add explicit markers to your files (without requiring this tool to change them).

## What it reads

| Source | Used for |
|---|---|
| `~/.claude/projects/*/*.jsonl` | Read tool calls + `cwd` discovery + Claude Code version |
| `~/.claude/CLAUDE.md`, `skills/`, `agents/`, `commands/` | Global asset inventory |
| `~/.claude/projects/*/{CLAUDE.md,MEMORY.md,memory/*.md}` | Per-project memory inventory |
| `<discovered cwd>/CLAUDE.md`, `<cwd>/.claude/**` | Workspace-side rules |

**It does not modify anything.** No hooks, no settings edits, no frontmatter changes to your files.

## Caveats

- **Session log schema is internal to Claude Code.** It may change without notice. cchygiene tolerates unknown fields, but a major schema overhaul will require code updates. The report includes which Claude Code `version` strings were observed, so you can detect drift.
- **Auto-memory directories are per-project.** A file marked `unread_rule` in one workspace may simply be irrelevant there.
- **Heuristic importance scoring** can mis-label files. Treat the categories as starting points for review, not verdicts.

## License

MIT — see [LICENSE](LICENSE).
