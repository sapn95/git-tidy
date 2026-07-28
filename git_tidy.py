#!/usr/bin/env python3
"""git-tidy — keep a directory full of git checkouts clean, in one pass.

A workspace that collects clones grows three kinds of cruft, and each needs a
different kind of care:

  git-tidy sync     Fetch, prune remote-tracking refs, and fast-forward every
                    repo onto its default branch. Never rebases, never merges,
                    never touches a repo whose worktree has uncommitted work.

  git-tidy prune    Delete local branches whose upstream is gone from the remote
                    — but only once their commits are contained in the default
                    branch, so unpushed work is reported instead of dropped.

  git-tidy clean    Remove build output and caches: everything .gitignore already
                    calls disposable, plus .terraform, node_modules, __pycache__
                    and whatever else the config names. Inside a repo, only
                    untracked or ignored paths are eligible; a tracked file is
                    never deleted by accident.

  git-tidy trash    Sweep loose junk — scratch files, stray logs, keyboard-mash
                    filenames — out of the workspace. Moves to a timestamped
                    quarantine with a manifest rather than deleting, so a wrong
                    guess costs one `git-tidy restore`.

  git-tidy doctor   Report what needs a human: credentials embedded in remote
                    URLs, detached HEADs, unpushed commits, repos with no remote.

  git-tidy run      All of the above, in the order they are listed.

  git-tidy init     Write a commented config file, globally or for one directory.

Three modes decide what happens to each thing found:

  --dry-run   (the default) print what would happen and change nothing
  --ask       prompt for every change: y/n, a/s for all of that kind, q to stop
  --apply     carry everything out without asking

Configuration is YAML, merged from the global file and then from a .git-tidy.yaml
in any directory between the workspace root and each repo — deepest wins, so a
single repo can opt out of a rule the workspace sets. `git-tidy config` prints
the result of that merge for any path.

Single file, standard library only: PyYAML is used when it is installed, and a
strict parser for the documented subset stands in when it is not.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

__version__ = "1.0.0"

CONFIG_NAMES = (".git-tidy.yaml", ".git-tidy.yml")
QUARANTINE_DIRNAME = ".git-tidy-trash"
MANIFEST_NAME = "manifest.json"

# A URL with a password or token in it, e.g. https://user:token@host/repo.git.
# Bitbucket and GitLab hand these out from their web UI, and a clone made that way
# leaves the secret sitting in .git/config in plain text.
CREDENTIAL_IN_URL = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<user>[^/@:]+):(?P<secret>[^/@]+)@"
)

# Vowel-free stretches and short repeated units are what a hand mashed on a
# keyboard looks like; real names very rarely produce either.
CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}", re.IGNORECASE)
ALPHA_ONLY = re.compile(r"^[a-z]+$", re.IGNORECASE)

DRY, ASK, AUTO = "dry", "ask", "auto"


class Failure(Exception):
    """A problem worth reporting to the user without a traceback."""


class Quit(Exception):
    """Raised when the user answers 'q' to a prompt: stop, but keep what is done."""


def worker_count(configured: Any) -> int:
    """How many workers to run. 0, the default, means one per CPU core.

    Everything here is either waiting on the network or waiting on the disk, so
    threads are the right tool and the core count is a sane starting point: a
    serial pass over a workspace with a few hundred repositories and tens of
    gigabytes of build output takes minutes, and the same pass across all cores
    takes seconds.
    """
    try:
        value = int(configured)
    except (TypeError, ValueError) as exc:
        raise Failure(f"jobs must be a number, not {configured!r}") from exc
    if value < 0:
        raise Failure("jobs cannot be negative")
    if value:
        return value
    return os.cpu_count() or 4


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# The shape below is also the documentation of the config schema: a key that does
# not appear here is rejected, so a typo fails loudly instead of silently doing
# nothing. Values are the defaults used when no config file sets them.
DEFAULTS: dict[str, Any] = {
    "exclude": [],
    "jobs": 0,
    "sync": {
        "enabled": True,
        "remote": "origin",
        "default_branch": "auto",
        "default_branch_candidates": ["main", "master", "trunk", "develop"],
        "switch": "clean-only",
        "fast_forward": True,
        "prune": True,
        "prune_tags": False,
        "submodules": "none",
        "gc": False,
    },
    "branches": {
        "enabled": True,
        "prune_gone": True,
        "require_merged": True,
        "prune_local_only": False,
        "keep": ["main", "master", "trunk", "develop", "release/*"],
    },
    "clean": {
        "enabled": True,
        "ignored": False,
        "ignored_keep": [
            ".env",
            ".env.*",
            ".envrc",
            ".direnv",
            "*.tfstate",
            "*.tfstate.*",
            "*.pem",
            "*.key",
            "*.p12",
            "id_rsa*",
            "secrets.*",
            "*.local",
        ],
        "dirs": [
            ".terraform",
            ".terragrunt-cache",
            ".scannerwork",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".tox",
            "htmlcov",
            ".sass-cache",
            ".gradle",
            ".next",
            ".nuxt",
        ],
        "extra_dirs": [],
        "files": [
            "*.pyc",
            "*.pyo",
            ".coverage",
            ".coverage.*",
            "coverage.xml",
            "*.tfplan",
            "crash.log",
            "crash.*.log",
            ".DS_Store",
            "npm-debug.log*",
            "yarn-error.log*",
        ],
        "extra_files": [],
        "regenerable": [
            ".terraform",
            ".terragrunt-cache",
            ".gradle",
            ".next",
            ".nuxt",
            ".tox",
        ],
        "dependency_dirs": ["node_modules", ".venv", "venv", "vendor", ".bundle"],
        "dependencies": False,
        "build_dirs": ["dist", "build", "target", "out"],
        "builds": False,
        "tracked": False,
        "keep": [],
        "quarantine": False,
    },
    "trash": {
        "enabled": False,
        "scope": "root",
        "patterns": [],
        "heuristics": ["mash", "empty", "temp"],
        "sensitive": ["*.pw", "*.secret", "*.pem", "*.key", "*creds*", "*token*", "*password*"],
        "min_age_days": 7,
        "keep": ["README*", "LICENSE*", "*.md", ".*"],
        "dirs": False,
        "quarantine": True,
        "retention_days": 30,
    },
    "doctor": {
        "enabled": True,
        "credentials_in_url": True,
        "detached_head": True,
        "unpushed": True,
        "no_remote": True,
        "large_git_mb": 512,
    },
}

# Why each setting exists, keyed by its dotted path. Used to write a config file
# that explains itself, so `git-tidy init` produces documentation rather than a
# wall of unexplained keys.
COMMENTS: dict[str, str] = {
    "exclude": "Repositories to skip entirely, as globs against the path relative to the\n"
    "workspace root, or against the directory name.",
    "jobs": "How much to do at once. 0 means one worker per CPU core, which is the\n"
    "right answer on almost every machine: walking 40 GB of build output one\n"
    "directory at a time takes minutes, and in parallel it takes seconds.\n"
    "Raise it above the core count for a workspace whose repositories are all\n"
    "remote and slow to fetch, since git spends that time waiting rather than\n"
    "computing. Forced to 1 by --ask, so the prompts do not interleave.",
    "sync": "Fetching and fast-forwarding.",
    "sync.enabled": "Turn the whole sync step off for this directory.",
    "sync.remote": "Which remote to fetch from and follow.",
    "sync.default_branch": '"auto" asks the remote which branch its HEAD points at.\n'
    "Set a name to force one.",
    "sync.default_branch_candidates": "Tried in order when the remote has no HEAD to ask\nabout.",
    "sync.switch": "always:     switch even with uncommitted changes (git still refuses to\n"
    "            clobber anything)\n"
    "clean-only: only switch when the worktree is clean\n"
    "never:      fast-forward whatever is checked out, do not switch",
    "sync.fast_forward": "Only ever fast-forward. A diverged repository is reported,\n"
    "never merged and never rebased.",
    "sync.prune": "Drop remote-tracking refs for branches deleted on the remote.",
    "sync.prune_tags": "Off by default: a tag that only exists locally also counts as\n"
    "gone, and deleting it loses it.",
    "sync.submodules": "none:   leave them alone\n"
    "init:   init and update missing ones\n"
    "update: also force existing ones onto the recorded commit",
    "sync.gc": "Repack loose objects when git itself thinks it is worth it.",
    "branches": "Deleting local branches that the remote no longer has.",
    "branches.enabled": "Turn the whole branch step off for this directory.",
    "branches.prune_gone": "Delete local branches whose upstream has disappeared from\nthe remote.",
    "branches.require_merged": "...but only once their commits already live in the\n"
    "default branch. Turning this off deletes unpushed work.",
    "branches.prune_local_only": "Branches that never had an upstream. Usually local\n"
    "scratch work, so they are reported rather than deleted.",
    "branches.keep": "Never deleted, whatever the rules above say. Globs allowed.",
    "clean": "Removing build output and caches.",
    "clean.enabled": "Turn the whole clean step off for this directory.",
    "clean.ignored": "Delete everything .gitignore already calls disposable, which is\n"
    "the same set as `git clean -Xd`. Thorough, and the fastest way to reclaim\n"
    "space, but it also catches local-only files that are ignored on purpose,\n"
    "which is what ignored_keep below is for.",
    "clean.ignored_keep": "Never deleted by clean.ignored: local state and credentials\n"
    "that are ignored precisely because they must not be committed.",
    "clean.dirs": "Directory names removed wherever they appear, ignored or not.\nGlobs allowed.",
    "clean.extra_dirs": "Appended to dirs instead of replacing it, so one repository\n"
    "can add a name without restating the whole list.",
    "clean.regenerable": "A directory holding a git repository is normally kept, because a\n"
    "vendored checkout can be somebody's work. These are the exceptions: caches\n"
    "whose contents a tool wrote and a tool can write again. `terraform init`\n"
    "clones every module into .terraform/modules, so without this list a single\n"
    "`terraform init` makes a gigabyte permanently unreclaimable.",
    "clean.files": "File names removed wherever they appear. Globs allowed.",
    "clean.extra_files": "Appended to files, as extra_dirs is to dirs.",
    "clean.dependency_dirs": "Dependency trees. Cheap to delete, expensive to restore\n"
    "without a network.",
    "clean.dependencies": "Turn the list above on.",
    "clean.build_dirs": "Build output. Off by default, because dist/ and build/ are\n"
    "also perfectly ordinary source directory names.",
    "clean.builds": "Turn the list above on.",
    "clean.tracked": "A tracked file is somebody's committed content, even when it\n"
    "looks like an artefact. Leave this off.",
    "clean.keep": "Paths never considered, as globs relative to the repository or the\n"
    "workspace root.",
    "clean.quarantine": "Move removals to the quarantine instead of deleting them.",
    "trash": "Sweeping loose junk out of the workspace itself.",
    "trash.enabled": "Off until you turn it on: deleting files nobody asked about is\n"
    "the one thing here that cannot be inferred from git.",
    "trash.scope": "root:      the workspace root only\n"
    "workspace: every directory that is not inside a git repository",
    "trash.patterns": "Explicit globs. Anything matching is junk, whatever the\n"
    "heuristics below say.",
    "trash.heuristics": "mash:  keyboard-mash filenames (asdkjfhaksdjf.txt, lalalala.log)\n"
    "empty: zero-byte files\n"
    "temp:  editor and OS leftovers (*~, *.swp, *.orig, *.rej, *.bak)",
    "trash.sensitive": "Reported as sensitive and always quarantined rather than\n"
    "deleted, even with quarantine off, because a token may be the only copy.",
    "trash.min_age_days": "Nothing younger than this is touched, so today's scratch\n"
    "file survives.",
    "trash.keep": "Never swept.",
    "trash.dirs": "Consider directories too. A directory of junk is much more often a\n"
    "project somebody forgot about, so this is off.",
    "trash.quarantine": "Move to quarantine instead of deleting. Strongly recommended.",
    "trash.retention_days": "How long a quarantine survives `git-tidy restore --expire`.",
    "doctor": "Reporting the things that need a decision rather than a command.",
    "doctor.enabled": "Turn the whole doctor step off for this directory.",
    "doctor.credentials_in_url": "Warn when a clone's remote URL carries a password or\na token.",
    "doctor.detached_head": "Warn when HEAD is not on a branch.",
    "doctor.unpushed": "Warn about commits that exist only locally.",
    "doctor.no_remote": "Warn about repositories with no remote at all.",
    "doctor.large_git_mb": "Warn when .git is bigger than this many megabytes.\n"
    "0 disables the check.",
}


# --------------------------------------------------------------------------- #
# YAML
# --------------------------------------------------------------------------- #


def load_yaml(text: str, source: str) -> Any:
    """Parse YAML, preferring PyYAML and falling back to the built-in subset.

    The fallback exists so the script runs from a checkout with nothing installed.
    It is deliberately strict: anything it does not understand raises, rather than
    being guessed at, because a misread config here deletes the wrong files.
    """
    try:
        # Imported here, not at the top: it is optional, and this is the only
        # place that would notice its absence.
        import yaml
    except ImportError:
        return _parse_yaml_subset(text, source)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - message shape varies
        raise Failure(f"{source}: {exc}") from exc


@dataclass(frozen=True)
class _Line:
    """One significant line of YAML: its indent, its content, where it came from."""

    indent: int
    text: str
    number: int


def _strip_comment(line: str) -> str:
    """Drop a trailing comment, respecting quotes.

    A '#' only starts a comment at the beginning of the content or after a space,
    so `url: http://x#y` keeps its fragment.

    A quote only opens one where a value could begin — the start of the line, or
    after whitespace or one of `:,[{`. Otherwise the apostrophe in
    `name: it's fine  # note` would be read as an opening quote, and the comment
    after it would survive into the value.
    """
    out: list[str] = []
    quote: str | None = None
    openers = " \t:,[{-"
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote and not (quote == '"' and i and line[i - 1] == "\\"):
                quote = None
            continue
        if ch in "\"'" and (i == 0 or line[i - 1] in openers):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _tokenize(text: str, source: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise Failure(f"{source}:{number}: tabs cannot be used for YAML indentation")
        content = _strip_comment(raw)
        if not content.strip():
            continue
        if content.lstrip().startswith(("---", "...")):
            continue
        lines.append(_Line(len(content) - len(content.lstrip()), content.strip(), number))
    return lines


def _parse_yaml_subset(text: str, source: str) -> Any:
    lines = _tokenize(text, source)
    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0].indent, source)
    if index != len(lines):
        raise Failure(f"{source}:{lines[index].number}: unexpected indentation")
    return value


def _parse_block(lines: list[_Line], index: int, indent: int, source: str) -> tuple[Any, int]:
    if lines[index].text.startswith("- ") or lines[index].text == "-":
        return _parse_sequence(lines, index, indent, source)
    return _parse_mapping(lines, index, indent, source)


def _parse_mapping(lines: list[_Line], index: int, indent: int, source: str) -> tuple[Any, int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if line.text.startswith("- "):
            raise Failure(f"{source}:{line.number}: a list item where a key was expected")
        key, sep, rest = line.text.partition(":")
        if not sep:
            raise Failure(f"{source}:{line.number}: expected 'key: value'")
        key, rest = key.strip(), rest.strip()
        index += 1
        if rest:
            result[_scalar(key, source, line.number)] = _scalar(rest, source, line.number)
            continue
        # An empty value means either a nested block on the following lines, or
        # genuinely nothing.
        if index < len(lines) and lines[index].indent > indent:
            child, index = _parse_block(lines, index, lines[index].indent, source)
            result[_scalar(key, source, line.number)] = child
        else:
            result[_scalar(key, source, line.number)] = None
    return result, index


def _parse_sequence(lines: list[_Line], index: int, indent: int, source: str) -> tuple[Any, int]:
    result: list[Any] = []
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if not (line.text.startswith("- ") or line.text == "-"):
            raise Failure(f"{source}:{line.number}: expected a '- ' list item")
        item = line.text[1:].lstrip()
        # The column the item's content starts at, so `- key: value` can be
        # re-read as a mapping that later lines at the same column extend.
        item_indent = indent + (len(line.text) - len(item))
        index += 1
        if not item:
            if index >= len(lines) or lines[index].indent <= indent:
                result.append(None)
                continue
            child, index = _parse_block(lines, index, lines[index].indent, source)
            result.append(child)
            continue
        if _looks_like_mapping(item):
            virtual = [_Line(item_indent, item, line.number)]
            while index < len(lines) and lines[index].indent == item_indent:
                virtual.append(lines[index])
                index += 1
            child, consumed = _parse_mapping(virtual, 0, item_indent, source)
            if consumed != len(virtual):  # pragma: no cover - defensive
                raise Failure(f"{source}:{line.number}: could not read this list item")
            result.append(child)
            continue
        result.append(_scalar(item, source, line.number))
    return result, index


def _looks_like_mapping(item: str) -> bool:
    """True for `key: value` and `key:`, false for a plain or flow scalar."""
    if item.startswith(("[", "{", '"', "'")):
        return False
    key, sep, _ = item.partition(":")
    return bool(sep) and " " not in key.strip()


def _split_flow(body: str, source: str, number: int) -> list[str]:
    """Split `a, b, [c, d]` on top-level commas only."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise Failure(f"{source}:{number}: unbalanced brackets")
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if quote or depth:
        raise Failure(f"{source}:{number}: unterminated quote or bracket")
    if "".join(current).strip():
        parts.append("".join(current))
    return [p.strip() for p in parts]


_BOOL_TRUE = {"true", "yes", "on"}
_BOOL_FALSE = {"false", "no", "off"}
_NULL = {"", "null", "~"}


def _scalar(token: str, source: str, number: int) -> Any:
    """Read one scalar, or a flow collection, the way PyYAML's safe loader would."""
    token = token.strip()
    flow = _flow_collection(token, source, number)
    if flow is not _NOT_FLOW:
        return flow
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        body = token[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\") if token[0] == '"' else body
    return _plain_scalar(token)


# A sentinel, because None is itself a perfectly good parsed value.
_NOT_FLOW = object()


def _flow_collection(token: str, source: str, number: int) -> Any:
    """`[a, b]` or `{a: 1}`, or _NOT_FLOW when the token is neither."""
    # An opening bracket with no closing one is a mistake, not a string that
    # happens to start with a bracket. Saying so beats silently reading
    # `keep: [main, release/*` as one long name.
    if token.startswith("[") != token.endswith("]"):
        raise Failure(f"{source}:{number}: unbalanced [ ] in {token!r}")
    if token.startswith("{") != token.endswith("}"):
        raise Failure(f"{source}:{number}: unbalanced {{ }} in {token!r}")
    if token.startswith("["):
        return [_scalar(p, source, number) for p in _split_flow(token[1:-1], source, number)]
    if token.startswith("{"):
        mapping: dict[str, Any] = {}
        for part in _split_flow(token[1:-1], source, number):
            key, sep, value = part.partition(":")
            if not sep:
                raise Failure(f"{source}:{number}: expected 'key: value' inside {{...}}")
            mapping[_scalar(key, source, number)] = _scalar(value, source, number)
        return mapping
    return _NOT_FLOW


def _plain_scalar(token: str) -> Any:
    """An unquoted token: null, a bool, a number, or the text itself."""
    lowered = token.lower()
    if lowered in _NULL:
        return None
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    for convert in (lambda t: int(t, 10), float):
        try:
            return convert(token)
        except ValueError:
            continue
    return token


_NEEDS_QUOTES = re.compile(r"^$|^[-?:,\[\]{}#&*!|>'\"%@`]|[:#]\s|\s$")


def dump_scalar(value: Any) -> str:
    """Render one scalar the way this file's parser reads it back."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    ambiguous = text.lower() in _BOOL_TRUE | _BOOL_FALSE | _NULL
    numeric = _looks_numeric(text)
    if ambiguous or numeric or _NEEDS_QUOTES.search(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def dump_yaml(value: Any, indent: int = 0) -> str:
    """Serialise the config subset: nested mappings, lists and scalars."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return f"{pad}{{}}\n"
        out = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                out.append(f"{pad}{dump_scalar(key)}:\n{dump_yaml(item, indent + 2)}")
            elif isinstance(item, dict):
                out.append(f"{pad}{dump_scalar(key)}: {{}}\n")
            elif isinstance(item, list):
                out.append(f"{pad}{dump_scalar(key)}: []\n")
            else:
                out.append(f"{pad}{dump_scalar(key)}: {dump_scalar(item)}\n")
        return "".join(out)
    if isinstance(value, list):
        if not value:
            return f"{pad}[]\n"
        out = []
        for item in value:
            if isinstance(item, (dict, list)):
                nested = dump_yaml(item, indent + 2)
                out.append(f"{pad}-\n{nested}")
            else:
                out.append(f"{pad}- {dump_scalar(item)}\n")
        return "".join(out)
    return f"{pad}{dump_scalar(value)}\n"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def global_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "git-tidy" / "config.yaml"


def _merge(base: dict[str, Any], overlay: dict[str, Any], where: str) -> dict[str, Any]:
    """Deep-merge one config over another.

    Mappings merge key by key; every other value, lists included, replaces what
    came before. Lists replacing rather than appending is what makes it possible
    to *shrink* a list further down the tree — `extra_dirs` and `extra_files`
    exist for the common case of only wanting to add something.
    """
    result = dict(base)
    for key, value in overlay.items():
        if key not in base:
            raise Failure(f"{where}: unknown setting {key!r}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise Failure(f"{where}: {key!r} takes a block of settings, not a single value")
            result[key] = _merge(base[key], value, f"{where}: {key}")
        elif isinstance(base[key], list):
            # `dirs:` with nothing after it parses to None, and means "empty",
            # not "crash the first time something iterates it".
            if value is None:
                result[key] = []
            elif not isinstance(value, list):
                raise Failure(f"{where}: {key!r} takes a list")
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def read_config_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Failure(f"cannot read {path}: {exc}") from exc
    data = load_yaml(text, str(path))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise Failure(f"{path}: the top level of the config must be a block of settings")
    return data


def config_files_for(directory: Path, root: Path) -> list[Path]:
    """Every config between the workspace root and `directory`, outermost first."""
    found: list[Path] = []
    chain = [directory, *directory.parents]
    for parent in reversed(chain):
        if root != parent and root not in parent.parents:
            continue
        for name in CONFIG_NAMES:
            candidate = parent / name
            if candidate.is_file():
                found.append(candidate)
                break
    return found


class ConfigResolver:
    """Merges global, workspace and per-directory config, with the results cached."""

    def __init__(self, root: Path, overrides: dict[str, Any] | None = None) -> None:
        self.root = root
        self.overrides = overrides or {}
        self._cache: dict[Path, dict[str, Any]] = {}
        self._lock = threading.Lock()
        base = dict(DEFAULTS)
        global_path = global_config_path()
        self.global_path = global_path
        if global_path.is_file():
            base = _merge(base, read_config_file(global_path), str(global_path))
        self.base = base

    def for_path(self, directory: Path) -> dict[str, Any]:
        directory = directory.resolve()
        with self._lock:
            if directory in self._cache:
                return self._cache[directory]
        merged = self.base
        for path in config_files_for(directory, self.root):
            merged = _merge(merged, read_config_file(path), str(path))
        if self.overrides:
            merged = _merge(merged, self.overrides, "command line")
        with self._lock:
            self._cache[directory] = merged
        return merged


def render_config(chosen: dict[str, Any], header: str) -> str:
    """Write a config file that documents itself.

    Every setting appears, commented out at its default, with the explanation
    from COMMENTS above it. Anything in `chosen` is written live instead, so the
    file is both a record of the choices made and a menu of the rest.
    """
    lines = [f"# {line}" if line else "#" for line in header.splitlines()]
    lines.append("")
    for section, value in DEFAULTS.items():
        lines.extend(_render_entry(section, value, chosen.get(section), section, 0))
    return "\n".join(lines).rstrip() + "\n"


def _render_entry(key: str, default: Any, override: Any, dotted: str, indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    comment = COMMENTS.get(dotted)
    if comment:
        if indent == 0:
            lines.append("")
        lines.extend(f"{pad}# {line}" for line in comment.splitlines())
    if isinstance(default, dict):
        live = isinstance(override, dict) and bool(override)
        lines.append(f"{pad}{key}:" if live else f"{pad}# {key}:")
        for child_key, child_default in default.items():
            child_override = override.get(child_key) if isinstance(override, dict) else None
            lines.extend(
                _render_entry(
                    child_key,
                    child_default,
                    child_override,
                    f"{dotted}.{child_key}",
                    indent + 2,
                )
            )
        return lines
    value = default if override is None else override
    body = dump_yaml({key: value}, indent).rstrip("\n").splitlines()
    if override is None:
        # Commented out, so the file shows the default without setting it.
        body = [f"{pad}# {line[indent:]}" for line in body]
    lines.extend(body)
    return lines


# --------------------------------------------------------------------------- #
# Modes: dry, ask, auto
# --------------------------------------------------------------------------- #


class Decider:
    """Decides whether one action happens, according to the mode.

    In --ask mode it prompts, and remembers 'all' and 'skip' answers per kind of
    action so a run of two hundred identical removals needs one keystroke. Ask
    mode runs single-threaded, so there is never more than one prompt at a time.
    """

    def __init__(
        self, mode: str, stream: Any = None, prompt_input: Callable[[str], str] | None = None
    ) -> None:
        if mode not in (DRY, ASK, AUTO):
            raise Failure(f"unknown mode {mode!r}")
        self.mode = mode
        self.stream = stream or sys.stdout
        self._input = prompt_input or input
        self._per_kind: dict[str, bool] = {}
        self._everything: bool | None = None
        self._lock = threading.Lock()

    @property
    def dry(self) -> bool:
        return self.mode == DRY

    def allow(self, action: Action) -> bool:
        """Say whether `action` may go ahead, annotating it with the outcome."""
        if self.mode == DRY:
            action.detail = f"would {action.detail}" if action.detail else "would change this"
            return False
        if self.mode == AUTO:
            return True
        with self._lock:
            answer = self._everything
            if answer is None:
                answer = self._per_kind.get(action.kind)
            if answer is None:
                answer = self._prompt(action)
        if not answer:
            action.skipped = True
            action.detail = f"declined: {action.detail}" if action.detail else "declined"
        return answer

    def _prompt(self, action: Action) -> bool:
        size = f" ({human_size(action.size)})" if action.size else ""
        question = f"  {action.scope}: {action.target}{size} — {action.detail or action.kind}"
        while True:
            print(question, file=self.stream)
            try:
                reply = self._input(
                    "  [y]es / [n]o / [a]ll of these / [s]kip these / [Y]es to all / [q]uit? "
                )
            except EOFError as exc:
                raise Failure("--ask needs a terminal to read answers from") from exc
            choice = reply.strip()
            if choice == "Y":
                self._everything = True
                return True
            if choice in ("y", ""):
                return True
            if choice == "n":
                return False
            if choice == "a":
                self._per_kind[action.kind] = True
                return True
            if choice == "s":
                self._per_kind[action.kind] = False
                return False
            if choice == "q":
                raise Quit
            print("  answer y, n, a, s, Y or q", file=self.stream)


def ask_yes_no(
    question: str, default: bool, prompt_input: Callable[[str], str] | None = None
) -> bool:
    """A standalone yes/no prompt, used by `git-tidy init`."""
    reader = prompt_input or input
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            reply = reader(f"{question} {suffix} ").strip().lower()
        except EOFError:
            return default
        if not reply:
            return default
        if reply in ("y", "yes"):
            return True
        if reply in ("n", "no"):
            return False


def ask_value(question: str, default: str, prompt_input: Callable[[str], str] | None = None) -> str:
    reader = prompt_input or input
    try:
        reply = reader(f"{question} [{default}] ").strip()
    except EOFError:
        return default
    return reply or default


# --------------------------------------------------------------------------- #
# Running git
# --------------------------------------------------------------------------- #


@dataclass
class Git:
    """Runs git inside one repository."""

    path: Path
    timeout: int = 300

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            # S603/S607: the argument list is built here, never from a shell
            # string, and git is deliberately taken from PATH so that a version
            # managed by Homebrew, asdf or the system all work.
            result = subprocess.run(  # noqa: S603
                ["git", "-C", str(self.path), *args],  # noqa: S607
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                # Git must never stop to ask for a password: a repo whose
                # credentials expired would otherwise hang the whole run.
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GCM_INTERACTIVE": "never",
                },
            )
        except FileNotFoundError as exc:
            raise Failure("git is not installed, or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise Failure(f"git {args[0]} timed out after {self.timeout}s") from exc
        if check and result.returncode != 0:
            raise Failure(f"git {' '.join(args)}: {last_line(result)}")
        return result

    def out(self, *args: str, check: bool = True) -> str:
        return self.run(*args, check=check).stdout.strip()

    def ok(self, *args: str) -> bool:
        return self.run(*args, check=False).returncode == 0


def plural(count: str | int, word: str) -> str:
    """ "1 commit", "3 commits" — git hands these back as strings, so take either."""
    try:
        number = int(count)
    except (TypeError, ValueError):
        return f"{count} {word}s"
    if number == 1:
        return f"{number} {word}"
    return f"{number} {word[:-1]}ies" if word.endswith("y") else f"{number} {word}s"


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    """The most useful line of a failed git command.

    The *first* line, not the last: git puts the cause at the top and generic
    advice underneath, so the tail of an unreachable remote is the useless
    "and the repository exists." while the top is
    "ssh: connect to host example.com port 22: Operation timed out".
    """
    for line in (result.stderr or result.stdout).splitlines():
        if line.strip():
            return line.strip()
    return "failed"


def is_repo(path: Path) -> bool:
    """True for a work tree root. A .git *file* means a worktree or submodule."""
    dot_git = path / ".git"
    return dot_git.is_dir() or dot_git.is_file()


def find_repos(root: Path, exclude: Sequence[str], follow_nested: bool = False) -> list[Path]:
    """Find repository roots under `root`, skipping anything the config excludes.

    Nested repositories are not descended into by default: a submodule is the
    parent's business, and a vendored checkout is rarely the user's to sync.
    """
    repos: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in sorted(dirnames)
            if d not in (".git", QUARANTINE_DIRNAME)
            and not _excluded(here / d, root, exclude)
            and not (here / d).is_symlink()
        ]
        if here != root and is_repo(here):
            repos.append(here)
            if not follow_nested:
                dirnames[:] = []
    return repos


def _excluded(path: Path, root: Path, patterns: Sequence[str]) -> bool:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - path is always under root here
        return False
    return any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern)
        for pattern in patterns
    )


# --------------------------------------------------------------------------- #
# What a run produces
# --------------------------------------------------------------------------- #


@dataclass
class Action:
    """One thing git-tidy did, or would do. The report is a list of these."""

    kind: str
    scope: str
    target: str
    detail: str = ""
    size: int = 0
    applied: bool = False
    skipped: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "target": self.target,
            "detail": self.detail,
            "size": self.size,
            "applied": self.applied,
            "skipped": self.skipped,
            "error": self.error,
        }


@dataclass
class Report:
    actions: list[Action] = field(default_factory=list)

    def add(self, action: Action) -> Action:
        self.actions.append(action)
        return action

    def extend(self, actions: Iterable[Action]) -> None:
        self.actions.extend(actions)

    @property
    def bytes_freed(self) -> int:
        return sum(a.size for a in self.actions if a.applied)

    @property
    def bytes_found(self) -> int:
        return sum(a.size for a in self.actions if not a.error and not a.skipped)

    @property
    def errors(self) -> list[Action]:
        return [a for a in self.actions if a.error]


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #


def default_branch(git: Git, cfg: dict[str, Any]) -> str | None:
    """Work out which branch this repo considers its trunk.

    Asks the remote's own HEAD first, because a repo may well not use `main`, and
    guessing wrong means checking out the wrong branch everywhere.
    """
    configured = cfg["default_branch"]
    if configured != "auto":
        return str(configured)
    remote = cfg["remote"]

    def exists(branch: str) -> bool:
        return git.ok("show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}")

    cached = _cached_head(git, remote)
    if cached and exists(cached):
        return cached
    # Either there is no cached HEAD, or it is stale — a repository renamed from
    # master to main upstream keeps pointing at a branch that is no longer there.
    # Both cases are answered by asking the remote again.
    if git.ok("remote", "set-head", remote, "--auto"):
        fresh = _cached_head(git, remote)
        if fresh and exists(fresh):
            return fresh
    for candidate in cfg["default_branch_candidates"]:
        if exists(candidate):
            return str(candidate)
    # Nothing resolved. Hand back the stale name if there was one, so the report
    # says which branch is missing rather than that there is no default at all.
    return cached


def _cached_head(git: Git, remote: str) -> str | None:
    head = git.out("symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", check=False)
    prefix = f"refs/remotes/{remote}/"
    return head[len(prefix) :] if head.startswith(prefix) else None


def is_dirty(git: Git) -> bool:
    return bool(git.out("status", "--porcelain", "--untracked-files=no", check=False))


def current_branch(git: Git) -> str:
    """The checked-out branch, or "" when HEAD is detached."""
    return git.out("symbolic-ref", "--quiet", "--short", "HEAD", check=False)


def sync_repo(path: Path, name: str, cfg: dict[str, Any], decider: Decider) -> list[Action]:
    """Fetch, then fast-forward this repo onto its default branch."""
    sync = cfg["sync"]
    git = Git(path)
    actions: list[Action] = []
    remote = sync["remote"]

    if not git.out("remote", check=False):
        return [Action("sync", name, "-", "no remote configured", skipped=True)]
    if remote not in git.out("remote", check=False).split():
        return [Action("sync", name, remote, "no such remote", skipped=True)]

    fetch_args = ["fetch", remote, "--quiet"]
    if sync["prune"]:
        fetch_args.append("--prune")
    if sync["prune_tags"]:
        fetch_args.append("--prune-tags")
    fetch = Action("fetch", name, remote, "fetch")
    if decider.allow(fetch):
        result = git.run(*fetch_args, check=False)
        if result.returncode != 0:
            fetch.error = last_line(result)
            return [fetch]
        fetch.applied = True
        fetch.detail = "fetched"
    actions.append(fetch)

    branch = default_branch(git, sync)
    if branch is None:
        actions.append(Action("sync", name, "-", "no default branch found", skipped=True))
        return actions

    target = f"{remote}/{branch}"
    if not git.ok("show-ref", "--verify", "--quiet", f"refs/remotes/{target}"):
        actions.append(Action("sync", name, target, "remote branch missing", skipped=True))
        return actions

    head = current_branch(git)
    if head != branch:
        outcome = _switch(git, name, head, branch, target, sync, decider)
        actions.extend(outcome.actions)
        if outcome.stop:
            return actions
        head = current_branch(git)

    actions.extend(_fast_forward(git, name, head, branch, target, sync, decider))
    actions.extend(_sync_submodules(git, name, sync, decider))
    if sync["gc"] and not decider.dry:
        git.run("gc", "--auto", "--quiet", check=False)
    return actions


@dataclass
class _Outcome:
    actions: list[Action]
    stop: bool = False


def _switch(
    git: Git,
    name: str,
    head: str,
    branch: str,
    target: str,
    sync: dict[str, Any],
    decider: Decider,
) -> _Outcome:
    policy = sync["switch"]
    where = head or "detached HEAD"
    if policy == "never":
        return _Outcome([Action("switch", name, branch, f"staying on {where}", skipped=True)])
    if policy not in ("always", "clean-only"):
        raise Failure(f"sync.switch must be always, clean-only or never, not {policy!r}")
    if policy == "clean-only" and is_dirty(git):
        return _Outcome(
            [Action("switch", name, branch, f"uncommitted changes on {where}", skipped=True)],
            stop=True,
        )
    # A branch can only be checked out in one worktree at a time, and a workspace
    # that keeps .worktrees/ next to the clones hits this constantly. Nothing is
    # wrong: the branch is simply in use elsewhere, so say so rather than fail.
    elsewhere = _checked_out_elsewhere(git, branch)
    if elsewhere:
        return _Outcome(
            [Action("switch", name, branch, f"checked out in {elsewhere}", skipped=True)],
            stop=True,
        )

    action = Action("switch", name, branch, f"switch from {where}")
    if not decider.allow(action):
        return _Outcome([action], stop=True)
    result = git.run("switch", branch, check=False)
    if result.returncode != 0 and not git.ok(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ):
        # It only exists on the remote so far, so it has to be created. Retrying
        # when a local branch already exists would replace the real error with
        # the useless "a branch named 'main' already exists".
        result = git.run("switch", "--create", branch, "--track", target, check=False)
    if result.returncode != 0:
        action.error = last_line(result)
        return _Outcome([action], stop=True)
    action.applied = True
    action.detail = f"switched from {where}"
    return _Outcome([action])


def _checked_out_elsewhere(git: Git, branch: str) -> str | None:
    """The other worktree holding `branch`, or None.

    Read from `git worktree list` rather than matched against git's error text,
    which varies by version and locale.
    """
    here = git.out("rev-parse", "--show-toplevel", check=False)
    where: str | None = None
    for line in git.out("worktree", "list", "--porcelain", check=False).splitlines():
        field, _, value = line.partition(" ")
        if field == "worktree":
            where = value
        elif field == "branch" and value == f"refs/heads/{branch}" and where != here:
            return where
    return None


def _fast_forward(
    git: Git,
    name: str,
    head: str,
    branch: str,
    target: str,
    sync: dict[str, Any],
    decider: Decider,
) -> list[Action]:
    if not sync["fast_forward"]:
        return []
    upstream = (
        target
        if head == branch
        else git.out("rev-parse", "--abbrev-ref", "@{upstream}", check=False)
    )
    if not head:
        return [Action("update", name, "HEAD", "detached, nothing to fast-forward", skipped=True)]
    if not upstream:
        return [Action("update", name, head, "no upstream", skipped=True)]

    counts = git.out("rev-list", "--left-right", "--count", f"{upstream}...HEAD", check=False)
    behind, _, ahead = counts.partition("\t")
    if not counts:
        return [Action("update", name, head, "cannot compare with upstream", skipped=True)]
    if behind == "0":
        return [Action("update", name, head, "already up to date", skipped=True)]
    if ahead != "0":
        return [
            Action("update", name, head, f"diverged: {ahead} ahead, {behind} behind", skipped=True)
        ]
    if is_dirty(git):
        # git would refuse anyway, with "Your local changes would be overwritten
        # by merge". Refusing first turns a failure into a plain statement of
        # fact, which is what it is.
        return [Action("update", name, head, f"uncommitted changes, {behind} behind", skipped=True)]
    action = Action("update", name, head, f"fast-forward {plural(behind, 'commit')}")
    if not decider.allow(action):
        return [action]
    result = git.run("merge", "--ff-only", "--quiet", upstream, check=False)
    if result.returncode != 0:
        action.error = last_line(result)
    else:
        action.applied = True
        action.detail = f"fast-forwarded {plural(behind, 'commit')}"
    return [action]


def _sync_submodules(git: Git, name: str, sync: dict[str, Any], decider: Decider) -> list[Action]:
    mode = sync["submodules"]
    if mode == "none":
        return []
    if mode not in ("init", "update"):
        raise Failure(f"sync.submodules must be none, init or update, not {mode!r}")
    if not (git.path / ".gitmodules").is_file():
        return []
    action = Action("submodules", name, mode, "update submodules")
    if not decider.allow(action):
        return [action]
    args = ["submodule", "update", "--init", "--recursive", "--quiet"]
    if mode == "update":
        args.insert(2, "--force")
    result = git.run(*args, check=False)
    if result.returncode != 0:
        action.error = last_line(result)
    else:
        action.applied = True
        action.detail = "updated submodules"
    return [action]


# --------------------------------------------------------------------------- #
# branch pruning
# --------------------------------------------------------------------------- #


@dataclass
class BranchInfo:
    name: str
    upstream: str
    track: str

    @property
    def gone(self) -> bool:
        return "gone" in self.track

    @property
    def local_only(self) -> bool:
        return not self.upstream


def list_branches(git: Git) -> list[BranchInfo]:
    fmt = "%(refname:short)%09%(upstream:short)%09%(upstream:track)"
    out = git.out("for-each-ref", f"--format={fmt}", "refs/heads", check=False)
    branches: list[BranchInfo] = []
    for line in out.splitlines():
        # Trailing fields are empty for a branch with no upstream, and git omits
        # them entirely rather than padding with tabs.
        name, _, rest = line.partition("\t")
        upstream, _, track = rest.partition("\t")
        if name:
            branches.append(BranchInfo(name, upstream, track))
    return branches


def prune_branches(path: Path, name: str, cfg: dict[str, Any], decider: Decider) -> list[Action]:
    """Delete local branches the remote no longer has, keeping unpushed work."""
    rules = cfg["branches"]
    git = Git(path)
    actions: list[Action] = []
    trunk = default_branch(git, cfg["sync"])
    remote = cfg["sync"]["remote"]
    head = current_branch(git)

    for branch in list_branches(git):
        if any(fnmatch.fnmatch(branch.name, pattern) for pattern in rules["keep"]):
            continue
        if branch.name == head:
            continue  # git refuses to delete the checked-out branch anyway
        if branch.gone and rules["prune_gone"]:
            reason = "upstream gone"
        elif branch.local_only and rules["prune_local_only"]:
            reason = "never pushed"
        else:
            continue

        if rules["require_merged"]:
            kept = _unmerged_reason(git, branch, trunk, remote)
            if kept is VANISHED:
                actions.append(
                    Action("branch", name, branch.name, f"already deleted ({reason})", skipped=True)
                )
                continue
            if kept is not None:
                actions.append(Action("branch", name, branch.name, str(kept), skipped=True))
                continue

        action = Action("branch", name, branch.name, f"delete ({reason})")
        if not decider.allow(action):
            actions.append(action)
            continue
        result = git.run("branch", "--delete", "--force", branch.name, check=False)
        if result.returncode == 0:
            action.applied = True
            action.detail = f"deleted ({reason})"
        elif not git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch.name}"):
            # Gone already. Two entries in the workspace can share one ref store —
            # a linked worktree, or a second clone made with --shared — so another
            # worker may have deleted it between the listing and here. The
            # outcome is the one that was wanted; it is not a failure.
            action.skipped = True
            action.detail = f"already deleted ({reason})"
        else:
            action.error = last_line(result)
        actions.append(action)
    return actions


# The branch was there when it was listed and is not there now: another worker
# sharing this ref store got to it first. Distinct from "keep it", which is what
# a bare reason string means.
VANISHED = object()


def _unmerged_reason(git: Git, branch: BranchInfo, trunk: str | None, remote: str) -> Any:
    """Why this branch must be kept, None when it is safe to delete.

    Returns VANISHED when the branch has disappeared underneath us, which a
    workspace holding linked worktrees or --shared clones of the same repository
    produces routinely.
    """
    if trunk is None:
        return "kept: cannot check merged, no trunk found"
    trunk_ref = f"{remote}/{trunk}"
    if not git.ok("show-ref", "--verify", "--quiet", f"refs/remotes/{trunk_ref}"):
        trunk_ref = trunk
        if not git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{trunk_ref}"):
            return f"kept: no {trunk} to compare against"
    if git.ok("merge-base", "--is-ancestor", branch.name, trunk_ref):
        return None
    unpushed = git.out("rev-list", "--count", f"{trunk_ref}..{branch.name}", check=False)
    if not unpushed and not git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch.name}"):
        return VANISHED
    return f"kept: {plural(unpushed, 'commit') if unpushed else 'some commits'} not in {trunk_ref}"


# --------------------------------------------------------------------------- #
# cleaning artefacts
# --------------------------------------------------------------------------- #


def directory_size(path: Path) -> int:
    return _measure(path)[0]


def _measure(path: Path) -> tuple[int, bool]:
    """Total size under `path`, and whether a git repository is buried in it.

    Both come from the same walk, because the second question has to be asked
    before deleting a directory wholesale and the first is needed for the report
    anyway. A vendored checkout inside node_modules or vendor/ is a real thing,
    and removing its parent would take the repository with it.
    """
    total = 0
    holds_repo = False
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        here = Path(dirpath)
        if ".git" in dirnames or ".git" in filenames:
            holds_repo = True
        for name in filenames:
            candidate = here / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
    return total, holds_repo


def tracked_paths(git: Git) -> set[str]:
    """Every tracked path, plus each of its parent directories.

    Membership then answers "would deleting this remove committed content?" for
    files and directories alike, with one git call per repo instead of one per
    candidate path.
    """
    out = git.run("ls-files", "-z", check=False).stdout
    paths: set[str] = set()
    for entry in out.split("\0"):
        if not entry:
            continue
        paths.add(entry)
        parent = Path(entry).parent
        while str(parent) != ".":
            paths.add(parent.as_posix())
            parent = parent.parent
    return paths


def ignored_paths(git: Git) -> list[str]:
    """What `git clean -Xd` would remove: everything .gitignore calls disposable.

    --directory collapses a wholly ignored directory into one entry, so a
    node_modules with 40,000 files inside comes back as a single path.
    """
    result = git.run(
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "--no-empty-directory",
        "-z",
        check=False,
    )
    return [entry.rstrip("/") for entry in result.stdout.split("\0") if entry]


def clean_patterns(clean: dict[str, Any]) -> tuple[list[str], list[str]]:
    dirs = [*clean["dirs"], *clean["extra_dirs"]]
    if clean["dependencies"]:
        dirs += clean["dependency_dirs"]
    if clean["builds"]:
        dirs += clean["build_dirs"]
    files = [*clean["files"], *clean["extra_files"]]
    return dirs, files


def clean_ignored(
    repo: Path,
    scope: str,
    cfg: dict[str, Any],
    decider: Decider,
    quarantine: Quarantine | None,
    git: Git,
) -> list[Action]:
    """Remove everything the repo's own .gitignore already calls disposable."""
    clean = cfg["clean"]
    protect = [*clean["ignored_keep"], *clean["keep"]]
    actions: list[Action] = []
    for relative in ignored_paths(git):
        # git should never hand back the repository root, but a "" or "." here
        # would resolve to the repo itself and take everything with it.
        if relative in ("", "."):
            continue
        path = repo / relative
        if path.is_symlink() or not path.exists():
            continue
        name = Path(relative).name
        if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(relative, p) for p in protect):
            actions.append(Action("ignored", scope, relative, "kept by ignored_keep", skipped=True))
            continue
        actions.append(
            _remove(
                path,
                repo,
                scope,
                decider,
                quarantine,
                is_dir=path.is_dir(),
                kind="ignored",
                protect_nested=not _matches(name, clean["regenerable"]),
            )
        )
    return actions


def clean_tree(
    root: Path,
    scope: str,
    cfg: dict[str, Any],
    decider: Decider,
    git: Git | None,
    quarantine: Quarantine | None,
    stay_inside: bool = True,
) -> list[Action]:
    """Remove artefact directories and files under `root`.

    `git` is set when `root` is a repository, in which case tracked paths are
    protected. `stay_inside` stops the walk crossing into a nested repository
    that will be, or has been, handled on its own.
    """
    clean = cfg["clean"]
    dir_patterns, file_patterns = clean_patterns(clean)
    keep = clean["keep"]
    tracked = tracked_paths(git) if git is not None and not clean["tracked"] else set()
    actions: list[Action] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        if ".git" in here.parts:
            dirnames[:] = []
            continue
        descend: list[str] = []
        matched: list[str] = []
        for name in sorted(dirnames):
            candidate = here / name
            if name in (".git", QUARANTINE_DIRNAME) or candidate.is_symlink():
                continue
            if stay_inside and candidate != root and is_repo(candidate):
                continue  # a repo of its own; not this walk's business
            if _matches(name, dir_patterns) and not _protected(candidate, root, keep, tracked):
                matched.append(name)
            else:
                descend.append(name)
        # Matched directories go whole, so they are not descended into.
        dirnames[:] = descend
        for name in matched:
            actions.append(
                _remove(
                    here / name,
                    root,
                    scope,
                    decider,
                    quarantine,
                    is_dir=True,
                    protect_nested=not _matches(name, clean["regenerable"]),
                )
            )

        for name in sorted(filenames):
            candidate = here / name
            if candidate.is_symlink() or not _matches(name, file_patterns):
                continue
            if _protected(candidate, root, keep, tracked):
                continue
            actions.append(_remove(candidate, root, scope, decider, quarantine, is_dir=False))
    return actions


def _matches(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _protected(path: Path, root: Path, keep: Sequence[str], tracked: set[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    if any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern)
        for pattern in keep
    ):
        return True
    return relative in tracked


def _remove(
    path: Path,
    root: Path,
    scope: str,
    decider: Decider,
    quarantine: Quarantine | None,
    is_dir: bool,
    kind: str = "remove",
    protect_nested: bool = True,
) -> Action:
    relative = path.relative_to(root).as_posix()
    holds_repo = False
    try:
        size, holds_repo = _measure(path) if is_dir else (path.stat().st_size, False)
    except OSError:
        size = 0
    action = Action(kind, scope, relative, f"remove {'directory' if is_dir else 'file'}", size=size)
    if holds_repo and protect_nested:
        # A vendored or forgotten checkout inside an artefact directory. Deleting
        # the parent would take the repository with it, and nothing in an
        # artefact directory is worth that. `clean.regenerable` lists the caches
        # where the nested repository is itself a tool's clone.
        action.detail = "kept: contains a git repository"
        action.skipped = True
        action.size = 0
        return action
    if not decider.allow(action):
        return action
    try:
        _guard(path, root)
        if quarantine is not None:
            quarantine.take(path)
        elif is_dir:
            shutil.rmtree(path)
        else:
            path.unlink()
    except (OSError, Failure) as exc:
        action.error = str(exc)
        return action
    action.applied = True
    action.detail = "removed"
    return action


def _guard(path: Path, root: Path) -> None:
    """Refuse to delete anything that is not really inside the tree being cleaned."""
    if path.is_symlink():
        raise Failure(f"refusing to follow the symlink {path}")
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise Failure(f"refusing to remove {path}: outside {root}")
    if ".git" in resolved.relative_to(resolved_root).parts:
        raise Failure(f"refusing to remove {path}: inside a .git directory")


# --------------------------------------------------------------------------- #
# quarantine
# --------------------------------------------------------------------------- #


class Quarantine:
    """A timestamped holding area, so a wrong guess is undoable.

    Files are moved, not copied, and a manifest records where each came from.
    `git-tidy restore` reads the manifest back.
    """

    def __init__(self, root: Path, workspace: Path, stamp: str | None = None) -> None:
        self.workspace = workspace
        self.stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = root / self.stamp
        self.entries: list[dict[str, str]] = []
        self._lock = threading.Lock()

    def take(self, path: Path) -> Path:
        relative = path.resolve().relative_to(self.workspace.resolve())
        with self._lock:
            destination = self.dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination = destination.with_name(f"{destination.name}.{len(self.entries)}")
            shutil.move(str(path), str(destination))
            self.entries.append({"from": str(path), "to": str(destination)})
        return destination

    def write_manifest(self) -> Path | None:
        if not self.entries:
            return None
        manifest = self.dir / MANIFEST_NAME
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "version": __version__,
                    "created": self.stamp,
                    "workspace": str(self.workspace),
                    "entries": self.entries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return manifest


def restore(quarantine_root: Path, stamp: str | None, decider: Decider) -> list[Action]:
    """Put quarantined files back where they came from."""
    if not quarantine_root.is_dir():
        raise Failure(f"no quarantine at {quarantine_root}")
    stamps = sorted(p.name for p in quarantine_root.iterdir() if (p / MANIFEST_NAME).is_file())
    if not stamps:
        raise Failure(f"no quarantine with a manifest under {quarantine_root}")
    chosen = stamp or stamps[-1]
    if chosen not in stamps:
        raise Failure(f"no quarantine {chosen!r}; available: {', '.join(stamps)}")
    manifest = json.loads((quarantine_root / chosen / MANIFEST_NAME).read_text(encoding="utf-8"))
    actions: list[Action] = []
    for entry in manifest["entries"]:
        source, destination = Path(entry["to"]), Path(entry["from"])
        action = Action("restore", chosen, str(destination), "restore")
        if not source.exists():
            action.error = "missing from the quarantine"
        elif destination.exists():
            action.error = "something is already at that path"
        elif decider.allow(action):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            action.applied = True
            action.detail = "restored"
        actions.append(action)
    return actions


def expire_quarantines(quarantine_root: Path, days: int, decider: Decider) -> list[Action]:
    if not quarantine_root.is_dir():
        return []
    cutoff = time.time() - days * 86400
    actions: list[Action] = []
    for entry in sorted(quarantine_root.iterdir()):
        if not entry.is_dir() or entry.stat().st_mtime > cutoff:
            continue
        action = Action("expire", "quarantine", entry.name, f"delete, older than {days} days")
        action.size = directory_size(entry)
        if decider.allow(action):
            shutil.rmtree(entry)
            action.applied = True
            action.detail = "deleted"
        actions.append(action)
    return actions


# --------------------------------------------------------------------------- #
# trash
# --------------------------------------------------------------------------- #


def looks_like_mash(name: str) -> bool:
    """True for names that look like a hand landed on the keyboard.

    Three signals, any one of which is enough: a run of five or more consonants,
    no vowel at all, or the whole name being one short unit repeated
    (lalalalala).

    Deliberately conservative — it would rather miss junk than sweep a real file,
    so something like `dluahsfduihacf`, which keeps an ordinary-looking vowel
    rhythm, is not caught. Name those with `trash.patterns` instead. The
    quarantine is the backstop for the cases it gets wrong in the other
    direction: `strengths` would be flagged, and is recoverable.
    """
    stem = Path(name).stem
    if len(stem) < 8 or not ALPHA_ONLY.match(stem):
        return False
    lowered = stem.lower()
    if CONSONANT_RUN.search(lowered):
        return True
    if not set(lowered) & set("aeiou"):
        return True
    for size in (1, 2, 3):
        unit = lowered[:size]
        whole = len(lowered) // size * size
        if len(lowered) >= size * 4 and unit * (len(lowered) // size) == lowered[:whole]:
            return True
    return False


TEMP_SUFFIXES = ("*~", "*.swp", "*.swo", "*.orig", "*.rej", "*.bak", "*.tmp", "*.old")


def classify_trash(path: Path, trash: dict[str, Any], now: float) -> tuple[bool, str, bool]:
    """Decide whether one path is junk. Returns (is_junk, why, is_sensitive)."""
    name = path.name
    sensitive = _matches(name, trash["sensitive"])
    explicit = _matches(name, trash["patterns"])
    if _matches(name, trash["keep"]) and not explicit:
        return False, "", sensitive
    try:
        stat = path.stat()
    except OSError:
        return False, "", sensitive
    age_days = (now - stat.st_mtime) / 86400
    if age_days < trash["min_age_days"]:
        return False, "", sensitive

    if explicit:
        return True, "matches a configured pattern", sensitive
    heuristics = trash["heuristics"]
    if "temp" in heuristics and _matches(name, TEMP_SUFFIXES):
        return True, "editor or OS leftover", sensitive
    if "empty" in heuristics and path.is_file() and stat.st_size == 0:
        return True, "empty file", sensitive
    if "mash" in heuristics and looks_like_mash(name):
        return True, "keyboard-mash filename", sensitive
    return False, "", sensitive


def sweep_trash(
    workspace: Path,
    cfg: dict[str, Any],
    decider: Decider,
    quarantine: Quarantine,
    repos: Sequence[Path],
) -> list[Action]:
    trash = cfg["trash"]
    if not trash["enabled"]:
        return []
    if trash["scope"] not in ("root", "workspace"):
        raise Failure(f"trash.scope must be root or workspace, not {trash['scope']!r}")
    now = time.time()
    repo_set = {p.resolve() for p in repos}
    actions: list[Action] = []

    for path in _trash_candidates(workspace, trash, repo_set):
        junk, why, sensitive = classify_trash(path, trash, now)
        if not junk:
            continue
        relative = path.relative_to(workspace).as_posix()
        # A file that might hold the only copy of a credential is never deleted
        # outright, whatever the quarantine setting says.
        use_quarantine = trash["quarantine"] or sensitive
        detail = f"sweep: {why}" + (" — sensitive, quarantined" if sensitive else "")
        action = Action("trash", "workspace", relative, detail)
        try:
            action.size = directory_size(path) if path.is_dir() else path.stat().st_size
        except OSError:
            action.size = 0
        if not decider.allow(action):
            actions.append(action)
            continue
        try:
            _guard(path, workspace)
            if use_quarantine:
                quarantine.take(path)
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            action.applied = True
            # Keep the "sensitive" marker in the finished message too: seeing
            # which credentials moved is the whole point of the report.
            action.detail = ("quarantined: " if use_quarantine else "deleted: ") + why
            if sensitive:
                action.detail += " — sensitive, kept in quarantine"
        except (OSError, Failure) as exc:
            action.error = str(exc)
        actions.append(action)
    return actions


def _trash_candidates(workspace: Path, trash: dict[str, Any], repos: set[Path]) -> Iterator[Path]:
    if trash["scope"] == "root":
        for entry in sorted(workspace.iterdir()):
            if entry.name == QUARANTINE_DIRNAME or entry.is_symlink():
                continue
            if entry.is_dir() and (not trash["dirs"] or is_repo(entry) or entry.resolve() in repos):
                continue
            yield entry
        return
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        here = Path(dirpath)
        if here.resolve() in repos or ".git" in here.parts:
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in sorted(dirnames) if d != QUARANTINE_DIRNAME and not is_repo(here / d)
        ]
        for name in sorted(filenames):
            yield here / name


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def redact(url: str) -> str:
    """Hide the secret in a URL so a report can be pasted somewhere."""
    match = CREDENTIAL_IN_URL.match(url)
    if not match:
        return url
    return f"{match.group('scheme')}://{match.group('user')}:***@" + url[match.end() :]


def doctor_repo(path: Path, name: str, cfg: dict[str, Any]) -> list[Action]:
    """Report the things that need a decision rather than a command."""
    checks = cfg["doctor"]
    git = Git(path)
    actions: list[Action] = []

    remotes = git.out("remote", check=False).split()
    if not remotes:
        if checks["no_remote"]:
            actions.append(Action("doctor", name, "-", "no remote configured", skipped=True))
        return actions

    if checks["credentials_in_url"]:
        actions += _check_credentials(git, name, remotes)
    if checks["detached_head"] and not current_branch(git):
        commit = git.out("rev-parse", "--short", "HEAD", check=False)
        actions.append(Action("doctor", name, "HEAD", f"detached at {commit}", skipped=True))
    if checks["unpushed"]:
        actions += _check_unpushed(git, name)
    if checks["large_git_mb"]:
        actions += _check_git_size(git, name, checks["large_git_mb"])
    return actions


def _check_credentials(git: Git, name: str, remotes: Sequence[str]) -> list[Action]:
    """A token in a remote URL is a secret sitting in plain text in .git/config."""
    found: list[Action] = []
    for remote in remotes:
        url = git.out("remote", "get-url", remote, check=False)
        if CREDENTIAL_IN_URL.match(url):
            found.append(
                Action(
                    "doctor",
                    name,
                    remote,
                    f"credential in the remote URL — {redact(url)}",
                    skipped=True,
                )
            )
    return found


def _check_unpushed(git: Git, name: str) -> list[Action]:
    found: list[Action] = []
    for branch in list_branches(git):
        if not branch.upstream:
            continue
        ahead = git.out("rev-list", "--count", f"{branch.upstream}..{branch.name}", check=False)
        if ahead and ahead != "0":
            found.append(
                Action(
                    "doctor",
                    name,
                    branch.name,
                    f"{plural(ahead, 'commit')} not pushed",
                    skipped=True,
                )
            )
    return found


def _check_git_size(git: Git, name: str, limit_mb: int) -> list[Action]:
    """How big .git is, from git's own accounting rather than by walking it.

    `directory_size` would stat every loose object in every repository, which on
    a workspace of a few hundred clones costs more than the rest of the run put
    together. `count-objects -v` reports the same thing in kibibytes for free.
    """
    kib = 0
    for line in git.out("count-objects", "-v", check=False).splitlines():
        field, _, value = line.partition(": ")
        if field in ("size", "size-pack", "size-garbage") and value.strip().isdigit():
            kib += int(value)
    megabytes = kib // 1024
    if megabytes < limit_mb:
        return []
    return [Action("doctor", name, ".git", f"{megabytes} MB — consider git gc", skipped=True)]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def human_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class Printer:
    """Prints as work finishes, so a long run is not a silent one."""

    def __init__(self, stream: Any, quiet: bool, color: bool) -> None:
        self.stream = stream
        self.quiet = quiet
        self.color = color
        self._lock = threading.Lock()

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def action(self, action: Action) -> None:
        if self.quiet:
            return
        if action.error:
            mark, code = "!", "31"
        elif action.applied:
            mark, code = "+", "32"
        elif action.skipped:
            mark, code = "-", "90"
        else:
            mark, code = "~", "33"
        size = f" ({human_size(action.size)})" if action.size else ""
        detail = action.error or action.detail
        line = f"  {self.paint(mark, code)} {action.scope}: {action.target}{size}"
        if detail:
            line += f" — {detail}"
        with self._lock:
            print(line, file=self.stream)

    def heading(self, text: str) -> None:
        if not self.quiet:
            print(f"\n{self.paint(text, '1')}", file=self.stream)

    def line(self, text: str = "") -> None:
        if not self.quiet:
            print(text, file=self.stream)


def summarise(report: Report, mode: str, printer: Printer) -> None:
    kinds: dict[str, list[Action]] = {}
    for action in report.actions:
        kinds.setdefault(action.kind, []).append(action)
    printer.heading("Summary")
    for kind, actions in sorted(kinds.items()):
        done = sum(1 for a in actions if a.applied)
        skipped = sum(1 for a in actions if a.skipped)
        failed = sum(1 for a in actions if a.error)
        pending = len(actions) - done - skipped - failed
        bits = []
        if done:
            bits.append(f"{done} done")
        if pending:
            bits.append(f"{pending} pending")
        if skipped:
            bits.append(f"{skipped} skipped")
        if failed:
            bits.append(f"{failed} failed")
        printer.line(f"  {kind:<12} {', '.join(bits) or 'nothing to do'}")
    freed = report.bytes_found if mode == DRY else report.bytes_freed
    if freed:
        printer.line(
            f"  {'disk':<12} {'would free' if mode == DRY else 'freed'} {human_size(freed)}"
        )
    # Never let a safety rule quietly shrink the result: a run that held twenty
    # directories back should say so, rather than read as "that was everything".
    held = [a for a in report.actions if a.skipped and a.detail.startswith("kept:")]
    if held:
        printer.line(f"  {'held back':<12} {plural(len(held), 'item')}, listed above with -")
    if mode == DRY and any(not a.skipped and not a.error for a in report.actions):
        printer.line(
            "\n  This was a dry run. Use --ask to confirm each change, or --apply for all."
        )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@dataclass
class Context:
    workspace: Path
    resolver: ConfigResolver
    decider: Decider
    printer: Printer
    repos: list[Path]
    quarantine: Quarantine

    def name_of(self, repo: Path) -> str:
        return repo.relative_to(self.workspace).as_posix()

    def config_for(self, repo: Path) -> dict[str, Any]:
        return self.resolver.for_path(repo)

    @property
    def jobs(self) -> int:
        # Prompts must not interleave, so asking is single-threaded.
        if self.decider.mode == ASK:
            return 1
        return worker_count(self.config_for(self.workspace)["jobs"])


def _in_parallel(context: Context, work: Callable[[Path], list[Action]], report: Report) -> None:
    """Run `work(repo)` over every repo, printing results as they land."""
    jobs = context.jobs
    if jobs <= 1:
        for repo in context.repos:
            for action in _guarded(work, repo, context):
                report.add(action)
                context.printer.action(action)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(_guarded, work, repo, context) for repo in context.repos]
        try:
            for future in concurrent.futures.as_completed(futures):
                for action in future.result():
                    report.add(action)
                    context.printer.action(action)
        except Quit:  # pragma: no cover - only reachable from --ask, which is serial
            for future in futures:
                future.cancel()
            raise


def _guarded(work: Callable[[Path], list[Action]], repo: Path, context: Context) -> list[Action]:
    """One repo's failure must not take the other two hundred down with it."""
    try:
        return work(repo)
    except Failure as exc:
        return [Action("error", context.name_of(repo), "-", "", error=str(exc))]
    except OSError as exc:  # pragma: no cover - filesystem level, hard to provoke
        return [Action("error", context.name_of(repo), "-", "", error=str(exc))]


def cmd_sync(context: Context, report: Report) -> None:
    context.printer.heading("Sync")

    def work(repo: Path) -> list[Action]:
        cfg = context.config_for(repo)
        if not cfg["sync"]["enabled"]:
            return []
        return sync_repo(repo, context.name_of(repo), cfg, context.decider)

    _in_parallel(context, work, report)


def cmd_prune(context: Context, report: Report) -> None:
    context.printer.heading("Branches")
    if context.decider.dry:
        # A branch is only marked [gone] once a pruning fetch has seen that the
        # remote dropped it, and a dry run does not fetch. Saying so beats an
        # empty section that reads like "nothing to do".
        context.printer.line(
            "  a dry run does not fetch, so branches whose upstream has just "
            "vanished are not visible yet"
        )

    def work(repo: Path) -> list[Action]:
        cfg = context.config_for(repo)
        if not cfg["branches"]["enabled"]:
            return []
        return prune_branches(repo, context.name_of(repo), cfg, context.decider)

    _in_parallel(context, work, report)


def cmd_clean(context: Context, report: Report) -> None:
    context.printer.heading("Artefacts")
    root_cfg = context.config_for(context.workspace)

    def work(repo: Path) -> list[Action]:
        cfg = context.config_for(repo)
        if not cfg["clean"]["enabled"]:
            return []
        holding = context.quarantine if cfg["clean"]["quarantine"] else None
        git = Git(repo)
        name = context.name_of(repo)
        actions: list[Action] = []
        # Ignored paths first: it removes whole directories in one step, which
        # leaves the pattern walk below far less ground to cover.
        if cfg["clean"]["ignored"]:
            actions += clean_ignored(repo, name, cfg, context.decider, holding, git)
        actions += clean_tree(repo, name, cfg, context.decider, git, holding)
        return actions

    _in_parallel(context, work, report)

    # Everything outside a repository: loose caches, stray dist/ directories and
    # the __pycache__ of scripts that were never committed anywhere.
    if root_cfg["clean"]["enabled"]:
        context.printer.heading("Artefacts outside repositories")
        holding = context.quarantine if root_cfg["clean"]["quarantine"] else None
        for action in _outside_repos(context, root_cfg, holding):
            report.add(action)
            context.printer.action(action)


def _outside_repos(
    context: Context, cfg: dict[str, Any], quarantine: Quarantine | None
) -> list[Action]:
    """Clean the parts of the workspace that no repository owns.

    The walk itself is cheap and stays serial; measuring and deleting is what
    costs, so that part is spread across the pool. A single .terraform directory
    can hold a gigabyte in tens of thousands of files, and there are usually
    dozens of them.
    """
    candidates = list(_loose_artefacts(context, cfg))
    if not candidates:
        return []
    workspace = context.workspace
    regenerable = cfg["clean"]["regenerable"]

    def handle(item: tuple[Path, bool]) -> Action:
        path, is_dir = item
        return _remove(
            path,
            workspace,
            "workspace",
            context.decider,
            quarantine,
            is_dir,
            protect_nested=not _matches(path.name, regenerable),
        )

    return _map_parallel(handle, candidates, context.jobs)


def _loose_artefacts(context: Context, cfg: dict[str, Any]) -> Iterator[tuple[Path, bool]]:
    """Every artefact path outside a repository, as (path, is_directory)."""
    repo_set = {p.resolve() for p in context.repos}
    dir_patterns, file_patterns = clean_patterns(cfg["clean"])
    keep = cfg["clean"]["keep"]
    for dirpath, dirnames, filenames in os.walk(context.workspace, followlinks=False):
        here = Path(dirpath)
        if here.resolve() in repo_set or ".git" in here.parts:
            dirnames[:] = []
            continue
        descend: list[str] = []
        for name in sorted(dirnames):
            candidate = here / name
            if (
                name == QUARANTINE_DIRNAME
                or candidate.is_symlink()
                or is_repo(candidate)
                or candidate.resolve() in repo_set
            ):
                continue
            if _matches(name, dir_patterns) and not _protected(
                candidate, context.workspace, keep, set()
            ):
                yield candidate, True  # removed whole, so it is not descended into
            else:
                descend.append(name)
        dirnames[:] = descend
        for name in sorted(filenames):
            candidate = here / name
            if candidate.is_symlink() or not _matches(name, file_patterns):
                continue
            if _protected(candidate, context.workspace, keep, set()):
                continue
            yield candidate, False


def _map_parallel(work: Callable[[Any], Action], items: Sequence[Any], jobs: int) -> list[Action]:
    """Apply `work` to every item, in order, across `jobs` threads."""
    if jobs <= 1 or len(items) == 1:
        return [work(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(work, items))


def cmd_trash(context: Context, report: Report) -> None:
    cfg = context.config_for(context.workspace)
    context.printer.heading("Trash")
    if not cfg["trash"]["enabled"]:
        context.printer.line("  trash is off; set trash.enabled: true to sweep loose files")
        return
    for action in sweep_trash(
        context.workspace, cfg, context.decider, context.quarantine, context.repos
    ):
        report.add(action)
        context.printer.action(action)


def cmd_doctor(context: Context, report: Report) -> None:
    context.printer.heading("Doctor")

    def work(repo: Path) -> list[Action]:
        cfg = context.config_for(repo)
        if not cfg["doctor"]["enabled"]:
            return []
        return doctor_repo(repo, context.name_of(repo), cfg)

    _in_parallel(context, work, report)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #

INIT_HEADER = """git-tidy configuration.

Every setting is listed with its default, commented out. Uncomment the ones you
want to change; anything left commented keeps the built-in default.

This file is merged over the global config, and a .git-tidy.yaml deeper in the
tree is merged over this one, so a single repository can opt out of a rule the
workspace sets. `git-tidy config <path>` prints the result of that merge.

Docs: https://github.com/sapn95/git-tidy"""


def cmd_init(
    target: Path,
    mode: str,
    force: bool,
    printer: Printer,
    prompt_input: Callable[[str], str] | None = None,
) -> int:
    """Write a config file, asking about the choices that actually vary."""
    if target.exists() and not force:
        raise Failure(f"{target} already exists; pass --force to overwrite it")

    chosen: dict[str, Any] = {}
    if mode == ASK:
        printer.line(f"Writing {target}. Press enter to accept each default.\n")
        jobs = ask_value("How many repositories at once?", str(DEFAULTS["jobs"]), prompt_input)
        if jobs != str(DEFAULTS["jobs"]):
            try:
                chosen["jobs"] = int(jobs)
            except ValueError as exc:
                raise Failure(f"{jobs!r} is not a number") from exc
        if ask_yes_no(
            "Delete everything .gitignore already calls disposable?", False, prompt_input
        ):
            chosen.setdefault("clean", {})["ignored"] = True
        if ask_yes_no(
            "Also delete dependency directories (node_modules, .venv, vendor)?", False, prompt_input
        ):
            chosen.setdefault("clean", {})["dependencies"] = True
        if ask_yes_no("Also delete build output (dist, build, target, out)?", False, prompt_input):
            chosen.setdefault("clean", {})["builds"] = True
        if ask_yes_no("Sweep loose junk files out of the workspace?", False, prompt_input):
            trash = chosen.setdefault("trash", {})
            trash["enabled"] = True
            if ask_yes_no(
                "  Look in every non-repository directory, not just the root?", False, prompt_input
            ):
                trash["scope"] = "workspace"
        if ask_yes_no("Delete local branches whose upstream is gone?", True, prompt_input):
            chosen.setdefault("branches", {})["prune_gone"] = True
        else:
            chosen.setdefault("branches", {})["prune_gone"] = False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(chosen, INIT_HEADER), encoding="utf-8")
    # Reading it straight back catches a template that this tool's own parser
    # cannot load, which would otherwise only surface on the next run.
    _merge(DEFAULTS, read_config_file(target), str(target))
    printer.line(f"wrote {target}")
    if chosen:
        printer.line(f"  set: {', '.join(sorted(_dotted(chosen)))}")
    return 0


def _dotted(values: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(_dotted(value, f"{path}."))
        else:
            out.append(f"{path}={value}")
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_common_options(parser: argparse.ArgumentParser, after_command: bool) -> None:
    """Add the options that make sense both before and after the subcommand.

    `git-tidy clean --apply` and `git-tidy --apply clean` must both work — the
    first is what everybody types. The copies attached to the subcommands default
    to SUPPRESS, so an option that was not given there leaves whatever the
    top-level parser already decided untouched.
    """
    hide: dict[str, Any] = {"default": argparse.SUPPRESS} if after_command else {}
    parser.add_argument(
        "-C",
        "--workspace",
        metavar="DIR",
        help="the directory holding the repositories (default: the current one)",
        **({"default": argparse.SUPPRESS} if after_command else {"default": "."}),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-n",
        "--dry-run",
        dest="mode",
        action="store_const",
        const=DRY,
        help="print what would happen and change nothing (the default)",
        **hide,
    )
    mode.add_argument(
        "-i",
        "--ask",
        dest="mode",
        action="store_const",
        const=ASK,
        help="confirm every change: y/n, a/s for all of that kind, q to stop",
        **hide,
    )
    mode.add_argument(
        "--apply",
        dest="mode",
        action="store_const",
        const=AUTO,
        help="carry every change out without asking",
        **hide,
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON", **hide)
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary", **hide)
    parser.add_argument("--no-color", action="store_true", help="never colour the output", **hide)
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        metavar="N",
        help="how much to do at once (default: one per CPU core)",
        **({"default": argparse.SUPPRESS} if after_command else {"default": None}),
    )
    # Appended rather than replaced, so `git-tidy --exclude a clean --exclude b`
    # means both. The two levels land in different dests and are merged in main().
    for name, what in (("include", "only"), ("exclude", "skip")):
        parser.add_argument(
            f"--{name}",
            action="append",
            metavar="GLOB",
            dest=f"{name}_after" if after_command else name,
            help=f"{what} repositories matching this glob (repeatable)",
            **({"default": argparse.SUPPRESS} if after_command else {"default": []}),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-tidy",
        description="Keep a directory full of git checkouts clean.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Nothing changes until you pass --ask or --apply.",
    )
    parser.add_argument("--version", action="version", version=f"git-tidy {__version__}")
    add_common_options(parser, after_command=False)
    parser.set_defaults(mode=DRY)

    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help_text)
        add_common_options(child, after_command=True)
        return child

    for name, help_text in (
        ("sync", "fetch and fast-forward every repository onto its default branch"),
        ("prune", "delete local branches whose upstream is gone"),
        ("clean", "remove build output and caches"),
        ("trash", "sweep loose junk into quarantine"),
        ("doctor", "report what needs a human"),
        ("run", "sync, prune, clean, trash and doctor, in that order"),
    ):
        command(name, help_text)

    config_cmd = command("config", "print the effective configuration")
    config_cmd.add_argument("path", nargs="?", help="show the config that applies to this path")

    init_cmd = command("init", "write a commented config file")
    where = init_cmd.add_mutually_exclusive_group()
    where.add_argument(
        "--global",
        dest="global_config",
        action="store_true",
        help=f"write {global_config_path()}",
    )
    where.add_argument("--path", metavar="DIR", help="write .git-tidy.yaml in this directory")
    init_cmd.add_argument("--force", action="store_true", help="overwrite an existing file")

    restore_cmd = command("restore", "put quarantined files back")
    restore_cmd.add_argument("stamp", nargs="?", help="which quarantine (default: the newest)")
    restore_cmd.add_argument("--list", action="store_true", help="list the quarantines and stop")
    restore_cmd.add_argument(
        "--expire", action="store_true", help="delete quarantines past trash.retention_days"
    )
    return parser


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse, then fold the after-the-subcommand copies of --include/--exclude in."""
    args = build_parser().parse_args(argv)
    for name in ("include", "exclude"):
        extra = getattr(args, f"{name}_after", None)
        if extra:
            setattr(args, name, [*getattr(args, name, []), *extra])
    return args


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "jobs", None) is not None:
        if args.jobs < 1:
            raise Failure("--jobs must be at least 1")
        overrides["jobs"] = args.jobs
    return overrides


def resolve_workspace(raw: str) -> Path:
    workspace = Path(raw).expanduser().resolve()
    if not workspace.is_dir():
        raise Failure(f"{workspace} is not a directory")
    # Cleaning $HOME or / would walk the entire machine, and almost certainly is
    # not what was meant.
    if workspace == Path(workspace.anchor) or workspace == Path.home():
        raise Failure(
            f"refusing to work on {workspace}: point --workspace at the directory that holds "
            "your checkouts, not at your home or filesystem root"
        )
    return workspace


def select_repos(root: Path, cfg: dict[str, Any], args: argparse.Namespace) -> list[Path]:
    exclude = [*cfg["exclude"], *args.exclude]
    repos = find_repos(root, exclude)
    if args.include:
        repos = [
            repo
            for repo in repos
            if any(
                fnmatch.fnmatch(repo.relative_to(root).as_posix(), pattern)
                or fnmatch.fnmatch(repo.name, pattern)
                for pattern in args.include
            )
        ]
    return repos


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    printer = Printer(
        sys.stdout,
        quiet=args.quiet or args.json,
        color=not args.no_color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None,
    )
    if args.mode == ASK and not sys.stdin.isatty():
        raise Failure("--ask needs a terminal; use --apply for an unattended run")

    if args.command == "init":
        target = (
            global_config_path()
            if args.global_config
            else Path(args.path or args.workspace).expanduser().resolve() / CONFIG_NAMES[0]
        )
        return cmd_init(target, args.mode, args.force, printer)

    workspace = resolve_workspace(args.workspace)
    resolver = ConfigResolver(workspace, overrides_from(args))
    root_cfg = resolver.for_path(workspace)
    quarantine_root = workspace / QUARANTINE_DIRNAME
    decider = Decider(args.mode)

    if args.command == "config":
        target = Path(args.path).expanduser().resolve() if args.path else workspace
        print(json.dumps(resolver.for_path(target), indent=2, sort_keys=True))
        return 0

    if args.command == "restore":
        return _restore_command(args, root_cfg, quarantine_root, decider, printer)

    repos = select_repos(workspace, root_cfg, args)
    suffix = {DRY: "  (dry run — nothing will be changed)", ASK: "  (asking before each change)"}
    printer.line(f"{workspace}: {plural(len(repos), 'repository')}{suffix.get(args.mode, '')}")
    quarantine = Quarantine(quarantine_root, workspace)
    context = Context(workspace, resolver, decider, printer, repos, quarantine)
    report = Report()

    steps: dict[str, list[Callable[[Context, Report], None]]] = {
        "sync": [cmd_sync],
        "prune": [cmd_prune],
        "clean": [cmd_clean],
        "trash": [cmd_trash],
        "doctor": [cmd_doctor],
        "run": [cmd_sync, cmd_prune, cmd_clean, cmd_trash, cmd_doctor],
    }
    try:
        for step in steps[args.command]:
            step(context, report)
    except Quit:
        printer.line("\n  stopped at your request; everything already done is kept")

    manifest = quarantine.write_manifest()
    if manifest and not args.json:
        printer.line(f"\n  Quarantined files are under {quarantine.dir}")
        printer.line(f"  Undo with: git-tidy -C {workspace} restore {quarantine.stamp} --apply")

    if args.json:
        print(
            json.dumps(
                {
                    "version": __version__,
                    "workspace": str(workspace),
                    "mode": args.mode,
                    "repositories": len(repos),
                    "bytes": report.bytes_found if args.mode == DRY else report.bytes_freed,
                    "actions": [a.as_dict() for a in report.actions],
                },
                indent=2,
            )
        )
    else:
        summarise(report, args.mode, printer)
    return 1 if report.errors else 0


def _restore_command(
    args: argparse.Namespace,
    cfg: dict[str, Any],
    quarantine_root: Path,
    decider: Decider,
    printer: Printer,
) -> int:
    if args.list:
        if not quarantine_root.is_dir():
            printer.line("no quarantines")
            return 0
        for entry in sorted(quarantine_root.iterdir()):
            manifest = entry / MANIFEST_NAME
            if manifest.is_file():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                printer.line(f"  {entry.name}  {len(data['entries'])} entries")
        return 0
    try:
        actions = (
            expire_quarantines(quarantine_root, cfg["trash"]["retention_days"], decider)
            if args.expire
            else restore(quarantine_root, args.stamp, decider)
        )
    except Quit:
        actions = []
    report = Report(list(actions))
    for action in actions:
        printer.action(action)
    summarise(report, args.mode, printer)
    return 1 if report.errors else 0


def entrypoint() -> NoReturn:
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"git-tidy: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\ngit-tidy: interrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    entrypoint()
