#!/usr/bin/env python3
"""What did a curation attempt actually do? Deterministic, no model, no deps.

claude-code-action runs the curator with its output hidden ("full output hidden
for security") and prints only the final counters. On 2026-10-14 that was
"permission_denials_count": 4 of 13 turns, and on 2026-10-09 5 of 14 on the
attempt that exited 0 without a usable issue.json. Which tools were refused, and
with what input, lived only in $RUNNER_TEMP/claude-execution-output.json, and that
file died with the runner whenever the attempt passed. The repo is public, so the
raw transcript cannot simply be uploaded either.

This reads that log and keeps the part worth keeping: turns, cost, duration,
error state, each denied tool with a clipped input, and the curator's own final
message. The workflow prints it after every attempt (and adds it to the run's
summary page); check_issue.py files the same dict with a rejected attempt in
place of the raw log.

  curator_summary.py [--log PATH] [--label L] [--markdown]

Never exits non-zero: a missing summary must not cost the edition.
"""

import argparse
import json
import os
import sys

DEFAULT_LOG = os.path.join(os.environ.get("RUNNER_TEMP", "."), "claude-execution-output.json")
INPUT_CLIP = 300
RESULT_CLIP = 2000


def _messages(text: str) -> list:
    """The log as a list of SDK messages. The action writes a JSON array; a single
    object or JSON Lines are accepted too, so a format change degrades to a
    thinner summary instead of none."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return [m for m in out if isinstance(m, dict)]
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        # Some wrappers nest the stream under a key; take it if it is there.
        for key in ("messages", "events"):
            if isinstance(data.get(key), list):
                return [m for m in data[key] if isinstance(m, dict)]
        return [data]
    return []


def _denial(d) -> dict:
    if not isinstance(d, dict):
        return {"tool_name": str(d)[:80], "input": ""}
    raw = d.get("tool_input", d.get("input"))
    try:
        enc = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        enc = str(raw)
    return {"tool_name": str(d.get("tool_name") or d.get("name") or "?"),
            "input": enc[:INPUT_CLIP]}


def summarize(path: str = DEFAULT_LOG) -> dict | None:
    """Summary of one curator run, or None when the log is missing or unreadable.
    A log with no result message (an attempt killed by its timeout) still yields a
    dict, with the counters left None, because "it never finished" is the finding."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            msgs = _messages(f.read())
    except Exception:
        return None
    if not msgs:
        return None
    result = next((m for m in reversed(msgs) if m.get("type") == "result"), None)
    if result is None:
        turns = sum(1 for m in msgs if m.get("type") == "assistant") or None
        return {"num_turns": turns, "total_cost_usd": None, "duration_ms": None,
                "is_error": None, "subtype": "no result message",
                "permission_denials": [], "result": None}
    text = result.get("result")
    return {
        "num_turns": result.get("num_turns"),
        "total_cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "is_error": result.get("is_error"),
        "subtype": result.get("subtype"),
        "permission_denials": [_denial(d) for d in result.get("permission_denials") or []],
        "result": text[:RESULT_CLIP] if isinstance(text, str) else None,
    }


def _fmt_cost(v) -> str:
    return f"${v:.2f}" if isinstance(v, (int, float)) else "?"


def _fmt_secs(v) -> str:
    return f"{v / 1000:.0f}s" if isinstance(v, (int, float)) else "?"


def plain(label: str, s: dict | None) -> str:
    if s is None:
        return f"{label}: no execution log (the attempt never ran, or died before writing one)"
    lines = [f"{label}: {s['subtype']}, error={s['is_error']}, turns={s['num_turns']}, "
             f"{_fmt_secs(s['duration_ms'])}, cost={_fmt_cost(s['total_cost_usd'])}, "
             f"{len(s['permission_denials'])} permission denial(s)"]
    for d in s["permission_denials"]:
        lines.append(f"  denied {d['tool_name']}: {d['input']}")
    if s["result"]:
        lines.append("  final message: " + s["result"].replace("\n", " ")[:400])
    return "\n".join(lines)


def _code(s: str) -> str:
    # Inline code cannot hold a backtick or a newline; these are diagnostics, so
    # flattening them is fine.
    return "`" + s.replace("`", "'").replace("\n", " ").replace("|", "\\|") + "`"


def markdown(label: str, s: dict | None) -> str:
    if s is None:
        return f"### Curation {label}\n\nNo execution log: the attempt never ran, or died before writing one.\n"
    out = [f"### Curation {label}", "",
           "| result | error | turns | duration | cost | denials |",
           "|---|---|---|---|---|---|",
           f"| {s['subtype']} | {s['is_error']} | {s['num_turns']} | "
           f"{_fmt_secs(s['duration_ms'])} | {_fmt_cost(s['total_cost_usd'])} | "
           f"{len(s['permission_denials'])} |", ""]
    if s["permission_denials"]:
        out += ["| denied tool | input (clipped) |", "|---|---|"]
        out += [f"| {_code(d['tool_name'])} | {_code(d['input'])} |"
                for d in s["permission_denials"]]
        out.append("")
    if s["result"]:
        body = s["result"].replace("~~~", "~ ~ ~")
        out += ["<details><summary>Final message</summary>", "", "~~~", body, "~~~",
                "", "</details>", ""]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--label", default="attempt")
    ap.add_argument("--markdown", action="store_true",
                    help="also append the summary to $GITHUB_STEP_SUMMARY")
    a = ap.parse_args()
    s = summarize(a.log)
    print(plain(a.label, s))
    if a.markdown and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(markdown(a.label, s))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # argparse errors included: this step is diagnostics only.
        if e.code not in (0, None):
            print(f"curator_summary: exited with {e.code}; ignored")
    except Exception as e:
        print(f"curator_summary: could not summarize ({type(e).__name__}: {e})")
    sys.exit(0)
