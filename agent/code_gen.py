"""
Code-generation utilities for Phase 5.

Responsibilities:
  - Parse LLM-generated output to extract FileChange objects
    (unified diffs for modifications, full content for new files)
  - Safely apply changes to disk (validates paths against registered project roots)
  - Pure-Python unified-diff applier as fallback when `patch` is unavailable
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------

@dataclass
class FileChange:
    action: str          # "create" | "modify"
    path: str            # relative path (as written in the LLM output)
    content: str = ""    # full file content  (action="create")
    diff: str = ""       # unified diff text  (action="modify")


# ------------------------------------------------------------------
# Parser — extract FileChange objects from LLM output
# ------------------------------------------------------------------

# Matches:  ### FILE: src/main/…/Foo.java [CREATE]
#       or  ### FILE: `src/…/Foo.java` [MODIFY]
_FILE_HEADER = re.compile(
    r"###\s+FILE:\s*[`\"]?([^\s`\"]+)[`\"]?\s*\[?(CREATE|MODIFY|create|modify)\]?",
    re.IGNORECASE,
)

# Matches a fenced code block: ```lang\n…\n```
_CODE_BLOCK = re.compile(r"```(\w*)\n([\s\S]*?)```", re.MULTILINE)

# Matches the +++ b/<path> line in a unified diff header
_DIFF_PATH = re.compile(r"^\+\+\+\s+(?:b/)?(.+)", re.MULTILINE)


def parse_file_changes(llm_output: str) -> list[FileChange]:
    """
    Extract FileChange objects from LLM output.

    Looks for structured ### FILE: <path> [CREATE|MODIFY] blocks first.
    Falls back to scanning bare ```diff blocks for the file path.
    """
    changes: list[FileChange] = []
    used_positions: set[int] = set()

    # Primary parser: ### FILE: ... [ACTION] followed by a code block
    for header_m in _FILE_HEADER.finditer(llm_output):
        path = header_m.group(1).strip()
        action = header_m.group(2).lower()
        search_start = header_m.end()

        # Find the next code block after this header
        block_m = _CODE_BLOCK.search(llm_output, search_start)
        if not block_m:
            continue

        used_positions.add(block_m.start())
        lang = block_m.group(1).lower()
        body = block_m.group(2)

        if action == "create":
            changes.append(FileChange(action="create", path=path, content=body))
        else:
            if lang == "diff":
                changes.append(FileChange(action="modify", path=path, diff=body))
            else:
                # Full-file replacement treated as create/overwrite
                changes.append(FileChange(action="create", path=path, content=body))

    # Fallback: bare ```diff blocks not preceded by a ### FILE: header
    for block_m in _CODE_BLOCK.finditer(llm_output):
        if block_m.start() in used_positions:
            continue
        if block_m.group(1).lower() != "diff":
            continue
        diff_text = block_m.group(2)
        path_m = _DIFF_PATH.search(diff_text)
        if path_m:
            path = path_m.group(1).strip()
            changes.append(FileChange(action="modify", path=path, diff=diff_text))

    return changes


# ------------------------------------------------------------------
# Apply — write changes to disk
# ------------------------------------------------------------------

def apply_change(
    change: FileChange,
    allowed_roots: list[Path],
    project_root: Path | None = None,
) -> tuple[bool, str]:
    """
    Apply one FileChange to disk.
    Returns (success, human_readable_message).
    All target paths are validated against allowed_roots before writing.
    """
    target = _resolve_path(change.path, allowed_roots, project_root)
    if target is None:
        return False, (
            f"Path '{change.path}' is not inside any registered project directory. "
            f"Registered roots: {[str(r) for r in allowed_roots]}"
        )

    if change.action == "create":
        return _write_file(target, change.content)

    if change.action == "modify":
        if not target.exists():
            return False, f"Cannot modify — file not found: {target}"
        return _apply_diff(target, change.diff, cwd=project_root or target.parent)

    return False, f"Unknown action: {change.action!r}"


def apply_raw_diff(
    diff_text: str,
    allowed_roots: list[Path],
    project_root: Path | None = None,
) -> tuple[bool, str, list[str]]:
    """
    Parse a raw unified diff string (may contain multiple file diffs) and apply all hunks.
    Returns (success, message, list_of_modified_paths).
    """
    # Split into per-file sections by 'diff --git …' or '--- a/…' headers
    file_diffs = _split_diff_by_file(diff_text)
    if not file_diffs:
        # Treat as a single-file diff
        path_m = _DIFF_PATH.search(diff_text)
        if path_m:
            file_diffs = {path_m.group(1).strip(): diff_text}
        else:
            return False, "Could not extract file path from diff", []

    modified: list[str] = []
    errors: list[str] = []
    for rel_path, single_diff in file_diffs.items():
        change = FileChange(action="modify", path=rel_path, diff=single_diff)
        ok, msg = apply_change(change, allowed_roots, project_root)
        if ok:
            modified.append(rel_path)
        else:
            errors.append(msg)

    if errors and not modified:
        return False, "; ".join(errors), []
    return True, f"Modified: {modified}" + (f"; errors: {errors}" if errors else ""), modified


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _resolve_path(
    rel_path: str,
    allowed_roots: list[Path],
    project_root: Path | None,
) -> Path | None:
    """
    Try every combination of allowed_roots × rel_path to find a valid target.
    Strips leading 'a/' or 'b/' prefixes produced by some diff tools.
    """
    clean = rel_path.lstrip("/")
    if clean.startswith(("a/", "b/")):
        clean = clean[2:]

    candidates: list[Path] = []
    if project_root:
        candidates.append(project_root / clean)

    for root in allowed_roots:
        candidates.append(root / clean)
        # If first segment of path matches a project name, strip it
        parts = Path(clean).parts
        if len(parts) > 1:
            candidates.append(root / Path(*parts[1:]))

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        for root in allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return resolved
            except ValueError:
                pass
    return None


def _write_file(path: Path, content: str) -> tuple[bool, str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        verb = "Overwrote" if path.exists() else "Created"
        return True, f"{verb}: {path}"
    except Exception as exc:
        return False, f"Failed to write {path}: {exc}"


def _apply_diff(file_path: Path, diff_text: str, cwd: Path) -> tuple[bool, str]:
    """Apply unified diff using `patch` subprocess, falling back to pure Python."""
    # 1. Try system `patch`
    try:
        result = subprocess.run(
            ["patch", "--forward", "-u", str(file_path)],
            input=diff_text.encode("utf-8"),
            capture_output=True,
            timeout=15,
            cwd=str(cwd),
        )
        if result.returncode == 0:
            return True, f"Applied diff to: {file_path}"
        stderr = result.stderr.decode()
        # If patch says already applied, treat as success
        if "already applied" in stderr.lower():
            return True, f"Diff already applied to: {file_path}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # patch not available, fall through

    # 2. Pure Python fallback
    try:
        original = file_path.read_text(encoding="utf-8")
        modified = _apply_unified_diff_python(original, diff_text)
        file_path.write_text(modified, encoding="utf-8")
        return True, f"Applied diff to: {file_path} (python applier)"
    except Exception as exc:
        return False, f"Failed to apply diff to {file_path}: {exc}"


def _apply_unified_diff_python(original: str, diff: str) -> str:
    """
    Minimal pure-Python unified diff applier.
    Handles the common case produced by LLMs: context + additions + deletions.
    """
    hunk_re = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    result = original.splitlines(keepends=True)

    # Collect hunks
    hunks: list[dict] = []
    current: dict | None = None
    for line in diff.splitlines(keepends=True):
        m = hunk_re.match(line)
        if m:
            if current:
                hunks.append(current)
            current = {
                "old_start": int(m.group(1)) - 1,   # 0-indexed
                "old_count": int(m.group(2) or 1),
                "lines": [],
            }
        elif current is not None and not line.startswith(("--- ", "+++ ")):
            if not line.startswith("\\"):   # ignore "No newline at end of file"
                current["lines"].append(line)
    if current:
        hunks.append(current)

    # Apply in reverse order to preserve 0-indexed positions
    for hunk in reversed(hunks):
        hunk_lines = hunk["lines"]
        old_start = hunk["old_start"]
        old_count = hunk["old_count"]

        new_block: list[str] = []
        for line in hunk_lines:
            if line.startswith("+"):
                new_block.append(line[1:])
            elif line.startswith("-"):
                pass  # removed
            else:
                new_block.append(line[1:] if line.startswith(" ") else line)

        result[old_start: old_start + old_count] = new_block

    return "".join(result)


def _split_diff_by_file(diff_text: str) -> dict[str, str]:
    """Split a multi-file patch into {relative_path: single_file_diff}."""
    # Detect diff --git headers (git format)
    git_split = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    if len(git_split) > 1:
        result: dict[str, str] = {}
        for chunk in git_split:
            if not chunk.strip():
                continue
            m = _DIFF_PATH.search(chunk)
            if m:
                result[m.group(1).strip()] = chunk
        return result

    # Classic unified diff: sections start at "--- "
    sections = re.split(r"(?=^--- )", diff_text, flags=re.MULTILINE)
    result = {}
    for chunk in sections:
        if not chunk.strip():
            continue
        m = _DIFF_PATH.search(chunk)
        if m:
            result[m.group(1).strip()] = chunk
    return result
