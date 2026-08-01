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

  git-tidy clean    Remove build output and caches: .terraform, __pycache__,
                    .pytest_cache and whatever else the config names, plus
                    everything .gitignore calls disposable once clean.ignored is
                    on. Dependency trees such as node_modules are off until
                    clean.dependencies says otherwise. Inside a repo, only
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
strict parser for the documented subset stands in when it is not. The two agree,
and where they cannot they both refuse. The released binaries carry no PyYAML,
so they always use the second one.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
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

__version__ = "3.0.3"

CONFIG_NAMES = (".git-tidy.yaml", ".git-tidy.yml")
QUARANTINE_DIRNAME = ".git-tidy-trash"
MANIFEST_NAME = "manifest.json"
# Swept content lives under here, not directly in the stamp directory: a file
# of the user's called manifest.json would otherwise land on git-tidy's own and
# be overwritten by it.
CONTENT_DIRNAME = "files"
# Appended to as files move, and folded into the manifest at the end. It is what
# survives a run that was killed part way through.
JOURNAL_NAME = "journal.jsonl"

# A URL with a password or token in it, e.g. https://user:token@host/repo.git.
# Bitbucket and GitLab hand these out from their web UI, and a clone made that way
# leaves the secret sitting in .git/config in plain text.
# Two shapes. Any scheme with `user:secret@` is a credential. A *bare* `token@`
# is one only over http and https, which is how every GitHub and GitLab personal
# access token is pasted — over ssh a bare username is `ssh://git@host`, which is
# how everyone's SSH remote looks and is not a secret at all.
# Case-insensitive, because HTTPS:// is a URL too.
CREDENTIAL_IN_URL = re.compile(
    r"^(?:(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<user>[^/@:]*):(?P<secret>[^/@]*)@"
    r"|(?P<webscheme>https?)://(?P<token>[^/@:]+)@)",
    re.IGNORECASE,
)

# Vowel-free stretches and short repeated units are what a hand mashed on a
# keyboard looks like; real names very rarely produce either.
CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}", re.IGNORECASE)
ALPHA_ONLY = re.compile(r"^[a-z]+$", re.IGNORECASE)

DRY, ASK, AUTO = "dry", "ask", "auto"


class Failure(Exception):
    """A problem worth reporting to the user without a traceback."""


class Quit(Exception):
    """Raised when the user answers 'q': stop, but keep and report what is done.

    Carries the actions accumulated so far, so a repository that had already
    removed twenty things before the prompt still reports them.
    """

    def __init__(self, done: list[Action] | None = None) -> None:
        super().__init__("stopped at your request")
        self.done: list[Action] = done or []


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
        "stash": False,
        "worktrees": "skip",
        "diverged": "report",
        "timeout": 120,
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
        "sensitive": [
            "*.pw",
            "*.secret",
            "*.pem",
            "*.key",
            "*.p12",
            "id_rsa*",
            "*creds*",
            "*token*",
            "*password*",
        ],
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
    "sync.fast_forward": "Only ever fast-forward: never a merge commit. A diverged\n"
    "repository is reported and left alone unless sync.diverged says otherwise.",
    "sync.prune": "Drop remote-tracking refs for branches deleted on the remote.",
    "sync.prune_tags": "Off by default: a tag that only exists locally also counts as\n"
    "gone, and deleting it loses it.",
    "sync.submodules": "none:   leave them alone\n"
    "init:   init and update missing ones\n"
    "update: also move existing ones onto the recorded commit. Never forced: a\n"
    "        submodule with uncommitted work is left alone and reported.",
    "sync.gc": "Repack loose objects when git itself thinks it is worth it.",
    "sync.worktrees": "What to do with a linked worktree (one made by `git worktree add`).\n"
    "  skip   — leave it on its branch: holding a branch of its own is the\n"
    "           entire reason it exists, and git allows one worktree per branch\n"
    "  switch — treat it like any other checkout. Since git allows one worktree\n"
    "           per branch, whichever one does not get the default branch first\n"
    "           is reported as already checked out elsewhere, and left alone",
    "sync.diverged": "A branch with local commits *and* commits upstream.\n"
    "report: say so and change nothing\n"
    "rebase: replay the local commits on top of the upstream ones. The originals\n"
    "        stay reachable through the reflog, but the commit ids change, so\n"
    "        this is off by default.",
    "sync.stash": "Stash uncommitted work rather than refusing to move. This is what\n"
    "--force turns on: the switch and the fast-forward then happen, and the\n"
    "changes come back with `git stash pop`. Nothing is discarded — a stash is\n"
    "the only way to force this that does not throw away work git cannot\n"
    "recover.",
    "sync.timeout": "Seconds any one git command may take. A remote behind a VPN that has\n"
    "just dropped does not refuse the connection, it stops answering, and\n"
    "without a bound the run simply appears to hang. Lower it for a workspace\n"
    "full of remotes that are sometimes unreachable.",
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
    "clean.ignored_keep": "Never deleted: local state and credentials that are ignored\n"
    "precisely because they must not be committed. A directory holding one is\n"
    "emptied out around it — the file stays exactly where it is, because an\n"
    "application reads it from that path, and the rest of the directory is\n"
    "still reclaimed.\n"
    "Two things it does not cover. Inside a path named in clean.regenerable it\n"
    "does not apply at all: those are caches a tool rebuilds, and every\n"
    ".terraform holds a terraform.tfstate that `terraform init` writes again.\n"
    "And the source-code exemption that applies to trash.sensitive does not\n"
    "apply here: a gitignored secrets.py is not committed source, and being\n"
    "local and untracked is exactly why this list names it.",
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
    "temp:  *~ *.swp *.swo *.orig *.rej *.bak *.tmp *.old. Note the last two:\n"
    "       with trash.dirs on, a project.old/ somebody parked goes whole.",
    "trash.sensitive": "Reported as sensitive and always quarantined rather than\n"
    "deleted, even with quarantine off, because a token may be the only copy.\n"
    "A file with a source-code extension is exempt: pygments/token.py matches\n"
    "*token* and is not a secret. Inside a directory that is being removed, the\n"
    "matches are lifted into quarantine and the rest of it still goes.",
    "trash.min_age_days": "Nothing younger than this is touched, so today's scratch\n"
    "file survives.",
    "trash.keep": "Never swept, whatever else matches. patterns win over the\n"
    "heuristics, not over this.",
    "trash.dirs": "Consider directories too. A directory of junk is much more often a\n"
    "project somebody forgot about, so this is off.",
    "trash.quarantine": "Move to quarantine instead of deleting. Strongly recommended.",
    "trash.retention_days": "How long a quarantine is kept. Anything older goes at the end\n"
    "of a clean, trash or run that applied something, or straight away with\n"
    "`git-tidy restore --expire`. 0 keeps them for ever.",
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


def _inside_quote(ch: str, quote: str, escaped: bool) -> tuple[str | None, bool]:
    """Track a quoted run inside a flow collection, honouring \\" as an escape.

    Without the escape, `keep: ["a\\"b", c]` was an unterminated quote here and
    loaded fine with PyYAML — and _strip_comment, one function up, already knew
    about it, so the two disagreed with each other.
    """
    if escaped:
        return quote, False
    if quote == '"' and ch == "\\":
        return quote, True
    return (None if ch == quote else quote), False


def _strip_comment(line: str) -> str:
    """Drop a trailing comment, respecting quotes.

    A '#' only starts a comment at the beginning of the content or after a space,
    so `url: http://x#y` keeps its fragment.

    A quote only opens one where a value could actually begin: at the start, or
    directly after one of `:,[{-` and any spaces. Otherwise the apostrophe in
    `name: it's fine  # note` would be read as an opening quote and the comment
    after it would survive into the value — and, the other way round, so would
    the one in `a: x 'y # z`, which PyYAML reads as the plain scalar `x 'y`.
    Merely "after a space" was not enough: it is only a place a value can begin
    if nothing has been written since the last delimiter.
    """
    out: list[str] = []
    quote: str | None = None
    can_open = True  # nothing written yet, so a value could start here
    escaped = False
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            # _inside_quote rather than a look at line[i - 1]: that read `\\"`
            # as an escaped quote when the backslash was itself escaped, so a
            # value ending in a backslash never closed and the whole config was
            # refused — while PyYAML read it without complaint.
            quote, escaped = _inside_quote(ch, quote, escaped)
            continue
        if ch in "\"'" and can_open:
            quote = ch
            out.append(ch)
            continue
        if ch in ":,[{-":
            can_open = True
        elif ch not in " \t":
            can_open = False
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _yaml_printable(char: str) -> bool:
    """PyYAML's Reader.NON_PRINTABLE, in the positive."""
    code = ord(char)
    return (
        char in "\t\n\r\x85"
        or 0x20 <= code <= 0x7E
        or 0xA0 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _outside_quotes(text: str) -> str:
    """The parts of a line that are not inside a quoted scalar."""
    out: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        out.append(char)
    return "".join(out)


def _refuse_control_characters(text: str, source: str, number: int) -> None:
    """PyYAML refuses these outright, so this has to as well.

    A tab anywhere outside a quoted scalar is a ScannerError there — `jobs: 4\t`
    is fatal with PyYAML installed and ran normally without it, which is the
    "works from a checkout, fails on every shipped build" shape in the other
    direction. NUL and the other C0 controls are the same.
    """
    # Two different refusals, from two different parts of PyYAML.
    #
    # Its Reader rejects anything outside a printable set, wherever it appears —
    # inside a quoted scalar as much as outside one. Checking only outside the
    # quotes let \x00 and \x7f through in a quoted value, which loads on every
    # shipped binary and is a ReaderError from a checkout.
    for char in text:
        if not _yaml_printable(char):
            raise Failure(f"{source}:{number}: character {ord(char):#04x} is not allowed in YAML")
    # Its Scanner rejects a tab used as structure. A tab *inside* a quoted
    # scalar is ordinary text, which is why this half looks outside them.
    if "\t" in _outside_quotes(text):
        raise Failure(f"{source}:{number}: a tab on this line; YAML wants spaces")


# Exactly the breaks PyYAML's scanner recognises. str.splitlines() adds \x0b,
# \x0c and \x1c-\x1e, which PyYAML refuses outright; split("\n") drops \r, \x85
# and \u2028, which PyYAML honours. Either way the same file meant two things
# depending on whether PyYAML happened to be installed, which is the one thing
# this parser exists not to do.
_YAML_BREAKS = re.compile(r"\r\n|\r|\n|\x85|\u2028|\u2029")


def _yaml_lines(text: str) -> list[str]:
    return _YAML_BREAKS.split(text)


def _tokenize(text: str, source: str) -> list[_Line]:
    # PyYAML strips this; an editor on Windows writes it. Without this the first
    # key becomes "\ufeffjobs" and the whole config is refused — but only where
    # PyYAML is absent, which is every shipped binary.
    text = text.lstrip("\ufeff")
    lines: list[_Line] = []
    ended = False
    for number, raw in enumerate(_yaml_lines(text), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise Failure(f"{source}:{number}: tabs cannot be used for YAML indentation")
        content = _strip_comment(raw)
        # After the comment is gone, and before the trailing whitespace is: a
        # tab in `jobs: 4  # workers\t(one per core)` is fine to PyYAML and used
        # to refuse the whole config on every shipped binary, while a tab *after*
        # the value is a ScannerError there and used to be stripped away here.
        _refuse_control_characters(content, source, number)
        content = content.rstrip()
        if not content.strip():
            continue
        if content.strip() == "...":
            ended = True
            continue
        if content.strip() == "---":
            if lines or ended:
                # A second document. PyYAML refuses one outright; merging them
                # here silently dropped whichever key came first.
                raise Failure(
                    f"{source}:{number}: more than one YAML document; this reads only one"
                )
            continue
        if content.lstrip().startswith("---"):
            # `--- {jobs: 4}` is one document with content on the marker line,
            # and `---jobs: 4` is a key called ---jobs. Dropping the line
            # outright threw the first away in silence and left the config
            # empty; PyYAML reads both. Neither is worth supporting, but
            # neither may pass quietly.
            raise Failure(f"{source}:{number}: put the document on its own lines, not after ---")
        if ended:
            raise Failure(f"{source}:{number}: content after the end of the document")
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
        # YAML starts a mapping on ": " or on a colon at end of line — not on any
        # colon. `ignored:true` is a plain scalar to PyYAML, so a config written
        # that way was a fatal error from a checkout and a working instruction to
        # delete things from every shipped binary, which carries no PyYAML.
        key, sep, rest = line.text.partition(": ")
        if not sep and line.text.endswith(":"):
            key, sep, rest = line.text[:-1], ":", ""
        if key.startswith(("[", "{")):
            # _scalar would hand back a list or a dict, and an unhashable dict
            # key is a bare TypeError rather than anything a person can act on.
            # PyYAML refuses it too, so refusing is also the agreeing answer.
            raise Failure(f"{source}:{line.number}: a list or mapping cannot be a key")
        if not sep:
            raise Failure(
                f"{source}:{line.number}: expected 'key: value' — a colon needs a space "
                f"after it, or nothing at all"
            )
        key, rest = key.strip(), rest.strip()
        index += 1
        if rest:
            result[_scalar(key, source, line.number)] = _scalar(rest, source, line.number)
            continue
        # An empty value means either a nested block on the following lines, or
        # genuinely nothing. A block sequence is allowed to sit at the parent
        # key's own column — that is the style yaml.dump() emits, and the
        # commonest way anyone writes a list by hand.
        if (
            index < len(lines)
            and lines[index].indent == indent
            and (lines[index].text.startswith("- ") or lines[index].text == "-")
        ):
            child, index = _parse_sequence(lines, index, indent, source)
            result[_scalar(key, source, line.number)] = child
            continue
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
            # The sequence ends here. At the parent key's own column the next
            # mapping key sits at this same indent, so stopping is what YAML
            # means — refusing would reject `exclude:` / `- a` / `jobs: 2`.
            break
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
    """True for `key: value` and `key:`, false for a plain or flow scalar.

    YAML starts a mapping on ": " or on a colon that ends the item — not on any
    colon at all. Without that, `- https://internal/mirror` in a list came out
    as {"https": "//internal/mirror"} here and as a string under PyYAML, so a
    config with a URL in it loaded from a checkout and failed on every shipped
    build.
    """
    if item.startswith(("[", "{", '"', "'")):
        return False
    if item.endswith(":"):
        return " " not in item[:-1].strip()
    key, sep, _ = item.partition(": ")
    return bool(sep) and " " not in key.strip()


def _split_flow(body: str, source: str, number: int) -> list[str]:
    """Split `a, b, [c, d]` on top-level commas only."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    escaped = False
    for ch in body:
        if quote:
            current.append(ch)
            quote, escaped = _inside_quote(ch, quote, escaped)
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


# YAML 1.1 resolution, as PyYAML's safe loader implements it. Copied deliberately
# rather than approximated: this parser only stands in when PyYAML is absent, and
# a config that means one thing with it installed and another without would make
# behaviour depend on the environment rather than on what was written.
_YAML_BOOL_TRUE = frozenset(["yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"])
_YAML_BOOL_FALSE = frozenset(["no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"])
_YAML_NULL = frozenset(["~", "null", "Null", "NULL", ""])
_YAML_INT = re.compile(
    r"""^(?:[-+]?0b[01_]+
        |[-+]?0[0-7_]+
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0x[0-9a-fA-F_]+
        |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$""",
    re.VERBOSE,
)
_YAML_FLOAT = re.compile(
    r"""^(?:[-+]?[0-9][0-9_]*\.[0-9_]*(?:[eE][-+][0-9]+)?
        |\.[0-9_]+(?:[eE][-+][0-9]+)?
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
    re.VERBOSE,
)
# Characters PyYAML refuses to start a plain scalar with. Accepting them here
# would mean `keep: *.pem` works without PyYAML and fails with it — and a glob is
# the most natural thing anyone writes in this config.
_YAML_INDICATORS = "*&!|>%@`,]}#"
# YAML 1.1 resolves these to a date or a datetime, which is never a valid value
# for any setting here — but PyYAML does it and this parser did not.
# Copied from PyYAML's resolver rather than approximated: an over-eager version
# refused `2024-1-2`, which PyYAML reads as an ordinary string, so a config
# loaded from a checkout and failed on every shipped build.
_YAML_TIMESTAMP = re.compile(
    r"""^(?:\d{4}-\d\d-\d\d
        |\d{4}-\d\d?-\d\d?(?:[Tt]|[ \t]+)\d\d?:\d\d:\d\d(?:\.\d*)?
           (?:[ \t]*(?:Z|[-+]\d\d?(?::\d\d)?))?)$""",
    re.VERBOSE,
)


def _yaml_int(token: str) -> int:
    body = token.replace("_", "")
    sign = -1 if body.startswith("-") else 1
    body = body.lstrip("+-")
    if ":" in body:  # sexagesimal, which YAML 1.1 still has
        value = 0
        for part in body.split(":"):
            value = value * 60 + int(part)
        return sign * value
    if body[:2].lower() == "0b":
        return sign * int(body[2:], 2)
    if body[:2].lower() == "0x":
        return sign * int(body[2:], 16)
    if body.startswith("0") and len(body) > 1:
        return sign * int(body, 8)
    return sign * int(body)


def _yaml_float(token: str) -> float:
    body = token.replace("_", "")
    if body.lstrip("+-").lower() == ".inf":
        return float("-inf") if body.startswith("-") else float("inf")
    if body.lower() == ".nan":
        return float("nan")
    if ":" in body:
        sign = -1 if body.startswith("-") else 1
        whole, _, fraction = body.lstrip("+-").partition(".")
        value = 0.0
        for part in whole.split(":"):
            value = value * 60 + float(part)
        return sign * (value + (float("0." + fraction) if fraction else 0.0))
    return float(body)


def _scalar(token: str, source: str, number: int) -> Any:
    """Read one scalar, or a flow collection, the way PyYAML's safe loader would."""
    token = token.strip()
    flow = _flow_collection(token, source, number)
    if flow is not _NOT_FLOW:
        return flow
    if not token:
        return None  # an empty scalar is null; indexing it below would not be
    if token[0] in "\"'" and not (len(token) >= 2 and token[-1] == token[0]):
        # Refused for the same reason an unbalanced bracket is: read as a plain
        # scalar, `keep: "docs/*` becomes a pattern that can never match, and
        # the directory it was written to protect is deleted. PyYAML rejects it.
        raise Failure(f"{source}:{number}: unterminated quote in {token!r}")
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        body = token[1:-1]
        if token[0] == '"':
            return _unescape(body, source, number)
        # In a single-quoted YAML scalar '' is a literal apostrophe.
        return body.replace("''", "'")
    return _plain_scalar(token, source, number)


# The escapes PyYAML's safe loader understands in a double-quoted scalar. Left
# literal, "a\tb" was a five-character glob here and a four-character one with
# PyYAML installed — silent either way, and the pattern simply stopped matching.
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_ESCAPE_HEX = {"x": 2, "u": 4, "U": 8}


def _unescape(body: str, source: str, number: int) -> str:
    """Resolve the backslash escapes of a double-quoted YAML scalar."""
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(body):
            raise Failure(f"{source}:{number}: a double-quoted value ends in a backslash")
        marker = body[index + 1]
        if marker in _ESCAPE_HEX:
            width = _ESCAPE_HEX[marker]
            digits = body[index + 2 : index + 2 + width]
            if len(digits) != width or any(d not in "0123456789abcdefABCDEF" for d in digits):
                raise Failure(f"{source}:{number}: \\{marker} needs {width} hex digits after it")
            out.append(chr(int(digits, 16)))
            index += 2 + width
            continue
        if marker not in _ESCAPES:
            # PyYAML refuses these, and guessing would make the same file mean
            # two things depending on whether PyYAML happens to be installed.
            raise Failure(f"{source}:{number}: unknown escape '\\{marker}' in a quoted value")
        out.append(_ESCAPES[marker])
        index += 2
    return "".join(out)


# A sentinel, because None is itself a perfectly good parsed value.
_NOT_FLOW = object()


def _flow_collection(token: str, source: str, number: int) -> Any:
    """`[a, b]` or `{a: 1}`, or _NOT_FLOW when the token is neither."""
    # An opening bracket with no closing one is a mistake, not a string that
    # happens to start with a bracket. Saying so beats silently reading
    # `keep: [main, release/*` as one long name.
    #
    # Only when it *opens* with one, though: `fixtures/[0-9]` merely ends with a
    # bracket, is a perfectly ordinary fnmatch character class, and PyYAML reads
    # it as the string it is. Refusing it here meant the config loaded from a
    # checkout and failed on every shipped binary.
    if token.startswith("[") and not token.endswith("]"):
        raise Failure(f"{source}:{number}: unbalanced [ ] in {token!r}")
    if token.startswith("{") and not token.endswith("}"):
        raise Failure(f"{source}:{number}: unbalanced {{ }} in {token!r}")
    if token.startswith("["):
        items = _split_flow(token[1:-1], source, number)
        for item in items:
            plain = not item.startswith(("[", "{", '"', "'"))
            special = ("?", "[", "]", "{", "}")
            if plain and (
                any(ch in item for ch in special) or item[:1] == ":" or item.endswith(":")
            ):
                raise Failure(
                    f"{source}:{number}: {item!r} means something special inside [ ]; "
                    f'quote it, as "{item}"'
                )
        return [_scalar(item, source, number) for item in items]
    if token.startswith("{"):
        mapping: dict[str, Any] = {}
        for part in _split_flow(token[1:-1], source, number):
            key, sep, value = part.partition(":")
            if not sep:
                raise Failure(f"{source}:{number}: expected 'key: value' inside {{...}}")
            mapping[_scalar(key, source, number)] = _scalar(value, source, number)
        return mapping
    return _NOT_FLOW


def _plain_scalar(token: str, source: str = "<config>", number: int = 0) -> Any:
    """An unquoted token: null, a bool, a number, or the text itself."""
    if not token:
        return None  # an empty scalar is null, as it is in _YAML_NULL below
    if token.startswith("-") and token[1:2] in ("", " "):
        # PyYAML reads this as a nested sequence, which nothing here has a use
        # for, and refuses it in a value position. Refusing it too keeps the two
        # parsers from disagreeing about what the config says.
        raise Failure(
            f"{source}:{number}: a value cannot start with '-' on its own; quote it, "
            "or put the list on its own lines"
        )
    if token.endswith(":"):
        # PyYAML reads a trailing colon as the start of a mapping and refuses it
        # where a scalar belongs, so `remote: upstream:` was an error there and
        # the string "upstream:" here — a remote name that matches nothing.
        raise Failure(
            f"{source}:{number}: {token!r} ends in a colon; quote it if the colon "
            "is part of the value"
        )
    if ": " in token:
        # `enabled: a: b` is a ScannerError in PyYAML and was a string here, so
        # the same file meant two different things depending on whether PyYAML
        # happened to be installed. This parser's whole stance is to refuse what
        # it cannot read rather than guess, so it refuses.
        raise Failure(
            f"{source}:{number}: {token!r} has a second ': ' in it; quote the value "
            "if the colon is part of it"
        )
    if token[:1] in _YAML_INDICATORS:
        raise Failure(
            f"{source}:{number}: a value starting with {token[0]!r} means something "
            f'special in YAML; quote it, as "{token}"'
        )
    if token in _YAML_NULL:
        return None
    if token in _YAML_BOOL_TRUE:
        return True
    if token in _YAML_BOOL_FALSE:
        return False
    if _YAML_INT.match(token):
        return _yaml_int(token)
    if _YAML_FLOAT.match(token):
        return _yaml_float(token)
    if _YAML_TIMESTAMP.match(token):
        # PyYAML resolves this to a date, which _merge then refuses because it
        # is not text. Refusing it here keeps both paths saying the same thing.
        raise Failure(
            f"{source}:{number}: {token!r} reads as a date; quote it if it is meant as text"
        )
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
    ambiguous = text in _YAML_BOOL_TRUE | _YAML_BOOL_FALSE | _YAML_NULL
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
            if value is None:
                # `ignored_keep:` with nothing after it used to mean "empty", and
                # emptying that list is how .env and *.tfstate become deletable.
                # The file `git-tidy init` writes is full of commented-out list
                # headers, so uncommenting one line and not the entries under it
                # is a single keystroke away — and it read as a deliberate
                # instruction to protect nothing. `[]` still says that, out loud.
                raise Failure(
                    f"{where}: {key!r} has nothing after the colon. Write the entries "
                    f"under it, or `{key}: []` if you really mean an empty list"
                )
            elif not isinstance(value, list):
                raise Failure(f"{where}: {key!r} takes a list")
            else:
                for item in value:
                    if not isinstance(item, str):
                        # fnmatch would raise deep inside a worker instead.
                        raise Failure(
                            f"{where}: every entry of {key!r} must be text, "
                            f"not {type(item).__name__} ({item!r})"
                        )
                result[key] = value
        else:
            _check_scalar(base[key], value, key, where)
            result[key] = value
    return result


def _check_scalar(default: Any, value: Any, key: str, where: str) -> None:
    """Refuse a value whose type would change what the setting means.

    `enabled: "false"` is a non-empty string, and a non-empty string is true.
    `retention_days: false` is a bool, and a bool is an int in Python, so it
    would sail through as zero days and expire every quarantine. `quarantine:`
    with nothing after it is null, which is falsy — and would delete rather than
    quarantine. None of those can be allowed to pass quietly in a tool that
    deletes things.
    """
    if value is None:
        raise Failure(f"{where}: {key!r} has no value; remove the line to keep the default")
    expected = type(default)
    if isinstance(default, bool) != isinstance(value, bool) or not isinstance(value, expected):
        raise Failure(
            f"{where}: {key!r} takes {expected.__name__}, not {type(value).__name__} ({value!r})"
        )
    if isinstance(value, int) and not isinstance(value, bool) and value < 0:
        # None of the numbers here has a meaning below zero, and a negative
        # retention_days moved the expiry cutoff into the *future*, which took
        # the quarantine the running command had just written.
        raise Failure(f"{where}: {key!r} cannot be negative ({value!r})")


def read_config_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
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
                answer = self._per_kind.get(action.consent_key)
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
                self._per_kind[action.consent_key] = True
                return True
            if choice == "s":
                self._per_kind[action.consent_key] = False
                return False
            if choice == "q":
                raise Quit()
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


def orphaned_worktree(path: Path) -> str | None:
    """The gitdir a linked worktree points at, when it is no longer there.

    `git worktree prune` in the parent removes the admin directory but leaves
    the files, so what is left looks like a repository and behaves like nothing
    at all — every git command in it fails with "not a git repository: (null)".
    Recognising it turns that into something a person can act on.
    """
    marker = path / ".git"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:") :].strip())
    if not target.is_absolute():
        target = (path / target).resolve()
    return None if target.exists() else str(target)


def cannot_look(path: Path) -> bool:
    """True when `path` cannot be listed, so no answer about it is reliable.

    Asked outright rather than inferred from an exception, because the two
    supported interpreters disagree: Path.is_dir() raises PermissionError on
    3.10 and 3.11 for a directory that cannot be read, and simply returns False
    from 3.12 on. Neither "crash the run" nor "there is nothing in there" is
    what an unreadable directory means to a guard deciding whether deleting
    something would destroy a repository — and the same workspace behaving
    differently on two interpreters is its own defect.
    """
    try:
        with os.scandir(path):
            return False
    except OSError:
        return True


def is_repo(path: Path) -> bool:
    """True for a work tree root. A .git *file* means a worktree or submodule.

    Strict on purpose: this answers "is this a checkout we can sync", and one
    that cannot be read is not. holds_git_data is the guard, and it is not.
    """
    dot_git = path / ".git"
    try:
        return dot_git.is_dir() or dot_git.is_file()
    except OSError:
        return False


def holds_git_data(path: Path) -> bool:
    """True for a repository in any form, bare included — or possibly one.

    is_repo answers "is this a checkout we can sync", which a bare clone is not.
    This answers "would deleting this destroy a repository", which it certainly
    would: `git clone --bare` and `--mirror` leave no .git entry at all, so every
    guard that looked for one walked straight past them. A directory nobody can
    list gets the same answer, for the same reason.
    """
    if is_repo(path):
        return True
    try:
        if (path / "HEAD").is_file() and (path / "objects").is_dir():
            return (path / "refs").is_dir()
    except OSError:
        return True
    return cannot_look(path)


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
    # Moved to the quarantine rather than deleted, so its bytes are still on
    # disk and must not be counted as reclaimed.
    quarantined: bool = False
    # The part of `size` that was lifted into the quarantine instead of being
    # deleted, when only part of a directory was. Without it a node_modules that
    # gave back 9 KB and moved 98 KB reported 107 KB freed.
    kept_size: int = 0
    # This path has been decided and no later step should look at it again —
    # removed, quarantined, declined, or refused for what is inside it. Set by
    # _remove, which is where all four of those happen.
    #
    # It exists because the alternative was reading the decision back out of the
    # detail string, and that went wrong twice: once matching too little, so
    # clean.dirs deleted a path clean.ignored had just refused, and once
    # matching too much, so clean.ignored stopped clean.dirs reclaiming
    # anything at all.
    settled: bool = False

    @property
    def consent_key(self) -> str:
        """What "all of these" and "skip these" mean, for this action.

        The kind alone is not enough: answering `a` to "quarantine directory
        because it is cache-creds" then hard-deleted every later artefact
        without asking, because both were kind "remove". The code already splits
        stash+switch from switch for exactly this reason, and the summary
        already counts remove+quarantined separately.
        """
        return f"{self.kind}+quarantined" if self.quarantined else self.kind

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "target": self.target,
            "detail": self.detail,
            "size": self.size,
            "kept_size": self.kept_size,
            "applied": self.applied,
            "skipped": self.skipped,
            "quarantined": self.quarantined,
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
        """Bytes actually reclaimed. Quarantined files are still on the disk."""
        return sum(a.size for a in self.actions if a.applied and not a.quarantined)

    @property
    def bytes_quarantined(self) -> int:
        return sum(a.size for a in self.actions if a.applied and a.quarantined)

    @property
    def bytes_found(self) -> int:
        """Bytes a dry run would actually reclaim. Quarantined ones only move."""
        return sum(
            a.size for a in self.actions if not a.error and not a.skipped and not a.quarantined
        )

    @property
    def bytes_found_quarantined(self) -> int:
        """What a dry run predicts will move rather than go.

        kept_size as well, or the dry run under-reports one of the two totals it
        exists to predict: a directory thinned around a 200 KB key showed 300 KB
        "to free" against a 500 KB line, with nothing accounting for the rest.
        """
        return sum(a.size for a in self.actions if not a.error and not a.skipped and a.quarantined)

    @property
    def bytes_kept_in_place(self) -> int:
        """Protected bytes left exactly where they were, inside a thinned tree.

        Neither freed nor moved, so it belongs to neither total — and saying
        nothing about it left a directory's size unaccounted for.
        """
        return sum(a.kept_size for a in self.actions if not a.error and not a.skipped)

    @property
    def errors(self) -> list[Action]:
        return [a for a in self.actions if a.error]


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #


def default_branch(git: Git, cfg: dict[str, Any], readonly: bool = False) -> str | None:
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
    # Both cases are answered by asking the remote again, which writes a ref and
    # goes to the network, so a dry run does neither.
    if not readonly and git.ok("remote", "set-head", remote, "--auto"):
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
    """Whether the worktree holds anything uncommitted — untracked files included.

    An untracked file is uncommitted work by any ordinary reading, and the README
    promises such a repository stays on its branch. Excluding them also opened a
    real hole: switch away from a feature branch, and a file the *default*
    branch's .gitignore happens to match becomes fair game for clean.ignored.

    Ignored files do not count. They are what clean.ignored exists to remove, and
    treating them as work would freeze every repository that has ever been built.
    """
    status = git.out("status", "--porcelain", "--untracked-files=normal", check=False)
    return bool(status)


def current_branch(git: Git) -> str:
    """The checked-out branch, or "" when HEAD is detached.

    Not --short: once a tag of the same name exists, git disambiguates by
    shortening refs/heads/main to "heads/main" rather than "main". Everything
    downstream then compares, switches and deletes the wrong name — and the
    repository silently stops being fast-forwarded.
    """
    ref = git.out("symbolic-ref", "--quiet", "HEAD", check=False)
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ""


def sync_repo(path: Path, name: str, cfg: dict[str, Any], decider: Decider) -> list[Action]:
    """Fetch, then fast-forward this repo onto its default branch."""
    sync = cfg["sync"]
    git = Git(path, timeout=int(sync["timeout"]))
    actions: list[Action] = []
    remote = sync["remote"]

    if not git.out("remote", check=False):
        # doctor reports this too, and counting it twice made a workspace with
        # twenty remote-less clones report forty. Its wording keeps it out of
        # the held-back tally; doctor's is the one that counts.
        return [Action("sync", name, "-", "nothing to fetch: no remote", skipped=True)]
    if remote not in git.out("remote", check=False).split():
        return [Action("sync", name, remote, "no such remote", skipped=True)]

    fetch = Action("fetch", name, remote, "fetch")
    # A configuration value is only validated once the code reaches it, which is
    # after the fetch. Losing the fetch on the way out would report nothing for
    # a run that pruned remote-tracking refs.
    with reporting(actions, name):
        # Accumulates into `actions`, so there is something to report even when
        # a later configuration value turns out to be invalid.
        actions[:] = _sync_from(
            git, name, sync, remote, fetch, actions, decider, cfg["clean"]["ignored_keep"]
        )
    return actions


# Substrings of git's own message that mean "this is the network, not this
# repository". Kept narrow on purpose: a wrong guess here would abandon a run
# over one broken remote URL.
# NOT "unable to access": git prefixes every HTTP failure with it, including
# `The requested URL returned error: 403`, which is a repository you cannot read
# rather than a network you cannot reach. The README promises that one carries
# on, and abandoning the run also meant clean, trash and doctor never ran.
OFFLINE_SIGNS = (
    "could not resolve host",
    "could not resolve hostname",
    "connection timed out",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "no route to host",
    "operation timed out",
    "failed to connect",
    "proxy connect",
    "ssl connect error",
    "timed out after",
    "temporary failure in name resolution",
    "kex_exchange_identification",
    "port 22: ",
)
# How many in a row before it is the network rather than the repositories.
OFFLINE_AFTER = 3


def looks_unreachable(message: str) -> bool:
    """Whether a failed fetch says more about the network than the repository."""
    lowered = message.lower()
    return any(sign in lowered for sign in OFFLINE_SIGNS)


def _sync_from(
    git: Git,
    name: str,
    sync: dict[str, Any],
    remote: str,
    fetch: Action,
    actions: list[Action],
    decider: Decider,
    ignored_keep: Sequence[str] = (),
) -> list[Action]:
    fetch_args = ["fetch", remote, "--quiet"]
    if sync["prune"]:
        fetch_args.append("--prune")
    if sync["prune_tags"]:
        fetch_args.append("--prune-tags")
    if decider.allow(fetch):
        result = git.run(*fetch_args, check=False)
        if result.returncode != 0:
            fetch.error = last_line(result)
            return [fetch]
        fetch.applied = True
        fetch.detail = "fetched"
    actions.append(fetch)

    branch = default_branch(git, sync, readonly=decider.dry)
    if branch is None:
        actions.append(Action("sync", name, "-", "no default branch found", skipped=True))
        return actions

    target = f"{remote}/{branch}"
    if not git.ok("show-ref", "--verify", "--quiet", f"refs/remotes/{target}"):
        actions.append(Action("sync", name, target, "remote branch missing", skipped=True))
        return actions

    head = current_branch(git)
    if head != branch:
        outcome = _switch(git, name, head, branch, target, sync, decider, ignored_keep)
        actions.extend(outcome.actions)
        if outcome.stop:
            return actions
        head = current_branch(git)

    actions.extend(
        _fast_forward(git, name, head, branch, target, sync, decider, ignored_keep, fetch.applied)
    )
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
    cfg_ignored_keep: Sequence[str] = (),
) -> _Outcome:
    policy = sync["switch"]
    where = head or "detached HEAD"
    blocked = _cannot_switch(git, name, branch, where, policy, sync, cfg_ignored_keep, target)
    if blocked is not None:
        return blocked

    action: Action | None = None
    stashed = False
    if is_dirty(git):
        room = _make_room(git, name, branch, where, policy, sync, decider)
        if isinstance(room, _Outcome):
            return room
        # Consent was given for "stash and switch", so that is the action being
        # carried out — and the one the summary should name.
        action = room
        stashed, problem = _stash(git, name)
        if problem is not None:
            return _Outcome([problem], stop=True)

    if action is None:
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
        undone = f" ({_unstash(git)})" if stashed else ""
        action.error = f"{last_line(result)}{undone}"
        return _Outcome([action], stop=True)
    action.applied = True
    if action.kind == "stash+switch" and not stashed:
        # There was dirt git could not stash, so nothing was put aside. The kind
        # is what the summary counts, and "stashed and switched" would be as
        # untrue there as it would be on the line itself.
        action.kind = "switch"
    action.detail = (
        f"stashed and switched from {where}, recover it with git stash pop"
        if action.kind == "stash+switch"
        else f"switched from {where}"
    )
    return _Outcome([action])


def _cannot_switch(
    git: Git,
    name: str,
    branch: str,
    where: str,
    policy: str,
    sync: dict[str, Any],
    ignored_keep: Sequence[str] = (),
    target: str = "",
) -> _Outcome | None:
    """The reasons a switch is not going to happen, before any work is done.

    Every one of them is checked here, before the stash: deciding not to switch
    after putting somebody's work aside would leave it in a stash nobody
    mentioned.
    """
    if sync["worktrees"] not in ("skip", "switch"):
        raise Failure(f"sync.worktrees must be skip or switch, not {sync['worktrees']!r}")
    if policy not in ("always", "clean-only", "never"):
        raise Failure(f"sync.switch must be always, clean-only or never, not {policy!r}")
    busy = _operation_in_progress(git)
    if busy:
        # Switching away resets HEAD, and neither a bisect nor an unfinished
        # merge survives that: `git bisect reset` is then the only way back, and
        # the commit it was reset from is not written down anywhere. _stash
        # already refused for this reason, but a *clean* repository mid-bisect
        # never reached it — it was simply switched off its detached HEAD.
        return _Outcome(
            [Action("sync", name, where, f"{busy} is in progress, left alone", skipped=True)]
        )
    if sync["worktrees"] == "skip" and is_linked_worktree(git):
        # Nothing is wrong here: this checkout exists to hold its own branch.
        return _Outcome(
            [Action("switch", name, branch, f"linked worktree, left on {where}", skipped=True)]
        )
    if policy == "never":
        return _Outcome([Action("switch", name, branch, f"staying on {where}", skipped=True)])
    # A branch lives in one worktree at a time, and a workspace that keeps
    # .worktrees/ next to the clones hits that constantly. Nothing is wrong: the
    # branch is simply in use elsewhere, so say so rather than fail.
    elsewhere = _checked_out_elsewhere(git, branch)
    if elsewhere:
        return _Outcome(
            [Action("switch", name, branch, f"checked out in {elsewhere}", skipped=True)],
            stop=True,
        )
    stranded = _commits_on_no_branch(git)
    if stranded:
        return _Outcome(
            [
                Action(
                    "switch",
                    name,
                    branch,
                    f"detached HEAD at {stranded}, on no branch — switching would "
                    "leave those commits reachable only through the reflog",
                    skipped=True,
                )
            ],
            stop=True,
        )
    # target, not branch: when the trunk exists only as origin/main the switch
    # creates it from there, and `cat-file -e main:...` would fail with "invalid
    # object name" and wave the overwrite through.
    local = git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    clobbered = _would_clobber_ignored(git, branch if local else target, ignored_keep)
    if clobbered is not None:
        return _Outcome(
            [
                Action(
                    "switch",
                    name,
                    branch,
                    f"{branch} tracks {clobbered}, which is ignored here and would be replaced",
                    skipped=True,
                )
            ],
            stop=True,
        )
    return None


def _commits_on_no_branch(git: Git) -> str:
    """The detached HEAD, when nothing else reaches the commits it is sitting on.

    git warns about this on stderr and exits 0, so it is easy to discard. The
    tool refuses to delete a branch holding one unpushed commit; abandoning the
    identical commit here, in the default mode and with no flag, would be the
    same loss by a quieter route. Doctor cannot catch it either — sync runs
    first, and by then the evidence is gone.
    """
    if current_branch(git):
        return ""
    # for-each-ref, not `branch --contains`: the latter lists a pseudo-entry for
    # the detached HEAD itself, so the answer was never empty.
    on = git.out(
        "for-each-ref", "--contains", "HEAD", "--format=%(refname)", "refs/heads", check=False
    )
    if on:
        return ""  # detached, but the commits are on a branch too
    return git.out("rev-parse", "--short", "HEAD", check=False) or "an unknown commit"


def _make_room(
    git: Git,
    name: str,
    branch: str,
    where: str,
    policy: str,
    sync: dict[str, Any],
    decider: Decider,
) -> _Outcome | Action:
    """Decide what to do about uncommitted work.

    Returns an _Outcome to stop, or the Action that was consented to. The consent
    happens here, before the stash: declining must leave the worktree exactly as
    it was, not tidied into a stash the user never agreed to.
    """
    # No decider.dry here: a dry run has to describe the run it is standing in
    # for. allow() refuses in dry mode anyway, so nothing is stashed — but the
    # line now reads "would stash and switch" instead of claiming it was blocked.
    if policy == "clean-only" or not sync["stash"]:
        return _Outcome(
            [Action("switch", name, branch, f"uncommitted changes on {where}", skipped=True)],
            stop=True,
        )
    # A distinct kind, so answering "all of these" to plain switches cannot leak
    # into consent for stashing somebody's uncommitted work.
    proposed = Action("stash+switch", name, branch, f"stash and switch from {where}")
    if not decider.allow(proposed):
        return _Outcome([proposed], stop=True)
    return proposed


def _why_uncountable(git: Git, upstream: str) -> str | None:
    """Why `rev-list upstream...HEAD` came back empty, or None if it can be fixed.

    The upstream ref being gone is the common case — a branch merged and deleted
    on the remote — and naming it beats "cannot compare", which tells nobody
    what to do. An unborn HEAD is the other: a clone of a repository that was
    empty at the time has a branch with no commit on it, and that one is not a
    problem at all, so it gets no message and is fast-forwarded like any other.
    """
    if not git.ok("show-ref", "--verify", "--quiet", f"refs/remotes/{upstream}"):
        return f"upstream {upstream} no longer exists"
    if git.ok("rev-parse", "--verify", "--quiet", "HEAD"):
        return "cannot compare with upstream"
    return None


def _fast_forward(
    git: Git,
    name: str,
    head: str,
    branch: str,
    target: str,
    sync: dict[str, Any],
    decider: Decider,
    ignored_keep: Sequence[str] = (),
    fetched: bool = False,
) -> list[Action]:
    if not sync["fast_forward"]:
        return []
    upstream = target if head == branch else _upstream_of(git, head)
    if not head:
        return [Action("update", name, "HEAD", "detached, nothing to fast-forward", skipped=True)]
    if not upstream:
        return [Action("update", name, head, "no upstream", skipped=True)]

    counts = git.out("rev-list", "--left-right", "--count", f"{upstream}...HEAD", check=False)
    behind, _, ahead = counts.partition("\t")
    if not counts:
        uncountable = _why_uncountable(git, upstream)
        if uncountable is not None:
            return [Action("update", name, head, uncountable, skipped=True)]
        # A clone of a repository that was empty at the time: nothing local to
        # lose, so every commit upstream is one this checkout is behind by.
        behind, ahead = git.out("rev-list", "--count", upstream, check=False) or "0", "0"
    if behind == "0":
        # Measured against the remote-tracking ref as it already stood, unless
        # this run refreshed it. A dry run does not fetch, and in --ask the
        # fetch can be declined — in both cases "up to date" on its own would
        # be claiming something this run never checked.
        when = "" if fetched else " as of the last fetch"
        return [Action("update", name, head, f"up to date{when}", skipped=True)]
    if ahead != "0":
        return _diverged(git, name, head, upstream, ahead, behind, sync, decider, ignored_keep)
    dirty = is_dirty(git)
    if dirty and not sync["stash"]:
        # git would refuse anyway, with "Your local changes would be overwritten
        # by merge". Refusing first turns a failure into a plain statement of
        # fact, which is what it is.
        return [Action("update", name, head, f"uncommitted changes, {behind} behind", skipped=True)]
    verb = "stash and fast-forward" if dirty else "fast-forward"
    # A different kind, so answering "all of these" to plain fast-forwards never
    # silently consents to stashing somebody's uncommitted work as well.
    kind = "stash+update" if dirty else "update"
    # git merge overwrites an ignored file the incoming commits track, exactly as
    # a checkout does, and just as silently.
    clobbered = _would_clobber_ignored(git, upstream, ignored_keep)
    if clobbered is not None:
        return [
            Action(
                "update",
                name,
                head,
                f"{upstream} tracks {clobbered}, which is ignored here and would be replaced",
                skipped=True,
            )
        ]
    return _carry_out_fast_forward(git, name, head, upstream, behind, verb, kind, dirty, decider)


def _carry_out_fast_forward(
    git: Git,
    name: str,
    head: str,
    upstream: str,
    behind: str,
    verb: str,
    kind: str,
    dirty: bool,
    decider: Decider,
) -> list[Action]:
    """Ask, stash if it has to, merge --ff-only, and say what happened."""
    action = Action(kind, name, head, f"{verb} {plural(behind, 'commit')}")
    # Consent covers the stash too: declining must leave the worktree as it was,
    # not tidied into a stash nobody agreed to.
    if not decider.allow(action):
        return [action]
    stashed = False
    if dirty:
        stashed, problem = _stash(git, name)
        if problem is not None:
            return [problem]
    result = git.run("merge", "--ff-only", "--quiet", upstream, check=False)
    _finish(action, result, git, stashed, f"fast-forwarded {plural(behind, 'commit')}")
    return [action]


@contextlib.contextmanager
def reporting(actions: list[Action], scope: str = "-") -> Iterator[None]:
    """Like keeping(), but for a Failure as well as a Quit.

    A step that has already changed something and then hits a bad config value
    must still report what it did.

    `scope` names the repository. Without it the line read `! -: - — git fetch
    timed out`, which in a workspace of 256 tells you nothing about which one.
    """
    try:
        yield
    except Quit as quit_now:
        quit_now.done.extend(actions)
        raise
    except Failure as exc:
        actions.append(Action("error", scope, "-", "", error=str(exc)))
        return


@contextlib.contextmanager
def keeping(actions: list[Action]) -> Iterator[None]:
    """Hand what has been done so far to a Quit on its way out.

    Every step that accumulates actions needs this, or answering 'q' half way
    through makes the report claim less happened than did.
    """
    try:
        yield
    except Quit as quit_now:
        quit_now.done.extend(actions)
        raise


# A rebase sets none of the *_HEAD markers — it writes a rebase-merge/ or
# rebase-apply/ directory instead, so it went straight past this and turned a
# clean report into "fatal: cannot switch branch while rebasing". Both spellings:
# rebase-apply is what the older `--am` backend leaves behind.
IN_PROGRESS = {
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "BISECT_LOG": "a bisect",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase",
}


def _operation_in_progress(git: Git) -> str:
    """Whatever git is half way through, or "" when it is not.

    `git stash push` resets to HEAD, which clears MERGE_HEAD along with it — and
    an uncommitted merge writes no reflog entry, so afterwards nothing in the
    repository records which commit was being merged. The stash holds the
    content but not the parentage, and `git stash pop` then restores it as
    ordinary work: following the advice this tool prints would produce a commit
    with the wrong parents.
    """
    git_dir = git.out("rev-parse", "--absolute-git-dir", check=False)
    if not git_dir:
        return ""
    return next(
        (what for marker, what in IN_PROGRESS.items() if (Path(git_dir) / marker).exists()),
        "",
    )


def _stash(git: Git, name: str) -> tuple[bool, Action | None]:
    """Put uncommitted work aside so a switch or fast-forward can proceed.

    Returns (a stash was created, the failure to report). Deliberately a stash
    and not `checkout --force`: the point of --force is to get the move done,
    not to destroy work that was never committed anywhere.

    The first half of that pair matters more than it looks. `git stash push`
    exits 0 saying "No local changes to save" when there is nothing it can
    stash, and git reaches that state while `git status` still reports the
    worktree dirty — a submodule with local edits whose recorded commit has not
    moved is the ordinary case. Reading exit 0 as "a stash now exists" made
    _unstash pop whichever stash happened to be on top, which is the user's.
    """
    busy = _operation_in_progress(git)
    if busy:
        return False, Action(
            "stash",
            name,
            "-",
            "",
            error=f"{busy} is in progress; finish or abort it first (a stash would lose it)",
        )
    before = git.out("rev-parse", "--quiet", "--verify", "refs/stash", check=False)
    result = git.run("stash", "push", "--include-untracked", "-m", f"git-tidy: {name}", check=False)
    if result.returncode != 0:
        return False, Action("stash", name, "-", "", error=last_line(result))
    after = git.out("rev-parse", "--quiet", "--verify", "refs/stash", check=False)
    # Nothing else on success: the action this made room for says "stashed and
    # switched" itself. Reporting it separately counted one event twice.
    return after != before, None


def is_linked_worktree(git: Git) -> bool:
    """True when this checkout was made by `git worktree add`.

    A linked worktree keeps its own .git *file* pointing into the parent's
    .git/worktrees/<name>, so its git-dir and its common git-dir differ.
    """
    own = git.out("rev-parse", "--absolute-git-dir", check=False)
    shared = git.out("rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    return bool(own) and bool(shared) and own != shared


def _would_clobber_ignored(git: Git, branch: str, protect: Sequence[str]) -> str | None:
    """An ignored file the target branch tracks, which switching would overwrite.

    git treats ignored files as expendable during a checkout: unlike an untracked
    file, it replaces them without a word. That is exactly the wrong outcome for
    a local .env or *.tfstate, which is what ignored_keep names. Only those are
    checked — asking about every ignored file would mean walking node_modules.
    """
    # refs/heads/ when it names a local branch, so a tag of the same name cannot
    # stand in for it; `branch` is also called with a remote-tracking ref such as
    # origin/main, which has no refs/heads/ entry, so that falls through as given.
    ref = f"refs/heads/{branch}"
    if not git.ok("show-ref", "--verify", "--quiet", ref):
        ref = branch
    for relative in ignored_paths(git, collapse="never"):
        name = Path(relative).name
        # _matches, not _protects: `protect` here is clean.ignored_keep on its
        # own, and a gitignored config/secrets.py is by definition not committed
        # source — being local and untracked is why the list names it.
        if not (_matches(name, protect) or any(fnmatch.fnmatch(relative, p) for p in protect)):
            continue
        if git.ok("cat-file", "-e", f"{ref}:{relative}"):
            return relative
    return None


def _unstash(git: Git) -> str:
    """Put a stash back after the operation it made room for failed.

    Without this, "nothing changed" would be untrue: the rebase or switch was
    undone, but the user's work would still be sitting in a stash they never
    asked for.
    """
    # --index, so what was staged is staged again. Without it the pop restores
    # the content, flattens the staged/unstaged split and then drops the stash —
    # and "nothing changed" is the one line somebody reads before deciding
    # whether to go and look.
    result = git.run("stash", "pop", "--index", check=False)
    if result.returncode != 0:
        # --index fails if the index cannot be reinstated cleanly; the plain pop
        # at least gets the content back.
        result = git.run("stash", "pop", check=False)
        if result.returncode == 0:
            return "your work is back, but what was staged is no longer staged"
    if result.returncode == 0:
        return "nothing changed"
    if "No stash entries found" in result.stdout + result.stderr:
        # Saying "your changes are in the stash" here was a contradiction, and
        # the alarming half of it was the wrong half: there is no stash because
        # something else already popped it.
        return "the stash was already empty; check `git stash list` and the reflog"
    return f"your changes are in the stash: {last_line(result)}"


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


def _upstream_of(git: Git, branch: str) -> str:
    """The upstream a branch tracks, or "" when it has none.

    Not `rev-parse --abbrev-ref @{upstream}`: when the upstream ref has been
    pruned that echoes the literal string "@{upstream}" on stdout and exits 128,
    which then turns up in the report as a branch name. for-each-ref knows the
    configured name whether or not the ref still exists.
    """
    name = git.out(
        "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}", check=False
    )
    return name if name and "@{" not in name else ""


def _diverged(
    git: Git,
    name: str,
    head: str,
    upstream: str,
    ahead: str,
    behind: str,
    sync: dict[str, Any],
    decider: Decider,
    ignored_keep: Sequence[str] = (),
) -> list[Action]:
    """Local commits and upstream commits both. Report, or replay ours on theirs."""
    summary = f"diverged: {ahead} ahead, {behind} behind"
    if sync["diverged"] != "rebase":
        if sync["diverged"] != "report":
            raise Failure(f"sync.diverged must be report or rebase, not {sync['diverged']!r}")
        return [Action("update", name, head, summary, skipped=True)]
    if is_dirty(git) and not sync["stash"]:
        return [Action("update", name, head, f"{summary}, and uncommitted changes", skipped=True)]
    # The same guard the switch and the fast-forward already have. A rebase
    # checks the upstream out as surely as they do, so an uncommitted .env that
    # the incoming commits happen to track is replaced — never committed, never
    # stashed, never quarantined. It went missing here alone.
    clobbered = _would_clobber_ignored(git, upstream, ignored_keep)
    if clobbered is not None:
        return [
            Action(
                "update",
                name,
                head,
                f"{summary}: {upstream} tracks {clobbered}, which is ignored here "
                "and would be replaced",
                skipped=True,
            )
        ]

    dirty = is_dirty(git)
    verb = "stash and rebase" if dirty else "rebase"
    # 11: its own kind, so the summary does not call a rebase a fast-forward.
    action = Action(
        "stash+rebase" if dirty else "rebase",
        name,
        head,
        f"{verb} {plural(ahead, 'commit')} onto {upstream}",
    )
    if not decider.allow(action):
        return [action]
    stashed = False
    if dirty:
        stashed, problem = _stash(git, name)
        if problem is not None:
            return [problem]
    # No --autostash: _stash has already put the work aside when there was any,
    # and letting git stash it too means two entries pushed and popped in an
    # order neither side agrees on. On an abort ours was already gone, and the
    # report said "your changes are in the stash: No stash entries found" —
    # which cannot both be true, and is a frightening thing to read.
    result = git.run("rebase", upstream, check=False)
    if result.returncode != 0:
        # Leave nothing half-applied: a conflicted rebase in 200 repositories is
        # far worse than a report saying it did not happen.
        git.run("rebase", "--abort", check=False)
        undone = _unstash(git) if stashed else "nothing changed"
        action.error = f"{last_line(result)} (rebase aborted, {undone})"
    else:
        action.applied = True
        done = f"rebased {plural(ahead, 'commit')} onto {upstream}"
        if not stashed:
            # As in _switch and _finish: the kind is what the summary counts.
            action.kind = action.kind.removeprefix("stash+")
        action.detail = f"stashed and {done}, recover it with git stash pop" if stashed else done
    return [action]


def _dirty_submodules(git: Git) -> list[str]:
    """Submodules with uncommitted work in them.

    `git submodule status` does not answer this — its '+' means the checked-out
    commit differs from the recorded one, not that the worktree is dirty — so
    each one is asked directly. git will happily move a submodule whose edits
    happen not to conflict, and asking first is the only way to keep the promise
    that they are left alone.
    """
    dirty: list[str] = []
    for line in git.out("submodule", "status", "--recursive", check=False).splitlines():
        fields = line[1:].split() if line[:1] in ("+", "-", "U", " ") else line.split()
        if len(fields) < 2:
            continue
        path = git.path / fields[1]
        if path.is_dir() and is_dirty(Git(path, timeout=git.timeout)):
            dirty.append(fields[1])
    return dirty


def _moved_submodules(git: Git) -> list[str]:
    """Submodules checked out at something other than the recorded commit."""
    out = git.out("submodule", "status", "--recursive", check=False)
    return [
        line[1:].split()[1]
        for line in out.splitlines()
        if line.startswith("+") and len(line[1:].split()) > 1
    ]


def _uninitialised_submodules(git: Git) -> list[str]:
    """Submodule paths git has not checked out yet, marked with a leading '-'."""
    out = git.out("submodule", "status", "--recursive", check=False)
    return [
        line[1:].split()[1]
        for line in out.splitlines()
        if line.startswith("-") and len(line[1:].split()) > 1
    ]


def _finish(action: Action, result: Any, git: Git, stashed: bool, done: str) -> None:
    """Record how a stash-and-move ended, and put the stash back if it failed.

    The "stashed and" only survives into the message when a stash was really
    made: telling somebody their work is recoverable with `git stash pop` when
    nothing was stashed sends them to pop whatever else is on the pile.
    """
    if result.returncode != 0:
        undone = f" ({_unstash(git)})" if stashed else ""
        action.error = f"{last_line(result)}{undone}"
        return
    action.applied = True
    if not stashed:
        # Nothing was put aside, so the summary must not count this among the
        # ones that were. See _stash on why exit 0 does not mean a stash exists.
        action.kind = action.kind.removeprefix("stash+")
    action.detail = f"stashed and {done}, recover it with git stash pop" if stashed else done


def _sync_submodules(git: Git, name: str, sync: dict[str, Any], decider: Decider) -> list[Action]:
    mode = sync["submodules"]
    if mode == "none":
        return []
    if mode not in ("init", "update"):
        raise Failure(f"sync.submodules must be none, init or update, not {mode!r}")
    if not (git.path / ".gitmodules").is_file():
        return []
    dirty = _dirty_submodules(git)
    if dirty:
        return [
            Action(
                "submodules",
                name,
                mode,
                f"uncommitted work in {dirty[0]}"
                + (f" and {len(dirty) - 1} more" if len(dirty) > 1 else ""),
                skipped=True,
            )
        ]
    missing = _uninitialised_submodules(git)
    moved = _moved_submodules(git) if mode == "update" else []
    if not missing and not moved:
        # Nothing to do, so nothing to report — least of all in a dry run, which
        # would be promising work that will not happen. `git submodule update`
        # exits 0 whether or not it moved anything, so this is the only way to
        # tell.
        return []
    action = Action("submodules", name, mode, "update submodules")
    if not decider.allow(action):
        return [action]
    # Deliberately no --force. `git submodule update --force` is a
    # `checkout --force` inside each submodule, which throws away uncommitted
    # work there exactly as `git checkout --force` would in the parent — and
    # nothing else in this tool does that. Without it git refuses to clobber and
    # the refusal is reported.
    args = ["submodule", "update", "--init", "--recursive", "--quiet"]
    if mode == "init":
        # Only the ones that are not there yet. A bare --init --recursive also
        # moves already-checked-out submodules onto the recorded commit, which is
        # what "update" is for.
        args += ["--", *missing]
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
    # %(refname), not %(refname:short): see current_branch on what a tag of the
    # same name does to the short form.
    fmt = "%(refname)%09%(upstream:short)%09%(upstream:track)"
    out = git.out("for-each-ref", f"--format={fmt}", "refs/heads", check=False)
    branches: list[BranchInfo] = []
    for line in out.splitlines():
        # Trailing fields are empty for a branch with no upstream, and git omits
        # them entirely rather than padding with tabs.
        name, _, rest = line.partition("\t")
        upstream, _, track = rest.partition("\t")
        name = name[len("refs/heads/") :] if name.startswith("refs/heads/") else name
        if name:
            branches.append(BranchInfo(name, upstream, track))
    return branches


def prune_branches(
    path: Path, name: str, cfg: dict[str, Any], decider: Decider, fetched: bool = False
) -> list[Action]:
    """Delete local branches the remote no longer has, keeping unpushed work."""
    rules = cfg["branches"]
    git = Git(path, timeout=int(cfg["sync"]["timeout"]))
    actions: list[Action] = []
    # prune does not fetch, so it must not go to the network here either.
    trunk = default_branch(git, cfg["sync"], readonly=True)
    remote = cfg["sync"]["remote"]
    head = current_branch(git)

    with keeping(actions):
        _consider_branches(git, name, rules, trunk, remote, head, fetched, decider, actions)
    return actions


def _consider_branches(
    git: Git,
    name: str,
    rules: dict[str, Any],
    trunk: str | None,
    remote: str,
    head: str,
    fetched: bool,
    decider: Decider,
    actions: list[Action],
) -> None:
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
        # A branch checked out in a linked worktree cannot be deleted either.
        # Saying so beats one failure line per branch, and --force cannot help:
        # the answer is to remove the worktree, which is a decision, not a detail.
        held_by = _checked_out_elsewhere(git, branch.name)
        if held_by:
            actions.append(
                Action(
                    "branch",
                    name,
                    branch.name,
                    f"in use by the worktree at {held_by}",
                    skipped=True,
                )
            )
            continue

        kept = _why_keep(git, branch, trunk, remote, reason, rules, fetched)
        if kept is not None:
            actions.append(Action("branch", name, branch.name, kept, skipped=True))
            continue

        action = Action("branch", name, branch.name, f"delete ({reason})")
        if not decider.allow(action):
            actions.append(action)
            continue
        # --delete without --force makes git re-check containment at the moment
        # of deletion, which closes the gap between the check above and here.
        # --force is only used where containment was deliberately waived.
        args = ["branch", "--delete", branch.name]
        if not rules["require_merged"]:
            args.insert(2, "--force")
        result = git.run(*args, check=False)
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


# The branch was there when it was listed and is not there now: another worker
# sharing this ref store got to it first. Distinct from "keep it", which is what
# a bare reason string means.
VANISHED = object()


def _why_keep(
    git: Git,
    branch: BranchInfo,
    trunk: str | None,
    remote: str,
    reason: str,
    rules: dict[str, Any],
    fetched: bool,
) -> str | None:
    """The message to report instead of deleting, or None to go ahead."""
    if rules["require_merged"]:
        return _keep_reason(git, branch, trunk, remote, reason)
    if not fetched:
        # Without the containment check, the only thing between this branch and
        # deletion is the [gone] mark — a cached observation. If it predates this
        # run the branch may have been recreated upstream since, and its commits
        # exist nowhere else.
        return (
            "kept: deleting unmerged work needs a fetch in this run — "
            "use `git-tidy run --force`, not `prune` on its own"
        )
    return None


def _keep_reason(
    git: Git, branch: BranchInfo, trunk: str | None, remote: str, reason: str
) -> str | None:
    """The message to report, or None when the branch may go."""
    kept = _unmerged_reason(git, branch, trunk, remote)
    if kept is VANISHED:
        return f"already deleted ({reason})"
    return None if kept is None else str(kept)


def _unmerged_reason(git: Git, branch: BranchInfo, trunk: str | None, remote: str) -> Any:
    """Why this branch must be kept, None when it is safe to delete.

    Returns VANISHED when the branch has disappeared underneath us, which a
    workspace holding linked worktrees or --shared clones of the same repository
    produces routinely.
    """
    if trunk is None:
        return "kept: cannot check merged, no trunk found"
    shown = f"{remote}/{trunk}"
    trunk_ref = f"refs/remotes/{shown}"
    if not git.ok("show-ref", "--verify", "--quiet", trunk_ref):
        shown, trunk_ref = trunk, f"refs/heads/{trunk}"
        if not git.ok("show-ref", "--verify", "--quiet", trunk_ref):
            return f"kept: no {trunk} to compare against"
    # refs/heads/, because git resolves a bare name as a tag first: a tag called
    # `feature` made every one of these walk the tag instead of the branch, and
    # doctor then reported nothing at all about the branch's unpushed commits.
    # current_branch and list_branches already knew this; the walks did not.
    head = f"refs/heads/{branch.name}"
    if git.ok("merge-base", "--is-ancestor", head, trunk_ref):
        return None
    unpushed = git.out("rev-list", "--count", f"{trunk_ref}..{head}", check=False)
    if not unpushed and not git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch.name}"):
        return VANISHED
    return f"kept: {plural(unpushed, 'commit') if unpushed else 'some commits'} not in {shown}"


# --------------------------------------------------------------------------- #
# cleaning artefacts
# --------------------------------------------------------------------------- #


def directory_size(path: Path) -> int:
    return _measure(path)[0]


class _Unreadable:
    """Records a subtree os.walk could not read, instead of walking past it.

    os.walk's default is to swallow the error and yield nothing for that
    directory, so a subtree nobody can list looks exactly like an empty one:
    size 0, no .git in it, no protected file in it. Every guard that asks "is
    there a repository in here?" then answers no, and the directory is removed —
    or, with quarantine on, renamed, which needs no read access at all and so
    succeeds. The answer has to be "I cannot tell", which is not the same as no.
    """

    def __init__(self) -> None:
        self.hit = False

    def __call__(self, _: OSError) -> None:
        self.hit = True


def _measure(path: Path) -> tuple[int, bool, bool]:
    """Total size under `path`, and whether it may hold a git repository.

    Both come from the same walk, because the second question has to be asked
    before deleting a directory wholesale and the first is needed for the report
    anyway. A vendored checkout inside node_modules or vendor/ is a real thing,
    and removing its parent would take the repository with it.
    """
    total = 0
    holds_repo = False
    unreadable = _Unreadable()
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False, onerror=unreadable):
        here = Path(dirpath)
        if ".git" in dirnames or ".git" in filenames or holds_git_data(here):
            holds_repo = True
        for name in filenames:
            candidate = here / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
    # Kept apart, because they are different sentences. Folding them together
    # made a project.old/ with one chmod-000 subdirectory in it and no git
    # anywhere report "kept: contains a git repository", which is simply untrue.
    return total, holds_repo, unreadable.hit


def tracked_paths(git: Git) -> set[str]:
    """Every tracked path, plus each of its parent directories.

    Membership then answers "would deleting this remove committed content?" for
    files and directories alike, with one git call per repo instead of one per
    candidate path.
    """
    result = git.run("ls-files", "-z", check=False)
    if result.returncode != 0:
        # An empty set here would mean "nothing is tracked", and every artefact
        # rule would then apply to committed files. Refusing isolates this one
        # repository instead; _guarded turns it into a reported failure.
        raise Failure(f"cannot read the index: {last_line(result)}")
    out = result.stdout
    paths: set[str] = set()
    for entry in out.split("\0"):
        if not entry:
            continue
        paths.add(entry)
        parent = Path(entry).parent
        while str(parent) != ".":
            paths.add(parent.as_posix())
            parent = parent.parent
    # macOS and Windows hand back a path whose case need not match the index —
    # after a case-only rename, git says Build/x while the disk says build/x.
    # Both spellings are protected: over-protecting keeps a file that could have
    # gone, under-protecting deletes one that was committed.
    return paths | {entry.lower() for entry in paths}


def ignored_paths(git: Git, collapse: str = "some") -> list[str]:
    """What `git clean -Xd` would remove: everything .gitignore calls disposable.

    --directory collapses a wholly ignored directory into one entry, so a
    node_modules with 40,000 files inside comes back as a single path.
    """
    collapsed = git.run(
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "--no-empty-directory",
        "-z",
        check=False,
    )
    whole = [entry.rstrip("/") for entry in collapsed.stdout.split("\0") if entry]
    if collapse == "only":
        return whole
    # --directory stops git descending into a directory it has already called
    # untracked, so an ignored file inside an *untracked* one is never listed —
    # which both broke the `git clean -Xd` parity this claims and hid files the
    # switch guard needs to see. The uncollapsed list fills those in; the
    # collapsed one is still used for the wholesale removals, so a node_modules
    # with forty thousand files stays one entry.
    every = git.run("ls-files", "--others", "--ignored", "--exclude-standard", "-z", check=False)
    listed = [entry for entry in every.stdout.split("\0") if entry]
    if collapse == "never":
        # Every path in its own right: the switch guard has to see the .env
        # inside a wholly ignored config/, which the collapsed form hides — and
        # the .env at the root, which the collapsed form lists but this must not
        # therefore skip.
        return listed
    inside = [entry for entry in listed if entry not in whole]
    covered = tuple(f"{one}/" for one in whole)
    return whole + [entry for entry in inside if not entry.startswith(covered)]


def clean_patterns(clean: dict[str, Any]) -> tuple[list[str], list[str]]:
    dirs = [*clean["dirs"], *clean["extra_dirs"]]
    if clean["dependencies"]:
        dirs += clean["dependency_dirs"]
    if clean["builds"]:
        dirs += clean["build_dirs"]
    files = [*clean["files"], *clean["extra_files"]]
    return dirs, files


def switched_off_rules(clean: dict[str, Any]) -> list[tuple[Sequence[str], str]]:
    """The subset of protection_rules that names whole trees kept by a switch.

    clean.ignored has to refuse a directory holding one of these, because git
    collapses a wholly ignored directory into a single entry and removing it
    would take the node_modules inside it with it — which is precisely what
    clean.dependencies being off says not to do.

    ignored_keep and clean.keep are deliberately *not* here. _remove lifts those
    out and removes the directory around them, which is the whole promise;
    refusing the directory for them instead meant turning clean.ignored on made
    the tool reclaim nothing.
    """
    return protection_rules(clean)[2:]


def protection_rules(clean: dict[str, Any]) -> list[tuple[Sequence[str], str]]:
    """Every rule that keeps a path, with the name of the rule that did it.

    Shared by both removal paths. Keeping them apart is how clean.dirs came to
    delete a terraform.tfstate that clean.ignored had refused in the same run —
    one consulted ignored_keep and the other did not.

    Each rule keeps its own name: pointing somebody at clean.ignored_keep for
    something node_modules kept sends them to a list it is not in. .gitignore
    covers node_modules and dist in practically every repository, so without the
    last two clean.ignored alone emptied the very lists that clean.dependencies
    and clean.builds exist to keep switched off.
    """
    rules: list[tuple[Sequence[str], str]] = [
        (clean["ignored_keep"], "kept by ignored_keep"),
        (clean["keep"], "kept by clean.keep"),
    ]
    if not clean["dependencies"]:
        rules.append((clean["dependency_dirs"], "kept: a dependency tree, clean.dependencies"))
    if not clean["builds"]:
        rules.append((clean["build_dirs"], "kept: build output, clean.builds"))
    return rules


def clean_ignored(
    repo: Path,
    scope: str,
    cfg: dict[str, Any],
    decider: Decider,
    quarantine: Quarantine | None,
    git: Git,
    holding: Quarantine | None = None,
) -> list[Action]:
    """Remove everything the repo's own .gitignore already calls disposable."""
    clean = cfg["clean"]
    rules = protection_rules(clean)
    actions: list[Action] = []
    # Its own guard: the caller's list does not have these until this returns,
    # so a Quit raised in here would take them with it.
    with keeping(actions):
        _remove_ignored(repo, scope, cfg, clean, rules, decider, quarantine, git, holding, actions)
    return actions


def _remove_ignored(
    repo: Path,
    scope: str,
    cfg: dict[str, Any],
    clean: dict[str, Any],
    rules: Sequence[tuple[Sequence[str], str]],
    decider: Decider,
    quarantine: Quarantine | None,
    git: Git,
    holding: Quarantine | None,
    actions: list[Action],
) -> None:
    for relative in ignored_paths(git):
        # git should never hand back the repository root, but a "" or "." here
        # would resolve to the repo itself and take everything with it.
        if relative in ("", "."):
            continue
        path = repo / relative
        if path.is_symlink() or not path.exists():
            continue
        name = Path(relative).name
        why = next(
            (
                reason
                for patterns, reason in rules
                if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(relative, p) for p in patterns)
            ),
            None,
        )
        if why is not None:
            actions.append(Action("ignored", scope, relative, why, skipped=True))
            continue
        # git collapses a wholly ignored directory into one entry, so a build/
        # holding a node_modules arrives here as a single path, and removing it
        # would take the tree clean.dependencies is switched off to keep.
        #
        # Only for those. A .env or a *.pem inside it is _remove's business: it
        # lifts them out and reclaims the rest, and refusing the directory for
        # them instead is how turning clean.ignored on came to free nothing.
        # Never for a path the user called regenerable.
        regenerable = _matches(name, clean["regenerable"])
        protect = [p for patterns, _ in switched_off_rules(clean) for p in patterns]
        if (
            path.is_dir()
            and not regenerable
            and protect
            and (holds := _holds_protected(path, protect, base=repo))
        ):
            actions.append(
                Action("ignored", scope, relative, f"kept: contains {holds}", skipped=True)
            )
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
                protect_nested=not regenerable,
                sensitive=(
                    guard := outright_guard(
                        cfg["trash"]["sensitive"], local_state_of(clean), regenerable
                    )
                )[0],
                local_state=guard[1],
                holding=holding,
            )
        )


def _protected_within(
    directory: Path,
    sensitive: Sequence[str],
    local_state: Sequence[str] = (),
    base: Path | None = None,
) -> tuple[list[Path] | None, str]:
    """Every path inside `directory` that must not be deleted outright.

    _holds_protected answers "is there one", which is all a refusal needs. This
    answers "which ones", because they are lifted into quarantine and the
    directory around them is then removed — the difference between reclaiming a
    dependency tree and renaming it into the workspace it was cluttering.

    Directories are returned whole and not descended into, so a `credentials/`
    arrives in the quarantine intact.

    The second return value is why, when the answer is "it cannot be emptied out
    safely and has to stay". Two ways that happens, and they are not the same
    sentence: a subtree nobody can list, and a *symlink* whose name is
    protected. The quarantine refuses to take a symlink pointing outside the
    workspace, as it should — but the refusal surfaced as a raw relative_to()
    message against an action whose target was "-", so the run reported a
    failure that named neither the file nor the directory.
    """
    root = base or directory
    found: list[Path] = []
    unreadable = _Unreadable()

    def wanted(entry: Path) -> bool:
        # Against the path as well as the name, as _holds_protected does and its
        # docstring promises: `build/config/*.pem` protects as surely as `*.pem`.
        # Testing only the name meant the file that produced a "kept" line was
        # not among the ones lifted out, and went with the directory.
        #
        # Through _protects both times, so the source exemption applies to the
        # path too — fnmatch's * crosses a /, so `*token*` matches
        # acorn/dist/tokenizer.js and the exemption would have been lost here.
        relative = None
        with contextlib.suppress(ValueError):
            relative = entry.relative_to(root).as_posix()
        return _protects(entry.name, sensitive, local_state, relative, entry)

    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False, onerror=unreadable):
        here = Path(dirpath)
        keep = [d for d in dirnames if wanted(here / d)]
        for name in (*keep, *(f for f in filenames if wanted(here / f))):
            if (here / name).is_symlink():
                return None, "kept: holds a protected symlink"
            found.append(here / name)
        dirnames[:] = [d for d in dirnames if d not in keep and not (here / d).is_symlink()]
    if unreadable.hit:
        return None, "kept: something in here cannot be read"
    return sorted(found), ""


def _sensitive_within(directory: Path, sensitive: Sequence[str]) -> str | None:
    """_holds_protected for trash.sensitive, which does exempt source code."""
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        here = Path(dirpath)
        for entry in (*dirnames, *filenames):
            if _protects(entry, sensitive, path=here / entry):
                return (here / entry).relative_to(directory).as_posix()
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
    return None


def _thin_out(directory: Path, keep: Sequence[Path]) -> tuple[int, str]:
    """Remove everything under `directory` except `keep`, and say how much went.

    The directory itself survives, holding only the protected entries. That is
    the difference between reclaiming a 400 MB node_modules and breaking the
    .env the application reads out of it: the file is not a copy of something,
    it is the thing, and moving it to a quarantine is not much better for its
    owner than deleting it.

    Nothing protected is deleted, so nothing needs a quarantine to fall back on:
    "never hard-deleted" is kept by not removing it at all.

    A kept *directory* is kept whole, contents and all — `credentials/` is
    protected as a unit — and the count is of bytes that actually went, measured
    after the fact, so a file that could not be removed is not reported as freed.

    Returns that count and the first error, if anything refused to go. Swallowing
    those and reporting the prediction meant a directory holding an unreadable
    subtree said "emptied out" and named the full size as freed.
    """
    kept = {one.resolve() for one in keep}
    # Every directory on the way down to something kept has to survive too.
    needed = {parent for one in kept for parent in one.parents}
    freed = 0
    trouble = ""

    def protected(path: Path) -> bool:
        resolved = path.resolve()
        return resolved in kept or any(one in resolved.parents for one in kept)

    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        here = Path(dirpath)
        for name in sorted(filenames):
            candidate = here / name
            if protected(candidate):
                continue
            gone, problem = _unlink_and_count(candidate)
            freed += gone
            trouble = trouble or problem
        stay = []
        for name in sorted(dirnames):
            candidate = here / name
            resolved = candidate.resolve()
            if candidate.is_symlink():
                # Not followed and not counted: a symlink holds nothing, and
                # unlinking it cannot destroy what it points at.
                try:
                    candidate.unlink()
                except OSError as exc:
                    trouble = trouble or str(exc)
                continue
            if protected(candidate) or resolved in needed:
                stay.append(name)
                continue
            before = _measure(candidate)[0]
            try:
                shutil.rmtree(candidate)
            except OSError as exc:
                trouble = trouble or str(exc)
            after = _measure(candidate)[0] if candidate.exists() else 0
            freed += before - after
        # Walked into only what has something kept below it; the rest is gone.
        dirnames[:] = [name for name in stay if not protected(here / name)]
    return freed, trouble


def _unlink_and_count(path: Path) -> tuple[int, str]:
    """Remove one file, returning the bytes that actually went and any error."""
    try:
        size = 0 if path.is_symlink() else path.stat().st_size
        path.unlink()
    except OSError as exc:
        return 0, str(exc)
    return size, ""


def _holds_protected(
    directory: Path, protect: Sequence[str], base: Path | None = None
) -> str | None:
    """The first ignored_keep match buried in `directory`, or None.

    Patterns are tested against the name *and* against the path relative to the
    repository, so `build/config/*.pem` protects as surely as `*.pem` does.

    A subtree that cannot be read is reported as a match: not being able to look
    is not the same as having looked and found nothing, and this decides whether
    something is moved or deleted outright.
    """
    if not protect:
        # Nothing can match, so nothing in there is protected — including the
        # part nobody can read. Answering "cannot tell" for an empty list made
        # the sweep descend into an artefact directory instead of removing it,
        # and print no line at all about why.
        return None
    root = base or directory
    unreadable = _Unreadable()
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False, onerror=unreadable):
        here = Path(dirpath)
        for entry in (*dirnames, *filenames):
            candidate = here / entry
            relative = candidate.relative_to(root).as_posix()
            if _matches(entry, protect) or any(fnmatch.fnmatch(relative, p) for p in protect):
                return candidate.relative_to(directory).as_posix()
        # Pruned only after their names have been considered: a symlink whose
        # own name is protected still protects its parent from removal.
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
    return "something here that cannot be read" if unreadable.hit else None


def clean_tree(
    root: Path,
    scope: str,
    cfg: dict[str, Any],
    decider: Decider,
    git: Git | None,
    quarantine: Quarantine | None,
    stay_inside: bool = True,
    already: set[str] | None = None,
    sensitive: Sequence[str] = (),
    local_state: Sequence[str] = (),
    holding: Quarantine | None = None,
) -> list[Action]:
    """Remove artefact directories and files under `root`.

    `git` is set when `root` is a repository, in which case tracked paths are
    protected. `stay_inside` stops the walk crossing into a nested repository
    that will be, or has been, handled on its own.
    """
    clean = cfg["clean"]
    dir_patterns, file_patterns = clean_patterns(clean)
    plan = _Sweep(
        root=root,
        scope=scope,
        clean=clean,
        dir_patterns=dir_patterns,
        file_patterns=file_patterns,
        keep=clean["keep"],
        tracked=tracked_paths(git) if git is not None and not clean["tracked"] else set(),
        decider=decider,
        quarantine=quarantine,
        stay_inside=stay_inside,
        already=already or set(),
        sensitive=sensitive,
        local_state=local_state,
        holding=holding,
    )
    actions: list[Action] = []
    try:
        _walk_and_remove(plan, actions)
    except Quit as quit_now:
        quit_now.done.extend(actions)
        raise
    return actions


@dataclass
class _Sweep:
    """Everything one artefact walk needs, so the walk itself stays readable."""

    root: Path
    scope: str
    clean: dict[str, Any]
    dir_patterns: list[str]
    file_patterns: list[str]
    keep: Sequence[str]
    tracked: set[str]
    decider: Decider
    quarantine: Quarantine | None
    stay_inside: bool
    already: set[str]
    # Consulted only when quarantine is off: a directory holding one of these is
    # moved rather than deleted. Split because clean.ignored_keep does not apply
    # inside a path the user listed in clean.regenerable — see outright_guard.
    sensitive: Sequence[str] = ()
    local_state: Sequence[str] = ()
    holding: Quarantine | None = None


def _walk_and_remove(plan: _Sweep, actions: list[Action]) -> None:
    for dirpath, dirnames, filenames in os.walk(plan.root, followlinks=False):
        here = Path(dirpath)
        if ".git" in here.parts:
            dirnames[:] = []
            continue
        descend, matched = _sort_directories(plan, here, dirnames)
        # Matched directories go whole, so they are not descended into.
        dirnames[:] = descend
        for name in matched:
            actions.append(
                _remove(
                    here / name,
                    plan.root,
                    plan.scope,
                    plan.decider,
                    plan.quarantine,
                    is_dir=True,
                    protect_nested=(guarded := not _matches(name, plan.clean["regenerable"])),
                    sensitive=(
                        guard := outright_guard(plan.sensitive, plan.local_state, not guarded)
                    )[0],
                    local_state=guard[1],
                    holding=plan.holding,
                )
            )
        for name in sorted(filenames):
            candidate = here / name
            if _wanted_file(plan, candidate, name):
                actions.append(
                    _remove(
                        candidate,
                        plan.root,
                        plan.scope,
                        plan.decider,
                        plan.quarantine,
                        is_dir=False,
                        sensitive=(
                            guard := outright_guard(plan.sensitive, plan.local_state, False)
                        )[0],
                        local_state=guard[1],
                        holding=plan.holding,
                    )
                )


def _sort_directories(plan: _Sweep, here: Path, dirnames: list[str]) -> tuple[list[str], list[str]]:
    """Split this level into what to walk into and what to remove whole."""
    descend: list[str] = []
    matched: list[str] = []
    for name in sorted(dirnames):
        candidate = here / name
        if name in (".git", QUARANTINE_DIRNAME) or candidate.is_symlink():
            continue
        if plan.stay_inside and candidate != plan.root and is_repo(candidate):
            continue  # a repo of its own; not this walk's business
        named = _matches(name, plan.dir_patterns)
        if (
            not named
            and not plan.clean["dependencies"]
            and _matches(name, plan.clean["dependency_dirs"])
        ):
            # A .venv full of __pycache__ is the package manager's business, not
            # ours. Descending into it produced hundreds of lines of output for a
            # few megabytes nobody asked about. Checked after the patterns, so
            # naming one in clean.dirs still does what clean.dirs says it does.
            continue
        if candidate.relative_to(plan.root).as_posix() in plan.already:
            # clean.ignored already accounted for this one. In a dry run it is
            # still on disk, and reporting it again would double both the count
            # and the size against a run that removes it once.
            continue
        if named and not _protected(candidate, plan.root, plan.keep, plan.tracked):
            # Removing it whole would take anything keep protects inside it, so
            # walk in instead and decide file by file.
            if _holds_protected(candidate, plan.keep, base=plan.root):
                descend.append(name)
            else:
                matched.append(name)
        else:
            descend.append(name)
    return descend, matched


def _wanted_file(plan: _Sweep, candidate: Path, name: str) -> bool:
    if candidate.is_symlink() or not _matches(name, plan.file_patterns):
        return False
    if _protected(candidate, plan.root, plan.keep, plan.tracked):
        return False
    return candidate.relative_to(plan.root).as_posix() not in plan.already


def _matches(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


# Extensions that make a file source code, whatever it happens to be called.
# trash.sensitive has to cast a wide net — *token*, *creds*, *password* — and
# that net caught acorn/dist/tokenizer.js, pygments/token.py and yaml/tokens.py.
# One of those in a node_modules was enough to hold the whole 400 MB tree back
# from deletion, which is the opposite of what the tool is for. A secret is not
# a .js file; if one is, it is committed source and clean will not touch it.
# Deliberately not .txt, .md, .tfvars or anything else people really do keep a
# secret in — the point is only to exclude what is unambiguously program text.
SOURCE_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".py",
        ".pyc",
        ".pyi",
        ".go",
        ".java",
        ".kt",
        ".scala",
        ".rb",
        ".php",
        ".cs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".rs",
        ".swift",
        ".m",
        ".mm",
        ".pl",
        ".lua",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".r",
        ".map",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".less",
        ".vue",
        ".svelte",
    }
)


def _protects(
    name: str,
    sensitive: Sequence[str],
    local_state: Sequence[str] = (),
    relative: str | None = None,
    path: Path | None = None,
) -> bool:
    """Whether `name` is something a removal must not take with it.

    Two lists, because the source-code exemption belongs to exactly one of them.
    trash.sensitive casts a wide net — *token*, *creds*, *password* — so a
    tokenizer.js matches it and is plainly not a secret. clean.ignored_keep is
    the opposite: every entry is there because the user said "this file is local
    and is not in git", and `.env.sh` or `secrets.py` is the whole point of it.
    Exempting source from that list deleted exactly the files it names.
    """
    names = (name,) if relative is None else (name, relative)
    # local_state first, and unconditionally: clean.ignored_keep and clean.keep
    # get no exemption at all — not the source-code one, not the certificate
    # one. Every entry in them is there because somebody wrote it down. Falling
    # through the sensitive branch with a `return` skipped this line entirely,
    # so a .pem holding no private key lost its ignored_keep protection and was
    # hard-deleted, which is the opposite of what three documents promise.
    if any(_matches(one, local_state) for one in names):
        return True
    if Path(name).suffix.lower() in SOURCE_SUFFIXES:
        return False
    return any(_matches(one, sensitive) for one in names) and not _plainly_not_a_secret(path)


# A certificate file holds a private key or it does not, and the difference is
# written in the file. Every .venv ships a certifi/cacert.pem: 130 public
# certificates, no key at all, and treating it as a credential is how a 200 MB
# virtualenv is kept back over 300 KB of published trust anchors.
CERTIFICATE_SUFFIXES = frozenset({".pem", ".crt", ".cer", ".ca-bundle"})
PRIVATE_KEY_MARKER = b"PRIVATE KEY"
# The whole file, up to a ceiling no certificate bundle approaches. A haproxy.pem
# is `cat fullchain.pem privkey.pem`, and a long chain puts the key past any
# small peek — 64 KB was not enough, and the key was deleted with the file.
# Anything bigger than this is not a certificate bundle, so it keeps its
# protection rather than being read.
CERTIFICATE_LIMIT = 4 * 1024 * 1024


def _plainly_not_a_secret(path: Path | None) -> bool:
    """Whether this path is provably harmless despite matching by name.

    Two cases, both from a real workspace and both costing more than they save:

    A certificate bundle with no private key in it. `*.pem` is in the default
    trash.sensitive because a .pem *can* be a key — but when the bytes say it is
    not, the name is not evidence of anything.

    A directory whose every file is source code. `*token*` matches eslint's
    `source-code/token-store/`, which is forty .js files. The suffix exemption
    reads the name, and a directory has no suffix, so it could never apply to
    one — while `credentials/` holding an id_rsa still must be kept, and is,
    because that directory holds something that is not source.
    """
    if path is None:
        return False
    try:
        if path.is_dir():
            return _only_source_inside(path)
        if path.suffix.lower() in CERTIFICATE_SUFFIXES:
            if path.stat().st_size > CERTIFICATE_LIMIT:
                return False
            return PRIVATE_KEY_MARKER not in path.read_bytes()
    except OSError:
        # Cannot read it, so cannot rule it out. Keeping it is the safe answer,
        # and the same one _Unreadable gives everywhere else.
        return False
    return False


def _only_source_inside(directory: Path) -> bool:
    """True when every file under `directory` is source code, and there is one."""
    found = False
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            if Path(name).suffix.lower() not in SOURCE_SUFFIXES:
                return False
            found = True
    return found


def _protected(path: Path, root: Path, keep: Sequence[str], tracked: set[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    if any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern)
        for pattern in keep
    ):
        return True
    # tracked holds both spellings; see tracked_paths.
    return relative in tracked or relative.lower() in tracked


def _what_to_keep(
    path: Path,
    root: Path,
    is_dir: bool,
    quarantine: Quarantine | None,
    holding: Quarantine | None,
    sensitive: Sequence[str],
    local_state: Sequence[str] = (),
) -> tuple[Quarantine | None, list[Path], str] | str:
    """Where this path goes, what has to be lifted out of it first, and why.

    A plain string instead means it cannot be emptied out safely and has to stay
    whole, and is the reason to report — see _protected_within.
    """
    if quarantine is not None or not (sensitive or local_state):
        return quarantine, [], ""
    if holding is not None and _protects(path.name, sensitive, local_state, path=path):
        # The name first, and for a directory as much as for a file: a directory
        # called credentials/ or tokens/ *is* the thing being protected, so it
        # goes whole rather than being emptied out. _protected_within walks the
        # children and never looks at the root, so without this a directory
        # called tokens/ was hard-deleted while a file called api-token.tfplan
        # beside it was correctly quarantined.
        #
        # This one needs somewhere to put it, which is why it is the only part
        # of the protection that depends on a quarantine existing. Emptying a
        # directory out does not: what is protected simply stays where it is,
        # and requiring a quarantine for that meant a run configured without
        # one deleted the very files these lists name.
        return holding, [], f" because it is {path.name}"
    if not is_dir:
        return quarantine, [], ""
    rescue, why = _protected_within(path, sensitive, local_state, base=root)
    if rescue is None:
        return why
    if not rescue:
        return quarantine, [], ""
    # Not the whole directory. Moving a 3.8 MB node_modules aside because one
    # file in it is called tokenizer.js reclaimed nothing at all — it renamed the
    # tree into a directory in the same workspace — while the README's first
    # promise is the space back. So the directory is thinned out instead: what
    # is protected stays, everything else goes.
    shown = rescue[0].relative_to(path).as_posix()
    more = f" and {len(rescue) - 1} more" if len(rescue) > 1 else ""
    return quarantine, rescue, f", keeping {shown}{more}"


def _remove(
    path: Path,
    root: Path,
    scope: str,
    decider: Decider,
    quarantine: Quarantine | None,
    is_dir: bool,
    kind: str = "remove",
    protect_nested: bool = True,
    sensitive: Sequence[str] = (),
    local_state: Sequence[str] = (),
    holding: Quarantine | None = None,
) -> Action:
    relative = path.relative_to(root).as_posix()
    holds_repo = unreadable = False
    try:
        size, holds_repo, unreadable = (
            _measure(path) if is_dir else (path.stat().st_size, False, False)
        )
    except OSError:
        size = 0
    action = Action(
        kind,
        scope,
        relative,
        f"remove {'directory' if is_dir else 'file'}",
        size=size,
        settled=True,
    )
    action.quarantined = quarantine is not None
    plan = _what_to_keep(path, root, is_dir, quarantine, holding, sensitive, local_state)
    if isinstance(plan, str):
        # Cannot be emptied out safely, so it stays whole, and plan says which
        # of the two reasons it is. Reporting one as the other is how a
        # .terraform with an unreadable subdirectory and no symlink anywhere
        # came to be "kept: holds a protected symlink".
        action.detail = plan
        action.skipped = True
        action.size = 0
        return action
    quarantine, rescue, because = plan
    action.quarantined = quarantine is not None
    kept_bytes = 0
    for one in rescue:
        with contextlib.suppress(OSError):
            kept_bytes += _measure(one)[0] if one.is_dir() else one.stat().st_size
    action.kept_size = kept_bytes
    # size is what this action frees, in a dry run as much as in an apply, so
    # the two summaries agree; kept_size is what stays behind, on its own line.
    action.size = max(0, action.size - kept_bytes)
    # Said before the decision, so a dry run names the outcome it is predicting.
    thinned = is_dir and bool(rescue) and quarantine is None
    verb = "empty out" if thinned else ("quarantine" if action.quarantined else "remove")
    what = "" if thinned else f" {'directory' if is_dir else 'file'}"
    action.detail = f"{verb}{what}{because}"
    if (holds_repo or unreadable) and protect_nested:
        # A vendored or forgotten checkout inside an artefact directory. Deleting
        # the parent would take the repository with it, and nothing in an
        # artefact directory is worth that. `clean.regenerable` lists the caches
        # where the nested repository is itself a tool's clone. A subtree that
        # cannot be read gets the same treatment and says so in its own words:
        # not being able to look is not the same as having looked.
        action.detail = (
            "kept: contains a git repository"
            if holds_repo
            else "kept: something in here cannot be read"
        )
        action.skipped = True
        action.size = 0
        return action
    if not decider.allow(action):
        return action
    try:
        _guard(path, root)
        if quarantine is not None:
            quarantine.take(path)
        elif is_dir and rescue:
            # Thinned out rather than removed: the protected entries stay
            # exactly where they are, because a .env is not a copy of anything
            # and an application reads it from that path.
            #
            # The returned count is of bytes that actually went. Discarding it
            # and keeping the prediction meant a directory holding an unreadable
            # subtree reported "emptied out" and the full size freed, with no
            # error, while 97.7 KB of it was still on the disk.
            action.size, failed = _thin_out(path, rescue)
            if failed:
                action.error = failed
                return action
        elif is_dir:
            # rmtree removes what it can and then raises, so the plain call
            # reported a directory as a pure failure while three files had
            # really gone — unmentioned and uncounted. _thin_out has measured
            # after the fact for several rounds; this path had not.
            problem = _rmtree_counting(path, action)
            if problem is not None:
                return problem
        else:
            path.unlink()
    except (OSError, Failure) as exc:
        action.error = str(exc)
        return action
    action.applied = True
    # "removed" would be untrue for a quarantined path: it is still on the disk,
    # a restore away.
    action.detail = (
        "emptied out" if thinned else "quarantined" if action.quarantined else "removed"
    ) + because
    return action


def _rmtree_counting(path: Path, action: Action) -> Action | None:
    """Remove a directory, and if it cannot finish, say how much of it went.

    rmtree deletes what it can and then raises, so the plain call reported a
    directory as a pure failure while three files had really gone — unmentioned
    and uncounted. _thin_out has measured after the fact for several rounds;
    this path had not.
    """
    before = action.size
    try:
        shutil.rmtree(path)
    except OSError as exc:
        left = _measure(path)[0] if path.exists() else 0
        action.size = max(0, before - left)
        action.error = str(exc)
        action.applied = action.size > 0
        if action.applied:
            action.detail = f"partly removed, {human_size(action.size)} of it"
        return action
    return None


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


def _free_stamp(root: Path) -> str:
    """Claim a quarantine directory, atomically.

    Checking that a name is free and then using it leaves a gap two processes
    can both walk through — and the loser's manifest would overwrite the
    winner's, stranding files `restore` promises to bring back. mkdir without
    exist_ok is the claim: whoever creates it owns it, and the other tries the
    next name.
    """
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for suffix in range(0, 1000):
        candidate = base if suffix == 0 else f"{base}-{suffix}"
        try:
            (root / candidate).mkdir(parents=True)
        except FileExistsError:
            continue
        except OSError as exc:
            raise Failure(f"cannot create a quarantine under {root}: {exc}") from exc
        return candidate
    raise Failure(f"cannot find an unused quarantine name under {root}")  # pragma: no cover


class Quarantine:
    """A timestamped holding area, so a wrong guess is undoable.

    Files are moved, not copied, and a manifest records where each came from.
    `git-tidy restore` reads the manifest back.
    """

    def __init__(self, root: Path, workspace: Path, stamp: str | None = None) -> None:
        self.workspace = workspace
        self.root = root
        self.entries: list[dict[str, str]] = []
        self._lock = threading.Lock()
        # Claimed on the first move, not here: a dry run must not leave a
        # directory behind while reporting that it changed nothing. The claim
        # itself is a mkdir, so two runs in the same second cannot share one.
        self._stamp = stamp
        if stamp is not None:
            (root / stamp).mkdir(parents=True, exist_ok=True)

    @property
    def stamp(self) -> str:
        if self._stamp is None:
            self._stamp = _free_stamp(self.root)
        return self._stamp

    @property
    def dir(self) -> Path:
        return self.root / self.stamp

    def take(self, path: Path) -> Path:
        relative = path.resolve().relative_to(self.workspace.resolve())
        with self._lock:
            destination = self.dir / CONTENT_DIRNAME / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Keep counting: one suffix can collide as easily as none, and the
            # loser of that race would be overwritten rather than kept.
            if destination.exists():
                stem, suffix = destination.name, 1
                while destination.exists():
                    destination = destination.with_name(f"{stem}.{suffix}")
                    suffix += 1
            # Recorded before the move, one appended line at a time. A crash, a
            # kill or a flat battery half way through then leaves a journal that
            # still says where everything came from; the alternative is a
            # directory of files nobody can put back. Appending rather than
            # rewriting matters: two thousand removals would otherwise serialise
            # two million records and hold the lock through all of them.
            entry = {"from": str(path), "to": str(destination)}
            self.entries.append(entry)
            self._append(entry)
            shutil.move(str(path), str(destination))
        return destination

    def _append(self, entry: dict[str, str]) -> None:
        journal = self.dir / JOURNAL_NAME
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write_manifest(self) -> Path | None:
        """Fold the journal into the manifest restore reads. Written once."""
        if not self.entries:
            return None
        manifest = self.dir / MANIFEST_NAME
        payload = {
            "version": __version__,
            "created": self.stamp,
            "workspace": str(self.workspace),
            "entries": self.entries,
        }
        # Written to a neighbour and renamed, so it is never found half-written.
        scratch = manifest.with_suffix(".partial")
        with scratch.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            # The rename is atomic; the bytes behind it are not, without this.
            os.fsync(handle.fileno())
        scratch.replace(manifest)
        return manifest


def restore(quarantine_root: Path, stamp: str | None, decider: Decider) -> list[Action]:
    """Put quarantined files back where they came from."""
    if not quarantine_root.is_dir():
        raise Failure(f"no quarantine at {quarantine_root}")
    stamps = sorted(
        p.name
        for p in quarantine_root.iterdir()
        if (p / MANIFEST_NAME).is_file() or (p / JOURNAL_NAME).is_file()
    )
    if not stamps:
        if stamp is not None:
            # Named one, so the name is the thing to say — the vaguer "no
            # quarantine with a manifest under <root>" sent people looking at
            # the directory rather than at what they typed.
            raise Failure(f"no quarantine {stamp!r} under {quarantine_root}")
        # Nothing to put back is not an error, any more than it is for
        # `restore --list`, which prints "no quarantines" and exits 0. A restore
        # is a thing you run again after one already worked, and the second one
        # should not fail.
        return [Action("restore", "-", "-", "nothing in the quarantine", skipped=True)]
    chosen = stamp or stamps[-1]
    if chosen not in stamps:
        raise Failure(f"no quarantine {chosen!r}; available: {', '.join(stamps)}")
    manifest = _read_manifest(quarantine_root / chosen)
    actions: list[Action] = []
    workspace = Path(manifest.get("workspace", quarantine_root.parent))
    if manifest.get("unreadable"):
        actions.append(
            Action(
                "restore",
                chosen,
                "-",
                f"{plural(manifest['unreadable'], 'record')} unreadable, left in the quarantine",
                skipped=True,
            )
        )
    try:
        with keeping(actions):
            _put_back(manifest, workspace, quarantine_root, chosen, decider, actions)
    finally:
        # Even on the way out: a manifest that still claims files already back
        # makes the next restore report them as failures.
        _forget_restored(quarantine_root / chosen, manifest, actions)
    return actions


def _forget_restored(directory: Path, manifest: dict[str, Any], actions: Sequence[Action]) -> None:
    """Drop the entries that are back where they belong.

    Leaving them in would make `restore --list` describe files that are no longer
    there, and make the next default restore pick a quarantine with nothing in
    it.
    """
    restored = {a.target for a in actions if a.applied}
    if not restored:
        return
    left = [entry for entry in manifest["entries"] if entry["from"] not in restored]
    # Records the journal could not read are not in `entries`, but the files
    # they describe are still under files/. Removing the directory would delete
    # exactly what was just reported as left in the quarantine.
    if left or manifest.get("unreadable"):
        payload = {**manifest, "entries": left}
        payload.pop("unreadable", None)
        scratch = directory / f"{MANIFEST_NAME}.partial"
        scratch.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        scratch.replace(directory / MANIFEST_NAME)
        return
    # Nothing left to restore, and an empty quarantine is just clutter.
    shutil.rmtree(directory, ignore_errors=True)


def _put_back(
    manifest: dict[str, Any],
    workspace: Path,
    quarantine_root: Path,
    chosen: str,
    decider: Decider,
    actions: list[Action],
) -> None:
    # Shallowest first: restoring a descendant re-creates its parent directory
    # on the way, and the ancestor's own restore would then find something in
    # its place and refuse for ever.
    ordered = sorted(manifest["entries"], key=lambda e: len(Path(e["from"]).parts))
    for entry in ordered:
        source, destination = Path(entry["to"]), Path(entry["from"])
        action = Action("restore", chosen, str(destination), "restore")
        # The manifest is a file on disk like any other. Restoring what it says
        # without checking would move anything anywhere.
        if not _within(source, quarantine_root) or not _within(destination, workspace):
            action.error = "manifest points outside the workspace"
        elif not source.exists():
            action.error = "missing from the quarantine"
        elif destination.exists():
            action.error = "something is already at that path"
        elif decider.allow(action):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            except OSError as exc:
                # One unwritable destination must not abandon the rest of the
                # sweep still sitting in the quarantine.
                action.error = str(exc)
            else:
                action.applied = True
                action.detail = "restored"
        actions.append(action)


def _read_manifest(directory: Path) -> dict[str, Any]:
    """The manifest, or the journal a killed run left behind.

    A manifest that will not parse is exactly the case the journal exists for —
    a rename is atomic but the bytes behind it need not have reached the disk —
    so an unreadable one falls through rather than ending the command.
    """
    manifest = directory / MANIFEST_NAME
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass  # fall through to the journal
    entries: list[dict[str, str]] = []
    torn = 0
    journal = directory / JOURNAL_NAME
    if not journal.is_file():
        raise Failure(f"{directory.name}: neither a readable manifest nor a journal")
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            # The kill that stopped the sweep can tear the record it was writing.
            # Losing that one entry is the cost; refusing to read the file would
            # lose every entry before it, which is what the journal exists to
            # prevent.
            torn += 1
    return {
        "workspace": str(directory.parent.parent),
        "entries": entries,
        "unreadable": torn,
    }


def _within(path: Path, root: Path) -> bool:
    """True when `path` really is inside `root`, symlinks resolved."""
    try:
        resolved_root = root.resolve()
        return resolved_root == path.resolve() or resolved_root in path.resolve().parents
    except OSError:  # pragma: no cover - unreadable path
        return False


def expire_quarantines(
    quarantine_root: Path, days: int, decider: Decider, only: str | None = None
) -> list[Action]:
    """Delete quarantines past their retention, or just the one named.

    A named stamp still has to be past its retention — `--expire` is the age
    rule, and naming one narrows it rather than overriding it. Saying so matters:
    both the too-young case and the misspelt-stamp case used to print an empty
    summary and exit 0, so the quarantine looked expired when it was still there.
    """
    if days <= 0:
        # 0 means never, in both callers. It used to mean "cutoff = now", so
        # `restore --expire` deleted every quarantine including one made seconds
        # earlier — while the automatic path, added later, read the same value as
        # "off". A negative one moved the cutoff into the future and took the
        # quarantine the current run had just written, before its manifest was
        # even flushed. Refused in _check_number as well; this is the backstop.
        return []
    if not quarantine_root.is_dir():
        if only is not None:
            raise Failure(f"no quarantine at {quarantine_root}")
        return []
    if only is not None:
        named = quarantine_root / only
        if not named.is_dir():
            raise Failure(f"no quarantine {only!r} in {quarantine_root}")
        if named.stat().st_mtime > time.time() - days * 86400:
            return [
                Action(
                    "expire",
                    "quarantine",
                    only,
                    f"kept: not yet {plural(days, 'day')} old",
                    skipped=True,
                )
            ]
    cutoff = time.time() - days * 86400
    actions: list[Action] = []
    with keeping(actions):
        _expire(quarantine_root, cutoff, days, decider, actions, only)
    return actions


def _expire(
    quarantine_root: Path,
    cutoff: float,
    days: int,
    decider: Decider,
    actions: list[Action],
    only: str | None = None,
) -> None:
    for entry in sorted(quarantine_root.iterdir()):
        if only is not None and entry.name != only:
            continue
        try:
            if not entry.is_dir() or entry.stat().st_mtime > cutoff:
                continue
        except OSError as exc:
            actions.append(Action("expire", "quarantine", entry.name, "", error=str(exc)))
            continue
        # Only quarantines this tool wrote. Something else that happens to sit
        # under the quarantine root is not ours to delete recursively.
        if not (entry / MANIFEST_NAME).is_file() and not (entry / JOURNAL_NAME).is_file():
            actions.append(
                Action("expire", "quarantine", entry.name, "kept: not a quarantine", skipped=True)
            )
            continue
        action = Action("expire", "quarantine", entry.name, f"delete, older than {days} days")
        action.size = directory_size(entry)
        if decider.allow(action):
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                action.error = str(exc)
            else:
                action.applied = True
                action.detail = "deleted"
        actions.append(action)


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
    sensitive = _protects(name, trash["sensitive"], path=path)
    # keep is absolute. patterns win over the heuristics, not over an explicit
    # instruction to leave something alone.
    if _matches(name, trash["keep"]):
        return False, "", sensitive
    explicit = _matches(name, trash["patterns"])
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
    resolver: ConfigResolver | None = None,
) -> list[Action]:
    trash = cfg["trash"]
    deeper = _anywhere_enabled(workspace, resolver, "trash")
    if not trash["enabled"] and not deeper:
        return []
    if trash["scope"] == "root" and (deeper or _anywhere_wide(workspace, resolver)):
        # The root's scope decides which candidates are generated at all, so a
        # deeper config is unreachable under the default "root" — whether it
        # switches trash on or only widens its own scope. `git-tidy config sub`
        # answered "scope: workspace" while the run swept nothing there, which is
        # the opposite of "the deepest one wins". Widen the search and let
        # _sweepable, which resolves per path, decide what may actually be swept.
        trash = {**trash, "scope": "workspace"}
    if trash["scope"] not in ("root", "workspace"):
        raise Failure(f"trash.scope must be root or workspace, not {trash['scope']!r}")
    now = time.time()
    repo_set = {p.resolve() for p in repos}
    tracked = tracked_from_outside(workspace)
    actions: list[Action] = []
    with keeping(actions):
        _sweep(workspace, trash, tracked, repo_set, now, decider, quarantine, actions, resolver)
    return actions


def _sweep(
    workspace: Path,
    trash: dict[str, Any],
    tracked: set[str],
    repo_set: set[Path],
    now: float,
    decider: Decider,
    quarantine: Quarantine,
    actions: list[Action],
    resolver: ConfigResolver | None = None,
) -> None:
    # A directory that is going as a whole takes its contents with it. Reporting
    # those separately would inflate a dry run against the apply that follows it.
    swept: list[str] = []
    for path in _trash_candidates(workspace, trash, repo_set, resolver):
        relative = path.relative_to(workspace).as_posix()
        # tracked holds both spellings; see tracked_paths. A case-only
        # difference is the same file on macOS and Windows.
        if relative in tracked or relative.lower() in tracked:
            continue
        if _inside_a_swept_directory(relative, swept):
            continue
        # Deepest wins here too, and for a directory the config that governs it
        # is its own: it is the thing about to be swept.
        #
        # _trash_candidates selected this path using the workspace config, which
        # is all it can see while walking. Everything that governs *whether* a
        # candidate may be swept is therefore re-checked here against the config
        # that actually applies to it — not one setting at a time, which is how
        # this was got wrong repeatedly.
        governs = path if path.is_dir() else path.parent
        here = resolver.for_path(governs)["trash"] if resolver is not None else trash
        junk, why, sensitive = (
            classify_trash(path, here, now)
            if _sweepable(path, here, workspace)
            else (False, "", False)
        )
        if not junk:
            continue
        if (kept := _holds_a_repository(path, relative)) is not None:
            actions.append(kept)
            continue
        sensitive, why = _credential_check(path, here, sensitive, why)
        use_quarantine = here["quarantine"] or sensitive
        detail = f"sweep: {why}" + (" — sensitive, quarantined" if sensitive else "")
        outcome = _sweep_one(
            path,
            workspace,
            relative,
            detail,
            why,
            sensitive,
            use_quarantine,
            decider,
            quarantine,
        )
        # Only now: a directory that was kept, declined or failed still holds
        # its contents, and they must go on being offered.
        # Not "applied": in a dry run nothing is, and the contents would still
        # go with it, so suppressing them keeps the prediction honest. What must
        # not suppress them is a directory that was kept, declined or failed.
        if path.is_dir() and not outcome.skipped and not outcome.error:
            swept.append(relative)
        actions.append(outcome)


def _sweep_one(
    path: Path,
    workspace: Path,
    relative: str,
    detail: str,
    why: str,
    sensitive: bool,
    use_quarantine: bool,
    decider: Decider,
    quarantine: Quarantine,
) -> Action:
    """Move or delete one swept path, and say which of the two it was."""
    action = Action("trash", "workspace", relative, detail, quarantined=use_quarantine)
    try:
        action.size = directory_size(path) if path.is_dir() else path.stat().st_size
    except OSError:
        action.size = 0
    if not decider.allow(action):
        return action
    try:
        _guard(path, workspace)
        if use_quarantine:
            quarantine.take(path)
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        action.applied = True
        # Keep the "sensitive" marker in the finished message too: seeing which
        # credentials moved is the whole point of the report.
        action.detail = ("quarantined: " if use_quarantine else "deleted: ") + why
        if sensitive:
            action.detail += " — sensitive, kept in quarantine"
    except (OSError, Failure) as exc:
        action.error = str(exc)
    return action


def tracked_from_outside(workspace: Path, timeout: int = 300) -> set[str]:
    """Tracked paths of the repository the workspace sits inside, if any.

    Returned relative to the workspace. No repository *below* the workspace
    claims these files, so without this they look like they belong to nobody —
    and sweeping or cleaning one would take committed content.
    """
    outer = enclosing_repo(workspace)
    if outer is None:
        return set()
    prefix = workspace.resolve().relative_to(outer.resolve()).as_posix()
    return {
        entry[len(prefix) + 1 :]
        for entry in tracked_paths(Git(outer, timeout=timeout))
        if entry.startswith(f"{prefix}/")
    }


def _inside_a_swept_directory(relative: str, swept: Sequence[str]) -> bool:
    """A directory going as a whole takes its contents with it."""
    return any(relative.startswith(f"{parent}/") for parent in swept)


def _holds_a_repository(path: Path, relative: str) -> Action | None:
    """Refuse to sweep a directory with a git repository buried in it.

    clean has refused this from the start; trash did not, and a forgotten clone
    under a `project.old/` directory is exactly what the rule is for. Sweeping
    it would take its unpushed commits with it.
    """
    if not path.is_dir():
        return None
    _, holds_repo, unreadable = _measure(path)
    if not (holds_repo or holds_git_data(path)):
        if unreadable:
            return Action(
                "trash",
                "workspace",
                relative,
                "kept: something in here cannot be read",
                skipped=True,
            )
        return None
    return Action("trash", "workspace", relative, "kept: contains a git repository", skipped=True)


def _credential_check(
    path: Path, trash: dict[str, Any], sensitive: bool, why: str
) -> tuple[bool, str]:
    """Whether this path must be quarantined rather than deleted, and why.

    A file that might hold the only copy of a credential is never deleted
    outright, whatever the quarantine setting says — and a directory holding one
    counts, since deleting it takes the credential with it.
    """
    buried = _sensitive_within(path, trash["sensitive"]) if path.is_dir() else None
    if buried is None:
        return sensitive, why
    return True, f"{why}, contains {buried}"


def _sweepable(path: Path, trash: dict[str, Any], workspace: Path) -> bool:
    """Whether the config governing this path allows it to be swept at all.

    Kept apart from classify_trash, which answers "is this junk". This answers
    the prior question: is sweeping switched on here, and does this kind of thing
    count — a directory only when trash.dirs says so, and anything below the
    workspace root only when the scope reaches that far.
    """
    if not trash["enabled"]:
        return False
    if path.is_dir() and not trash["dirs"]:
        return False
    return not (trash["scope"] == "root" and path.parent != workspace)


def _anywhere_wide(workspace: Path, resolver: ConfigResolver | None) -> bool:
    """Whether any config below the root asks for trash.scope: workspace."""
    if resolver is None:
        return False
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in (".git", QUARANTINE_DIRNAME)]
        if any(name in filenames for name in CONFIG_NAMES) and (
            resolver.for_path(here)["trash"]["scope"] == "workspace"
        ):
            return True
    return False


def _anywhere_enabled(workspace: Path, resolver: ConfigResolver | None, section: str) -> bool:
    """Whether any config in the tree turns this step on.

    Candidates are generated from the workspace root, so a deeper config could
    turn a step off but never on: `git-tidy config ./proj` said enabled, and the
    run said "trash is off". The deeper answer still decides what happens to
    each path; this only decides whether to go looking.
    """
    if resolver is None:
        return False
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in (".git", QUARANTINE_DIRNAME)]
        if (
            any(name in filenames for name in CONFIG_NAMES)
            and (resolver.for_path(here)[section]["enabled"])
        ):
            return True
    return False


def _trash_candidates(
    workspace: Path,
    trash: dict[str, Any],
    repos: set[Path],
    resolver: ConfigResolver | None = None,
) -> Iterator[Path]:
    if trash["scope"] == "root":
        for entry in sorted(workspace.iterdir()):
            if entry.name == QUARANTINE_DIRNAME or entry.is_symlink():
                continue
            if entry.is_dir() and (
                not trash["dirs"] or holds_git_data(entry) or entry.resolve() in repos
            ):
                continue
            yield entry
        return
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        here = Path(dirpath)
        if here.resolve() in repos or ".git" in here.parts:
            dirnames[:] = []
            continue
        # Deepest wins for what is *offered*, not only for what is swept.
        trash = resolver.for_path(here)["trash"] if resolver is not None else trash
        keep_walking = [
            d
            for d in sorted(dirnames)
            if d != QUARANTINE_DIRNAME
            and not holds_git_data(here / d)
            and not (here / d).is_symlink()
        ]
        dirnames[:] = keep_walking
        if trash["dirs"]:
            for name in keep_walking:
                yield here / name
        for name in sorted(filenames):
            candidate = here / name
            # Never a symlink: its size is its target's, which may not even be
            # in the workspace, and _guard refuses to follow it anyway — so
            # offering it would predict an action that cannot happen.
            if not candidate.is_symlink():
                yield candidate


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def without_credential(url: str) -> str | None:
    """The same URL with the user and secret taken out, or None if there is none.

    Built from the match rather than substituted into: `sub(r"\\1", url)` looked
    right and quietly ate the `://` along with the credential, leaving a remote
    reading `httpsexample.invalid/r.git`. The user is dropped as well as the
    password — git's credential helper and the SSH agent are what should be
    holding both, and a username left in a URL still pins the clone to one
    account.
    """
    match = CREDENTIAL_IN_URL.match(url)
    if not match:
        return None
    scheme = match.group("scheme") or match.group("webscheme")
    return f"{scheme}://" + url[match.end() :]


def redact(url: str) -> str:
    """Hide the secret in a URL so a report can be pasted somewhere."""
    match = CREDENTIAL_IN_URL.match(url)
    if not match:
        return url
    scheme = match.group("scheme") or match.group("webscheme")
    who = match.group("user") if match.group("scheme") else match.group("token")
    hidden = f"{who}:***" if match.group("scheme") else "***"
    return f"{scheme}://{hidden}@" + url[match.end() :]


def doctor_repo(
    path: Path,
    name: str,
    cfg: dict[str, Any],
    fix: Decider | None = None,
) -> list[Action]:
    """Report the things that need a decision rather than a command.

    With `fix`, also carry out the three remedies that cannot cost a commit:
    put a detached HEAD back on its branch, take a credential out of a remote
    URL, and pack an oversized .git. Everything else doctor reports — unpushed
    commits, a branch that exists only here, no remote at all — is a decision
    somebody has to make, and is still only reported.
    """
    checks = cfg["doctor"]
    git = Git(path, timeout=int(cfg["sync"]["timeout"]))
    actions: list[Action] = []
    # doctor is the only other step that writes now, and every step that
    # accumulates needs this: answering 'q' half way through made the report
    # claim less had happened than had, while the HEAD really had moved.
    with keeping(actions):
        _doctor_checks(git, name, cfg, checks, fix, actions)
    return actions


def _doctor_checks(
    git: Git,
    name: str,
    cfg: dict[str, Any],
    checks: dict[str, Any],
    fix: Decider | None,
    actions: list[Action],
) -> None:

    # Neither of these needs a remote, and a repository without one is where
    # they matter most: nothing in it is pushed anywhere, and sync leaves it
    # alone, so a detached HEAD there stays detached.
    if checks["detached_head"] and not current_branch(git):
        actions.append(_detached(git, name, cfg, fix))
    if checks["large_git_mb"]:
        actions += _check_git_size(git, name, checks["large_git_mb"], fix)

    remotes = git.out("remote", check=False).split()
    if not remotes:
        if checks["no_remote"]:
            actions.append(Action("doctor", name, "-", "no remote configured", skipped=True))
        return actions

    if checks["credentials_in_url"]:
        _check_credentials(git, name, remotes, fix, actions)
    if checks["unpushed"]:
        actions += _check_unpushed(git, name, cfg["sync"])
    return actions


def _detached(git: Git, name: str, cfg: dict[str, Any], fix: Decider | None) -> Action:
    """A HEAD that is on no branch, and the way back to one.

    Only when the commit it is sitting on is already contained in that branch:
    otherwise the detached HEAD *is* the work, and switching away would leave it
    reachable from nothing but the reflog. That is the line between this and
    the things doctor only reports.
    """
    commit = git.out("rev-parse", "--short", "HEAD", check=False)
    detail = f"detached at {commit}"
    if fix is None:
        return Action("doctor", name, "HEAD", detail, skipped=True)

    refused = _cannot_leave_a_detached_head(git, name, cfg, detail)
    if refused is not None:
        return refused
    trunk = default_branch(git, cfg["sync"], readonly=True) or _local_trunk(git, cfg["sync"])

    action = Action("switch back", name, "HEAD", f"switch back to {trunk} from a detached {commit}")
    if not fix.allow(action):
        return action
    result = git.run("switch", trunk, check=False)
    if result.returncode != 0:
        action.error = last_line(result)
        return action
    action.applied = True
    action.detail = f"switched back to {trunk} from a detached {commit}"
    return action


def _local_trunk(git: Git, sync: dict[str, Any]) -> str | None:
    """The trunk of a repository that has no remote to ask.

    default_branch resolves through refs/remotes only, so a repository with no
    remote always came back None — and a detached HEAD there was refused with
    "no branch to go back to" while local main sat right there containing it.
    A repository with no remote is where a detached HEAD matters most: nothing
    in it is pushed anywhere, and sync leaves it alone, so it stays detached.
    """
    for candidate in sync["default_branch_candidates"]:
        if git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"):
            return str(candidate)
    return None


def _cannot_leave_a_detached_head(
    git: Git, name: str, cfg: dict[str, Any], detail: str
) -> Action | None:
    """Every reason not to move this HEAD, checked before anything is written.

    All four of these are gates _switch, _fast_forward and _diverged have had
    for several rounds each. --fix went round the outside of them, which is what
    happens when a fourth thing learns to move a HEAD and does not reuse the
    list of reasons not to.
    """

    def no(why: str) -> Action:
        return Action("doctor", name, "HEAD", f"{detail}, {why}", skipped=True)

    busy = _operation_in_progress(git)
    if busy:
        # A clean tree mid-bisect is the case _cannot_switch names in its own
        # comment: switching resets HEAD, the BISECT_* files survive, and the
        # next `git bisect good` marks the trunk tip. Nothing warns you.
        return no(f"and {busy} is in progress")
    if cfg["sync"]["worktrees"] == "skip" and is_linked_worktree(git):
        # Holding its own HEAD is the entire reason a linked worktree exists,
        # and sync.worktrees says so. --fix was overriding that setting in
        # silence, leaving the worktree holding the trunk the main checkout
        # then could never be switched onto.
        return no("and it is a linked worktree, which sync.worktrees keeps out of this")
    trunk = default_branch(git, cfg["sync"], readonly=True) or _local_trunk(git, cfg["sync"])
    if trunk is None:
        return no("and no branch to go back to")
    if is_dirty(git):
        # Its own wording, deliberately not "uncommitted changes": that phrase is
        # in FORCE_CAN_FIX, and --force sets sync.stash, which this function does
        # not read and has no stash path for. The summary was offering `run
        # --force` for something --force cannot do — and in a repository with no
        # remote, which is the case _local_trunk exists for, sync will not rescue
        # it either. It is also not "left on their branch": it is on no branch.
        return no("and there is uncommitted work on it")
    head = f"refs/heads/{trunk}"
    if not git.ok("show-ref", "--verify", "--quiet", head):
        # The trunk exists only on the remote, which is what a fresh clone whose
        # local main was deleted looks like. _switch creates and tracks it;
        # saying "those commits are not in main" was simply untrue.
        return no(f"and there is no local {trunk} to go back to")
    if not git.ok("merge-base", "--is-ancestor", "HEAD", head):
        holding = _commits_on_no_branch(git)
        where = "are on no branch" if holding else f"are not in {trunk}"
        return no(f"and those commits {where}")
    if _would_clobber_ignored(git, trunk, cfg["clean"]["ignored_keep"]) is not None:
        # The hole this whole function was written after: is_dirty deliberately
        # ignores ignored files, so a local .env is invisible to it, and
        # `git switch` replaces one the target branch tracks without a word. In
        # one run --fix --apply the tool printed sync's refusal, did the thing
        # it had refused, and summarised it as held back.
        clobbered = _would_clobber_ignored(git, trunk, cfg["clean"]["ignored_keep"])
        return no(f"and {trunk} tracks {clobbered}, which is ignored here and would be replaced")
    elsewhere = _checked_out_elsewhere(git, trunk)
    if elsewhere is not None:
        return no(f"and {trunk} is in use by the worktree {elsewhere}")
    return None


def _check_credentials(
    git: Git,
    name: str,
    remotes: Sequence[str],
    fix: Decider | None,
    found: list[Action],
) -> None:
    """A token in a remote URL is a secret sitting in plain text in .git/config.

    Appends to the caller's list rather than returning one. A repository can
    hold several credentialed URLs, and answering `q` at the second prompt threw
    away the record of the first — which had really been rewritten — while the
    run printed "everything already done is kept". Every other accumulator in
    this file takes the list it fills; this was the one that did not.
    """
    for remote in remotes:
        for setting, url in _configured_urls(git, remote):
            if not CREDENTIAL_IN_URL.match(url):
                continue
            where = setting.rsplit(".", 1)[-1]
            detail = f"credential in the remote {where} — {redact(url)}"
            if fix is None:
                found.append(Action("doctor", name, remote, detail, skipped=True))
                continue
            found.append(_strip_credential(git, name, remote, setting, url, detail, fix))


def _configured_urls(git: Git, remote: str) -> list[tuple[str, str]]:
    """Every URL actually written in .git/config for this remote.

    `git remote get-url` expands url.<base>.insteadOf, so a credential living in
    somebody's ~/.gitconfig was reported as a credential in *this* repository's
    config and then "fixed" by writing the already-clean value back — the same
    finding, every run, for ever. It also returns only the first fetch URL, so a
    pushurl and any second url= were never looked at: two secrets sitting in
    plaintext exactly where doctor promises to look.
    """
    found: list[tuple[str, str]] = []
    for setting in (f"remote.{remote}.url", f"remote.{remote}.pushurl"):
        # -z, and no strip: the value has to come back exactly as git stores it,
        # because that is what --fixed-value compares against. Trimming it meant
        # a URL saved with a trailing space never matched itself, and git
        # appended one more url= on every run instead of replacing.
        raw = git.run("config", "-z", "--get-all", setting, check=False).stdout
        found.extend((setting, url) for url in raw.split("\0") if url)
    return found


def _strip_credential(
    git: Git, name: str, remote: str, setting: str, url: str, detail: str, fix: Decider
) -> Action:
    """Rewrite the remote without the credential in it.

    Reversible in the only sense that matters: the URL is printed, redacted,
    before and after, and git's credential helper or the SSH agent is what
    should have been holding it anyway. It does not touch a single commit.
    """
    # The setting, not just the remote: a repository with both a url and a
    # pushurl asked twice with two identical prompts and no way to tell them
    # apart.
    where = setting.rsplit(".", 1)[-1]
    action = Action(
        "strip credential", name, f"{remote} {where}", f"take the credential out of {setting}"
    )
    if not fix.allow(action):
        return action
    stripped = without_credential(url)
    if stripped is None or stripped == url:  # pragma: no cover - it matched to get here
        action.error = "could not work out the URL without the credential"
        return action
    # The exact setting, replaced in place: `remote set-url` rewrites the first
    # fetch URL whatever was asked for, which is the wrong one for a pushurl or
    # a second url=.
    #
    # --replace-all's third argument is a POSIX regex, not a literal. A password
    # holding a +, ? or * therefore did not match its own value, and git
    # *appended* instead: the secret stayed in .git/config, the remote grew a
    # second push URL, and the report said the credential had been taken out.
    # Anchored and escaped, and then read back to make sure.
    # --fixed-value where git has it (2.30 and later): the value-pattern is a
    # regex, and building it from a *trimmed* copy of the value meant a URL
    # stored with a trailing space never matched itself, so git appended one
    # more url= on every run for ever. An anchored, escaped pattern is the
    # fallback and covers the metacharacters; only whitespace defeats it.
    result = git.run(
        "config", "--fixed-value", "--replace-all", setting, stripped, url, check=False
    )
    if result.returncode != 0 and "fixed-value" in (result.stdout + result.stderr):
        result = git.run(
            "config", "--replace-all", setting, stripped, "^" + re.escape(url) + "$", check=False
        )
    if result.returncode != 0:
        action.error = last_line(result)
        return action
    # This setting, not every URL the remote has: a repository with a credential
    # in both url and pushurl reported the first strip as failed, because the
    # second one was still there and had not been reached yet.
    # This value, not every credentialed value of this setting: a remote with
    # two credentialed url= lines reported the *first* strip as failed, because
    # the second had not been reached yet — and sent somebody to hand-edit a
    # file that was about to be clean.
    if any(one == url for _, one in _configured_urls(git, remote)):
        # Never report a secret removed without having looked. This is the one
        # remedy whose failure leaves somebody believing they need not rotate.
        action.error = f"the credential is still in {setting}; take it out by hand"
        return action
    action.applied = True
    action.detail = f"credential taken out of {setting}, now {stripped}"
    return action


def _check_unpushed(git: Git, name: str, sync: dict[str, Any]) -> list[Action]:
    found: list[Action] = []
    trunk = default_branch(git, sync, readonly=True)
    for branch in list_branches(git):
        if not branch.upstream:
            # Never pushed, so its commits are on no remote at all — the larger
            # half of "commits that exist only locally", and nothing else
            # reports it: prune passes over these unless prune_local_only is on,
            # and that setting is about deleting them.
            found.extend(_never_pushed(git, name, branch, trunk, sync))
            continue
        against = branch.upstream
        if branch.gone:
            # The upstream ref is gone, so rev-list against it exits 128 and the
            # count came back empty — silently dropping the one case where those
            # commits exist nowhere else. Measure against the trunk instead.
            if trunk is None:
                continue
            against = f"refs/remotes/{sync['remote']}/{trunk}"
            if not git.ok("show-ref", "--verify", "--quiet", against):
                against = f"refs/heads/{trunk}"
        # refs/heads/, so a tag of the same name cannot stand in for the branch.
        ahead = git.out("rev-list", "--count", f"{against}..refs/heads/{branch.name}", check=False)
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


def _never_pushed(
    git: Git, name: str, branch: BranchInfo, trunk: str | None, sync: dict[str, Any]
) -> list[Action]:
    """A local-only branch, and how much of it exists nowhere else."""
    if trunk is None or branch.name == trunk:
        return []
    against = f"refs/remotes/{sync['remote']}/{trunk}"
    if not git.ok("show-ref", "--verify", "--quiet", against):
        against = f"refs/heads/{trunk}"
        if not git.ok("show-ref", "--verify", "--quiet", against):
            return []
    ahead = git.out("rev-list", "--count", f"{against}..refs/heads/{branch.name}", check=False)
    if not ahead or ahead == "0":
        return []
    return [
        Action(
            "doctor",
            name,
            branch.name,
            f"no upstream: {plural(ahead, 'commit')} not pushed",
            skipped=True,
        )
    ]


def _check_git_size(git: Git, name: str, limit_mb: int, fix: Decider | None = None) -> list[Action]:
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
    if fix is None:
        return [Action("doctor", name, ".git", f"{megabytes} MB — consider git gc", skipped=True)]
    action = Action("pack", name, ".git", f"pack .git with git gc, {megabytes} MB")
    if not fix.allow(action):
        return [action]
    # Plain gc, so gc.pruneExpire applies: nothing unreachable goes until it is
    # two weeks old, and no reflog entry younger than gc.reflogExpire goes at
    # all, which is how an accidental reset stays recoverable. Deliberately not
    # --aggressive, which costs minutes per repository and gains little.
    result = git.run("gc", "--quiet", check=False)
    if result.returncode != 0:
        action.error = last_line(result)
        return [action]
    action.applied = True
    after = 0
    for line in git.out("count-objects", "-v", check=False).splitlines():
        field, _, value = line.partition(": ")
        if field in ("size", "size-pack", "size-garbage") and value.strip().isdigit():
            after += int(value)
    action.size = max(0, (kib - after) * 1024)
    action.detail = f"packed, {megabytes} MB down to {after // 1024} MB"
    return [action]


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


# Kinds where one line per path is noise rather than information: a workspace
# produces thousands of them, and no one reads past the first screen.
# Doctor findings are deliberately not here: each one says something different,
# and collapsing them would print the first one's message over all of them.
ROLLED_UP = ("remove", "ignored")


def _past_tense(action: Action) -> str:
    """What a group of identical actions did, in one word.

    From the flags, not from the wording: "removed, keeping id_rsa in
    quarantine" contains the word quarantine, so sniffing for it labelled a
    group of deleted directories "quarantined" — telling the reader the bytes
    were a restore away when they were gone.
    """
    if action.quarantined:
        return "quarantined" if action.applied else "would quarantine"
    if action.kept_size:
        return "emptied out" if action.applied else "would empty out"
    return "removed" if action.applied else "would remove"


class Printer:
    """Prints as work finishes, so a long run is not a silent one."""

    def __init__(self, stream: Any, quiet: bool, color: bool, verbose: bool = False) -> None:
        self.stream = stream
        self.quiet = quiet
        self.color = color
        self.verbose = verbose
        self._lock = threading.Lock()

    def batch(self, actions: Sequence[Action]) -> None:
        """Print one repository's worth of work, rolled up unless -v is on."""
        if self.quiet:
            return
        # Grouped by outcome as well as by kind: a batch that quarantined some
        # paths and deleted others must not be summarised with whichever verb
        # happened to come first.
        rolled: dict[tuple[str, str, bool, bool, str], list[Action]] = {}
        for action in actions:
            if self.verbose or action.kind not in ROLLED_UP or action.error:
                self.action(action)
                continue
            key = (
                action.kind,
                action.scope,
                action.skipped,
                action.quarantined,
                # Why, not only whether: five paths skipped for four different
                # reasons must not all be labelled with the first one's.
                _reason_of(action.detail) if action.skipped else "",
            )
            rolled.setdefault(key, []).append(action)
        for (kind, scope, skipped, quarantined, _reason), group in rolled.items():
            if len(group) == 1:
                self.action(group[0])
                continue
            size = sum(a.size for a in group)
            # The group key already holds the reason; using it means two groups
            # never print the same word for different causes.
            verb = (_reason or group[0].detail.split(":")[0]) if skipped else _past_tense(group[0])
            self.action(
                Action(
                    kind,
                    scope,
                    plural(len(group), "path"),
                    verb,
                    size=size,
                    applied=group[0].applied,
                    skipped=skipped,
                    quarantined=quarantined,
                )
            )

    def loud(self) -> Printer:
        """The same printer with quiet lifted, for output -q still owes."""
        if not self.quiet:
            return self
        twin = Printer(self.stream, quiet=False, color=self.color, verbose=self.verbose)
        twin._lock = self._lock
        return twin

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


# How each kind of action reads in the summary, in the order it is listed.
DID: tuple[tuple[str, str, str], ...] = (
    # kind, what happened, what would happen
    ("switch back", "detached HEADs put back on the trunk", "detached HEADs to put back"),
    ("strip credential", "credentials taken out of remote URLs", "credentials to take out"),
    ("pack", "repositories packed", "repositories to pack"),
    ("fetch", "repositories fetched", "repositories to fetch"),
    ("switch", "branches switched", "branches to switch"),
    ("update", "repositories fast-forwarded", "repositories to fast-forward"),
    ("stash+update", "stashed and fast-forwarded", "to stash and fast-forward"),
    ("rebase", "repositories rebased", "repositories to rebase"),
    ("stash+rebase", "stashed and rebased", "to stash and rebase"),
    ("stash+switch", "stashed and switched", "to stash and switch"),
    ("submodules", "submodules updated", "submodules to update"),
    ("branch", "branches deleted", "branches to delete"),
    ("remove", "artefacts removed", "artefacts to remove"),
    ("remove+quarantined", "artefacts quarantined", "artefacts to quarantine"),
    ("ignored", "ignored paths removed", "ignored paths to remove"),
    ("ignored+quarantined", "ignored paths quarantined", "ignored paths to quarantine"),
    ("trash", "loose files swept", "loose files to sweep"),
    # trash.quarantine defaults to true, so this is the ordinary case, not the
    # exception — without it a default sweep produced no count at all.
    ("trash+quarantined", "loose files quarantined", "loose files to quarantine"),
    ("restore", "files restored", "files to restore"),
    ("expire", "quarantines deleted", "quarantines to delete"),
)


def summarise(report: Report, mode: str, printer: Printer, forced: bool = False) -> None:
    """A summary a person can act on: what happened, what needs them, what broke.

    Printed even under -q. That flag says "only print the summary"; suppressing
    this as well left `git-tidy trash --apply -q` moving files and emitting
    nothing at all.
    """
    printer = printer.loud()
    done: dict[str, int] = {}
    for action in report.actions:
        if action.applied or (mode == DRY and not action.skipped and not action.error):
            # A quarantined path was moved, not removed, and the disk line below
            # says exactly that in bytes. Counting it as removed contradicted it.
            key = action.consent_key
            done[key] = done.get(key, 0) + 1

    printer.heading("Summary")
    for kind, did, would in DID:
        if done.get(kind):
            printer.line(f"  {done[kind]:>6}  {would if mode == DRY else did}")

    freed = report.bytes_found if mode == DRY else report.bytes_freed
    if freed:
        printer.line(f"  {human_size(freed):>6}  {'to free' if mode == DRY else 'freed'}")
    held = report.bytes_found_quarantined if mode == DRY else report.bytes_quarantined
    if held:
        # Still on the disk, deliberately. Calling it freed would be a lie the
        # next `df` would expose.
        moved = "would move to quarantine" if mode == DRY else "moved to quarantine"
        printer.line(f"  {human_size(held):>6}  {moved}, not reclaimed")
    stayed = report.bytes_kept_in_place
    if stayed:
        left = "would stay" if mode == DRY else "stayed"
        printer.line(f"  {human_size(stayed):>6}  {left} in place: local state and credentials")

    _summarise_held_back(report, printer, forced)
    _summarise_errors(report, printer)

    if mode == DRY and any(not a.skipped and not a.error for a in report.actions):
        printer.line("\n  Nothing was changed. --ask to confirm each one, --apply to do all.")


FORCE_CAN_FIX = (
    "branches with commits not in the trunk",
    "uncommitted changes — left on their branch",
)


def _summarise_held_back(report: Report, printer: Printer, forced: bool = False) -> None:
    """Group everything skipped by *why*, because the why is the actionable part."""
    reasons: dict[str, int] = {}
    for action in report.actions:
        if not action.skipped:
            continue
        reason = _reason_of(action.detail)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    if not reasons:
        return
    printer.line("\n  Held back — these need you:")
    for reason, count in sorted(reasons.items(), key=lambda pair: -pair[1]):
        printer.line(f"  {count:>6}  {reason}")
    # Only suggest --force where it would actually change something. Offering it
    # against a branch a worktree holds, or a repository that has diverged, is
    # advice that cannot work.
    if not forced and any(reason in FORCE_CAN_FIX for reason in reasons):
        # `run`, not the bare subcommand: --force only reaches unmerged branches
        # after a fetch in the same run, and prune does not fetch.
        printer.line("           `run --force` does the ones it safely can; -v shows each one")
    else:
        printer.line("           -v shows each one")


# Substring seen in a per-item message -> the class of problem it belongs to.
# Ordered, because "not in the trunk" and "not pushed" both mention commits.
NOT_HELD_BACK = (
    "up to date",
    "already deleted",
    "nothing to fast-forward",
    "nothing to fetch",
    # Not "linked worktree" on its own: that also matches the *refusal* to move
    # a linked worktree's detached HEAD, which is held back and must be counted.
    "linked worktree, left on",
    "staying on",
)
REASONS: tuple[tuple[str, str], ...] = (
    # First, and deliberately: "declined: switch from detached HEAD" is about
    # the answer, not about the HEAD. Every other needle here describes a state
    # the tool found; this one describes what the person said.
    ("declined", "declined at the prompt"),
    ("uncommitted work on it", "detached HEADs with uncommitted work on them"),
    ("uncommitted changes", "uncommitted changes — left on their branch"),
    ("needs a fetch in this run", "unmerged branches waiting on a fetch — use `run --force`"),
    ("not in origin", "branches with commits not in the trunk"),
    ("commit not in", "branches with commits not in the trunk"),
    ("commits not in", "branches with commits not in the trunk"),
    ("diverged", "diverged from upstream — needs a merge or rebase by hand"),
    ("contains a git repository", "directories holding a git repository"),
    ("in use by the worktree", "branches a worktree still has checked out"),
    ("uncommitted work in", "submodules with uncommitted work"),
    ("would be replaced", "a local file the incoming commits would overwrite"),
    ("kept: contains", "directories holding something that must not go"),
    ("on no branch", "commits on a detached HEAD and no branch"),
    ("cannot check merged", "branches with no trunk to compare against"),
    ("to compare against", "branches with no trunk to compare against"),
    ("not a quarantine", "directories under the quarantine that are not ours"),
    ("unreadable", "quarantine records that could not be read"),
    ("checked out in", "default branch checked out in another worktree"),
    ("ignored_keep", "ignored files kept as local state (.env, *.tfstate, keys)"),
    ("kept by clean.keep", "paths clean.keep protects"),
    ("a dependency tree", "dependency trees — clean.dependencies is off"),
    ("build output", "build output — clean.builds is off"),
    ("orphaned worktree", "orphaned worktrees — the parent pruned them away"),
    ("are on no branch", "detached HEADs holding commits that are on no branch"),
    ("are not in", "detached HEADs ahead of the trunk"),
    ("no branch to go back to", "detached HEADs with no branch to return to"),
    ("no local", "detached HEADs whose trunk is only on the remote"),
    ("linked worktree, which sync.worktrees", "linked worktrees, which sync.worktrees skips"),
    ("nothing in the quarantine", "nothing left to restore"),
    ("cannot be read", "directories with something in them nobody can read"),
    ("protected symlink", "directories holding a symlink named like a credential"),
    ("not yet", "quarantines not yet past trash.retention_days"),
    ("is in progress", "a merge, rebase, cherry-pick or bisect is unfinished"),
    ("no upstream", "on a local-only branch, never pushed"),
    ("no such remote", "the configured remote is not there"),
    ("no remote", "no remote configured"),
    ("credential in the remote", "a credential sits in a remote URL"),
    ("no longer exists", "on a branch whose upstream was deleted"),
    ("cannot compare", "cannot be compared with its upstream"),
    ("not pushed", "branches with commits that exist only here"),
    ("detached", "detached HEAD"),
    ("remote branch missing", "no usable default branch"),
    ("no default branch", "no usable default branch"),
    ("consider git gc", ".git big enough to be worth a git gc"),
)


def _reason_of(detail: str) -> str | None:
    """Collapse a per-item message into the class of problem it belongs to."""
    text = detail.lower()
    if any(quiet in text for quiet in NOT_HELD_BACK):
        return None  # nothing was held back; there was nothing to do
    for needle, reason in REASONS:
        if needle in text:
            return reason
    return "other, see the lines marked -"


def _summarise_errors(report: Report, printer: Printer) -> None:
    if not report.errors:
        return
    printer.line(f"\n  {len(report.errors)} failed:")
    seen: dict[str, int] = {}
    for action in report.errors:
        seen[action.error or "failed"] = seen.get(action.error or "failed", 0) + 1
    for message, count in sorted(seen.items(), key=lambda pair: -pair[1])[:5]:
        prefix = f"{count}x " if count > 1 else ""
        printer.line(f"      {prefix}{message[:100]}")


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
    # Which repositories actually fetched in this run, so prune knows whether
    # the [gone] marks it is about to act on were observed now or long ago. One
    # flag for the whole run would let a repository that never fetched inherit
    # another one's freshness.
    fetched: set[Path] = field(default_factory=set)
    # True only for the step that should report an orphaned worktree. Every step
    # sees the same broken directory, and four identical lines for one of them
    # made five orphans read as twenty.
    report_orphans: bool = False
    # The decider doctor is allowed to act through, or None when it is only
    # reporting — which is the default, and what `doctor` has always been.
    fix: Decider | None = None
    # Fetches that failed for a reason that is about the network rather than
    # about the repository, and the lock that guards the count.
    unreachable: set[str] = field(default_factory=set)
    reached_since: bool = False
    last_unreachable: str = ""
    _offline_lock: threading.Lock = field(default_factory=threading.Lock)

    def note_unreachable(self, url: str, message: str) -> None:
        """Record a fetch that failed for a reason that is about the network.

        Recorded, not raised: _guarded turns a Failure from one repository into
        that repository's error line, which is exactly right for a broken remote
        and exactly wrong for this. giving_up is read between items instead.

        Keyed on the remote URL, because a repository and its linked worktrees
        are separate entries in context.repos and all fetch the same remote —
        one dead URL with two `git worktree add` checkouts reached the threshold
        on its own, which is the false positive this whole guard is supposed to
        avoid. And it is what gets counted in the message: three checkouts of
        one unreachable remote is one unreachable remote.
        """
        with self._offline_lock:
            self.unreachable.add(url)
            self.last_unreachable = message

    def note_reachable(self) -> None:
        """A fetch that worked, which means the network is not the problem.

        Without this, "three in a row" was "three ever": three dead remotes
        scattered anywhere in a 256-repository workspace abandoned every run
        from then on, and `run` never reached clean or trash.
        """
        with self._offline_lock:
            self.unreachable.clear()
            self.reached_since = True

    @property
    def giving_up(self) -> bool:
        """Whether enough fetches have failed on the network to stop trying.

        256 repositories times a 120-second timeout is eight and a half hours of
        waiting to be told the VPN is off. Three is enough: a real remote-side
        outage looks the same from here, and stopping is right for that too.
        """
        with self._offline_lock:
            return len(self.unreachable) >= OFFLINE_AFTER

    def offline_failure(self, done: bool) -> Failure:
        with self._offline_lock:
            count, message = len(self.unreachable), self.last_unreachable
        # "Nothing needing the network was changed" was printed two lines above
        # a summary reading "3 repositories fast-forwarded". Whatever landed
        # before the network went is still there, and saying otherwise is the
        # one line somebody reads before deciding whether to go and look.
        already = (
            "What had already been fetched or fast-forwarded is done and is listed above; "
            "nothing was left half-applied.\n  "
            if done
            else "Nothing was changed.\n  "
        )
        return Failure(
            f"could not reach {plural(count, 'remote')} in a row, so the rest were left "
            f"alone rather than waiting on the same timeout.\n"
            f"  Last error: {message}\n"
            f"  {already}"
            "Check the VPN, the proxy (http_proxy, https_proxy and git's own "
            "http.proxy), DNS, and your SSH agent.\n"
            "  `git-tidy clean` and `git-tidy trash` do not need the network."
        )

    def name_of(self, repo: Path) -> str:
        return repo.relative_to(self.workspace).as_posix()

    def config_for(self, repo: Path) -> dict[str, Any]:
        return self.resolver.for_path(repo)

    @property
    def timeout(self) -> int:
        """Applies to every git call, not only the ones sync makes."""
        return int(self.config_for(self.workspace)["sync"]["timeout"])

    @property
    def jobs(self) -> int:
        # Prompts must not interleave, so asking is single-threaded.
        if self.decider.mode == ASK:
            return 1
        return worker_count(self.config_for(self.workspace)["jobs"])


def _families(repos: Sequence[Path], timeout: int = 300) -> list[list[Path]]:
    """Group checkouts that share one .git, so they never run at the same time.

    A linked worktree shares its parent's object store, index lock and refs.
    Two of them fetching or deleting branches concurrently produces
    "error: could not write index" and branches that vanish between being listed
    and being deleted. Families run in parallel with each other; within a family
    the work is serial.
    """
    families: dict[str, list[Path]] = {}
    for repo in repos:
        try:
            shared = Git(repo, timeout=timeout).out(
                "rev-parse", "--path-format=absolute", "--git-common-dir", check=False
            )
        except Failure:
            # A hung or broken repository gets a family of its own rather than
            # stopping the grouping for everything else. Whatever is wrong with
            # it will be reported when its own turn comes.
            shared = ""
        families.setdefault(shared or str(repo), []).append(repo)
    return list(families.values())


def _remote_url(repo: Path, remote: str) -> str:
    """What this checkout would fetch from, for counting unreachable *remotes*.

    A short timeout of its own: this runs on the path where the network has just
    failed, and inheriting sync.timeout would add another two minutes per
    repository to the wait the give-up exists to cut short. Reading a config
    value needs no network anyway.
    """
    url = Git(repo, timeout=10).out("config", "--get", f"remote.{remote}.url", check=False)
    return url or str(repo)


def _in_parallel(context: Context, work: Callable[[Path], list[Action]], report: Report) -> None:
    """Run `work(repo)` over every repo, printing results as they land."""
    jobs = context.jobs
    if jobs <= 1:
        for repo in context.repos:
            if context.giving_up:
                break
            results = _guarded(work, repo, context)
            report.extend(results)
            context.printer.batch(results)
        if context.giving_up:
            raise context.offline_failure(context.reached_since)
        return

    def whole_family(family: list[Path]) -> list[Action]:
        found: list[Action] = []
        for repo in family:
            if context.giving_up:
                break
            found.extend(_guarded(work, repo, context))
        return found

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        families = _families(context.repos, context.timeout)
        futures = [pool.submit(whole_family, family) for family in families]
        try:
            for future in concurrent.futures.as_completed(futures):
                results = future.result()
                report.extend(results)
                context.printer.batch(results)
        except Quit:  # pragma: no cover - only reachable from --ask, which is serial
            for future in futures:
                future.cancel()
            raise
    if context.giving_up:
        raise context.offline_failure(context.reached_since)


def _guarded(work: Callable[[Path], list[Action]], repo: Path, context: Context) -> list[Action]:
    """One repo's failure must not take the other two hundred down with it."""
    gone = orphaned_worktree(repo)
    if gone is not None:
        if not context.report_orphans:
            # Every step calls this, and four identical lines for one directory
            # made five orphans read as twenty. doctor is where it belongs.
            return []
        return [
            Action(
                "sync",
                context.name_of(repo),
                "-",
                f"orphaned worktree: {gone} no longer exists, so git cannot work here. "
                "The files are still on disk; the directory can go once you have them.",
                skipped=True,
            )
        ]
    try:
        return work(repo)
    except Quit as quit_now:
        # Keep what this repository had already done; losing it would make the
        # report claim less happened than did.
        quit_now.done.append(
            Action("error", context.name_of(repo), "-", "", error="interrupted before finishing")
        )
        raise
    except Failure as exc:
        # A timed-out fetch arrives here rather than as a fetch action with an
        # error on it, because Git.run raises. That is the shape a dropped VPN
        # actually has — it hangs rather than refusing — so without this the
        # give-up never fired on the case it was written for, and 256
        # repositories each waited the full sync.timeout.
        if looks_unreachable(str(exc)):
            # The configured remote, not a guess: under a renamed one this fell
            # back to the path, which de-duplicates by checkout rather than by
            # URL and revives the linked-worktree false positive the counting is
            # meant to avoid. And _remote_url can time out in here, inside an
            # except clause, which would end the whole run.
            with contextlib.suppress(Failure, OSError):
                remote = context.config_for(repo)["sync"]["remote"]
                context.note_unreachable(_remote_url(repo, remote), str(exc))
        return [Action("error", context.name_of(repo), "-", "", error=str(exc))]
    except (OSError, ValueError) as exc:
        # Anything a single repository can do to itself — an unreadable config,
        # a path that will not decode — stays that repository's problem.
        return [Action("error", context.name_of(repo), "-", "", error=str(exc))]


def cmd_sync(context: Context, report: Report) -> None:
    context.printer.heading("Sync")

    def work(repo: Path) -> list[Action]:
        cfg = context.config_for(repo)
        if not cfg["sync"]["enabled"]:
            return []
        name = context.name_of(repo)
        actions = sync_repo(repo, name, cfg, context.decider)
        # Checked here rather than inside sync_repo, which knows nothing about
        # the run as a whole. context.giving_up is read between items, which is
        # what stops the rest.
        for action in actions:
            if action.kind == "fetch" and action.applied:
                context.note_reachable()
            elif action.error and looks_unreachable(action.error):
                # Any errored action, not only a fetch one: Git.run *raises* on a
                # timeout, and reporting() turns that into an "error" action. A
                # dropped VPN hangs rather than refusing, so that is the shape
                # the give-up was written for and the one it could not see.
                context.note_unreachable(_remote_url(repo, cfg["sync"]["remote"]), action.error)
        return actions

    _in_parallel(context, work, report)
    by_name = {context.name_of(repo): repo for repo in context.repos}
    context.fetched = {
        by_name[a.scope]
        for a in report.actions
        if a.kind == "fetch" and a.applied and a.scope in by_name
    }


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
        return prune_branches(
            repo, context.name_of(repo), cfg, context.decider, fetched=repo in context.fetched
        )

    _in_parallel(context, work, report)


def cmd_clean(context: Context, report: Report) -> None:
    context.printer.heading("Artefacts")
    root_cfg = context.config_for(context.workspace)

    def work(repo: Path) -> list[Action]:
        cfg = context.config_for(repo)
        if not cfg["clean"]["enabled"]:
            return []
        actions: list[Action] = []
        # `actions` accumulates across both mechanisms, and clean_tree guards
        # only its own list, so the guard belongs around the pair of them.
        with keeping(actions):
            _clean_repo(repo, context.name_of(repo), cfg, context, actions)
        return actions

    _in_parallel(context, work, report)

    # Everything outside a repository: loose caches, stray dist/ directories and
    # the __pycache__ of scripts that were never committed anywhere.
    if root_cfg["clean"]["enabled"] or _anywhere_enabled(
        context.workspace, context.resolver, "clean"
    ):
        context.printer.heading("Artefacts outside repositories")
        loose = _outside_repos(context, root_cfg, context.quarantine)
        report.extend(loose)
        context.printer.batch(loose)


def local_state_of(clean: dict[str, Any]) -> list[str]:
    """The paths a removal must lift out rather than take with it.

    clean.keep as well as clean.ignored_keep. switched_off_rules leaves both to
    _remove on purpose — refusing the whole directory for them is what made
    turning clean.ignored on reclaim nothing — but only ignored_keep was ever
    handed over, so a file clean.keep named by hand, sitting inside an ignored
    directory, was hard-deleted and did not even reach the quarantine.
    """
    return [*clean["ignored_keep"], *clean["keep"]]


def outright_guard(
    sensitive: Sequence[str], local_state: Sequence[str], regenerable: bool
) -> tuple[list[str], list[str]]:
    """The patterns that turn a deletion into a move, for one particular path.

    Split because the two halves answer different questions. trash.sensitive is
    "could this be the only copy of a credential?" and holds everywhere.
    clean.ignored_keep is "is this local state a tool would not rebuild?" — and
    inside something the user listed in clean.regenerable the answer is no by
    definition. Every .terraform holds a terraform.tfstate (the backend pointer,
    which `terraform init` writes again in seconds) and every .venv holds a
    certifi/cacert.pem, so applying both lists everywhere quietly turned nearly
    every cache into a rename into .git-tidy-trash/ in the same workspace —
    which reclaims nothing, while the README opens by promising 40 GB back.
    """
    return list(sensitive), [] if regenerable else list(local_state)


def _clean_repo(
    repo: Path,
    name: str,
    cfg: dict[str, Any],
    context: Context,
    actions: list[Action],
) -> None:
    """Both cleaning mechanisms for one repository, accumulating in place."""
    holding = context.quarantine if cfg["clean"]["quarantine"] else None
    git = Git(repo, timeout=int(cfg["sync"]["timeout"]))
    # Ignored paths first: it removes whole directories in one step, which
    # leaves the pattern walk below far less ground to cover.
    already: set[str] = set()
    if cfg["clean"]["ignored"]:
        ignored = clean_ignored(repo, name, cfg, context.decider, holding, git, context.quarantine)
        actions += ignored
        # Every path clean.ignored actually decided about — removed, quarantined,
        # declined, or refused for what is inside it. A path it merely did not
        # match ("build output, clean.builds is off") is not decided and is
        # still clean.dirs's business, which is why this reads a flag rather
        # than the wording of a reason.
        already = {a.target for a in ignored if a.settled}
    actions += clean_tree(
        repo,
        name,
        cfg,
        context.decider,
        git,
        holding,
        already=already,
        sensitive=cfg["trash"]["sensitive"],
        local_state=local_state_of(cfg["clean"]),
        holding=context.quarantine,
    )


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

    done: list[Action] = []

    def handle(item: tuple[Path, bool, dict[str, Any]]) -> Action:
        path, is_dir, here_cfg = item
        # Both the quarantine decision and the regenerable list come from the
        # config that governs *this* path, not the workspace root's.
        return _remove(
            path,
            workspace,
            "workspace",
            context.decider,
            quarantine if here_cfg["quarantine"] else None,
            is_dir,
            protect_nested=(guarded := not _matches(path.name, here_cfg["regenerable"])),
            sensitive=(
                guard := outright_guard(
                    here_cfg["sensitive_names"], local_state_of(here_cfg), not guarded
                )
            )[0],
            local_state=guard[1],
            holding=quarantine,
        )

    # Guarded like every other step that accumulates: answering 'q' must not
    # un-report the deletions that already happened.
    with keeping(done):
        return _map_parallel(handle, candidates, context.jobs, done)


def enclosing_repo(workspace: Path) -> Path | None:
    """The repository the workspace sits inside, if it sits inside one.

    Nothing below the workspace claims those files, so without this they look
    like loose artefacts belonging to nobody — and tracked content would be
    deleted by the pass that cleans the gaps between repositories.
    """
    for parent in workspace.parents:
        if is_repo(parent):
            return parent
    return None


def _loose_artefacts(
    context: Context, cfg: dict[str, Any]
) -> Iterator[tuple[Path, bool, dict[str, Any]]]:
    """Every artefact path outside a repository, with the config that governs it.

    Config is resolved per directory, not once for the workspace: deepest wins
    applies out here as much as it does inside a repository, and a
    .git-tidy.yaml in a loose directory has to be able to protect it.
    """
    repo_set = {p.resolve() for p in context.repos}
    tracked = (
        set()
        if cfg["clean"]["tracked"]
        else tracked_from_outside(context.workspace, context.timeout)
    )
    for dirpath, dirnames, filenames in os.walk(context.workspace, followlinks=False):
        here = Path(dirpath)
        if here.resolve() in repo_set or ".git" in here.parts:
            dirnames[:] = []
            continue
        governing = context.config_for(here)
        here_cfg = {
            **governing["clean"],
            "sensitive_names": governing["trash"]["sensitive"],
        }
        if not here_cfg["enabled"]:
            # Nothing from *this* directory, but the walk goes on: a deeper
            # config can switch clean back on, and pruning here made that
            # impossible for anything below a root that had it off.
            continue
        dir_patterns, file_patterns = clean_patterns(here_cfg)
        keep = here_cfg["keep"]
        descend: list[str] = []
        for name in sorted(dirnames):
            candidate = here / name
            if _not_ours(candidate, name, here_cfg, repo_set):
                continue
            if _matches(name, dir_patterns) and not _protected(
                candidate, context.workspace, keep, tracked
            ):
                # It is about to go whole, so its own config gets the last word:
                # a .git-tidy.yaml inside it would otherwise be deleted along
                # with everything it was written to protect.
                governing_own = context.config_for(candidate)
                own = {
                    **governing_own["clean"],
                    "sensitive_names": governing_own["trash"]["sensitive"],
                }
                own_dirs, _ = clean_patterns(own)
                if (
                    not own["enabled"]
                    or _matches(name, own["keep"])
                    or not _matches(name, own_dirs)
                ):
                    # Its own config decides whether it is an artefact at all,
                    # not only whether it is protected.
                    continue
                own_protect = [p for patterns, _ in protection_rules(own) for p in patterns]
                if _holds_protected(candidate, own_protect, base=context.workspace):
                    # Something inside is kept, so walk in rather than take it all.
                    descend.append(name)
                    continue
                yield candidate, True, own  # whole, so not descended into
            else:
                descend.append(name)
        dirnames[:] = descend
        for name in sorted(filenames):
            candidate = here / name
            if candidate.is_symlink() or not _matches(name, file_patterns):
                continue
            if _protected(candidate, context.workspace, keep, tracked):
                continue
            yield candidate, False, here_cfg


def _not_ours(candidate: Path, name: str, clean: dict[str, Any], repo_set: set[Path]) -> bool:
    """Directories the loose walk has no business touching.

    The quarantine, symlinks, repositories, and — unless clean.dependencies says
    otherwise — dependency trees, which belong to whatever installed them and
    need a network to put back. The in-repository walk applies the same rule.
    """
    if name == QUARANTINE_DIRNAME or candidate.is_symlink():
        return True
    if holds_git_data(candidate) or candidate.resolve() in repo_set:
        return True
    if _matches(name, clean_patterns(clean)[0]):
        return False  # named in clean.dirs, which says "wherever they appear"
    return not clean["dependencies"] and _matches(name, clean["dependency_dirs"])


def _map_parallel(
    work: Callable[[Any], Action],
    items: Sequence[Any],
    jobs: int,
    done: list[Action] | None = None,
) -> list[Action]:
    """Apply `work` to every item, in order, across `jobs` threads.

    Results land in `done` as they arrive, so a Quit raised part way through
    still has the finished ones to hand back.
    """
    collected = done if done is not None else []
    if jobs <= 1 or len(items) == 1:
        for item in items:
            collected.append(work(item))
        return list(collected)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for action in pool.map(work, items):
            collected.append(action)
    return list(collected)


def cmd_trash(context: Context, report: Report) -> None:
    cfg = context.config_for(context.workspace)
    context.printer.heading("Trash")
    if not cfg["trash"]["enabled"] and not _anywhere_enabled(
        context.workspace, context.resolver, "trash"
    ):
        context.printer.line("  trash is off; set trash.enabled: true to sweep loose files")
        return
    swept = sweep_trash(
        context.workspace,
        cfg,
        context.decider,
        context.quarantine,
        context.repos,
        context.resolver,
    )
    report.extend(swept)
    context.printer.batch(swept)


def cmd_doctor(context: Context, report: Report) -> None:
    context.printer.heading("Doctor")
    context.report_orphans = True

    def work(repo: Path) -> list[Action]:
        cfg = context.config_for(repo)
        if not cfg["doctor"]["enabled"]:
            return []
        return doctor_repo(repo, context.name_of(repo), cfg, context.fix)

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


def _number(question: str, default: str, prompt_input: Callable[[str], str] | None) -> int:
    answer = ask_value(question, default, prompt_input)
    try:
        return int(answer)
    except ValueError as exc:
        raise Failure(f"{answer!r} is not a number") from exc


def _interview(printer: Printer, prompt_input: Callable[[str], str] | None) -> dict[str, Any]:
    """The handful of questions whose answers actually differ between people."""
    chosen: dict[str, Any] = {}
    # Says what 0 will actually do on this machine. "[0]" on its own reads like
    # the wrong answer to "how many workers?" — and writing the number instead
    # would pin it, so the config stops following the machine it is copied to.
    here = worker_count(DEFAULTS["jobs"])
    jobs = _number(
        f"Workers? 0 = one per CPU core, so {here} here",
        str(DEFAULTS["jobs"]),
        prompt_input,
    )
    worker_count(jobs)  # refuse here rather than on every later run
    if jobs != DEFAULTS["jobs"]:
        chosen["jobs"] = jobs
    if ask_yes_no("Delete everything .gitignore covers?", False, prompt_input):
        chosen.setdefault("clean", {})["ignored"] = True
    if ask_yes_no("Also node_modules, .venv, vendor?", False, prompt_input):
        chosen.setdefault("clean", {})["dependencies"] = True
    if ask_yes_no("Also dist, build, target, out?", False, prompt_input):
        chosen.setdefault("clean", {})["builds"] = True
    if not ask_yes_no("Delete branches whose upstream is gone?", True, prompt_input):
        chosen.setdefault("branches", {})["prune_gone"] = False
    if ask_yes_no("Stash uncommitted changes so a repository can be updated?", False, prompt_input):
        # The report says which stash holds it, and nothing is discarded — but
        # it is still the one answer here that moves somebody's work, so it is
        # asked rather than left to be discovered in the config file.
        chosen.setdefault("sync", {})["stash"] = True
    if ask_yes_no("Rebase repositories that have diverged?", False, prompt_input):
        chosen.setdefault("sync", {})["diverged"] = "rebase"
    if ask_yes_no("Sweep loose junk files in the workspace?", False, prompt_input):
        trash = chosen.setdefault("trash", {})
        trash["enabled"] = True
        trash["min_age_days"] = _number("  Older than how many days?", "14", prompt_input)
    printer.line("")
    return chosen


def cmd_init(
    target: Path,
    mode: str,
    force: bool,
    printer: Printer,
    prompt_input: Callable[[str], str] | None = None,
    explicit_dry: bool = False,
) -> int:
    """Write a config file, asking about the choices that actually vary."""
    if target.exists() and not force and not explicit_dry:
        # Not under -n: nothing is written, so nothing can be overwritten — and
        # `git-tidy init -n > .git-tidy.yaml`, which the man page recommends,
        # creates the target before this runs. Refusing there left a zero-byte
        # config behind and printed advice about a flag that would not help.
        raise Failure(f"{target} already exists; pass --force to overwrite it")

    chosen: dict[str, Any] = {}
    # init does not inherit the global dry-run: every other command defaults to
    # changing nothing, but an init that neither asks nor writes is useless. It
    # asks whenever there is somebody to ask, and --ask says there is one even
    # when stdin is a pipe — answers piped in are how a setup script uses this.
    # -q writes the plain commented template instead.
    interactive = not explicit_dry and (
        prompt_input is not None or mode == ASK or (sys.stdin.isatty() and not printer.quiet)
    )
    if interactive:
        printer.line(f"Writing {target}. Enter accepts the default in brackets.\n")
        chosen = _interview(printer, prompt_input)

    body = render_config(chosen, INIT_HEADER)
    if explicit_dry:
        # -n means change nothing, here as everywhere else. Printing it is still
        # useful: `git-tidy init -n > .git-tidy.yaml` is a reasonable thing to do
        # — which is exactly why the note goes to stderr. On stdout it ended up
        # inside the redirected file, and both parsers then rejected it.
        printer.loud().line(body.rstrip())
        print(f"\n  Not written. Run without -n, or redirect this into {target}.", file=sys.stderr)
        return 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        raise Failure(f"cannot write {target}: {exc}") from exc
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
    # Recorded separately because -n and "no flag at all" both mean DRY, and
    # init has to tell them apart: the default writes, -n prints.

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
    parser.add_argument(
        "--force",
        action="store_true",
        help="also do what the safety rules held back (see the description)",
        **hide,
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON", **hide)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print every path, instead of one line per repository",
        **hide,
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary", **hide)
    parser.add_argument("--no-color", action="store_true", help="never colour the output", **hide)
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        metavar="N",
        help="how much to do at once; 0 = one per CPU core (the default)",
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

    for name in ("doctor", "run"):
        # On run as well, because run *is* doctor among other things, and having
        # to type the step out separately to fix what it just reported is the
        # kind of thing nobody does twice.
        sub.choices[name].add_argument(
            "--fix",
            action="store_true",
            help="also put right what doctor reports and can fix without risking a commit",
        )

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
    # --force comes from the common options: for init it means the same thing it
    # means everywhere else, "do it even though something is in the way".
    where.add_argument("--path", metavar="DIR", help="write .git-tidy.yaml in this directory")

    restore_cmd = command("restore", "put quarantined files back")
    restore_cmd.add_argument("stamp", nargs="?", help="which quarantine (default: the newest)")
    restore_cmd.add_argument("--list", action="store_true", help="list the quarantines and stop")
    restore_cmd.add_argument(
        "--expire", action="store_true", help="delete quarantines past trash.retention_days"
    )

    return parser


# Short options that swallow the rest of the word as their value, so `-C/tmp`
# and `-j4` are one argv word each and nothing after the letter is a flag.
_TAKES_A_VALUE = "Cj"


def _asked_for_dry_run(given: Sequence[str]) -> bool:
    """Whether argv asks for a dry run, clustered short flags included.

    `-qn` is the same request as `-q -n`, so whole-word comparison missed it —
    but scanning every word for an "n" then read `-C/tmp/notes` as a dry run and
    printed advice about a flag nobody passed. Only letters that are really
    flags count, and a short option's attached value is not one of them.
    """
    for word in given:
        if word == "--dry-run":
            return True
        if word == "--":
            break
        if not word.startswith("-") or word.startswith("--") or word == "-":
            continue
        for position, letter in enumerate(word[1:], start=1):
            if letter == "n":
                return True
            if letter in _TAKES_A_VALUE and position < len(word) - 1:
                break  # the rest of the word is that option's value
    return False


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse, then fold the after-the-subcommand copies of --include/--exclude in."""
    args = build_parser().parse_args(argv)
    # -n and "no mode flag at all" both resolve to DRY, and init has to tell
    # them apart: with no flag it writes the file, with -n it prints it.
    given = sys.argv[1:] if argv is None else list(argv)
    args.explicit_dry = _asked_for_dry_run(given)
    for name in ("include", "exclude"):
        extra = getattr(args, f"{name}_after", None)
        if extra:
            setattr(args, name, [*getattr(args, name, []), *extra])
    return args


# What --force lowers, and what it deliberately does not.
#
# It lowers the guards that hold back git-tidy's own actions: keeping a branch
# whose commits are not in the trunk, keeping an artefact directory because
# something cloned a repository into it, staying on a branch because the worktree
# is dirty.
#
# It does not touch clean.tracked, clean.ignored_keep or trash.sensitive. Those
# protect committed content, local-only state like .env and *.tfstate, and files
# that may hold the only copy of a credential — none of which this tool can put
# back, and none of which anyone means when they say "force".
# What --force is allowed to override. Deliberately not clean.regenerable: that
# list is what turns off the guard against removing a directory with a git
# repository inside it, and widening it to everything meant `--force` deleted a
# vendored checkout and its unpushed commits — while the summary was offering
# "--force does the ones it safely can". The repositories a workspace holds are
# the one thing no flag here may take. The default list already covers the real
# case, .terraform and its module clones.
FORCE_OVERRIDES: dict[str, Any] = {
    "branches": {"require_merged": False},
    "sync": {"switch": "always", "stash": True},
}


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "force", False):
        overrides = {k: dict(v) for k, v in FORCE_OVERRIDES.items()}
    if getattr(args, "jobs", None) is not None:
        if args.jobs < 0:
            raise Failure("--jobs cannot be negative")
        overrides["jobs"] = args.jobs
    return overrides


def _workspace_of(target: Path) -> Path:
    """The outermost directory above `target` that carries a config.

    Outermost, not nearest: config_files_for merges nothing above the root it is
    given, so rooting at the nearest one silently drops every setting that lives
    further up — which is the layout the README describes.
    """
    outermost = None
    for parent in target.parents:
        if any((parent / name).is_file() for name in CONFIG_NAMES):
            outermost = parent
    if outermost is not None:
        return outermost
    return target if target.is_dir() else target.parent


def _expand(raw: str) -> Path:
    """`~user` for an account that is not on this machine raises RuntimeError."""
    try:
        return Path(raw).expanduser().resolve()
    except RuntimeError as exc:
        raise Failure(f"cannot work out what {raw!r} means: {exc}") from exc


def resolve_workspace(raw: str) -> Path:
    workspace = _expand(raw)
    if not workspace.is_dir():
        raise Failure(f"{workspace} is not a directory")
    # find_repos only looks *below* the root, so a root that is itself a
    # repository would have its own tracked files cleaned as if they belonged to
    # nobody. Point at the directory that holds the checkouts instead.
    if is_repo(workspace):
        raise Failure(
            f"{workspace} is itself a git repository. Point --workspace at the directory "
            "that holds your checkouts, not at one of them"
        )
    # Cleaning $HOME or / would walk the entire machine, and almost certainly is
    # not what was meant.
    # Both sides resolved: a symlinked or automounted home is ordinary, and
    # comparing a resolved path against an unresolved one let it through.
    if workspace == Path(workspace.anchor) or workspace == Path.home().resolve():
        raise Failure(
            f"refusing to work on {workspace}: point --workspace at the directory that holds "
            "your checkouts, not at your home or filesystem root"
        )
    return workspace


def _expire_old_quarantines(
    context: Context, report: Report, cfg: dict[str, Any], command: str
) -> None:
    """Drop quarantines past trash.retention_days, at the end of a run.

    Expiring only ever happened when somebody typed `restore --expire`, so a
    daily `git-tidy run --apply` grew the quarantine without bound — which is
    the opposite of a tool whose first promise is disk space back. Only the
    steps that can *write* one clear old ones, so `doctor` and `config` still
    change nothing at all.

    Everything the manual path checks still applies: only directories this tool
    wrote, only past their retention, reported and refusable like any other
    removal.
    """
    if command not in ("clean", "trash", "run"):
        return
    days = cfg["trash"]["retention_days"]
    root = context.workspace / QUARANTINE_DIRNAME
    if days <= 0 or not root.is_dir():
        return
    if not any(action.applied for action in report.actions) and not context.decider.dry:
        # The README and the man page both say "a clean, trash or run that
        # applies anything". Keying off the command name alone meant a
        # `clean --apply` in a workspace with clean switched off still deleted
        # quarantines — the one thing that command had been told not to do.
        return
    expired = expire_quarantines(root, days, context.decider)
    if not expired:
        return
    # Heading first: the --ask prompt for these was printed under whatever
    # heading the previous step had left behind. And a declined one belongs in
    # the report like every other declined action — dropping it left an empty
    # summary and no sign the question had been asked at all.
    context.printer.heading("Quarantines past their retention")
    report.extend(expired)
    context.printer.batch(expired)


def _excludes_itself(repo: Path, root: Path, resolver: ConfigResolver) -> bool:
    """Whether a repository's own config asks to be left out.

    A config this cannot read is not an answer either way, so the repository
    stays in the list and its worker reports the problem against it — rather
    than one unreadable file ending the run before any work has started.
    """
    try:
        return _excluded(repo, root, resolver.for_path(repo)["exclude"])
    except Failure:
        return False


def select_repos(
    root: Path,
    cfg: dict[str, Any],
    args: argparse.Namespace,
    resolver: ConfigResolver | None = None,
) -> list[Path]:
    exclude = [*cfg["exclude"], *args.exclude]
    repos = find_repos(root, exclude)
    if resolver is not None:
        # A repository can exclude itself. Discovery cannot know that — it has
        # not read the repository's own config yet — so the check happens here,
        # where deepest-wins has actually been applied.
        repos = [repo for repo in repos if not _excludes_itself(repo, root, resolver)]
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
        verbose=args.verbose,
    )
    # init is exempt: its questions are read with input(), which a pipe answers
    # perfectly well, and an unanswered one falls back to its default. The check
    # is for the steps that would prompt per change and find nothing there.
    if args.mode == ASK and args.command != "init" and not sys.stdin.isatty():
        raise Failure("--ask needs a terminal; use --apply for an unattended run")

    if args.command == "init":
        target = (
            global_config_path()
            if args.global_config
            else _expand(args.path or args.workspace) / CONFIG_NAMES[0]
        )
        return cmd_init(target, args.mode, args.force, printer, explicit_dry=args.explicit_dry)

    # config neither reads a workspace nor writes one: it answers a question
    # about a path, and the most natural place to ask it is inside a checkout.
    workspace = (
        _expand(args.workspace) if args.command == "config" else resolve_workspace(args.workspace)
    )
    resolver = ConfigResolver(workspace, overrides_from(args))
    root_cfg = resolver.for_path(workspace)
    quarantine_root = workspace / QUARANTINE_DIRNAME
    decider = Decider(args.mode)

    if args.command == "config":
        target = _expand(args.path) if args.path else workspace
        # A path outside -C would silently get none of its own .git-tidy.yaml
        # files, which is the opposite of what this command is for.
        # Also when no path was given: -C defaults to cwd, and inside a checkout
        # that root has none of the configs above it.
        if args.path is None or (target != workspace and workspace not in target.parents):
            resolver = ConfigResolver(_workspace_of(target), overrides_from(args))
        print(json.dumps(resolver.for_path(target), indent=2, sort_keys=True))
        return 0

    if args.command == "restore":
        return _restore_command(args, root_cfg, quarantine_root, decider, printer)

    repos = select_repos(workspace, root_cfg, args, resolver)
    suffix = {DRY: "  (dry run — nothing will be changed)", ASK: "  (asking before each change)"}
    printer.line(f"{workspace}: {plural(len(repos), 'repository')}{suffix.get(args.mode, '')}")
    quarantine = Quarantine(quarantine_root, workspace)
    context = Context(workspace, resolver, decider, printer, repos, quarantine)
    # doctor reports by default and always has. --fix hands it the same decider
    # every other step uses, so -n still prints what it would do, --ask still
    # asks, and --apply is the only way anything happens.
    context.fix = decider if getattr(args, "fix", False) else None
    report = Report()

    steps: dict[str, list[Callable[[Context, Report], None]]] = {
        "sync": [cmd_sync],
        "prune": [cmd_prune],
        "clean": [cmd_clean],
        "trash": [cmd_trash],
        "doctor": [cmd_doctor],
        "run": [cmd_sync, cmd_prune, cmd_clean, cmd_trash, cmd_doctor],
    }
    interrupted = False
    try:
        for step in steps[args.command]:
            step(context, report)
        _expire_old_quarantines(context, report, root_cfg, args.command)
    except Quit as quit_now:
        interrupted = True
        report.extend(quit_now.done)
        for action in quit_now.done:
            printer.action(action)
        printer.line("\n  stopped at your request; everything already done is kept")
    except Failure as exc:
        # The steps that already ran have changed the disk. Letting this out of
        # main would lose the report and the quarantine manifest with it, so
        # there would be no record of what happened and no stamp to restore.
        interrupted = True
        report.add(Action("error", "-", "-", "", error=str(exc)))
        printer.line(f"\n  stopped: {exc}")

    manifest = quarantine.write_manifest()
    if manifest and not args.json:
        # loud(): -q means "only the summary", and where the files went is part
        # of it — the run moved them and this is the only way back.
        printer.loud().line(f"\n  Quarantined files are under {quarantine.dir}")
        printer.loud().line(
            f"  Undo with: git-tidy -C {workspace} restore {quarantine.stamp} --apply"
        )

    if args.json:
        print(
            json.dumps(
                {
                    "version": __version__,
                    "workspace": str(workspace),
                    "mode": args.mode,
                    "interrupted": interrupted,
                    "repositories": len(repos),
                    "bytes": report.bytes_found if args.mode == DRY else report.bytes_freed,
                    "actions": [a.as_dict() for a in report.actions],
                },
                indent=2,
            )
        )
    else:
        summarise(report, args.mode, printer, forced=getattr(args, "force", False))
    return 1 if report.errors or interrupted else 0


def _list_quarantines(root: Path) -> list[dict[str, Any]]:
    """What is in the quarantine, for both the text listing and --json."""
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        complete = (entry / MANIFEST_NAME).is_file()
        if not complete and not (entry / JOURNAL_NAME).is_file():
            continue
        with contextlib.suppress(Failure, OSError, json.JSONDecodeError):
            found.append(
                {
                    "stamp": entry.name,
                    "entries": len(_read_manifest(entry)["entries"]),
                    "complete": complete,
                }
            )
    return found


def _restore_command(
    args: argparse.Namespace,
    cfg: dict[str, Any],
    quarantine_root: Path,
    decider: Decider,
    printer: Printer,
) -> int:
    if args.list:
        listed = _list_quarantines(quarantine_root)
        if args.json:
            print(json.dumps({"version": __version__, "quarantines": listed}, indent=2))
            return 0
        if not listed:
            printer.loud().line("no quarantines")
            return 0
        for one in listed:
            unfinished = "" if one["complete"] else "  (unfinished)"
            printer.loud().line(f"  {one['stamp']}  {plural(one['entries'], 'entry')}{unfinished}")
        return 0

    interrupted = False
    try:
        actions = (
            expire_quarantines(quarantine_root, cfg["trash"]["retention_days"], decider, args.stamp)
            if args.expire
            else restore(quarantine_root, args.stamp, decider)
        )
    except Quit as quit_now:
        interrupted = True
        actions = quit_now.done
    report = Report(list(actions))
    for action in actions:
        printer.action(action)
    if args.json:
        print(
            json.dumps(
                {
                    "version": __version__,
                    "mode": args.mode,
                    "interrupted": interrupted,
                    "actions": [a.as_dict() for a in report.actions],
                },
                indent=2,
            )
        )
    else:
        summarise(report, args.mode, printer)
    # A run that stopped part way through did not do what was asked of it.
    return 1 if report.errors or interrupted else 0


def entrypoint(argv: Sequence[str] | None = None) -> NoReturn:
    """The console script: main(), plus the mapping from Failure to exit 2.

    `argv` exists so that mapping can be tested. The man page states the
    contract — 0 on success, 1 if anything failed, 2 on a usage or
    configuration error — and scripts branch on it.
    """
    try:
        sys.exit(main(argv))
    except Failure as exc:
        print(f"git-tidy: {exc}", file=sys.stderr)
        sys.exit(2)
    except BrokenPipeError:
        # `| head` closes stdout while the run is still printing. Nothing is
        # wrong with the run itself, so it must not end in a traceback — and the
        # manifest is written before this is reached.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\ngit-tidy: interrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    entrypoint()
