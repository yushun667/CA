"""Validate qizai structure, navigation, document budgets, and Git readiness."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
TOKEN_PATTERN = re.compile(r"\{\{[A-Z0-9_]+\}\}")
REQUIRED_PATHS = (
    "AGENTS.md",
    ".agents/AGENTS.md",
    ".agents/governance.json",
    ".agents/validate.py",
    ".agents/architecture/AGENTS.md",
    ".agents/architecture/overview.md",
    ".agents/planning/AGENTS.md",
    ".agents/planning/roadmap.md",
    ".agents/planning/current-phase.md",
    ".agents/planning/progress.md",
    ".agents/planning/archive/AGENTS.md",
    ".agents/issues/AGENTS.md",
    ".agents/issues/open/AGENTS.md",
    ".agents/issues/resolved/AGENTS.md",
    ".agents/skills/AGENTS.md",
    ".agents/standards/AGENTS.md",
    ".agents/standards/code-review.md",
    ".agents/standards/document-review.md",
    ".agents/standards/git.md",
)


def git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
    )


def nul_paths(result: subprocess.CompletedProcess[bytes], operation: str) -> list[str]:
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{operation} failed: {message}")
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def project_files(root: Path) -> list[str]:
    return nul_paths(
        git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"),
        "git file inventory",
    )


def load_config(root: Path) -> dict[str, object]:
    path = root / ".agents" / "governance.json"
    return json.loads(path.read_text(encoding="utf-8"))


def limit_for(relative: str, config: dict[str, object]) -> dict[str, int] | None:
    limits = config["document_limits"]
    if relative == "AGENTS.md":
        return limits["root_agents"]
    if relative == ".agents/AGENTS.md":
        return limits["agents_root"]
    if relative.endswith("/AGENTS.md"):
        return limits["directory_agents"]
    if relative.startswith(".agents/") and relative.endswith(".md"):
        return limits["managed_document"]
    return None


def check_required(root: Path, errors: list[str]) -> None:
    decision_paths = (
        ('.agents/notes/AGENTS.md',)
        if (root / '.agents/notes/AGENTS.md').is_file()
        else tuple(f'.agents/decisions/{part}AGENTS.md' for part in ('', 'proposed/', 'active/', 'rejected/', 'superseded/'))
    )
    for relative in REQUIRED_PATHS + decision_paths:
        if not (root / relative).is_file():
            errors.append(f"missing required file: {relative}")


def check_overrides(root: Path, files: list[str], errors: list[str]) -> None:
    checked: set[PurePosixPath] = {PurePosixPath()}
    checked.update(maintained_directories(files))
    for directory in sorted(checked, key=str):
        override = root / directory.as_posix() / "AGENTS.override.md"
        if override.exists():
            relative = override.relative_to(root)
            errors.append(f"AGENTS.override.md shadows directory guidance: {relative}")

    for relative in files:
        if PurePosixPath(relative).name == "AGENTS.override.md":
            message = f"AGENTS.override.md shadows directory guidance: {relative}"
            if message not in errors:
                errors.append(message)


def maintained_directories(files: list[str]) -> set[PurePosixPath]:
    directories: set[PurePosixPath] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            directories.add(parent)
            parent = parent.parent
    return directories


def check_directory_navigation(root: Path, files: list[str], errors: list[str]) -> None:
    directories = maintained_directories(files)
    for directory in sorted(directories, key=str):
        agents_relative = directory / "AGENTS.md"
        agents_path = root / agents_relative.as_posix()
        if not agents_path.is_file():
            errors.append(f"maintained directory lacks AGENTS.md: {directory}")
            continue

        parent = directory.parent
        parent_agents = root / (
            "AGENTS.md" if str(parent) == "." else (parent / "AGENTS.md").as_posix()
        )
        if not parent_agents.is_file():
            continue
        expected_link = f"{directory.name}/AGENTS.md"
        parent_text = parent_agents.read_text(encoding="utf-8")
        if expected_link not in parent_text:
            errors.append(
                f"parent index {parent_agents.relative_to(root)} does not link {expected_link}"
            )

    for relative in files:
        pure = PurePosixPath(relative)
        if pure.name == 'CLAUDE.md' and (root / relative).is_symlink() and (root / relative).readlink() == Path('AGENTS.md'):
            continue
        if not str(pure).startswith(".agents/") or pure.name == "AGENTS.md":
            continue
        local_agents = root / pure.parent.as_posix() / "AGENTS.md"
        if local_agents.is_file() and pure.name not in local_agents.read_text(
            encoding="utf-8"
        ):
            errors.append(
                f"management file is absent from its directory index: {relative}"
            )


def check_documents(
    root: Path, files: list[str], config: dict[str, object], errors: list[str]
) -> None:
    candidates = set(files)
    candidates.add("AGENTS.md")
    for relative in sorted(candidates):
        limits = limit_for(relative, config)
        if limits is None:
            continue
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        lines = len(text.splitlines())
        chars = len(text)
        if lines > limits["max_lines"]:
            errors.append(
                f"document exceeds {limits['max_lines']} lines: {relative} ({lines})"
            )
        if chars > limits["max_chars"]:
            errors.append(
                f"document exceeds {limits['max_chars']} chars: {relative} ({chars})"
            )
        tokens = sorted(set(TOKEN_PATTERN.findall(text)))
        if tokens:
            errors.append(
                f"unresolved template token in {relative}: {', '.join(tokens)}"
            )
        frozen = relative.startswith(('.agents/notes/archived/', '.agents/decisions/superseded/'))
        if not frozen or path.name == 'AGENTS.md':
            check_links(root, path, text, errors)


def agents_chain(root: Path, directory: PurePosixPath) -> list[Path]:
    chain = [root / "AGENTS.md"]
    cursor = PurePosixPath()
    if str(directory) == ".":
        return chain
    for part in directory.parts:
        cursor = cursor / part
        candidate = root / cursor.as_posix() / "AGENTS.md"
        if candidate.is_file():
            chain.append(candidate)
    return chain


def check_context_budgets(
    root: Path, files: list[str], config: dict[str, object], errors: list[str]
) -> None:
    limits = config["document_limits"]
    max_chain_bytes = limits["automatic_agents_chain_max_bytes"]
    for directory in sorted(maintained_directories(files), key=str):
        chain = agents_chain(root, directory)
        size = sum(len(path.read_bytes()) for path in chain)
        if size > max_chain_bytes:
            errors.append(
                f"automatic AGENTS chain exceeds {max_chain_bytes} bytes at {directory}: {size}"
            )

    max_task_chars = limits["task_management_docs_max_chars"]
    for relative in files:
        pure = PurePosixPath(relative)
        if not str(pure).startswith(".agents/") or not relative.endswith(".md"):
            continue
        if pure.name == "AGENTS.md":
            continue
        path = root / relative
        chain = agents_chain(root, pure.parent)
        chars = sum(len(item.read_text(encoding="utf-8")) for item in chain)
        chars += len(path.read_text(encoding="utf-8"))
        if chars > max_task_chars:
            errors.append(
                f"management route exceeds {max_task_chars} chars for {relative}: {chars}"
            )


def check_links(root: Path, source: Path, text: str, errors: list[str]) -> None:
    for raw_target in LINK_PATTERN.findall(text):
        target = raw_target.strip().strip("<>").split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (source.parent / target).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            errors.append(
                f"link escapes project root in {source.relative_to(root)}: {raw_target}"
            )
            continue
        if not resolved.exists():
            errors.append(f"broken link in {source.relative_to(root)}: {raw_target}")


def check_precommit(root: Path, errors: list[str]) -> tuple[bool, bool]:
    staged = nul_paths(
        git(root, "diff", "--cached", "--name-only", "-z"), "staged diff"
    )
    untracked = nul_paths(
        git(root, "ls-files", "--others", "--exclude-standard", "-z"),
        "untracked file check",
    )
    if not staged:
        errors.append("precommit mode requires a non-empty staged diff")
    if untracked:
        errors.append(
            "untracked non-ignored files remain: " + ", ".join(sorted(untracked))
        )

    document_suffixes = {".md", ".rst", ".txt"}
    code_suffixes = {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
        ".yaml",
        ".yml",
    }
    needs_docs = any(Path(path).suffix.lower() in document_suffixes for path in staged)
    needs_code = any(Path(path).suffix.lower() in code_suffixes for path in staged)
    if any(path == ".agents/governance.json" for path in staged):
        needs_docs = True
    return needs_code, needs_docs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate qizai project governance.")
    parser.add_argument(
        "--root", default=".", help="Project root; defaults to current directory"
    )
    parser.add_argument(
        "--mode", choices=("structure", "precommit"), default="structure"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    errors: list[str] = []

    try:
        if not root.is_dir():
            raise RuntimeError(f"project root is not a directory: {root}")
        top = git(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise RuntimeError("project is not managed by Git")
        discovered = Path(top.stdout.decode("utf-8").strip()).resolve()
        if discovered != root:
            raise RuntimeError(f"--root is not the Git root: {discovered}")

        check_required(root, errors)
        if errors:
            raise RuntimeError("required governance files are missing")
        config = load_config(root)
        files = project_files(root)
        check_overrides(root, files, errors)
        check_directory_navigation(root, files, errors)
        check_documents(root, files, config, errors)
        check_context_budgets(root, files, config, errors)
        needs_code = needs_docs = False
        if args.mode == "precommit":
            needs_code, needs_docs = check_precommit(root, errors)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, RuntimeError) as exc:
        errors.append(str(exc))
        needs_code = needs_docs = False

    if errors:
        for error in dict.fromkeys(errors):
            print(f"ERROR {error}")
        return 1

    print("OK qizai governance structure is valid")
    if args.mode == "precommit":
        print(f"REVIEW code={'required' if needs_code else 'not-applicable'}")
        print(f"REVIEW documentation={'required' if needs_docs else 'not-applicable'}")
        print(
            "GATE confirm the staged diff has one logical purpose and the commit explains why"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
