#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
"""cchygiene — Health check for Claude Code context assets.

Scans Claude Code session logs (~/.claude/projects/*/*.jsonl) and the on-disk
context files (CLAUDE.md, MEMORY.md, memory/, skills/, agents/, commands/)
to surface:

  Stale         — old, not modified, not read recently        (delete candidate)
  Unread rule   — marked important but Claude isn't reading it (rule not firing)
  Active        — being read in the recent window              (healthy)
  New           — created recently, evaluation pending
  Quiet         — neither important nor stale, just unread

Read-only: never modifies any Claude Code file or setting.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"

IMPORTANCE_KEYWORDS = (
    "CRITICAL", "⚠️", "MUST", "IMPORTANT",
    "必ず", "禁止", "DO NOT", "NEVER",
)


@dataclass
class Asset:
    path: Path
    mtime: datetime
    size: int
    lines: int
    importance_score: int = 0
    importance_reasons: list[str] = field(default_factory=list)
    read_count_total: int = 0
    read_count_window: int = 0
    last_read: datetime | None = None
    category: str = ""

    @property
    def display(self) -> str:
        try:
            return "~/" + str(self.path.relative_to(Path.home()))
        except ValueError:
            return str(self.path)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Session log parsing ──────────────────────────────────────────────────────

def iter_session_files() -> Iterator[Path]:
    if not PROJECTS_DIR.is_dir():
        return
    for proj in PROJECTS_DIR.iterdir():
        if proj.is_dir():
            yield from proj.glob("*.jsonl")


def iter_session_records(path: Path) -> Iterator[dict]:
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def scan_sessions() -> tuple[dict[str, list[datetime]], set[str], set[str], int]:
    """Return (reads_by_path, cwds_seen, versions_seen, session_count)."""
    reads: dict[str, list[datetime]] = defaultdict(list)
    cwds: set[str] = set()
    versions: set[str] = set()
    n_sessions = 0
    for sf in iter_session_files():
        n_sessions += 1
        for rec in iter_session_records(sf):
            v = rec.get("version")
            if v:
                versions.add(str(v))
            cwd = rec.get("cwd")
            if cwd:
                cwds.add(cwd)
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            ts = parse_ts(rec.get("timestamp"))
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") != "tool_use" or blk.get("name") != "Read":
                    continue
                fp = (blk.get("input") or {}).get("file_path")
                if fp:
                    reads[fp].append(ts)
    return reads, cwds, versions, n_sessions


# ─── Asset discovery ──────────────────────────────────────────────────────────

GLOBAL_GLOBS = (
    "CLAUDE.md",
    "skills/**/*.md",
    "agents/**/*.md",
    "commands/**/*.md",
)
PROJECT_GLOBS = (
    "CLAUDE.md",
    "MEMORY.md",
    "memory/*.md",
)
WORKSPACE_GLOBS = (
    "CLAUDE.md",
    ".claude/**/*.md",
    ".claude/**/*.json",
)

# Path segments we never treat as user-authored context.
# These live under ~/.claude/ but are machine state, marketplace caches,
# or already covered by PROJECT_GLOBS (the projects/ tree).
EXCLUDED_SEGMENTS = (
    "/.claude/plugins/",
    "/.claude/cache/",
    "/.claude/backups/",
    "/.claude/file-history/",
    "/.claude/downloads/",
    "/.claude/session-env/",
    "/.claude/shell-snapshots/",
    "/.claude/sessions/",
    "/.claude/projects/",   # scanned separately via PROJECT_GLOBS
    "/.claude/ide/",
)


def is_excluded(path: Path) -> bool:
    s = str(path)
    return any(seg in s for seg in EXCLUDED_SEGMENTS)


def discover_assets(cwds: Iterable[str]) -> list[Path]:
    found: set[Path] = set()

    for pat in GLOBAL_GLOBS:
        for f in CLAUDE_HOME.glob(pat):
            if f.is_file() and not is_excluded(f):
                found.add(f)

    if PROJECTS_DIR.is_dir():
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for pat in PROJECT_GLOBS:
                for f in proj.glob(pat):
                    if f.is_file():
                        found.add(f)

    for cwd_str in cwds:
        cwd = Path(cwd_str)
        if not cwd.is_dir():
            continue
        for pat in WORKSPACE_GLOBS:
            for f in cwd.glob(pat):
                if f.is_file() and not is_excluded(f):
                    found.add(f)

    return sorted(found)


# ─── Importance heuristics ────────────────────────────────────────────────────

def score_importance(path: Path, text: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    name = path.name.lower()

    if name.startswith("feedback_") or "feedback-" in name:
        score += 2
        reasons.append("feedback rule (filename)")
    if "critical" in name or "must" in name or "rule" in name:
        score += 2
        reasons.append("criticality in filename")

    hits = [k for k in IMPORTANCE_KEYWORDS if k in text]
    if hits:
        score += 1
        reasons.append("importance markers: " + ", ".join(hits[:3]))

    if text.count("\n") >= 30:
        score += 1
        reasons.append("substantial (30+ lines)")

    return score, reasons


# ─── Asset metadata + categorisation ──────────────────────────────────────────

def build_asset(path: Path, reads_by_path: dict[str, list[datetime]],
                window_days: int) -> Asset:
    stat = path.stat()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    lines = text.count("\n") + (0 if not text or text.endswith("\n") else 1)
    score, reasons = score_importance(path, text)

    # Match Read calls by both the raw key and resolved-absolute string,
    # because tools sometimes log either form.
    keys = {str(path), str(path.resolve())}
    timestamps = [t for k in keys for t in reads_by_path.get(k, []) if t]

    cutoff = now_utc() - timedelta(days=window_days)
    in_window = [t for t in timestamps if t >= cutoff]

    return Asset(
        path=path,
        mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        size=stat.st_size,
        lines=lines,
        importance_score=score,
        importance_reasons=reasons,
        read_count_total=len(timestamps),
        read_count_window=len(in_window),
        last_read=max(timestamps, default=None),
    )


def categorise(a: Asset, *, stale_days: int) -> str:
    age_days = (now_utc() - a.mtime).days
    if age_days <= 14:
        return "new"
    if a.read_count_window >= 1:
        return "active"
    if a.importance_score >= 2:
        return "unread_rule"
    if age_days >= stale_days:
        return "stale"
    return "quiet"


CATEGORY_DISPLAY = {
    "unread_rule": ("🟡", "Important-but-unread — rule may not be firing"),
    "stale":       ("🔴", "Stale — old & unread; delete candidate"),
    "quiet":       ("⚫", "Quiet — unread but not important"),
    "active":      ("🟢", "Active — read in window"),
    "new":         ("⚪", "New — created within 14 days"),
}
CATEGORY_ORDER = ("unread_rule", "stale", "quiet", "active", "new")


# ─── Reporting ────────────────────────────────────────────────────────────────

def fmt_markdown(assets: list[Asset], *, sessions: int, versions: set[str],
                 cwds: int, window: int, stale_days: int) -> str:
    by_cat: dict[str, list[Asset]] = defaultdict(list)
    for a in assets:
        by_cat[a.category].append(a)

    lines = [
        f"# cchygiene report — {now_utc():%Y-%m-%d %H:%M UTC}",
        "",
        "_Read-only audit; nothing was modified._",
        "",
        "## Scan summary",
        f"- Sessions scanned: **{sessions}**",
        f"- Workspaces seen (via session `cwd`): **{cwds}**",
        f"- Assets discovered: **{len(assets)}**",
        f"- Window for 'recent read': **{window} days**",
        f"- Stale threshold: **{stale_days} days** without modification",
        f"- Claude Code session-log versions seen: {', '.join(sorted(versions)) or 'n/a'}",
        "",
        "## Counts by category",
    ]
    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat, [])
        emoji, label = CATEGORY_DISPLAY[cat]
        lines.append(f"- {emoji} **{cat}** ({label}): {len(items)}")
    lines.append("")

    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat, [])
        if not items:
            continue
        emoji, label = CATEGORY_DISPLAY[cat]
        lines.append(f"## {emoji} {label} ({len(items)})")
        lines.append("")
        lines.append("| File | mtime | lines | importance | reads (total / window) | last read |")
        lines.append("|---|---|---:|---|---:|---|")
        for a in sorted(items, key=lambda x: (-x.importance_score, x.mtime)):
            last = a.last_read.strftime("%Y-%m-%d") if a.last_read else "—"
            why = "; ".join(a.importance_reasons) if a.importance_reasons else "—"
            lines.append(
                f"| `{a.display}` "
                f"| {a.mtime:%Y-%m-%d} "
                f"| {a.lines} "
                f"| {a.importance_score} ({why}) "
                f"| {a.read_count_total} / {a.read_count_window} "
                f"| {last} |"
            )
        lines.append("")
    return "\n".join(lines)


def fmt_json(assets: list[Asset], **meta) -> str:
    payload = {
        "meta": meta,
        "generated_at": now_utc().isoformat(),
        "assets": [
            {
                "path": str(a.path),
                "category": a.category,
                "mtime": a.mtime.isoformat(),
                "size": a.size,
                "lines": a.lines,
                "importance_score": a.importance_score,
                "importance_reasons": a.importance_reasons,
                "read_count_total": a.read_count_total,
                "read_count_window": a.read_count_window,
                "last_read": a.last_read.isoformat() if a.last_read else None,
            }
            for a in assets
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--window", type=int, default=30,
                   help="Days considered 'recent' for read history (default: 30)")
    p.add_argument("--stale-days", type=int, default=90,
                   help="Days w/o mtime change to flag as stale (default: 90)")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--output", "-o", help="Write report to this path (default: stdout)")
    args = p.parse_args(argv)

    if not PROJECTS_DIR.is_dir():
        print(f"error: {PROJECTS_DIR} not found — is Claude Code installed?",
              file=sys.stderr)
        return 1

    reads, cwds, versions, n_sessions = scan_sessions()
    files = discover_assets(cwds)
    assets = [build_asset(f, reads, args.window) for f in files]
    for a in assets:
        a.category = categorise(a, stale_days=args.stale_days)

    meta = dict(sessions=n_sessions, versions=versions, cwds=len(cwds),
                window=args.window, stale_days=args.stale_days)

    if args.format == "json":
        out = fmt_json(assets, **{k: (sorted(v) if isinstance(v, set) else v)
                                  for k, v in meta.items()})
    else:
        out = fmt_markdown(assets, **meta)

    if args.output:
        Path(args.output).write_text(out)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
