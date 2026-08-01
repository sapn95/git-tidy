"""Tests for git-tidy.

Almost everything here runs against real git repositories in a temp directory —
a bare repo standing in for the remote, and clones of it for the work trees.
Mocking git would only prove that the mock behaves the way the test author
imagined, and the interesting cases (a branch whose upstream is gone, a repo that
has diverged, a path that .gitignore covers) are precisely the ones where that
imagination tends to be wrong.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import git_tidy as gt

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    # Keep the developer's own git config out of the tests.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    """Never read the machine's real ~/.config/git-tidy/config.yaml."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    return result.stdout.strip()


# Permission bits mean nothing to root, and CI runs the tests in a container as
# root. Skipping is honest: the behaviour these cover is real and is verified on
# any ordinary machine, and pretending otherwise would need a fake filesystem
# that proves nothing about the real one.
needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores permission bits, so nothing is unreadable"
)


def commit(path: Path, name: str, text: str = "x") -> None:
    (path / name).write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-m", f"add {name}")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare repository with one commit on main, to clone from."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    commit(seed, "README.md", "hello\n")
    git(seed, "init", "--bare", "-q", str(origin))
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return origin


@pytest.fixture
def workspace(tmp_path: Path, remote: Path) -> Path:
    """A workspace holding one clone of the remote."""
    space = tmp_path / "space"
    space.mkdir()
    git(space, "clone", "-q", str(remote), "repo")
    return space


def config(**overrides):
    """The defaults, deep-merged with a partial override."""
    return gt._merge(gt.DEFAULTS, overrides, "test")


def run(mode=gt.AUTO):
    return gt.Decider(mode)


# --------------------------------------------------------------------------- #
# YAML: the fallback parser
# --------------------------------------------------------------------------- #

SAMPLES = [
    "",
    "jobs: 4\n",
    "jobs: 0\nexclude: []\n",
    "sync:\n  enabled: true\n  remote: origin\n",
    "branches:\n  keep:\n    - main\n    - 'release/*'\n",
    "clean:\n  dirs: [.terraform, node_modules]\n  builds: false\n",
    "# just a comment\njobs: 2  # trailing comment\n",
    "trash:\n  patterns:\n    - '*.log'\n  min_age_days: 0\n",
    "a: null\nb: ~\nc: yes\nd: no\ne: 1.5\nf: -3\n",
    "g: \"quoted: colon\"\nh: 'single'\n",
    "nested:\n  - name: one\n    value: 1\n  - name: two\n    value: 2\n",
    "flow: {a: 1, b: two}\n",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_yaml_subset_matches_pyyaml(text):
    """The built-in parser must agree with PyYAML wherever PyYAML is available."""
    yaml = pytest.importorskip("yaml")
    assert gt._parse_yaml_subset(text, "<test>") == yaml.safe_load(text)


def test_yaml_rejects_tabs():
    with pytest.raises(gt.Failure, match="tabs"):
        gt._parse_yaml_subset("a:\n\tb: 1\n", "<test>")


def test_yaml_rejects_a_key_without_a_colon():
    with pytest.raises(gt.Failure, match="key: value"):
        gt._parse_yaml_subset("just a line\n", "<test>")


def test_yaml_rejects_an_unbalanced_bracket():
    with pytest.raises(gt.Failure, match=r"unterminated|unbalanced"):
        gt._parse_yaml_subset("a: [1, 2\n", "<test>")


def test_yaml_keeps_a_url_fragment():
    assert gt._parse_yaml_subset("a: http://x#y\n", "<test>") == {"a": "http://x#y"}


def test_yaml_strips_a_real_comment():
    assert gt._parse_yaml_subset("a: 1 # two\n", "<test>") == {"a": 1}


def test_yaml_document_markers_are_ignored():
    assert gt._parse_yaml_subset("---\na: 1\n...\n", "<test>") == {"a": 1}


def test_yaml_apostrophe_does_not_swallow_a_comment():
    """An apostrophe mid-word is not an opening quote, so the comment still goes."""
    assert gt._parse_yaml_subset("a: it's fine  # note\n", "<test>") == {"a": "it's fine"}


@pytest.mark.parametrize("key", ["ignored_keep", "keep", "dirs", "regenerable"])
def test_a_list_setting_with_nothing_after_the_colon_is_refused(key: str):
    """It used to mean "empty", and emptying ignored_keep makes .env deletable.

    `git-tidy init` writes commented-out list headers, so uncommenting one line
    and not the entries under it is a single keystroke — and it read as an
    instruction to protect nothing.
    """
    with pytest.raises(gt.Failure, match="nothing after the colon"):
        gt._merge(gt.DEFAULTS, {"clean": {key: None}}, "test")


def test_an_explicitly_empty_list_is_still_allowed():
    merged = gt._merge(gt.DEFAULTS, {"clean": {"dirs": []}}, "test")
    assert merged["clean"]["dirs"] == []
    assert gt.clean_patterns(merged["clean"]) == ([], list(gt.DEFAULTS["clean"]["files"]))


# --------------------------------------------------------------------------- #
# YAML: the dumper
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1},
        {"a": True, "b": False, "c": None},
        {"a": ["x", "y"], "b": []},
        {"a": {"b": {"c": "deep"}}},
        {"a": "yes"},  # must be quoted, or it reads back as a bool
        {"a": "1.5"},  # must be quoted, or it reads back as a float
        {"a": "release/*"},
        {"a": ""},
        {"a": "has: colon"},
    ],
)
def test_dump_round_trips(value):
    assert gt._parse_yaml_subset(gt.dump_yaml(value), "<dump>") == value


def test_dump_round_trips_through_pyyaml_too():
    yaml = pytest.importorskip("yaml")
    value = {"a": "yes", "b": ["release/*", "1.5"], "c": None}
    assert yaml.safe_load(gt.dump_yaml(value)) == value


def test_dump_empty_containers():
    assert gt.dump_yaml({"a": {}, "b": []}) == "a: {}\nb: []\n"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_merge_rejects_an_unknown_key():
    with pytest.raises(gt.Failure, match="unknown setting"):
        gt._merge(gt.DEFAULTS, {"jbos": 4}, "test")


def test_merge_rejects_a_scalar_where_a_block_belongs():
    with pytest.raises(gt.Failure, match="block of settings"):
        gt._merge(gt.DEFAULTS, {"sync": True}, "test")


def test_merge_rejects_a_scalar_where_a_list_belongs():
    with pytest.raises(gt.Failure, match="takes a list"):
        gt._merge(gt.DEFAULTS, {"exclude": "one"}, "test")


def test_merge_is_deep_and_leaves_siblings_alone():
    merged = gt._merge(gt.DEFAULTS, {"sync": {"remote": "upstream"}}, "test")
    assert merged["sync"]["remote"] == "upstream"
    assert merged["sync"]["prune"] is gt.DEFAULTS["sync"]["prune"]
    assert gt.DEFAULTS["sync"]["remote"] == "origin", "the defaults must not be mutated"


def test_deeper_config_wins(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / ".git-tidy.yaml").write_text("jobs: 2\n", encoding="utf-8")
    (tmp_path / "sub" / ".git-tidy.yaml").write_text("jobs: 5\n", encoding="utf-8")
    resolver = gt.ConfigResolver(tmp_path)
    assert resolver.for_path(tmp_path)["jobs"] == 2
    assert resolver.for_path(tmp_path / "sub")["jobs"] == 5


def test_config_above_the_workspace_is_ignored(tmp_path: Path):
    (tmp_path / ".git-tidy.yaml").write_text("jobs: 9\n", encoding="utf-8")
    inner = tmp_path / "inner"
    inner.mkdir()
    assert gt.ConfigResolver(inner).for_path(inner)["jobs"] == gt.DEFAULTS["jobs"]


def test_global_config_is_read(tmp_path: Path, monkeypatch):
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    path = gt.global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("jobs: 3\n", encoding="utf-8")
    assert gt.ConfigResolver(tmp_path).for_path(tmp_path)["jobs"] == 3


def test_a_config_that_is_not_a_mapping_is_rejected(tmp_path: Path):
    (tmp_path / ".git-tidy.yaml").write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(gt.Failure, match="block of settings"):
        gt.ConfigResolver(tmp_path).for_path(tmp_path)


def test_an_empty_config_file_is_fine(tmp_path: Path):
    (tmp_path / ".git-tidy.yaml").write_text("# nothing\n", encoding="utf-8")
    assert gt.ConfigResolver(tmp_path).for_path(tmp_path)["jobs"] == gt.DEFAULTS["jobs"]


@pytest.mark.parametrize("value,expected", [(0, os.cpu_count() or 4), (1, 1), (32, 32)])
def test_worker_count(value, expected):
    assert gt.worker_count(value) == expected


def test_worker_count_rejects_nonsense():
    with pytest.raises(gt.Failure, match="must be a number"):
        gt.worker_count("many")
    with pytest.raises(gt.Failure, match="negative"):
        gt.worker_count(-1)


# --------------------------------------------------------------------------- #
# init / render_config
# --------------------------------------------------------------------------- #


def test_render_config_is_all_comments_by_default():
    text = gt.render_config({}, "header")
    for line in text.splitlines():
        assert not line.strip() or line.lstrip().startswith("#"), line
    # An all-comments file parses to nothing, so it changes none of the defaults.
    assert gt._parse_yaml_subset(text, "<render>") is None


def test_render_config_writes_the_chosen_values():
    text = gt.render_config({"clean": {"ignored": True}, "jobs": 4}, "header")
    parsed = gt._parse_yaml_subset(text, "<render>")
    assert parsed == {"jobs": 4, "clean": {"ignored": True}}


def test_init_writes_a_loadable_file(tmp_path: Path, capsys):
    target = tmp_path / ".git-tidy.yaml"
    assert gt.cmd_init(target, gt.AUTO, force=False, printer=quiet_printer()) == 0
    assert target.is_file()
    gt.ConfigResolver(tmp_path).for_path(tmp_path)  # must not raise


def test_init_refuses_to_overwrite(tmp_path: Path):
    target = tmp_path / ".git-tidy.yaml"
    target.write_text("jobs: 1\n", encoding="utf-8")
    with pytest.raises(gt.Failure, match="already exists"):
        gt.cmd_init(target, gt.AUTO, force=False, printer=quiet_printer())
    assert gt.cmd_init(target, gt.AUTO, force=True, printer=quiet_printer()) == 0


def quiet_printer() -> gt.Printer:
    return gt.Printer(io.StringIO(), quiet=True, color=False)


# --------------------------------------------------------------------------- #
# Decider
# --------------------------------------------------------------------------- #


def test_dry_mode_allows_nothing():
    decider = gt.Decider(gt.DRY)
    action = gt.Action("remove", "repo", "x", "remove file")
    assert decider.allow(action) is False
    assert action.detail.startswith("would ")


def test_auto_mode_allows_everything():
    assert gt.Decider(gt.AUTO).allow(gt.Action("remove", "repo", "x")) is True


def test_ask_mode_remembers_all_for_a_kind():
    replies = iter(["a"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    assert decider.allow(gt.Action("remove", "r", "one")) is True
    assert decider.allow(gt.Action("remove", "r", "two")) is True  # no second prompt


def test_ask_mode_remembers_skip_for_a_kind():
    replies = iter(["s"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    assert decider.allow(gt.Action("remove", "r", "one")) is False
    second = gt.Action("remove", "r", "two")
    assert decider.allow(second) is False
    assert second.skipped


def test_ask_mode_yes_to_everything_crosses_kinds():
    replies = iter(["Y"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    assert decider.allow(gt.Action("remove", "r", "one")) is True
    assert decider.allow(gt.Action("branch", "r", "two")) is True


def test_ask_mode_quits():
    replies = iter(["q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    with pytest.raises(gt.Quit):
        decider.allow(gt.Action("remove", "r", "one"))


def test_ask_mode_re_asks_on_nonsense():
    replies = iter(["what", "n"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    assert decider.allow(gt.Action("remove", "r", "one")) is False


def test_unknown_mode_is_rejected():
    with pytest.raises(gt.Failure, match="unknown mode"):
        gt.Decider("sideways")


def test_ask_yes_no_defaults_on_empty_input():
    assert gt.ask_yes_no("?", True, lambda _: "") is True
    assert gt.ask_yes_no("?", False, lambda _: "") is False
    assert gt.ask_yes_no("?", False, lambda _: "yes") is True


def test_ask_value_falls_back_to_the_default():
    assert gt.ask_value("?", "8", lambda _: "") == "8"
    assert gt.ask_value("?", "8", lambda _: "3") == "3"


# --------------------------------------------------------------------------- #
# Repo discovery
# --------------------------------------------------------------------------- #


def test_find_repos(workspace: Path):
    (workspace / "not-a-repo").mkdir()
    assert [p.name for p in gt.find_repos(workspace, [])] == ["repo"]


def test_find_repos_honours_exclude(workspace: Path):
    assert gt.find_repos(workspace, ["repo"]) == []


def test_find_repos_does_not_descend_into_a_repo(workspace: Path):
    inner = workspace / "repo" / "inner"
    inner.mkdir()
    git(inner, "init", "-q")
    assert [p.name for p in gt.find_repos(workspace, [])] == ["repo"]


def test_find_repos_skips_the_quarantine(workspace: Path):
    holding = workspace / gt.QUARANTINE_DIRNAME / "stamp" / "old"
    holding.mkdir(parents=True)
    git(holding, "init", "-q")
    assert [p.name for p in gt.find_repos(workspace, [])] == ["repo"]


def test_is_repo_accepts_a_git_file(tmp_path: Path):
    (tmp_path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert gt.is_repo(tmp_path)


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #


def test_sync_fast_forwards(workspace: Path, remote: Path, tmp_path: Path):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(other, "new.txt")
    git(other, "push", "-q")

    repo = workspace / "repo"
    actions = gt.sync_repo(repo, "repo", config(), run())
    assert (repo / "new.txt").is_file()
    assert any(a.kind == "update" and a.applied for a in actions)


def test_sync_is_a_no_op_when_up_to_date(workspace: Path):
    actions = gt.sync_repo(workspace / "repo", "repo", config(), run())
    update = [a for a in actions if a.kind == "update"]
    assert update and update[0].skipped and "up to date" in update[0].detail


def test_sync_switches_back_to_the_default_branch(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    actions = gt.sync_repo(repo, "repo", config(), run())
    assert gt.current_branch(gt.Git(repo)) == "main"
    assert any(a.kind == "switch" and a.applied for a in actions)


def test_sync_leaves_a_dirty_repo_on_its_branch(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    actions = gt.sync_repo(repo, "repo", config(), run())
    assert gt.current_branch(gt.Git(repo)) == "side"
    assert any("uncommitted changes" in a.detail for a in actions)


def test_sync_switch_never_stays_put(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    gt.sync_repo(repo, "repo", config(sync={"switch": "never"}), run())
    assert gt.current_branch(gt.Git(repo)) == "side"


def test_sync_reports_a_diverged_branch(workspace: Path, remote: Path, tmp_path: Path):
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")
    repo = workspace / "repo"
    commit(repo, "mine.txt")

    actions = gt.sync_repo(repo, "repo", config(), run())
    diverged = [a for a in actions if "diverged" in a.detail]
    assert diverged and diverged[0].skipped


def test_sync_reports_a_branch_held_by_another_worktree(workspace: Path, tmp_path: Path):
    """A branch lives in one worktree at a time. That is a fact, not a failure."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "main")

    actions = gt.sync_repo(repo, "repo", config(), run())
    switch = [a for a in actions if a.kind == "switch"]
    assert switch and switch[0].skipped and "checked out in" in switch[0].detail
    assert not any(a.error for a in actions)
    assert gt.current_branch(gt.Git(repo)) == "side"


def test_sync_does_not_fast_forward_over_uncommitted_changes(
    workspace: Path, remote: Path, tmp_path: Path
):
    """Already on the default branch, but the worktree is dirty."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")

    repo = workspace / "repo"
    (repo / "README.md").write_text("mine\n", encoding="utf-8")
    actions = gt.sync_repo(repo, "repo", config(), run())

    update = [a for a in actions if a.kind == "update"]
    assert update and update[0].skipped and "uncommitted changes" in update[0].detail
    assert not any(a.error for a in actions)
    assert (repo / "README.md").read_text(encoding="utf-8") == "mine\n"


def test_checked_out_elsewhere_finds_the_other_worktree(workspace: Path, tmp_path: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "main")

    where = gt._checked_out_elsewhere(gt.Git(repo), "main")
    assert where and Path(where).name == "wt"
    assert gt._checked_out_elsewhere(gt.Git(repo), "side") is None, "this worktree does not count"


def test_switch_creates_a_branch_that_only_exists_on_the_remote(workspace: Path, remote: Path):
    """The --create path is still taken when there is no local branch."""
    repo = workspace / "repo"
    git(repo, "push", "-q", "origin", "main:release")
    git(repo, "fetch", "-q", "origin")
    git(repo, "switch", "-q", "-c", "side")

    cfg = config(sync={"default_branch": "release"})
    actions = gt.sync_repo(repo, "repo", cfg, run())
    assert gt.current_branch(gt.Git(repo)) == "release"
    assert any(a.kind == "switch" and a.applied for a in actions)


def test_sync_reports_a_repo_with_no_remote(tmp_path: Path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    git(lonely, "init", "-q", "-b", "main")
    commit(lonely, "a.txt")
    actions = gt.sync_repo(lonely, "lonely", config(), run())
    assert actions[0].detail == "nothing to fetch: no remote"
    # doctor reports the repository itself; sync counting it too doubled every
    # remote-less clone in the summary.
    assert gt._reason_of(actions[0].detail) is None


def test_sync_reports_a_missing_named_remote(workspace: Path):
    actions = gt.sync_repo(workspace / "repo", "repo", config(sync={"remote": "upstream"}), run())
    assert actions[0].detail == "no such remote"


def test_sync_dry_run_changes_nothing(workspace: Path, remote: Path, tmp_path: Path):
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "new.txt")
    git(tmp_path / "other", "push", "-q")
    repo = workspace / "repo"
    actions = gt.sync_repo(repo, "repo", config(), run(gt.DRY))
    assert not (repo / "new.txt").exists()
    assert all(not a.applied for a in actions)


def test_default_branch_falls_back_to_a_candidate(workspace: Path):
    repo = workspace / "repo"
    git(repo, "update-ref", "-d", "refs/remotes/origin/HEAD")
    # Point the remote at a path that cannot answer, so set-head has to fail.
    git(repo, "remote", "set-url", "origin", str(workspace / "gone.git"))
    assert gt.default_branch(gt.Git(repo), config()["sync"]) == "main"


def test_default_branch_recovers_from_a_stale_cached_head(workspace: Path):
    """A repo renamed master -> main upstream keeps a HEAD pointing at nothing.

    Trusting that cached name reports "remote branch missing" and syncs nothing,
    for as long as the clone lives.
    """
    repo = workspace / "repo"
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    assert gt.default_branch(gt.Git(repo), config()["sync"]) == "main"


def test_default_branch_reports_the_stale_name_when_nothing_resolves(workspace: Path):
    """With the remote unreachable too, say which branch is missing."""
    repo = workspace / "repo"
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    git(repo, "remote", "set-url", "origin", str(workspace / "gone.git"))
    assert gt.default_branch(gt.Git(repo), config()["sync"]) == "master"


def test_default_branch_can_be_forced(workspace: Path):
    cfg = config(sync={"default_branch": "release"})["sync"]
    assert gt.default_branch(gt.Git(workspace / "repo"), cfg) == "release"


def test_sync_rejects_an_unknown_switch_policy(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    # Reported against the repository rather than raised: the fetch has already
    # happened, and losing it would report nothing for a run that pruned refs.
    actions = gt.sync_repo(repo, "repo", config(sync={"switch": "sideways"}), run())
    assert any(a.error and "sync.switch" in a.error for a in actions)


def test_sync_rejects_an_unknown_submodule_mode(workspace: Path):
    with pytest.raises(gt.Failure, match=r"sync\.submodules"):
        gt._sync_submodules(gt.Git(workspace / "repo"), "repo", {"submodules": "maybe"}, run())


def test_submodules_are_left_alone_without_gitmodules(workspace: Path):
    assert (
        gt._sync_submodules(gt.Git(workspace / "repo"), "repo", {"submodules": "init"}, run()) == []
    )


# --------------------------------------------------------------------------- #
# branch pruning
# --------------------------------------------------------------------------- #


def make_gone_branch(repo: Path, remote: Path, name: str, extra_commit: bool = False) -> None:
    """Push a branch, optionally add an unpushed commit, then delete it upstream."""
    git(repo, "switch", "-q", "-c", name)
    git(repo, "push", "-q", "-u", "origin", name)
    if extra_commit:
        commit(repo, f"{name}.txt")
    git(repo, "switch", "-q", "main")
    git(remote, "branch", "-D", name)
    git(repo, "fetch", "-q", "--prune", "origin")


def test_prune_deletes_a_merged_gone_branch(workspace: Path, remote: Path):
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature")
    actions = gt.prune_branches(repo, "repo", config(), run())
    assert any(a.kind == "branch" and a.applied for a in actions)
    assert "feature" not in git(repo, "branch", "--format=%(refname:short)").split()


def test_prune_keeps_a_gone_branch_with_unpushed_commits(workspace: Path, remote: Path):
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature", extra_commit=True)
    actions = gt.prune_branches(repo, "repo", config(), run())
    kept = [a for a in actions if a.target == "feature"]
    assert kept and kept[0].skipped and "not in" in kept[0].detail
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").split()


def test_prune_deletes_unmerged_when_told_to(workspace: Path, remote: Path):
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature", extra_commit=True)
    cfg = config(branches={"require_merged": False})
    actions = gt.prune_branches(repo, "repo", cfg, run(), fetched=True)
    assert any(a.applied for a in actions)


def test_prune_honours_keep(workspace: Path, remote: Path):
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "release/1.0")
    actions = gt.prune_branches(repo, "repo", config(), run())
    assert not actions
    assert "release/1.0" in git(repo, "branch", "--format=%(refname:short)").split()


def test_prune_leaves_local_only_branches_alone(workspace: Path):
    repo = workspace / "repo"
    git(repo, "branch", "scratch")
    assert gt.prune_branches(repo, "repo", config(), run()) == []


def test_prune_can_delete_local_only_branches(workspace: Path):
    repo = workspace / "repo"
    git(repo, "branch", "scratch")
    cfg = config(branches={"prune_local_only": True})
    actions = gt.prune_branches(repo, "repo", cfg, run())
    assert any(a.target == "scratch" and a.applied for a in actions)


def test_prune_never_touches_the_checked_out_branch(workspace: Path, remote: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "push", "-q", "-u", "origin", "feature")
    git(remote, "branch", "-D", "feature")
    git(repo, "fetch", "-q", "--prune", "origin")
    assert gt.prune_branches(repo, "repo", config(), run()) == []


def test_a_branch_deleted_by_a_parallel_worker_is_not_an_error(workspace: Path, remote: Path):
    """Two clones can share one ref store, so the same branch is seen twice.

    Whichever worker gets there second finds it gone. The outcome is the one
    that was asked for, so it is reported as already deleted, not as a failure.
    """
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature")
    branch = gt.BranchInfo("feature", "origin/feature", "[gone]")

    # Stand in for the other worker: the branch vanishes after it was listed.
    original = gt.list_branches

    def list_then_vanish(g: gt.Git) -> list[gt.BranchInfo]:
        git(repo, "branch", "-D", "feature")
        return [branch]

    gt.list_branches = list_then_vanish
    try:
        actions = gt.prune_branches(repo, "repo", config(), run())
    finally:
        gt.list_branches = original

    assert len(actions) == 1
    assert actions[0].skipped and "already deleted" in actions[0].detail
    assert not actions[0].error


def test_a_branch_that_vanishes_just_before_the_delete_is_not_an_error(
    workspace: Path, remote: Path
):
    """The other ordering: it survives the merged check, then disappears."""
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature")
    original = gt._unmerged_reason

    def merged_then_vanish(g, branch, trunk, remote_name):
        verdict = original(g, branch, trunk, remote_name)
        git(repo, "branch", "-D", branch.name)
        return verdict

    gt._unmerged_reason = merged_then_vanish
    try:
        actions = gt.prune_branches(repo, "repo", config(), run())
    finally:
        gt._unmerged_reason = original

    assert len(actions) == 1
    assert actions[0].skipped and "already deleted" in actions[0].detail
    assert not actions[0].error


def test_prune_dry_run_deletes_nothing(workspace: Path, remote: Path):
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature")
    actions = gt.prune_branches(repo, "repo", config(), run(gt.DRY))
    assert actions and not any(a.applied for a in actions)
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").split()


# --------------------------------------------------------------------------- #
# clean
# --------------------------------------------------------------------------- #


def test_clean_removes_artefact_directories(workspace: Path):
    repo = workspace / "repo"
    (repo / ".terraform" / "providers").mkdir(parents=True)
    (repo / ".terraform" / "providers" / "big.bin").write_bytes(b"0" * 2048)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"0")

    actions = gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert not (repo / ".terraform").exists()
    assert not (repo / "__pycache__").exists()
    assert sum(a.size for a in actions if a.applied) >= 2048


def test_clean_removes_artefact_files(workspace: Path):
    repo = workspace / "repo"
    (repo / ".coverage").write_text("x", encoding="utf-8")
    (repo / "keep.txt").write_text("x", encoding="utf-8")
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert not (repo / ".coverage").exists()
    assert (repo / "keep.txt").exists()


def test_clean_never_removes_a_tracked_path(workspace: Path):
    repo = workspace / "repo"
    (repo / "dist").mkdir()
    (repo / "dist" / "committed.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "track dist")
    cfg = config(clean={"builds": True})
    actions = gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert (repo / "dist" / "committed.txt").exists()
    assert not any(a.applied for a in actions)


def test_clean_removes_a_tracked_path_when_explicitly_allowed(workspace: Path):
    repo = workspace / "repo"
    (repo / "dist").mkdir()
    (repo / "dist" / "committed.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "track dist")
    cfg = config(clean={"builds": True, "tracked": True})
    gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert not (repo / "dist").exists()


def test_clean_honours_keep(workspace: Path):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    cfg = config(clean={"keep": ["__pycache__"]})
    gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert (repo / "__pycache__").exists()


def test_clean_does_not_follow_symlinks(workspace: Path, tmp_path: Path):
    repo = workspace / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("x", encoding="utf-8")
    (repo / "__pycache__").symlink_to(outside, target_is_directory=True)
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert (outside / "precious.txt").exists()


def test_clean_skips_a_nested_repo(workspace: Path):
    repo = workspace / "repo"
    inner = repo / "vendored"
    inner.mkdir()
    git(inner, "init", "-q")
    (inner / "__pycache__").mkdir()
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert (inner / "__pycache__").exists()


def test_clean_dry_run_removes_nothing(workspace: Path):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    actions = gt.clean_tree(repo, "repo", config(), run(gt.DRY), gt.Git(repo), None)
    assert (repo / "__pycache__").exists()
    assert actions and all(not a.applied for a in actions)


def test_clean_ignored_removes_what_gitignore_covers(workspace: Path):
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("generated/\n*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / "generated").mkdir()
    (repo / "generated" / "out.bin").write_bytes(b"0" * 100)
    (repo / "debug.log").write_text("noise", encoding="utf-8")
    (repo / "src.txt").write_text("kept", encoding="utf-8")

    gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert not (repo / "generated").exists()
    assert not (repo / "debug.log").exists()
    assert (repo / "src.txt").exists()


def test_clean_ignored_protects_local_state(workspace: Path):
    repo = workspace / "repo"
    (repo / ".gitignore").write_text(".env\n*.tfstate\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / ".env").write_text("SECRET=1", encoding="utf-8")
    (repo / "terraform.tfstate").write_text("{}", encoding="utf-8")

    actions = gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert (repo / ".env").exists()
    assert (repo / "terraform.tfstate").exists()
    assert all(a.skipped for a in actions)


def test_ignored_paths_lists_directories_whole(workspace: Path):
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    deep = repo / "node_modules" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "index.js").write_text("x", encoding="utf-8")
    assert gt.ignored_paths(gt.Git(repo)) == ["node_modules"]


# --------------------------------------------------------------------------- #
# Adversarial: layouts where a careless cleaner would destroy something
# --------------------------------------------------------------------------- #


def test_a_repo_buried_in_an_artefact_directory_survives(workspace: Path):
    """A vendored checkout inside node_modules must not go with its parent."""
    repo = workspace / "repo"
    buried = repo / "node_modules" / "some-dep"
    buried.mkdir(parents=True)
    git(buried, "init", "-q", "-b", "main")
    commit(buried, "src.txt", "somebody's work")

    cfg = config(clean={"dependencies": True})
    actions = gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert (buried / ".git").exists()
    assert (buried / "src.txt").read_text(encoding="utf-8") == "somebody's work"
    kept = [a for a in actions if a.target == "node_modules"]
    assert kept and kept[0].skipped and "git repository" in kept[0].detail


def test_a_tool_cache_is_removed_even_though_it_holds_clones(workspace: Path):
    """`terraform init` clones modules into .terraform, and that is still a cache.

    Without this exception a single `terraform init` would make a gigabyte
    permanently unreclaimable, which is the opposite of the point.
    """
    repo = workspace / "repo"
    cloned = repo / ".terraform" / "modules" / "vpc"
    cloned.mkdir(parents=True)
    git(cloned, "init", "-q", "-b", "main")
    commit(cloned, "main.tf")

    actions = gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert not (repo / ".terraform").exists()
    assert any(a.target == ".terraform" and a.applied for a in actions)


def test_the_regenerable_exception_is_configurable(workspace: Path):
    repo = workspace / "repo"
    cloned = repo / ".terraform" / "modules" / "vpc"
    cloned.mkdir(parents=True)
    git(cloned, "init", "-q", "-b", "main")

    cfg = config(clean={"regenerable": []})
    actions = gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert (repo / ".terraform").exists()
    assert any(a.skipped and "git repository" in a.detail for a in actions)


def test_a_repo_buried_in_a_loose_artefact_directory_survives(workspace: Path):
    """The same, for the walk that covers everything outside a repository."""
    buried = workspace / "dist" / "checkout"
    buried.mkdir(parents=True)
    git(buried, "init", "-q", "-b", "main")
    commit(buried, "src.txt")

    (workspace / ".git-tidy.yaml").write_text("clean:\n  builds: true\n", encoding="utf-8")
    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (buried / ".git").exists()


def test_a_symlinked_artefact_directory_is_left_alone(workspace: Path, tmp_path: Path):
    """The symlink is not followed, and the real directory is untouched."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    (workspace / "node_modules").symlink_to(outside, target_is_directory=True)

    (workspace / ".git-tidy.yaml").write_text("clean:\n  dependencies: true\n", encoding="utf-8")
    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert (workspace / "node_modules").is_symlink()


def test_same_relative_path_in_two_repos_does_not_collide_in_quarantine(workspace: Path):
    """Quarantine keys are relative to the workspace, so repo names disambiguate."""
    git(workspace, "clone", "-q", str(workspace / "repo"), "second")
    for name, text in (("repo", "first"), ("second", "second")):
        cache = workspace / name / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "m.pyc").write_text(text, encoding="utf-8")

    holding = _holding(workspace)
    for name in ("repo", "second"):
        gt.clean_tree(workspace / name, name, config(), run(), gt.Git(workspace / name), holding)
    holding.write_manifest()

    assert (holding.dir / gt.CONTENT_DIRNAME / "repo" / "__pycache__" / "m.pyc").read_text(
        encoding="utf-8"
    ) == "first"
    assert (holding.dir / gt.CONTENT_DIRNAME / "second" / "__pycache__" / "m.pyc").read_text(
        encoding="utf-8"
    ) == "second"


def test_a_gitignore_that_ignores_everything_keeps_tracked_files(workspace: Path):
    """`*` in .gitignore is `git clean -Xd` territory: tracked content still stays."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("*\n", encoding="utf-8")
    git(repo, "add", "-f", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore everything")
    (repo / "scratch.txt").write_text("untracked", encoding="utf-8")

    gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert (repo / "README.md").is_file(), "tracked files are not 'others'"
    assert (repo / ".gitignore").is_file()
    assert (repo / ".git").is_dir()
    assert not (repo / "scratch.txt").exists()


def test_clean_never_reaches_into_dot_git(workspace: Path):
    repo = workspace / "repo"
    planted = repo / ".git" / "__pycache__"
    planted.mkdir()
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert planted.exists(), ".git is git's own business"


def test_a_branch_checked_out_in_another_worktree_is_reported_not_lost(
    workspace: Path, remote: Path, tmp_path: Path
):
    """git cannot delete it, and saying so beats one failure line per branch."""
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature")
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "feature")

    actions = gt.prune_branches(repo, "repo", config(), run())
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").split()
    held = [a for a in actions if a.target == "feature"]
    assert held and held[0].skipped and "worktree" in held[0].detail
    assert not any(a.error for a in actions)
    # Not the default-branch category: a branch reaching here is [gone] or
    # local-only, so it is never the trunk.
    assert gt._reason_of(held[0].detail) == "branches a worktree still has checked out"


def test_force_does_not_help_a_branch_a_worktree_holds(
    workspace: Path, remote: Path, tmp_path: Path
):
    """--force lowers git-tidy's guards, not git's. Removing a worktree is a decision."""
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature", extra_commit=True)
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "feature")

    forced = gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force")
    actions = gt.prune_branches(repo, "repo", forced, run())
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").split()
    assert not any(a.error for a in actions)


def test_force_stashes_instead_of_discarding(workspace: Path, remote: Path, tmp_path: Path):
    """--force gets the switch done without throwing uncommitted work away."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")

    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    forced = gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force")
    actions = gt.sync_repo(repo, "repo", forced, run())

    assert gt.current_branch(gt.Git(repo)) == "main", "the switch happened"
    assert any(a.kind == "stash+switch" and a.applied for a in actions)
    assert not any(a.error for a in actions)
    # The work is not gone, it is one `git stash pop` away.
    assert "git-tidy: repo" in git(repo, "stash", "list")
    git(repo, "switch", "-q", "side")
    git(repo, "stash", "pop")
    assert (repo / "README.md").read_text(encoding="utf-8") == "work in progress\n"


def test_without_force_uncommitted_work_is_never_stashed(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    gt.sync_repo(repo, "repo", config(), run())
    assert git(repo, "stash", "list") == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "work in progress\n"


def test_guard_refuses_to_escape_the_root(tmp_path: Path):
    with pytest.raises(gt.Failure, match="outside"):
        gt._guard(tmp_path.parent, tmp_path)


def test_guard_refuses_the_root_itself(tmp_path: Path):
    with pytest.raises(gt.Failure, match="outside"):
        gt._guard(tmp_path, tmp_path)


def test_guard_refuses_inside_dot_git(workspace: Path):
    repo = workspace / "repo"
    with pytest.raises(gt.Failure, match=r"\.git"):
        gt._guard(repo / ".git" / "config", repo)


def test_directory_size_ignores_symlinks(tmp_path: Path):
    (tmp_path / "real.bin").write_bytes(b"0" * 512)
    (tmp_path / "link.bin").symlink_to(tmp_path / "real.bin")
    assert gt.directory_size(tmp_path) == 512


# --------------------------------------------------------------------------- #
# quarantine
# --------------------------------------------------------------------------- #


def test_quarantine_moves_and_restores(workspace: Path):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    assert not (repo / "__pycache__").exists()
    assert holding.write_manifest() is not None

    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert all(a.applied for a in actions)
    assert (repo / "__pycache__" / "m.pyc").is_file()


def test_restore_refuses_to_overwrite(workspace: Path):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    holding.write_manifest()
    (repo / "__pycache__").mkdir()  # something is back in the way

    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, None, run())
    assert actions[0].error


def test_restore_without_a_quarantine(tmp_path: Path):
    with pytest.raises(gt.Failure, match="no quarantine"):
        gt.restore(tmp_path / "nope", None, run())


def test_restore_with_an_unknown_stamp(workspace: Path):
    root = workspace / gt.QUARANTINE_DIRNAME
    (root / "stamp").mkdir(parents=True)
    (root / "stamp" / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    with pytest.raises(gt.Failure, match=r"no quarantine 'other'"):
        gt.restore(root, "other", run())


def test_expire_removes_old_quarantines(workspace: Path):
    root = workspace / gt.QUARANTINE_DIRNAME
    old = root / "old"
    old.mkdir(parents=True)
    (old / "x").write_text("x", encoding="utf-8")
    (old / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    long_ago = time.time() - 60 * 86400
    os.utime(old, (long_ago, long_ago))

    actions = gt.expire_quarantines(root, 30, run())
    assert actions and actions[0].applied
    assert not old.exists()


def test_expire_keeps_recent_quarantines(workspace: Path):
    root = workspace / gt.QUARANTINE_DIRNAME
    (root / "fresh").mkdir(parents=True)
    assert gt.expire_quarantines(root, 30, run()) == []


# --------------------------------------------------------------------------- #
# trash
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "asjfoisjdgipfdspigfjdpi.txt",  # consonant run of five
        "lalalalala.log",  # one unit, repeated
        "ksdcnwifcnweivfbn",  # consonant run of six
        "bcdfghjkl",  # no vowel at all
        "lidfghsidfghweuoghwesioughwsoeu",
    ],
)
def test_mash_detects_junk(name):
    assert gt.looks_like_mash(name)


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "deploy.sh",
        "terraform.tfstate",
        "main.py",
        "notes.txt",
        "backup",
        "background.png",  # four consonants in a row, and a real word
        "dluahsfduihacf",  # junk, but with an ordinary vowel rhythm
    ],
)
def test_mash_leaves_real_names_alone(name):
    """The heuristic is conservative on purpose: missing junk beats sweeping work."""
    assert not gt.looks_like_mash(name)


def age(path: Path, days: int) -> None:
    when = time.time() - days * 86400
    os.utime(path, (when, when))


def test_trash_sweeps_junk_into_quarantine(workspace: Path):
    junk = workspace / "asjfoisjdgipfdspigfjdpi.txt"
    junk.write_text("nonsense", encoding="utf-8")
    age(junk, 30)
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    cfg = config(trash={"enabled": True})
    actions = gt.sweep_trash(workspace, cfg, run(), holding, [])
    assert not junk.exists()
    assert actions and actions[0].applied
    assert (workspace / gt.QUARANTINE_DIRNAME / "stamp" / gt.CONTENT_DIRNAME / junk.name).is_file()


def test_trash_leaves_young_files_alone(workspace: Path):
    junk = workspace / "asjfoisjdgipfdspigfjdpi.txt"
    junk.write_text("nonsense", encoding="utf-8")
    cfg = config(trash={"enabled": True})
    assert gt.sweep_trash(workspace, cfg, run(), _holding(workspace), []) == []
    assert junk.exists()


def test_trash_always_quarantines_a_sensitive_file(workspace: Path):
    secret = workspace / "splunk_token.pw"
    secret.write_text("s3cret", encoding="utf-8")
    age(secret, 30)
    holding = _holding(workspace)
    cfg = config(trash={"enabled": True, "quarantine": False, "patterns": ["*.pw"]})

    actions = gt.sweep_trash(workspace, cfg, run(), holding, [])
    assert actions[0].applied and "sensitive" in actions[0].detail
    assert (holding.dir / gt.CONTENT_DIRNAME / "splunk_token.pw").is_file(), (
        "a token must never be hard-deleted"
    )


def test_trash_keeps_readmes(workspace: Path):
    readme = workspace / "README.md"
    readme.write_text("x", encoding="utf-8")
    age(readme, 100)
    cfg = config(trash={"enabled": True})
    assert gt.sweep_trash(workspace, cfg, run(), _holding(workspace), []) == []


def test_trash_off_by_default(workspace: Path):
    assert gt.sweep_trash(workspace, config(), run(), _holding(workspace), []) == []


def test_trash_rejects_an_unknown_scope(workspace: Path):
    cfg = config(trash={"enabled": True, "scope": "everywhere"})
    with pytest.raises(gt.Failure, match=r"trash\.scope"):
        gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])


def test_trash_workspace_scope_skips_repos(workspace: Path):
    inside = workspace / "repo" / "lalalalala.log"
    inside.write_text("x", encoding="utf-8")
    age(inside, 30)
    loose = workspace / "loose"
    loose.mkdir()
    outside = loose / "lalalalala.log"
    outside.write_text("x", encoding="utf-8")
    age(outside, 30)

    cfg = config(trash={"enabled": True, "scope": "workspace"})
    repos = gt.find_repos(workspace, [])
    gt.sweep_trash(workspace, cfg, run(), _holding(workspace), repos)
    assert inside.exists(), "files inside a repository are git's business"
    assert not outside.exists()


def test_trash_finds_empty_files(workspace: Path):
    empty = workspace / "chore.txt"
    empty.touch()
    age(empty, 30)
    cfg = config(trash={"enabled": True, "keep": []})
    actions = gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])
    assert actions and "empty" in actions[0].detail


def test_trash_finds_editor_leftovers(workspace: Path):
    leftover = workspace / "main.py.orig"
    leftover.write_text("x", encoding="utf-8")
    age(leftover, 30)
    cfg = config(trash={"enabled": True})
    actions = gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])
    assert actions and "leftover" in actions[0].detail


def _holding(workspace: Path) -> gt.Quarantine:
    return gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_finds_a_credential_in_a_remote_url(workspace: Path):
    repo = workspace / "repo"
    git(
        repo, "remote", "set-url", "origin", "https://user:s3cr3t-token-value@example.invalid/x.git"
    )
    actions = gt.doctor_repo(repo, "repo", config())
    hit = [a for a in actions if "credential" in a.detail]
    assert hit
    assert "s3cr3t-token-value" not in hit[0].detail, "the report must not leak the token"
    assert "***" in hit[0].detail


def test_doctor_finds_a_detached_head(workspace: Path):
    repo = workspace / "repo"
    git(repo, "checkout", "-q", "--detach")
    assert any("detached" in a.detail for a in gt.doctor_repo(repo, "repo", config()))


def test_doctor_finds_unpushed_commits(workspace: Path):
    repo = workspace / "repo"
    commit(repo, "local.txt")
    assert any("not pushed" in a.detail for a in gt.doctor_repo(repo, "repo", config()))


def test_doctor_finds_a_repo_with_no_remote(tmp_path: Path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    git(lonely, "init", "-q", "-b", "main")
    actions = gt.doctor_repo(lonely, "lonely", config())
    assert actions[0].detail == "no remote configured"


def test_doctor_warns_about_a_large_git_dir(workspace: Path):
    repo = workspace / "repo"
    actions = gt.doctor_repo(repo, "repo", config(doctor={"large_git_mb": 0}))
    # large_git_mb of 0 disables the check, so nothing about .git may appear.
    assert not any(a.target == ".git" for a in actions)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://u:p@host/x.git", "https://u:***@host/x.git"),
        ("ssh://git@host:7999/x.git", "ssh://git@host:7999/x.git"),
        ("git@github.com:me/x.git", "git@github.com:me/x.git"),
    ],
)
def test_redact(url, expected):
    assert gt.redact(url) == expected


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "count,expected",
    [(0, "0 B"), (512, "512 B"), (1536, "1.5 KB"), (1048576, "1.0 MB"), (2 * 1024**3, "2.0 GB")],
)
def test_human_size(count, expected):
    assert gt.human_size(count) == expected


def test_error_message_reports_the_cause_not_the_boilerplate():
    """git puts the reason first and generic advice underneath."""
    failed = subprocess.CompletedProcess(
        args=["git", "fetch"],
        returncode=128,
        stdout="",
        stderr=(
            "ssh: connect to host example.invalid port 22: Operation timed out\n"
            "fatal: Could not read from remote repository.\n"
            "\n"
            "Please make sure you have the correct access rights\n"
            "and the repository exists.\n"
        ),
    )
    assert gt.last_line(failed).startswith("ssh: connect to host")


def test_error_message_falls_back_when_git_says_nothing():
    empty = subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="")
    assert gt.last_line(empty) == "failed"


def test_an_unreachable_remote_is_reported_per_repo(workspace: Path):
    """A dead remote must be one failed line, not an exception mid-run."""
    repo = workspace / "repo"
    git(repo, "remote", "set-url", "origin", str(workspace / "does-not-exist.git"))
    actions = gt.sync_repo(repo, "repo", config(), run())
    assert len(actions) == 1
    assert actions[0].kind == "fetch" and actions[0].error


def test_report_totals():
    report = gt.Report(
        [
            gt.Action("remove", "r", "a", size=100, applied=True),
            gt.Action("remove", "r", "b", size=50),
            gt.Action("remove", "r", "c", size=25, skipped=True),
            gt.Action("remove", "r", "d", error="boom"),
        ]
    )
    assert report.bytes_freed == 100
    assert report.bytes_found == 150
    assert len(report.errors) == 1


def test_action_as_dict_is_json_serialisable():
    json.dumps(gt.Action("remove", "r", "a").as_dict())


# --------------------------------------------------------------------------- #
# End to end, through main()
# --------------------------------------------------------------------------- #


def test_main_dry_run_changes_nothing(workspace: Path, capsys):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    assert gt.main(["-C", str(workspace), "clean"]) == 0
    assert (repo / "__pycache__").exists()
    assert "dry run" in capsys.readouterr().out


def test_main_apply_cleans(workspace: Path, capsys):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (workspace / ".ruff_cache").mkdir()
    assert gt.main(["-C", str(workspace), "clean", "--apply"]) == 0
    assert not (repo / "__pycache__").exists()
    assert not (workspace / ".ruff_cache").exists(), "loose caches outside repos count too"


def test_main_json_output(workspace: Path, capsys):
    (workspace / "repo" / "__pycache__").mkdir()
    gt.main(["-C", str(workspace), "clean", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry"
    assert payload["repositories"] == 1
    assert any(a["kind"] == "remove" for a in payload["actions"])


def test_main_config_prints_the_merged_settings(workspace: Path, capsys):
    (workspace / ".git-tidy.yaml").write_text("jobs: 7\n", encoding="utf-8")
    gt.main(["-C", str(workspace), "config"])
    assert json.loads(capsys.readouterr().out)["jobs"] == 7


def test_main_include_and_exclude(workspace: Path, capsys):
    git(workspace, "clone", "-q", str(workspace / "repo"), "second")
    gt.main(["-C", str(workspace), "--include", "second", "doctor"])
    assert "1 repositor" in capsys.readouterr().out
    gt.main(["-C", str(workspace), "--exclude", "*", "doctor"])
    assert "0 repositor" in capsys.readouterr().out


def test_main_refuses_home(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(gt.Failure, match="refusing to work on"):
        gt.main(["-C", str(tmp_path), "clean"])


def test_main_refuses_a_missing_directory(tmp_path: Path):
    with pytest.raises(gt.Failure, match="not a directory"):
        gt.main(["-C", str(tmp_path / "nope"), "clean"])


def test_main_run_does_every_step(workspace: Path, capsys):
    (workspace / "repo" / "__pycache__").mkdir()
    assert gt.main(["-C", str(workspace), "run", "--apply"]) == 0
    out = capsys.readouterr().out
    for heading in ("Sync", "Branches", "Artefacts", "Trash", "Doctor", "Summary"):
        assert heading in out


def test_main_restore_lists_quarantines(workspace: Path, capsys):
    root = workspace / gt.QUARANTINE_DIRNAME / "stamp"
    root.mkdir(parents=True)
    (root / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    gt.main(["-C", str(workspace), "restore", "--list"])
    assert "stamp" in capsys.readouterr().out


def test_main_init_writes_a_config(workspace: Path, capsys):
    assert gt.main(["-C", str(workspace), "init", "--apply"]) == 0
    assert (workspace / ".git-tidy.yaml").is_file()


def test_main_ask_without_a_terminal_is_refused(workspace: Path):
    with pytest.raises(gt.Failure, match="needs a terminal"):
        gt.main(["-C", str(workspace), "clean", "--ask"])


def test_main_parallel_and_serial_agree(workspace: Path, tmp_path: Path):
    """Whatever the worker count, the same things are found."""
    for i in range(4):
        git(workspace, "clone", "-q", str(workspace / "repo"), f"clone{i}")
        (workspace / f"clone{i}" / "__pycache__").mkdir()

    serial = _found(workspace, jobs="1")
    for i in range(4):
        (workspace / f"clone{i}" / "__pycache__").mkdir(exist_ok=True)
    parallel = _found(workspace, jobs="8")
    assert serial == parallel and len(serial) == 4


def _found(workspace: Path, jobs: str) -> set[str]:
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        gt.main(["-C", str(workspace), "-j", jobs, "clean", "--json"])
    payload = json.loads(buffer.getvalue())
    return {a["scope"] + "/" + a["target"] for a in payload["actions"] if a["kind"] == "remove"}


def test_a_broken_repo_does_not_stop_the_others(workspace: Path, capsys):
    broken = workspace / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    (workspace / "repo" / "__pycache__").mkdir()
    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (workspace / "repo" / "__pycache__").exists()


# --------------------------------------------------------------------------- #
# Linked worktrees: siblings sharing one .git
# --------------------------------------------------------------------------- #


def test_a_linked_worktree_is_left_on_its_branch(workspace: Path, tmp_path: Path):
    """Holding a branch of its own is the entire reason a worktree exists."""
    repo = workspace / "repo"
    side = tmp_path / "side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))

    actions = gt.sync_repo(side, "side", config(), run())
    assert gt.current_branch(gt.Git(side)) == "side"
    switch = [a for a in actions if a.kind == "switch"]
    assert switch and switch[0].skipped and "linked worktree" in switch[0].detail
    assert gt._reason_of(switch[0].detail) is None, "nothing here needs a person"


def test_a_linked_worktree_can_still_be_switched_on_request(workspace: Path, tmp_path: Path):
    repo = workspace / "repo"
    side = tmp_path / "side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))
    git(repo, "switch", "-q", "-c", "elsewhere")  # free up main

    gt.sync_repo(side, "side", config(sync={"worktrees": "switch"}), run())
    assert gt.current_branch(gt.Git(side)) == "main"


def test_is_linked_worktree(workspace: Path, tmp_path: Path):
    repo = workspace / "repo"
    side = tmp_path / "side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))
    assert gt.is_linked_worktree(gt.Git(side))
    assert not gt.is_linked_worktree(gt.Git(repo))


def test_siblings_sharing_a_git_dir_are_one_family(workspace: Path, tmp_path: Path):
    """They share an index and a ref store, so they must not run concurrently."""
    repo = workspace / "repo"
    side = tmp_path / "side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))
    git(workspace, "clone", "-q", str(repo), "unrelated")

    families = gt._families([repo, side, workspace / "unrelated"])
    assert len(families) == 2
    assert sorted(len(f) for f in families) == [1, 2]


def test_diverged_is_reported_by_default(workspace: Path, remote: Path, tmp_path: Path):
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")
    repo = workspace / "repo"
    commit(repo, "mine.txt")

    actions = gt.sync_repo(repo, "repo", config(), run())
    assert any("diverged" in a.detail and a.skipped for a in actions)
    assert not (repo / "theirs.txt").exists()


def test_diverged_can_be_rebased(workspace: Path, remote: Path, tmp_path: Path):
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")
    repo = workspace / "repo"
    commit(repo, "mine.txt")

    actions = gt.sync_repo(repo, "repo", config(sync={"diverged": "rebase"}), run())
    assert any(a.kind == "rebase" and a.applied for a in actions)
    assert (repo / "theirs.txt").exists(), "their commit arrived"
    assert (repo / "mine.txt").exists(), "and mine is still on top"
    assert git(repo, "rev-list", "--count", "HEAD") == "3"


def test_a_failed_rebase_leaves_nothing_half_applied(workspace: Path, remote: Path, tmp_path: Path):
    """Both sides changed the same line, so the replay cannot succeed."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "README.md", "theirs\n")
    git(tmp_path / "other", "push", "-q")
    repo = workspace / "repo"
    commit(repo, "README.md", "mine\n")

    before = git(repo, "rev-parse", "HEAD")
    actions = gt.sync_repo(repo, "repo", config(sync={"diverged": "rebase"}), run())
    failed = [a for a in actions if a.error]
    assert failed and "aborted" in failed[0].error
    assert git(repo, "rev-parse", "HEAD") == before
    assert (repo / "README.md").read_text(encoding="utf-8") == "mine\n"


def test_init_asks_without_needing_the_ask_flag(tmp_path: Path):
    """Answering questions is the point of init, not a mode you have to opt into."""
    target = tmp_path / ".git-tidy.yaml"
    answers = iter(["4", "y", "n", "n", "y", "n", "n", "y", "30"])
    gt.cmd_init(
        target, gt.AUTO, force=False, printer=quiet_printer(), prompt_input=lambda _: next(answers)
    )
    parsed = gt._parse_yaml_subset(target.read_text(encoding="utf-8"), "<init>")
    assert parsed["jobs"] == 4
    assert parsed["clean"]["ignored"] is True
    assert parsed["trash"] == {"enabled": True, "min_age_days": 30}


def test_init_dry_run_prints_the_template_and_writes_nothing(tmp_path: Path, capsys):
    """-n means change nothing, for init as for everything else."""
    target = tmp_path / ".git-tidy.yaml"
    gt.main(["-C", str(tmp_path), "init", "-n", "--path", str(tmp_path)])
    assert not target.exists()
    out = capsys.readouterr().out
    assert "git-tidy configuration" in out
    assert gt._parse_yaml_subset(out.split("  Not written")[0], "<init>") is None


# --------------------------------------------------------------------------- #
# Round one of review: the ways this could still have lost something
# --------------------------------------------------------------------------- #


def test_declining_a_switch_does_not_stash_anyway(workspace: Path):
    """Consent comes before the stash, or --ask means nothing."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: "n")
    forced = gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force")
    gt.sync_repo(repo, "repo", forced, decider)

    assert git(repo, "stash", "list") == "", "declining must leave the worktree alone"
    assert (repo / "README.md").read_text(encoding="utf-8") == "work in progress\n"
    assert gt.current_branch(gt.Git(repo)) == "side"


def test_clean_ignored_protects_a_key_buried_in_an_ignored_directory(workspace: Path):
    """git hands back one entry for a wholly ignored directory."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore generated")
    deep = repo / "generated" / "config"
    deep.mkdir(parents=True)
    (deep / "server.pem").write_text("PRIVATE KEY", encoding="utf-8")
    (repo / "generated" / "out.bin").write_bytes(b"0" * 64)

    actions = gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert (deep / "server.pem").is_file(), "the key must survive its parent"
    # Emptied out around it rather than kept whole: the key is not a copy of
    # anything, so it stays at its path, and the rest of the tree still goes.
    assert actions and "server.pem" in actions[0].detail


def test_clean_ignored_still_removes_a_directory_with_nothing_protected(workspace: Path):
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore generated")
    (repo / "generated" / "sub").mkdir(parents=True)
    (repo / "generated" / "sub" / "out.bin").write_bytes(b"0" * 64)

    gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert not (repo / "generated").exists()


def test_a_workspace_that_is_itself_a_repository_is_refused(workspace: Path):
    """Its own tracked files would be cleaned as if they belonged to nobody."""
    with pytest.raises(gt.Failure, match="itself a git repository"):
        gt.resolve_workspace(str(workspace / "repo"))


def test_a_dry_run_does_not_write_refs(workspace: Path):
    """`remote set-head --auto` writes a ref and goes to the network."""
    repo = workspace / "repo"
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert gt.default_branch(gt.Git(repo), config()["sync"], readonly=True) == "main"
    # Resolved from the refs that exist, without recreating the one just removed.
    assert gt._cached_head(gt.Git(repo), "origin") is None


def test_unmerged_branches_are_not_deleted_on_stale_information(workspace: Path, remote: Path):
    """require_merged off removes the only other check, so the [gone] must be fresh."""
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature", extra_commit=True)
    cfg = config(branches={"require_merged": False})

    actions = gt.prune_branches(repo, "repo", cfg, run(), fetched=False)
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").split()
    assert actions and "needs a fetch" in actions[0].detail

    actions = gt.prune_branches(repo, "repo", cfg, run(), fetched=True)
    assert any(a.applied for a in actions)


def test_a_merged_check_uses_git_own_verification(workspace: Path, remote: Path):
    """--delete without --force makes git re-check containment as it deletes."""
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature")
    actions = gt.prune_branches(repo, "repo", config(), run(), fetched=True)
    assert any(a.applied for a in actions)
    assert "feature" not in git(repo, "branch", "--format=%(refname:short)").split()


def test_a_killed_sweep_is_still_restorable(workspace: Path):
    """The journal is written as files move, so a crash leaves a usable record."""
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    stamp_dir = workspace / gt.QUARANTINE_DIRNAME / "stamp"
    assert (stamp_dir / gt.JOURNAL_NAME).is_file(), "written during the sweep"
    assert not (stamp_dir / gt.MANIFEST_NAME).exists(), "the manifest comes at the end"

    # Nothing folded the journal into a manifest — as after a kill — and restore
    # still puts everything back.
    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert actions and all(a.applied for a in actions)
    assert (repo / "__pycache__" / "m.pyc").is_file()


def test_the_journal_is_appended_not_rewritten(workspace: Path):
    """Rewriting it per file would serialise the whole run on one lock."""
    repo = workspace / "repo"
    for name in ("a", "b", "c"):
        cache = repo / name / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "m.pyc").write_text(name, encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    lines = (
        (workspace / gt.QUARANTINE_DIRNAME / "stamp" / gt.JOURNAL_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 3, "one line per move, not one rewrite per move"
    assert all(json.loads(line)["from"] for line in lines)


def test_restore_refuses_a_manifest_pointing_outside_the_workspace(workspace: Path, tmp_path: Path):
    root = workspace / gt.QUARANTINE_DIRNAME / "stamp"
    root.mkdir(parents=True)
    (root / "loot").write_text("x", encoding="utf-8")
    (root / gt.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "entries": [{"to": str(root / "loot"), "from": str(tmp_path / "elsewhere.txt")}],
            }
        ),
        encoding="utf-8",
    )
    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert actions[0].error and "outside the workspace" in actions[0].error
    assert not (tmp_path / "elsewhere.txt").exists()


def test_declining_a_fast_forward_does_not_stash_anyway(
    workspace: Path, remote: Path, tmp_path: Path
):
    """The same consent rule as the switch, on the fast-forward path."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")

    repo = workspace / "repo"
    git(repo, "fetch", "-q", "origin")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: "n")
    gt.sync_repo(repo, "repo", gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force"), decider)

    assert git(repo, "stash", "list") == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "work in progress\n"


def test_fetch_freshness_is_per_repository(workspace: Path, remote: Path, capsys):
    """One repository's fetch must not vouch for another's [gone] marks."""
    git(workspace, "clone", "-q", str(remote), "second")
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature", extra_commit=True)
    # repo cannot fetch; second can.
    git(repo, "remote", "set-url", "origin", str(workspace / "gone.git"))
    (workspace / ".git-tidy.yaml").write_text(
        "branches:\n  require_merged: false\n", encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "run", "--apply"])
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").split()


def test_ignored_keep_matches_a_relative_pattern_too(workspace: Path):
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore generated")
    deep = repo / "generated" / "config"
    deep.mkdir(parents=True)
    (deep / "server.crt").write_text("CERT", encoding="utf-8")

    cfg = config(clean={"ignored_keep": ["generated/config/*.crt"]})
    actions = gt.clean_ignored(repo, "repo", cfg, run(), None, gt.Git(repo))
    assert (deep / "server.crt").is_file()
    assert actions and "server.crt" in actions[0].detail


def test_a_workspace_inside_a_repository_keeps_its_tracked_files(tmp_path: Path):
    """Nothing below the workspace claims them, but they are still committed."""
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q", "-b", "main")
    space = outer / "space"
    (space / "dist").mkdir(parents=True)
    (space / "dist" / "committed.txt").write_text("real content", encoding="utf-8")
    (space / "__pycache__").mkdir()
    git(outer, "add", "-A")
    git(outer, "commit", "-q", "-m", "track the workspace")

    (space / ".git-tidy.yaml").write_text("clean:\n  builds: true\n", encoding="utf-8")
    gt.main(["-C", str(space), "clean", "--apply"])
    assert (space / "dist" / "committed.txt").is_file(), "tracked, even from outside"
    assert not (space / "__pycache__").exists(), "untracked artefacts still go"


def test_enclosing_repo(tmp_path: Path):
    outer = tmp_path / "outer"
    (outer / "space").mkdir(parents=True)
    git(outer, "init", "-q", "-b", "main")
    assert gt.enclosing_repo(outer / "space") == outer
    assert gt.enclosing_repo(tmp_path) is None


# --------------------------------------------------------------------------- #
# Round three of review
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overlay,message",
    [
        ({"clean": {"enabled": "false"}}, "takes bool"),
        ({"clean": {"tracked": "no"}}, "takes bool"),
        ({"jobs": "8"}, "takes int"),
        ({"sync": {"remote": 4}}, "takes str"),
    ],
)
def test_a_scalar_of_the_wrong_type_is_refused(overlay, message):
    """`enabled: "false"` is a non-empty string, and a non-empty string is true."""
    with pytest.raises(gt.Failure, match=message):
        gt._merge(gt.DEFAULTS, overlay, "test")


def test_single_quoted_apostrophes_match_pyyaml():
    yaml = pytest.importorskip("yaml")
    text = "branches:\n  keep:\n    - 'team''s/*'\n"
    assert gt._parse_yaml_subset(text, "<test>") == yaml.safe_load(text)
    assert gt._parse_yaml_subset(text, "<test>")["branches"]["keep"] == ["team's/*"]


def test_an_unknown_worktrees_mode_is_refused(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    actions = gt.sync_repo(repo, "repo", config(sync={"worktrees": "skpi"}), run())
    assert any(a.error and "sync.worktrees" in a.error for a in actions)


def test_all_of_these_does_not_leak_consent_into_stashing(workspace: Path):
    """Answering 'a' to plain switches must not agree to stash somebody's work."""
    plain = gt.Action("switch", "a", "main", "switch from side")
    stashing = gt.Action("stash+switch", "b", "main", "stash and switch from side")
    replies = iter(["a", "n"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))

    assert decider.allow(plain) is True
    assert decider.allow(stashing) is False, "a different question gets asked again"


def test_expire_leaves_anything_that_is_not_a_quarantine(workspace: Path):
    root = workspace / gt.QUARANTINE_DIRNAME
    stranger = root / "someone-elses-directory"
    stranger.mkdir(parents=True)
    (stranger / "important.txt").write_text("not ours", encoding="utf-8")
    long_ago = time.time() - 60 * 86400
    os.utime(stranger, (long_ago, long_ago))

    actions = gt.expire_quarantines(root, 30, run())
    assert (stranger / "important.txt").is_file()
    assert actions and actions[0].skipped and "not a quarantine" in actions[0].detail


def test_trash_workspace_scope_can_sweep_directories(workspace: Path):
    loose = workspace / "loose"
    loose.mkdir()
    junk = loose / "lalalalala"
    junk.mkdir()
    (junk / "x").write_text("x", encoding="utf-8")
    age(junk, 30)

    cfg = config(trash={"enabled": True, "scope": "workspace", "dirs": True, "keep": []})
    actions = gt.sweep_trash(
        workspace, cfg, run(), _holding(workspace), gt.find_repos(workspace, [])
    )
    assert not junk.exists()
    assert actions and actions[0].applied


def test_quitting_keeps_the_work_already_done(workspace: Path, capsys):
    """'q' stops the run; it does not un-report what already happened."""
    repo = workspace / "repo"
    for name in ("a", "b", "c"):
        cache = repo / name / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "m.pyc").write_text(name, encoding="utf-8")

    replies = iter(["y", "y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    with pytest.raises(gt.Quit) as stopped:
        gt.clean_tree(repo, "repo", config(), decider, gt.Git(repo), None)

    applied = [a for a in stopped.value.done if a.applied]
    assert len(applied) == 2, "the two that were agreed to are reported"


def test_a_run_that_was_stopped_does_not_report_success(workspace: Path, monkeypatch):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "q")
    assert gt.main(["-C", str(workspace), "clean", "--ask"]) == 1


def test_restore_honours_json(workspace: Path, capsys):
    root = workspace / gt.QUARANTINE_DIRNAME / "stamp"
    root.mkdir(parents=True)
    (root / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    gt.main(["-C", str(workspace), "restore", "--json"])
    assert json.loads(capsys.readouterr().out)["actions"] == []


def test_the_timeout_applies_outside_sync(workspace: Path):
    (workspace / ".git-tidy.yaml").write_text("sync:\n  timeout: 7\n", encoding="utf-8")
    resolver = gt.ConfigResolver(workspace)
    context = gt.Context(
        workspace,
        resolver,
        run(gt.DRY),
        quiet_printer(),
        gt.find_repos(workspace, []),
        _holding(workspace),
    )
    assert context.timeout == 7


# --------------------------------------------------------------------------- #
# Round four of review
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overlay",
    [
        {"trash": {"retention_days": False}},  # bool is an int in Python
        {"trash": {"quarantine": None}},  # a blank value is falsy, not "default"
        {"jobs": True},
    ],
)
def test_a_value_that_would_change_the_meaning_is_refused(overlay):
    with pytest.raises(gt.Failure):
        gt._merge(gt.DEFAULTS, overlay, "test")


def test_a_list_of_the_wrong_thing_is_refused_at_load_time():
    """fnmatch would otherwise raise inside a worker, half way through a run."""
    with pytest.raises(gt.Failure, match="must be text"):
        gt._merge(gt.DEFAULTS, {"branches": {"keep": [{}]}}, "test")


def test_family_discovery_survives_a_broken_repository(workspace: Path):
    broken = workspace / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    families = gt._families([workspace / "repo", broken], timeout=5)
    assert len(families) == 2, "the healthy one is still grouped"


def test_restore_list_shows_an_unfinished_sweep(workspace: Path, capsys):
    root = workspace / gt.QUARANTINE_DIRNAME / "stamp"
    root.mkdir(parents=True)
    (root / gt.JOURNAL_NAME).write_text(
        json.dumps({"from": "/a", "to": "/b"}) + "\n", encoding="utf-8"
    )
    gt.main(["-C", str(workspace), "restore", "--list"])
    out = capsys.readouterr().out
    assert "stamp" in out and "unfinished" in out


def test_a_stash_is_not_counted_as_its_own_action(workspace: Path):
    """It is part of the switch it made room for, not a second thing that happened."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    actions = gt.sync_repo(repo, "repo", gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force"), run())
    assert not [a for a in actions if a.kind == "stash"], "no separate success entry"
    assert len([a for a in actions if a.applied and "stash" in a.kind]) == 1
    assert "git-tidy: repo" in git(repo, "stash", "list")


def test_a_repository_can_shorten_its_own_timeout(workspace: Path):
    (workspace / ".git-tidy.yaml").write_text("sync:\n  timeout: 90\n", encoding="utf-8")
    (workspace / "repo" / ".git-tidy.yaml").write_text("sync:\n  timeout: 5\n", encoding="utf-8")
    resolver = gt.ConfigResolver(workspace)
    assert resolver.for_path(workspace)["sync"]["timeout"] == 90
    assert resolver.for_path(workspace / "repo")["sync"]["timeout"] == 5


# --------------------------------------------------------------------------- #
# Round five of review
# --------------------------------------------------------------------------- #


def test_a_dry_run_predicts_what_force_would_do(workspace: Path):
    """A dry run stands in for the real one; it must not describe a different run."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")
    forced = gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force")

    actions = gt.sync_repo(repo, "repo", forced, run(gt.DRY))
    switch = [a for a in actions if "switch" in a.kind]
    assert switch and "would stash and switch" in switch[0].detail
    # ...and still changed nothing.
    assert git(repo, "stash", "list") == ""
    assert gt.current_branch(gt.Git(repo)) == "side"


def test_a_dry_run_predicts_a_forced_fast_forward(workspace: Path, remote: Path, tmp_path: Path):
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")
    repo = workspace / "repo"
    git(repo, "fetch", "-q", "origin")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    forced = gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force")
    actions = gt.sync_repo(repo, "repo", forced, run(gt.DRY))
    update = [a for a in actions if "update" in a.kind]
    assert update and "would stash and fast-forward" in update[0].detail
    assert git(repo, "stash", "list") == ""


def test_a_config_that_is_not_utf8_stops_one_repo_not_the_run(workspace: Path, capsys):
    repo = workspace / "repo"
    (repo / ".git-tidy.yaml").write_bytes(b"jobs: \xff\xfe\n")
    (repo / "__pycache__").mkdir()
    git(workspace, "clone", "-q", str(repo), "second")
    (workspace / "second" / "__pycache__").mkdir()

    assert gt.main(["-C", str(workspace), "clean", "--apply"]) == 1
    assert (repo / "__pycache__").exists(), "the broken one was skipped"
    assert not (workspace / "second" / "__pycache__").exists(), "the healthy one was not"


def test_the_generated_config_does_not_promise_what_it_can_lift():
    text = gt.render_config({}, "header")
    assert "never rebased" not in text
    assert "sync.diverged" not in text or "rebase" in text


# --------------------------------------------------------------------------- #
# Round six of review
# --------------------------------------------------------------------------- #


def test_an_untracked_file_counts_as_uncommitted_work(workspace: Path):
    """The README promises such a repository stays on its branch."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "side")
    (repo / "notes.txt").write_text("not committed anywhere\n", encoding="utf-8")

    assert gt.is_dirty(gt.Git(repo))
    actions = gt.sync_repo(repo, "repo", config(), run())
    assert gt.current_branch(gt.Git(repo)) == "side"
    assert any("uncommitted changes" in a.detail for a in actions)
    assert (repo / "notes.txt").is_file()


def test_an_ignored_file_does_not_freeze_a_repository(workspace: Path):
    """Otherwise every repository that has ever been built would stop syncing."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore logs")
    git(repo, "switch", "-q", "-c", "side")
    (repo / "build.log").write_text("noise", encoding="utf-8")

    assert not gt.is_dirty(gt.Git(repo))
    gt.sync_repo(repo, "repo", config(), run())
    assert gt.current_branch(gt.Git(repo)) == "main"


def test_two_sweeps_in_one_second_do_not_share_a_quarantine(workspace: Path):
    """The second manifest would otherwise replace the first, stranding its files."""
    root = workspace / gt.QUARANTINE_DIRNAME
    first = gt.Quarantine(root, workspace)
    (workspace / "one.pyc").write_text("first", encoding="utf-8")
    first.take(workspace / "one.pyc")
    first.write_manifest()

    second = gt.Quarantine(root, workspace)
    assert second.stamp != first.stamp, "same second, different sweep"
    (workspace / "two.pyc").write_text("second", encoding="utf-8")
    second.take(workspace / "two.pyc")
    second.write_manifest()

    # Both are listed, and both restore.
    assert len([p for p in root.iterdir() if (p / gt.MANIFEST_NAME).is_file()]) == 2
    gt.restore(root, first.stamp, run())
    gt.restore(root, second.stamp, run())
    assert (workspace / "one.pyc").read_text(encoding="utf-8") == "first"
    assert (workspace / "two.pyc").read_text(encoding="utf-8") == "second"


def test_a_quarantine_name_collision_keeps_both_files(workspace: Path):
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    (workspace / "a").mkdir()
    for round_number in ("first", "second", "third"):
        (workspace / "a" / "m.pyc").write_text(round_number, encoding="utf-8")
        holding.take(workspace / "a" / "m.pyc")

    kept = sorted(
        p.read_text(encoding="utf-8")
        for p in (holding.dir / gt.CONTENT_DIRNAME / "a").iterdir()
        if p.is_file()
    )
    assert kept == ["first", "second", "third"], "no sweep overwrote another"


# --------------------------------------------------------------------------- #
# Round seven of review
# --------------------------------------------------------------------------- #


def test_a_submodule_with_uncommitted_work_is_not_forced(workspace: Path, tmp_path: Path):
    """`submodule update --force` is a checkout --force inside the submodule."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    commit(inner, "lib.txt", "v1\n")

    repo = workspace / "repo"
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    git(repo, "commit", "-q", "-m", "add submodule")
    (repo / "vendor" / "lib.txt").write_text("work in progress\n", encoding="utf-8")

    gt._sync_submodules(gt.Git(repo), "repo", config(sync={"submodules": "update"})["sync"], run())
    assert (repo / "vendor" / "lib.txt").read_text(encoding="utf-8") == "work in progress\n"


def test_trash_does_not_sweep_an_enclosing_repositorys_tracked_file(tmp_path: Path):
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q", "-b", "main")
    space = outer / "space"
    space.mkdir()
    (space / "lalalalala.log").write_text("committed, oddly named", encoding="utf-8")
    git(outer, "add", "-A")
    git(outer, "commit", "-q", "-m", "track it")
    age(space / "lalalalala.log", 30)

    cfg = config(trash={"enabled": True, "min_age_days": 7})
    actions = gt.sweep_trash(space, cfg, run(), _holding(space), [])
    assert (space / "lalalalala.log").is_file(), "committed content is not junk"
    assert actions == []


def test_trash_still_sweeps_when_nothing_encloses_the_workspace(workspace: Path):
    junk = workspace / "lalalalala.log"
    junk.write_text("x", encoding="utf-8")
    age(junk, 30)
    cfg = config(trash={"enabled": True})
    actions = gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])
    assert actions and actions[0].applied
    assert not junk.exists()


# --------------------------------------------------------------------------- #
# Round eight of review
# --------------------------------------------------------------------------- #


def test_quarantined_bytes_are_not_reported_as_freed(workspace: Path, capsys):
    """They are still on the disk; the next df would expose the claim."""
    junk = workspace / "lalalalala.log"
    junk.write_text("x" * 4096, encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text(
        "trash:\n  enabled: true\n  min_age_days: 7\n", encoding="utf-8"
    )
    gt.main(["-C", str(workspace), "trash", "--apply"])
    out = capsys.readouterr().out
    assert "moved to quarantine, not reclaimed" in out
    assert "freed" not in out


def test_a_config_outside_a_repository_protects_its_own_directory(workspace: Path):
    """Deepest wins applies out here as much as it does inside a repository."""
    loose = workspace / "loose"
    (loose / "__pycache__").mkdir(parents=True)
    (loose / ".git-tidy.yaml").write_text('clean:\n  keep: ["__pycache__"]\n', encoding="utf-8")
    other = workspace / "other"
    (other / "__pycache__").mkdir(parents=True)

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (loose / "__pycache__").exists(), "kept by the config next to it"
    assert not (other / "__pycache__").exists()


def test_a_dry_run_does_not_count_the_same_path_twice(workspace: Path, capsys):
    """clean.ignored and the pattern walk both see it while it is still there."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore caches")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.pyc").write_bytes(b"0" * 2048)
    (workspace / ".git-tidy.yaml").write_text("clean:\n  ignored: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--json"])
    actions = json.loads(capsys.readouterr().out)["actions"]
    caches = [a for a in actions if a["target"] == "__pycache__"]
    assert len(caches) == 1, "reported once, as it will be removed once"


# --------------------------------------------------------------------------- #
# Round nine of review
# --------------------------------------------------------------------------- #


def test_an_unreadable_index_stops_the_repo_rather_than_the_protection(workspace: Path):
    """An empty tracked set would mean every artefact rule applies to commits."""
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / ".git" / "index").write_bytes(b"not an index")

    with pytest.raises(gt.Failure, match="cannot read the index"):
        gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert (repo / "__pycache__").exists(), "nothing was removed on a broken index"


def test_a_broken_index_is_reported_and_the_others_still_run(workspace: Path):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / ".git" / "index").write_bytes(b"not an index")
    git(workspace, "clone", "-q", str(remote_of(repo)), "second")
    (workspace / "second" / "__pycache__").mkdir()

    assert gt.main(["-C", str(workspace), "clean", "--apply"]) == 1
    assert (repo / "__pycache__").exists()
    assert not (workspace / "second" / "__pycache__").exists()


def remote_of(repo: Path) -> str:
    return git(repo, "remote", "get-url", "origin")


def test_restore_reports_a_filesystem_failure(workspace: Path, monkeypatch):
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    holding.write_manifest()

    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(gt.shutil, "move", refuse)
    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert actions[0].error and "read-only" in actions[0].error


def test_expire_reports_a_filesystem_failure(workspace: Path, monkeypatch):
    root = workspace / gt.QUARANTINE_DIRNAME
    old = root / "old"
    old.mkdir(parents=True)
    (old / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    long_ago = time.time() - 60 * 86400
    os.utime(old, (long_ago, long_ago))

    def refuse(*_args, **_kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(gt.shutil, "rmtree", refuse)
    actions = gt.expire_quarantines(root, 30, run())
    assert actions and actions[0].error and "in use" in actions[0].error
    assert old.exists()


# --------------------------------------------------------------------------- #
# Round ten of review
# --------------------------------------------------------------------------- #


def test_a_repository_can_exclude_itself(workspace: Path):
    """Deepest wins applies to `exclude` as much as to anything else."""
    repo = workspace / "repo"
    (repo / ".git-tidy.yaml").write_text('exclude: ["*"]\n', encoding="utf-8")
    (repo / "__pycache__").mkdir()
    git(workspace, "clone", "-q", str(repo), "second")
    (workspace / "second" / "__pycache__").mkdir()

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (repo / "__pycache__").exists(), "it asked to be left alone"
    assert not (workspace / "second" / "__pycache__").exists()


# --------------------------------------------------------------------------- #
# Round eleven of review
# --------------------------------------------------------------------------- #


def test_a_quarantined_artefact_is_not_reported_as_removed(workspace: Path):
    """It is a restore away, so calling it removed would be untrue."""
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    cfg = config(clean={"quarantine": True})
    actions = gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), holding)
    assert actions[0].applied and actions[0].detail == "quarantined"
    assert actions[0].quarantined
    assert (holding.dir / gt.CONTENT_DIRNAME / "repo" / "__pycache__").exists()


# --------------------------------------------------------------------------- #
# Round twelve of review
# --------------------------------------------------------------------------- #


def test_a_loose_directory_can_ask_for_its_own_quarantine(workspace: Path):
    """The root's setting must not decide what happens to a deeper directory."""
    careful = workspace / "careful"
    (careful / "__pycache__").mkdir(parents=True)
    (careful / "__pycache__" / "m.pyc").write_text("keep me recoverable", encoding="utf-8")
    (careful / ".git-tidy.yaml").write_text("clean:\n  quarantine: true\n", encoding="utf-8")
    plain = workspace / "plain"
    (plain / "__pycache__").mkdir(parents=True)

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (careful / "__pycache__").exists()
    assert not (plain / "__pycache__").exists()
    quarantined = list(
        (workspace / gt.QUARANTINE_DIRNAME).glob("*/files/careful/__pycache__/m.pyc")
    )
    assert quarantined, "the deeper config asked for this one to be recoverable"
    assert not list((workspace / gt.QUARANTINE_DIRNAME).glob("*/files/plain/*")), (
        "and only that one"
    )


# --------------------------------------------------------------------------- #
# Round thirteen of review
# --------------------------------------------------------------------------- #


def test_quitting_keeps_branch_work_already_done(workspace: Path, remote: Path):
    """Every step that accumulates must hand its work to the Quit, not only clean."""
    repo = workspace / "repo"
    for name in ("one", "two", "three"):
        make_gone_branch(repo, remote, name)

    replies = iter(["y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    with pytest.raises(gt.Quit) as stopped:
        gt.prune_branches(repo, "repo", config(), decider, fetched=True)

    assert [a for a in stopped.value.done if a.applied], "the one agreed to is reported"


def test_quitting_keeps_trash_already_swept(workspace: Path):
    for name in ("lalalalala.log", "lalalalalala.log", "lalalog.log"):
        junk = workspace / name
        junk.write_text("x", encoding="utf-8")
        age(junk, 30)

    replies = iter(["y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    cfg = config(trash={"enabled": True, "patterns": ["lala*"]})
    with pytest.raises(gt.Quit) as stopped:
        gt.sweep_trash(workspace, cfg, decider, _holding(workspace), [])

    assert [a for a in stopped.value.done if a.applied]


def test_quitting_keeps_restores_already_made(workspace: Path):
    repo = workspace / "repo"
    for name in ("a", "b"):
        cache = repo / name / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "m.pyc").write_text(name, encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    holding.write_manifest()

    replies = iter(["y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    with pytest.raises(gt.Quit) as stopped:
        gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", decider)

    assert [a for a in stopped.value.done if a.applied]


# --------------------------------------------------------------------------- #
# Round fourteen of review
# --------------------------------------------------------------------------- #


def test_a_directory_about_to_go_whole_gets_the_last_word(workspace: Path):
    """Its own config would otherwise be deleted along with what it protects."""
    protected = workspace / "dist"
    protected.mkdir()
    (protected / "important.bin").write_text("not really build output", encoding="utf-8")
    (protected / ".git-tidy.yaml").write_text("clean:\n  enabled: false\n", encoding="utf-8")
    ordinary = workspace / "elsewhere" / "dist"
    ordinary.mkdir(parents=True)

    (workspace / ".git-tidy.yaml").write_text("clean:\n  builds: true\n", encoding="utf-8")
    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (protected / "important.bin").is_file(), "it said not to"
    assert not ordinary.exists()


# --------------------------------------------------------------------------- #
# Round fifteen of review
# --------------------------------------------------------------------------- #


def test_a_case_only_difference_still_protects_a_tracked_path(workspace: Path):
    """On macOS and Windows the index spelling need not match the disk."""
    repo = workspace / "repo"
    (repo / "Dist").mkdir()
    (repo / "Dist" / "committed.txt").write_text("real content", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "track Dist")

    tracked = gt.tracked_paths(gt.Git(repo))
    assert gt._protected(repo / "Dist", repo, [], tracked)
    # The same directory reached by the other spelling is the same directory.
    assert gt._protected(repo / "dist", repo, [], tracked)


# --------------------------------------------------------------------------- #
# Round sixteen of review
# --------------------------------------------------------------------------- #


def test_a_failed_rebase_gives_the_stashed_work_back(workspace: Path, remote: Path, tmp_path: Path):
    """Otherwise "nothing changed" is untrue: the work sits in an unasked-for stash."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "README.md", "theirs\n")
    git(tmp_path / "other", "push", "-q")

    repo = workspace / "repo"
    commit(repo, "README.md", "mine\n")
    git(repo, "fetch", "-q", "origin")
    (repo / "notes.txt").write_text("work in progress\n", encoding="utf-8")

    cfg = config(sync={"diverged": "rebase", "stash": True, "switch": "always"})
    actions = gt.sync_repo(repo, "repo", cfg, run())

    failed = [a for a in actions if a.error]
    assert failed and "nothing changed" in failed[0].error
    assert git(repo, "stash", "list") == "", "the stash was put back"
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "work in progress\n"


# --------------------------------------------------------------------------- #
# Round seventeen of review
# --------------------------------------------------------------------------- #


def test_a_mixed_batch_is_not_summarised_with_one_verb(workspace: Path):
    """Some quarantined, some deleted: the roll-up must not pick one for both."""
    stream = io.StringIO()
    printer = gt.Printer(stream, quiet=False, color=False)

    printer.batch(
        [
            gt.Action("remove", "repo", "a", "removed", size=10, applied=True),
            gt.Action("remove", "repo", "b", "removed", size=10, applied=True),
            gt.Action(
                "remove", "repo", "c", "quarantined", size=10, applied=True, quarantined=True
            ),
            gt.Action(
                "remove", "repo", "d", "quarantined", size=10, applied=True, quarantined=True
            ),
        ]
    )
    joined = stream.getvalue()
    assert "removed" in joined and "quarantined" in joined
    assert joined.count("2 paths") == 2, "two groups, one per outcome"


# --------------------------------------------------------------------------- #
# Round eighteen of review
# --------------------------------------------------------------------------- #


def test_keep_protects_a_file_inside_a_matched_directory(workspace: Path):
    """Removing the parent whole would take what keep was written to protect."""
    repo = workspace / "repo"
    (repo / "dist").mkdir()
    (repo / "dist" / "important.dat").write_text("keep me", encoding="utf-8")
    (repo / "dist" / "junk.bin").write_bytes(b"0" * 32)

    cfg = config(clean={"builds": True, "keep": ["dist/important.dat"]})
    gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert (repo / "dist" / "important.dat").is_file()


def test_keep_protects_inside_a_loose_directory_too(workspace: Path):
    loose = workspace / "loose" / "dist"
    loose.mkdir(parents=True)
    (loose / "important.dat").write_text("keep me", encoding="utf-8")
    (workspace / ".git-tidy.yaml").write_text(
        'clean:\n  builds: true\n  keep: ["loose/dist/important.dat"]\n', encoding="utf-8"
    )
    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (loose / "important.dat").is_file()


# --------------------------------------------------------------------------- #
# Round nineteen of review
# --------------------------------------------------------------------------- #


def test_a_successful_stash_says_where_the_work_went(workspace: Path, remote: Path, tmp_path: Path):
    """Leaving it stashed is the design; leaving it stashed silently is not."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")

    repo = workspace / "repo"
    git(repo, "fetch", "-q", "origin")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    actions = gt.sync_repo(repo, "repo", gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force"), run())
    applied = [a for a in actions if a.applied and "stash" in a.kind]
    assert applied and "git stash pop" in applied[0].detail
    assert "git-tidy: repo" in git(repo, "stash", "list")


# --------------------------------------------------------------------------- #
# Round twenty of review
# --------------------------------------------------------------------------- #


def test_a_swept_directorys_contents_are_not_reported_separately(workspace: Path):
    """The dry run must match the apply that follows it."""
    junk = workspace / "loose" / "lalalalala"
    junk.mkdir(parents=True)
    (junk / "lalalog.log").write_text("x", encoding="utf-8")
    age(junk / "lalalog.log", 30)
    age(junk, 30)

    cfg = config(trash={"enabled": True, "scope": "workspace", "dirs": True, "patterns": ["lala*"]})
    dry = gt.sweep_trash(workspace, cfg, run(gt.DRY), _holding(workspace), [])
    assert len(dry) == 1 and dry[0].target.endswith("lalalalala")

    applied = gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])
    assert len(applied) == len(dry), "the dry run predicted exactly this"
    assert not junk.exists()


# --------------------------------------------------------------------------- #
# Round twenty-one of review
# --------------------------------------------------------------------------- #


def test_a_torn_journal_record_does_not_cost_the_rest(workspace: Path):
    """The kill that stopped the sweep can tear the line it was writing."""
    repo = workspace / "repo"
    for name in ("a", "b"):
        cache = repo / name / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "m.pyc").write_text(name, encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)

    journal = workspace / gt.QUARANTINE_DIRNAME / "stamp" / gt.JOURNAL_NAME
    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"from": "/half-written')  # power went out here

    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert [a for a in actions if a.applied], "the intact records still restore"
    assert any("unreadable" in a.detail for a in actions), "and the loss is stated"
    assert (repo / "a" / "__pycache__" / "m.pyc").is_file()


# --------------------------------------------------------------------------- #
# Round twenty-two of review
# --------------------------------------------------------------------------- #


def test_a_switch_that_would_replace_a_local_env_is_refused(workspace: Path):
    """git replaces ignored files during a checkout without a word."""
    repo = workspace / "repo"
    # main tracks .env; here it is ignored and holds local credentials.
    (repo / ".env").write_text("SECRET=production\n", encoding="utf-8")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-q", "-m", "add .env to main")
    git(repo, "switch", "-q", "-c", "side")
    git(repo, "rm", "-q", "--cached", ".env")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore .env on side")
    (repo / ".env").write_text("SECRET=mine, and the only copy\n", encoding="utf-8")

    actions = gt.sync_repo(repo, "repo", config(), run())
    assert (repo / ".env").read_text(encoding="utf-8") == "SECRET=mine, and the only copy\n"
    assert gt.current_branch(gt.Git(repo)) == "side"
    assert any("would be replaced" in a.detail for a in actions)


def test_an_ordinary_ignored_file_does_not_block_a_switch(workspace: Path):
    """Only what ignored_keep names is worth stopping for."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore logs")
    git(repo, "switch", "-q", "-c", "side")
    (repo / "build.log").write_text("noise", encoding="utf-8")

    gt.sync_repo(repo, "repo", config(), run())
    assert gt.current_branch(gt.Git(repo)) == "main"


# --------------------------------------------------------------------------- #
# Round twenty-three of review
# --------------------------------------------------------------------------- #


def test_submodule_init_does_not_move_an_existing_checkout(workspace: Path, tmp_path: Path):
    """ "init" means init. Moving a checked-out submodule is what "update" is for."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    commit(inner, "lib.txt", "v1\n")

    repo = workspace / "repo"
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    git(repo, "commit", "-q", "-m", "add submodule")
    # Move the submodule somewhere of its own, as a person would while working.
    commit(inner, "lib.txt", "v2\n")
    git(repo / "vendor", "fetch", "-q", "origin")
    git(repo / "vendor", "checkout", "-q", "origin/main")
    moved = git(repo / "vendor", "rev-parse", "HEAD")

    gt._sync_submodules(gt.Git(repo), "repo", config(sync={"submodules": "init"})["sync"], run())
    assert git(repo / "vendor", "rev-parse", "HEAD") == moved, "left where it was"


def test_a_directory_can_turn_trash_off_for_itself(workspace: Path):
    loose = workspace / "keepme"
    loose.mkdir()
    junk = loose / "lalalalala.log"
    junk.write_text("x", encoding="utf-8")
    age(junk, 30)
    (loose / ".git-tidy.yaml").write_text("trash:\n  enabled: false\n", encoding="utf-8")
    (workspace / ".git-tidy.yaml").write_text(
        "trash:\n  enabled: true\n  scope: workspace\n", encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "trash", "--apply"])
    assert junk.is_file(), "the directory said no"


def test_a_credential_inside_a_disposable_directory_is_lifted_out_of_it(workspace: Path):
    """The promise trash makes, kept by clean — without keeping the cache too.

    The credential goes to quarantine and the directory around it really goes:
    moving a whole node_modules aside because one file in it matched reclaimed
    nothing at all, which is the one thing the tool exists to do.
    """
    repo = workspace / "repo"
    cache = repo / ".terraform"
    cache.mkdir()
    (cache / "provider.bin").write_bytes(b"0" * 64)
    (cache / "client.pem").write_text("PRIVATE KEY", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    actions = gt.clean_tree(
        repo,
        "repo",
        config(),
        run(),
        gt.Git(repo),
        None,
        sensitive=gt.DEFAULTS["trash"]["sensitive"],
        holding=holding,
    )
    assert (cache / "client.pem").read_text(encoding="utf-8") == "PRIVATE KEY"
    assert not (cache / "provider.bin").exists(), "the disposable part is reclaimed"
    assert any("keeping client.pem" in a.detail for a in actions), actions


# --------------------------------------------------------------------------- #
# Round twenty-four of review
# --------------------------------------------------------------------------- #


def test_a_fast_forward_that_would_replace_a_local_env_is_refused(
    workspace: Path, remote: Path, tmp_path: Path
):
    """git merge overwrites ignored files as silently as a checkout does."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore .env")
    git(repo, "push", "-q")
    (repo / ".env").write_text("SECRET=mine, and the only copy\n", encoding="utf-8")

    # Upstream starts tracking the very path this checkout ignores.
    git(tmp_path, "clone", "-q", str(remote), "other")
    other = tmp_path / "other"
    (other / ".env").write_text("SECRET=theirs\n", encoding="utf-8")
    git(other, "add", "-f", ".env")
    git(other, "commit", "-q", "-m", "track .env upstream")
    git(other, "push", "-q")
    git(repo, "fetch", "-q", "origin")

    actions = gt.sync_repo(repo, "repo", config(), run())
    assert (repo / ".env").read_text(encoding="utf-8") == "SECRET=mine, and the only copy\n"
    assert any("would be replaced" in a.detail for a in actions)


# --------------------------------------------------------------------------- #
# Round twenty-five of review
# --------------------------------------------------------------------------- #


def test_two_processes_cannot_claim_the_same_quarantine(workspace: Path, monkeypatch):
    """Checking a name is free and then using it leaves a gap both can walk through."""
    root = workspace / gt.QUARANTINE_DIRNAME
    first = gt._free_stamp(root)
    assert (root / first).is_dir(), "claimed by creating it, not by looking"

    # A second process, in the same second, gets a different one.
    second = gt._free_stamp(root)
    assert second != first
    assert (root / second).is_dir()


def test_a_reclaimed_stamp_does_not_strand_the_other_sweep(workspace: Path):
    root = workspace / gt.QUARANTINE_DIRNAME
    (workspace / "one.pyc").write_text("first", encoding="utf-8")
    (workspace / "two.pyc").write_text("second", encoding="utf-8")

    a, b = gt.Quarantine(root, workspace), gt.Quarantine(root, workspace)
    assert a.stamp != b.stamp
    a.take(workspace / "one.pyc")
    b.take(workspace / "two.pyc")
    a.write_manifest()
    b.write_manifest()

    gt.restore(root, a.stamp, run())
    gt.restore(root, b.stamp, run())
    assert (workspace / "one.pyc").read_text(encoding="utf-8") == "first"
    assert (workspace / "two.pyc").read_text(encoding="utf-8") == "second"


# --------------------------------------------------------------------------- #
# Round twenty-six of review
# --------------------------------------------------------------------------- #


def test_a_dry_run_leaves_no_quarantine_behind(workspace: Path):
    """Claiming the directory eagerly would contradict "nothing was changed"."""
    junk = workspace / "lalalalala.log"
    junk.write_text("x", encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text("trash:\n  enabled: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "run"])
    assert not (workspace / gt.QUARANTINE_DIRNAME).exists()
    assert junk.is_file()


def test_the_quarantine_is_claimed_on_the_first_move(workspace: Path):
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace)
    assert not (workspace / gt.QUARANTINE_DIRNAME).exists(), "nothing yet"

    (workspace / "one.pyc").write_text("x", encoding="utf-8")
    holding.take(workspace / "one.pyc")
    assert holding.dir.is_dir(), "claimed when it was needed"


# --------------------------------------------------------------------------- #
# Round twenty-seven of review
# --------------------------------------------------------------------------- #


def test_an_ignored_credential_file_is_quarantined_not_deleted(workspace: Path):
    """The guarantee covers a single file as much as a directory holding one."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("*.txt\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore txt")
    (repo / "api-token.txt").write_text("the only copy", encoding="utf-8")
    (repo / "notes.txt").write_text("disposable", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    actions = gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo), holding)
    assert not (repo / "api-token.txt").exists()
    assert not (repo / "notes.txt").exists()
    assert (holding.dir / gt.CONTENT_DIRNAME / "repo" / "api-token.txt").read_text(
        encoding="utf-8"
    ) == "the only copy"
    assert any(a.quarantined and "api-token" in a.target for a in actions)


# --------------------------------------------------------------------------- #
# Round twenty-eight of review
# --------------------------------------------------------------------------- #


def test_a_credential_matched_by_a_file_pattern_is_quarantined(workspace: Path):
    """The guarantee cannot depend on which mechanism found the file."""
    repo = workspace / "repo"
    (repo / "api-token.tfplan").write_text("the only copy", encoding="utf-8")
    (repo / "module.tfplan").write_text("disposable", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    gt.clean_tree(
        repo,
        "repo",
        config(),
        run(),
        gt.Git(repo),
        None,
        sensitive=gt.DEFAULTS["trash"]["sensitive"],
        holding=holding,
    )
    assert not (repo / "module.tfplan").exists()
    assert (holding.dir / gt.CONTENT_DIRNAME / "repo" / "api-token.tfplan").read_text(
        encoding="utf-8"
    ) == "the only copy"


def test_a_loose_credential_is_quarantined_too(workspace: Path):
    loose = workspace / "loose"
    loose.mkdir()
    (loose / "api-token.tfplan").write_text("the only copy", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    found = list((workspace / gt.QUARANTINE_DIRNAME).glob("*/files/loose/api-token.tfplan"))
    assert found and found[0].read_text(encoding="utf-8") == "the only copy"


# --------------------------------------------------------------------------- #
# Round twenty-nine of review
# --------------------------------------------------------------------------- #


def test_a_directory_being_swept_is_governed_by_its_own_config(workspace: Path):
    """It is the thing about to go, so its own .git-tidy.yaml decides."""
    loose = workspace / "loose"
    junk = loose / "lalalalala"
    junk.mkdir(parents=True)
    (junk / ".git-tidy.yaml").write_text("trash:\n  enabled: false\n", encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text(
        'trash:\n  enabled: true\n  scope: workspace\n  dirs: true\n  patterns: ["lala*"]\n',
        encoding="utf-8",
    )

    gt.main(["-C", str(workspace), "trash", "--apply"])
    assert junk.is_dir(), "it said not to"


# --------------------------------------------------------------------------- #
# Round thirty of review
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "own,survives",
    [
        ("trash:\n  enabled: false\n", True),
        ("trash:\n  dirs: false\n", True),
        ("trash:\n  scope: root\n", True),
        ("trash:\n  min_age_days: 0\n", False),
    ],
)
def test_a_directorys_own_config_decides_whether_it_can_be_swept(
    workspace: Path, own: str, survives: bool
):
    """Every setting that governs sweeping, not one at a time."""
    junk = workspace / "loose" / "lalalalala"
    junk.mkdir(parents=True)
    (junk / "x").write_text("x", encoding="utf-8")
    (junk / ".git-tidy.yaml").write_text(own, encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text(
        'trash:\n  enabled: true\n  scope: workspace\n  dirs: true\n  patterns: ["lala*"]\n',
        encoding="utf-8",
    )

    gt.main(["-C", str(workspace), "trash", "--apply"])
    assert junk.exists() is survives


# --------------------------------------------------------------------------- #
# Round thirty-one of review
# --------------------------------------------------------------------------- #


def test_a_blocked_switch_never_stashes_first(workspace: Path):
    """Deciding not to switch after stashing would hide the work in a stash."""
    repo = workspace / "repo"
    (repo / ".env").write_text("SECRET=production\n", encoding="utf-8")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-q", "-m", "add .env to main")
    git(repo, "switch", "-q", "-c", "side")
    git(repo, "rm", "-q", "--cached", ".env")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore .env on side")
    (repo / ".env").write_text("SECRET=mine\n", encoding="utf-8")
    (repo / "README.md").write_text("work in progress\n", encoding="utf-8")

    forced = gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force")
    actions = gt.sync_repo(repo, "repo", forced, run())

    assert git(repo, "stash", "list") == "", "nothing was put aside"
    assert (repo / "README.md").read_text(encoding="utf-8") == "work in progress\n"
    assert (repo / ".env").read_text(encoding="utf-8") == "SECRET=mine\n"
    assert any("would be replaced" in a.detail for a in actions)


# --------------------------------------------------------------------------- #
# Round thirty-two of review
# --------------------------------------------------------------------------- #


def test_a_swept_directory_holding_a_credential_is_quarantined(workspace: Path):
    """Deleting the directory would take the credential with it."""
    junk = workspace / "loose" / "lalalalala"
    junk.mkdir(parents=True)
    (junk / "notes").write_text("disposable", encoding="utf-8")
    (junk / "id_rsa.pem").write_text("the only copy", encoding="utf-8")
    age(junk, 30)
    holding = _holding(workspace)

    cfg = config(
        trash={
            "enabled": True,
            "scope": "workspace",
            "dirs": True,
            "quarantine": False,
            "patterns": ["lala*"],
        }
    )
    actions = gt.sweep_trash(workspace, cfg, run(), holding, [])
    assert not junk.exists()
    assert (holding.dir / gt.CONTENT_DIRNAME / "loose" / "lalalalala" / "id_rsa.pem").is_file()
    assert actions and "contains" in actions[0].detail


# --------------------------------------------------------------------------- #
# Round thirty-three of review
# --------------------------------------------------------------------------- #


def test_a_deeper_sensitive_list_protects_a_loose_credential(workspace: Path):
    """The guarantee has to follow the config that governs the path."""
    loose = workspace / "loose"
    loose.mkdir()
    (loose / "vault.tfplan").write_text("the only copy", encoding="utf-8")
    (loose / ".git-tidy.yaml").write_text('trash:\n  sensitive: ["vault*"]\n', encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    found = list((workspace / gt.QUARANTINE_DIRNAME).glob("*/files/loose/vault.tfplan"))
    assert found and found[0].read_text(encoding="utf-8") == "the only copy"


# --------------------------------------------------------------------------- #
# Round thirty-four of review
# --------------------------------------------------------------------------- #


def test_a_dry_run_separates_what_would_move_from_what_would_go(workspace: Path, capsys):
    """Quarantined bytes stay inside the workspace; calling them freed is untrue."""
    junk = workspace / "lalalalala.log"
    junk.write_text("x" * 4096, encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text(
        "trash:\n  enabled: true\n  min_age_days: 7\n", encoding="utf-8"
    )
    gt.main(["-C", str(workspace), "trash"])
    out = capsys.readouterr().out
    assert "would move to quarantine, not reclaimed" in out
    assert "to free" not in out


# --------------------------------------------------------------------------- #
# Confirmation round
# --------------------------------------------------------------------------- #


def test_trash_protects_a_tracked_path_whose_case_differs(tmp_path: Path):
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q", "-b", "main")
    space = outer / "space"
    space.mkdir()
    (space / "Lalalalala.log").write_text("committed", encoding="utf-8")
    git(outer, "add", "-A")
    git(outer, "commit", "-q", "-m", "track it")
    age(space / "Lalalalala.log", 30)

    cfg = config(trash={"enabled": True, "patterns": ["lala*", "Lala*"]})
    actions = gt.sweep_trash(space, cfg, run(), _holding(space), [])
    assert (space / "Lalalalala.log").is_file()
    assert actions == []


def test_a_directory_can_drop_the_pattern_that_matched_it(workspace: Path):
    """Its own config decides whether it is an artefact at all."""
    kept = workspace / "loose" / "__pycache__"
    kept.mkdir(parents=True)
    (kept / "m.pyc").write_text("x", encoding="utf-8")
    (kept / ".git-tidy.yaml").write_text("clean:\n  dirs: []\n  files: []\n", encoding="utf-8")
    ordinary = workspace / "other" / "__pycache__"
    ordinary.mkdir(parents=True)

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert kept.is_dir(), "it said it is not an artefact"
    assert not ordinary.exists()


def test_submodule_init_reports_nothing_when_there_is_nothing_to_do(
    workspace: Path, tmp_path: Path
):
    """A dry run must not promise work that will not happen."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    commit(inner, "lib.txt")

    repo = workspace / "repo"
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    git(repo, "commit", "-q", "-m", "add submodule")

    cfg = config(sync={"submodules": "init"})["sync"]
    assert gt._sync_submodules(gt.Git(repo), "repo", cfg, run(gt.DRY)) == []


# --------------------------------------------------------------------------- #
# Round thirty-seven of review
# --------------------------------------------------------------------------- #


def test_keep_cannot_be_overridden_by_an_explicit_pattern(workspace: Path):
    """keep says never swept; patterns win over the heuristics, not over that."""
    protected = workspace / "notes.md"
    protected.write_text("mine", encoding="utf-8")
    age(protected, 30)

    cfg = config(trash={"enabled": True, "patterns": ["*.md"]})
    actions = gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])
    assert protected.is_file()
    assert actions == []


def test_a_dirty_submodule_is_left_alone(workspace: Path, tmp_path: Path):
    """git moves one whose edits happen not to conflict, so ask first."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    commit(inner, "lib.txt", "v1\n")

    repo = workspace / "repo"
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    git(repo, "commit", "-q", "-m", "add submodule")
    (repo / "vendor" / "lib.txt").write_text("work in progress\n", encoding="utf-8")

    cfg = config(sync={"submodules": "update"})["sync"]
    actions = gt._sync_submodules(gt.Git(repo), "repo", cfg, run())
    assert actions and actions[0].skipped and "uncommitted work" in actions[0].detail
    assert (repo / "vendor" / "lib.txt").read_text(encoding="utf-8") == "work in progress\n"


def test_a_restored_entry_leaves_the_manifest(workspace: Path, capsys):
    """Otherwise --list describes files that are no longer there."""
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    holding.write_manifest()

    gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert not (workspace / gt.QUARANTINE_DIRNAME / "stamp").exists(), "nothing left in it"

    gt.main(["-C", str(workspace), "restore", "--list"])
    assert "stamp" not in capsys.readouterr().out


def test_a_dry_run_says_quarantine_when_it_means_quarantine(workspace: Path):
    """ "would remove" for something that would only be moved is the wrong word."""
    loose = workspace / "loose"
    loose.mkdir()
    (loose / "api-token.tfplan").write_text("the only copy", encoding="utf-8")

    context = gt.Context(
        workspace,
        gt.ConfigResolver(workspace),
        run(gt.DRY),
        quiet_printer(),
        gt.find_repos(workspace, []),
        _holding(workspace),
    )
    actions = gt._outside_repos(context, config(), context.quarantine)
    token = [a for a in actions if "token" in a.target]
    assert token and token[0].quarantined
    assert "quarantine" in token[0].detail and "remove" not in token[0].detail


def test_a_deleted_upstream_is_named_not_shrugged_at(workspace: Path, remote: Path):
    """ "cannot compare with upstream" tells nobody what to do about it."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "push", "-q", "-u", "origin", "feature")
    git(remote, "branch", "-D", "feature")
    git(repo, "fetch", "-q", "--prune", "origin")

    actions = gt.sync_repo(repo, "repo", config(sync={"switch": "never"}), run())
    update = [a for a in actions if a.kind == "update"]
    assert update and "no longer exists" in update[0].detail
    assert gt._reason_of(update[0].detail) == "on a branch whose upstream was deleted"


# --------------------------------------------------------------------------- #
# Claude review, round one: parser parity
# --------------------------------------------------------------------------- #

PARITY_SCALARS = [
    "true",
    "false",
    "yes",
    "no",
    "on",
    "off",
    "Yes",
    "TRue",
    "y",
    "n",
    "null",
    "~",
    "0",
    "7",
    "-3",
    "1.5",
    ".5",
    "1e3",
    "1E3",
    "0755",
    "+5",
    "1_000",
    ".inf",
    ".nan",
    "-.inf",
    "0x1f",
    "0o17",
    '"quoted"',
    "'single'",
    "'it''s'",
    "plain",
    "plain with spaces",
    "release/*",
    ".env",
    '"has: colon"',
    "#notcomment",
    "a#b",
    "[]",
    "{}",
    "[a, b]",
    "{a: 1}",
    "",
]


@pytest.mark.parametrize("scalar", PARITY_SCALARS)
def test_the_fallback_parser_agrees_with_pyyaml(scalar):
    """Behaviour must not depend on whether PyYAML happens to be installed."""
    yaml = pytest.importorskip("yaml")
    for text in (f"k: {scalar}\n", f"k:\n  - {scalar}\n"):
        try:
            mine = gt._parse_yaml_subset(text, "<parity>")
        except gt.Failure:
            mine = "REJECTED"
        try:
            theirs = yaml.safe_load(text)
        except Exception:
            theirs = "REJECTED"
        assert repr(mine) == repr(theirs), f"{text!r}: {mine!r} vs {theirs!r}"


def test_an_unquoted_glob_is_refused_the_way_pyyaml_refuses_it():
    """`keep: *.pem` is a YAML alias. Accepting it here only when PyYAML is
    absent would make the config mean different things on different machines."""
    with pytest.raises(gt.Failure, match="quote it"):
        gt._parse_yaml_subset("clean:\n  keep: *.pem\n", "<test>")


def test_the_generated_config_quotes_what_needs_quoting():
    """Whatever init writes must read back as what it meant."""
    yaml = pytest.importorskip("yaml")
    text = gt.render_config({"clean": {"keep": ["*.pem", "release/*", "yes", "0755"]}}, "h")
    assert yaml.safe_load(text)["clean"]["keep"] == ["*.pem", "release/*", "yes", "0755"]
    assert gt._parse_yaml_subset(text, "<r>")["clean"]["keep"] == [
        "*.pem",
        "release/*",
        "yes",
        "0755",
    ]


# --------------------------------------------------------------------------- #
# Claude review, round one
# --------------------------------------------------------------------------- #


def test_quitting_keeps_loose_artefacts_already_removed(workspace: Path):
    """The loose-artefact pass accumulates too, and 'q' must not un-report it."""
    for name in ("one", "two"):
        (workspace / name / "__pycache__").mkdir(parents=True)
        (workspace / name / "__pycache__" / "m.pyc").write_text(name, encoding="utf-8")

    replies = iter(["y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    context = gt.Context(
        workspace,
        gt.ConfigResolver(workspace),
        decider,
        quiet_printer(),
        gt.find_repos(workspace, []),
        _holding(workspace),
    )
    with pytest.raises(gt.Quit) as stopped:
        gt._outside_repos(context, config(), context.quarantine)

    assert [a for a in stopped.value.done if a.applied], "the one agreed to is reported"


def test_quitting_keeps_ignored_paths_already_removed(workspace: Path):
    """clean.ignored's own list was outside the guard clean_tree provides."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("a/\nb/\nc/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    for name in ("a", "b", "c"):
        (repo / name).mkdir()
        (repo / name / "out.bin").write_bytes(b"0" * 1024)
    (workspace / ".git-tidy.yaml").write_text("clean:\n  ignored: true\n", encoding="utf-8")

    replies = iter(["y", "y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    context = gt.Context(
        workspace,
        gt.ConfigResolver(workspace),
        decider,
        quiet_printer(),
        [repo],
        _holding(workspace),
    )
    actions: list[gt.Action] = []
    with pytest.raises(gt.Quit) as stopped, gt.keeping(actions):
        gt._clean_repo(repo, "repo", context.config_for(repo), context, actions)

    removed = [a for a in stopped.value.done if a.applied]
    assert len(removed) == 2, "both agreed-to removals are reported"
    assert sum(a.size for a in removed) >= 2048


def test_quitting_keeps_expired_quarantines_already_deleted(workspace: Path):
    """Expiry deletes irreversibly, so an unrecorded deletion is the worst kind."""
    root = workspace / gt.QUARANTINE_DIRNAME
    long_ago = time.time() - 60 * 86400
    for name in ("one", "two"):
        stamp = root / name
        stamp.mkdir(parents=True)
        (stamp / gt.JOURNAL_NAME).write_text("{}\n", encoding="utf-8")
        (stamp / "file").write_text(name, encoding="utf-8")
        os.utime(stamp, (long_ago, long_ago))

    replies = iter(["y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    with pytest.raises(gt.Quit) as stopped:
        gt.expire_quarantines(root, 30, decider)

    assert [a for a in stopped.value.done if a.applied], "the deletion is reported"


def test_trash_refuses_a_directory_holding_a_repository(workspace: Path):
    """clean has always refused this; trash used to destroy it."""
    forgotten = workspace / "project.old"
    clone = forgotten / "clone"
    clone.mkdir(parents=True)
    git(clone, "init", "-q", "-b", "main")
    commit(clone, "work.txt", "never pushed anywhere")
    age(forgotten, 30)

    cfg = config(trash={"enabled": True, "dirs": True, "quarantine": False})
    actions = gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])
    assert (clone / ".git").is_dir(), "the repository and its commit survive"
    assert (clone / "work.txt").read_text(encoding="utf-8") == "never pushed anywhere"
    assert actions and actions[0].skipped and "git repository" in actions[0].detail


def test_a_bad_config_value_mid_run_still_leaves_a_report(workspace: Path, capsys):
    """The disk was already changed; losing the report would hide that."""
    (workspace / "loose" / ".ruff_cache").mkdir(parents=True)
    (workspace / "loose" / ".ruff_cache" / "big").write_bytes(b"0" * 4096)
    (workspace / ".git-tidy.yaml").write_text(
        'trash:\n  enabled: true\n  scope: "all"\n', encoding="utf-8"
    )

    assert gt.main(["-C", str(workspace), "run", "--apply", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["interrupted"] is True
    assert any(a["error"] and "trash.scope" in a["error"] for a in payload["actions"])
    assert any(a["kind"] == "remove" and a["applied"] for a in payload["actions"]), (
        "what was deleted before the failure is still reported"
    )


def test_force_does_not_destroy_a_vendored_repository(workspace: Path):
    """--force widens what may be deleted; it does not reach committed work.

    Setting clean.regenerable to everything turned off the guard against
    removing a directory with a repository inside it, so --force deleted a
    vendored checkout and its unpushed commits — while the summary was offering
    "--force does the ones it safely can".
    """
    repo = workspace / "repo"
    vendored = repo / "node_modules" / "dep"
    vendored.mkdir(parents=True)
    git(vendored, "init", "-q", "-b", "main")
    commit(vendored, "src.txt", "never pushed anywhere")
    (workspace / ".git-tidy.yaml").write_text("clean:\n  dependencies: true\n", encoding="utf-8")

    assert gt.main(["-C", str(workspace), "clean", "--apply", "--force"]) == 0
    assert (vendored / ".git").is_dir()
    assert (vendored / "src.txt").read_text(encoding="utf-8") == "never pushed anywhere"


def test_force_overrides_do_not_touch_regenerable():
    """The one guard no flag may lift."""
    assert "clean" not in gt.FORCE_OVERRIDES
    assert not any("git repository" in reason for reason in gt.FORCE_CAN_FIX)


def test_a_tool_cache_is_still_reclaimable_without_force(workspace: Path):
    """Removing the override must not cost the case it was meant for."""
    repo = workspace / "repo"
    cloned = repo / ".terraform" / "modules" / "vpc"
    cloned.mkdir(parents=True)
    git(cloned, "init", "-q", "-b", "main")

    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), None)
    assert not (repo / ".terraform").exists()


def test_a_forced_prune_without_a_fetch_says_what_to_do(workspace: Path, remote: Path):
    """ "other, see the lines marked -" is not an answer anybody can act on."""
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature", extra_commit=True)

    cfg = config(branches={"require_merged": False})
    actions = gt.prune_branches(repo, "repo", cfg, run(), fetched=False)
    assert actions and actions[0].skipped
    assert "run --force" in actions[0].detail
    assert gt._reason_of(actions[0].detail) == (
        "unmerged branches waiting on a fetch — use `run --force`"
    )


def test_a_local_only_branch_is_named_not_binned_as_other(workspace: Path):
    """A branch that was never pushed is a state, not a mystery."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "scratch")
    actions = gt.sync_repo(repo, "repo", config(sync={"switch": "never"}), run())
    update = [a for a in actions if a.detail == "no upstream"]
    assert update
    assert gt._reason_of(update[0].detail) == "on a local-only branch, never pushed"


def test_every_message_the_tool_produces_has_a_category():
    """Anything that lands in "other" is a message nobody can act on."""
    uncategorised = [
        detail
        for detail in (
            "no upstream",
            "no remote configured",
            "already up to date",
            "cannot compare with upstream",
            "kept: contains a git repository",
            "kept by ignored_keep",
            "linked worktree, left on side",
            "detached at abc1234",
        )
        if gt._reason_of(detail) == "other, see the lines marked -"
    ]
    assert uncategorised == []


def test_an_orphaned_worktree_is_recognised(workspace: Path, tmp_path: Path):
    """`git worktree prune` leaves the files and takes the admin directory.

    Every git command in what is left fails with "not a git repository: (null)",
    which is git's answer, not one a person can do anything with.
    """
    repo = workspace / "repo"
    side = workspace / "side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))
    # The parent forgets about it, as `git worktree prune` would after the
    # directory had been moved away and back.
    shutil.rmtree(repo / ".git" / "worktrees" / "side")

    assert gt.orphaned_worktree(side) is not None
    assert gt.orphaned_worktree(repo) is None

    context = gt.Context(
        workspace,
        gt.ConfigResolver(workspace),
        run(gt.DRY),
        quiet_printer(),
        [side],
        _holding(workspace),
    )
    # Only the step that reports orphans does; the others stay silent so one
    # broken directory is not counted once per step.
    assert gt._guarded(sync_repo_boom, side, context) == []
    context.report_orphans = True
    actions = gt._guarded(sync_repo_boom, side, context)
    assert actions and actions[0].skipped
    assert "orphaned worktree" in actions[0].detail
    assert gt._reason_of(actions[0].detail) == ("orphaned worktrees — the parent pruned them away")


def sync_repo_boom(_repo: Path) -> list[gt.Action]:
    raise AssertionError("an orphaned worktree must not reach the work function")


def test_a_healthy_worktree_is_not_mistaken_for_an_orphan(workspace: Path):
    repo = workspace / "repo"
    side = workspace / "side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))
    assert gt.orphaned_worktree(side) is None


# --------------------------------------------------------------------------- #
# Claude review, round two
# --------------------------------------------------------------------------- #


def test_the_quarantine_does_not_land_on_its_own_bookkeeping(workspace: Path):
    """A file called manifest.json used to be overwritten by git-tidy's own."""
    precious = workspace / "manifest.json"
    precious.write_text("IMPORTANT USER DATA", encoding="utf-8")
    (workspace / "journal.jsonl").write_text("also mine", encoding="utf-8")
    for path in (precious, workspace / "journal.jsonl"):
        age(path, 30)
    (workspace / ".git-tidy.yaml").write_text(
        'trash:\n  enabled: true\n  patterns: ["*.json", "*.jsonl"]\n  min_age_days: 7\n',
        encoding="utf-8",
    )

    gt.main(["-C", str(workspace), "trash", "--apply"])
    stamps = list((workspace / gt.QUARANTINE_DIRNAME).iterdir())
    assert len(stamps) == 1
    kept = stamps[0] / gt.CONTENT_DIRNAME / "manifest.json"
    assert kept.read_text(encoding="utf-8") == "IMPORTANT USER DATA"
    assert (stamps[0] / gt.CONTENT_DIRNAME / "journal.jsonl").read_text(
        encoding="utf-8"
    ) == "also mine"

    gt.main(["-C", str(workspace), "restore", "--apply"])
    assert precious.read_text(encoding="utf-8") == "IMPORTANT USER DATA"


def test_an_ancestor_and_what_lives_in_it_both_restore(workspace: Path):
    """Restoring a descendant first re-creates its parent and blocks the ancestor."""
    junk = workspace / "old.bak"
    (junk / ".terraform").mkdir(parents=True)
    (junk / ".terraform" / "creds.pem").write_text("KEY", encoding="utf-8")
    (junk / "notes.txt").write_text("notes", encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text(
        "trash:\n  enabled: true\n  scope: workspace\n  dirs: true\n  min_age_days: 7\n",
        encoding="utf-8",
    )

    gt.main(["-C", str(workspace), "run", "--apply"])
    assert gt.main(["-C", str(workspace), "restore", "--apply"]) == 0
    assert (junk / "notes.txt").read_text(encoding="utf-8") == "notes"
    assert (junk / ".terraform" / "creds.pem").read_text(encoding="utf-8") == "KEY"


def test_an_unreadable_manifest_falls_back_to_the_journal(workspace: Path, capsys):
    """This is the crash the journal exists to survive."""
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    gt.clean_tree(repo, "repo", config(), run(), gt.Git(repo), holding)
    holding.write_manifest()
    (workspace / gt.QUARANTINE_DIRNAME / "stamp" / gt.MANIFEST_NAME).write_text(
        "{ broken json", encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "restore", "--list"])
    assert "stamp" in capsys.readouterr().out
    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert [a for a in actions if a.applied]
    assert (repo / "__pycache__" / "m.pyc").is_file()


def test_a_loose_dependency_tree_is_left_alone(workspace: Path):
    """The rule the in-repository walk has always applied, outside one too."""
    loose = workspace / "scratch"
    (loose / "node_modules" / "pkg" / "dist").mkdir(parents=True)
    (loose / "node_modules" / "pkg" / "dist" / "bundle.js").write_text("x", encoding="utf-8")
    (loose / ".venv" / "lib" / "__pycache__").mkdir(parents=True)
    (loose / ".venv" / "lib" / "__pycache__" / "a.pyc").write_text("x", encoding="utf-8")
    (workspace / ".git-tidy.yaml").write_text("clean:\n  builds: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (loose / "node_modules" / "pkg" / "dist" / "bundle.js").is_file()
    assert (loose / ".venv" / "lib" / "__pycache__" / "a.pyc").is_file()


def test_trash_never_offers_a_symlink(workspace: Path):
    """Its size is its target's, and _guard refuses to follow it anyway."""
    real = workspace / "realdir"
    real.mkdir()
    (real / "blob").write_bytes(b"0" * 4096)
    link = workspace / "stuff.bak"
    link.symlink_to(real, target_is_directory=True)

    cfg = config(trash={"enabled": True, "scope": "workspace", "dirs": True, "min_age_days": 0})
    actions = gt.sweep_trash(workspace, cfg, run(gt.DRY), _holding(workspace), [])
    assert not [a for a in actions if "stuff.bak" in a.target]
    assert link.is_symlink() and (real / "blob").is_file()


def test_a_block_sequence_at_the_parent_column_parses():
    """The style yaml.dump() emits, and the commonest way anyone writes a list."""
    yaml = pytest.importorskip("yaml")
    text = 'exclude:\n- "archive/*"\njobs: 2\n'
    assert gt._parse_yaml_subset(text, "<t>") == yaml.safe_load(text)
    nested = "clean:\n  keep:\n  - a\n  - b\n"
    assert gt._parse_yaml_subset(nested, "<t>") == yaml.safe_load(nested)


def test_an_anchor_is_refused_rather_than_misread():
    """Without this it became the literal string '&upstream origin'."""
    with pytest.raises(gt.Failure, match="quote it"):
        gt._parse_yaml_subset("sync:\n  remote: &upstream origin\n", "<t>")


def test_expire_honours_the_stamp_it_was_given(workspace: Path):
    root = workspace / gt.QUARANTINE_DIRNAME
    long_ago = time.time() - 60 * 86400
    for name in ("20250101T000000Z", "20250202T000000Z"):
        stamp = root / name
        stamp.mkdir(parents=True)
        (stamp / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
        os.utime(stamp, (long_ago, long_ago))

    gt.main(["-C", str(workspace), "restore", "--expire", "20250101T000000Z", "--apply"])
    assert not (root / "20250101T000000Z").exists()
    assert (root / "20250202T000000Z").exists(), "only the one that was named"


def test_a_pruned_upstream_is_named_from_the_ref(workspace: Path, remote: Path):
    """rev-parse echoes the literal '@{upstream}' once the ref is pruned."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "push", "-q", "-u", "origin", "feature")
    git(remote, "branch", "-D", "feature")
    git(repo, "fetch", "-q", "--prune", "origin")

    actions = gt.sync_repo(repo, "repo", config(sync={"switch": "never"}), run())
    update = [a for a in actions if a.kind == "update"]
    assert update and "@{upstream}" not in update[0].detail
    assert "origin/feature" in update[0].detail


def test_config_answers_for_a_path_outside_the_workspace(tmp_path: Path, capsys):
    """The command exists to explain why a rule applied; it must use that tree."""
    elsewhere = tmp_path / "other"
    (elsewhere / "repo").mkdir(parents=True)
    (elsewhere / ".git-tidy.yaml").write_text("clean:\n  ignored: true\n", encoding="utf-8")
    here = tmp_path / "here"
    here.mkdir()

    gt.main(["-C", str(here), "config", str(elsewhere / "repo")])
    assert json.loads(capsys.readouterr().out)["clean"]["ignored"] is True


def test_a_rolled_up_batch_keeps_each_reason(workspace: Path):
    """Five paths skipped for four reasons must not all take the first one's."""
    stream = io.StringIO()
    printer = gt.Printer(stream, quiet=False, color=False)
    printer.batch(
        [
            gt.Action("ignored", "repo", "a", "kept by ignored_keep", skipped=True),
            gt.Action("ignored", "repo", "b", "declined: remove file", skipped=True),
            gt.Action("ignored", "repo", "c", "declined: remove file", skipped=True),
        ]
    )
    out = stream.getvalue()
    assert "kept by ignored_keep" in out
    assert "declined" in out
    assert "2 paths" in out, "the two declined ones are one group"


def test_a_rolled_up_dry_run_says_quarantine_when_it_means_it(workspace: Path):
    stream = io.StringIO()
    printer = gt.Printer(stream, quiet=False, color=False)
    printer.batch(
        [
            gt.Action("remove", "repo", "a", "would quarantine directory", quarantined=True),
            gt.Action("remove", "repo", "b", "would quarantine directory", quarantined=True),
        ]
    )
    assert "would quarantine" in stream.getvalue()


def test_init_reads_answers_from_a_pipe(tmp_path: Path, monkeypatch, capsys):
    """--ask was accepted and then ignored when stdin was not a terminal."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    answers = iter(["4", "y", "n", "n", "y", "n", "n", "y", "30"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    target = tmp_path / ".git-tidy.yaml"
    assert gt.main(["-C", str(tmp_path), "init", "--ask", "--path", str(tmp_path)]) == 0
    parsed = gt._parse_yaml_subset(target.read_text(encoding="utf-8"), "<init>")
    assert parsed["jobs"] == 4
    assert parsed["clean"]["ignored"] is True
    assert parsed["trash"] == {"enabled": True, "min_age_days": 30}


def test_ask_still_needs_a_terminal_for_the_other_commands(workspace: Path, monkeypatch):
    """Those prompt per change, and a pipe has nothing to answer with."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(gt.Failure, match="needs a terminal"):
        gt.main(["-C", str(workspace), "clean", "--ask"])


# --------------------------------------------------------------------------- #
# Claude review, round three
# --------------------------------------------------------------------------- #


def submodule_repo(workspace: Path, tmp_path: Path) -> Path:
    """A clone with a submodule whose worktree is dirty but whose gitlink is not.

    git status calls this dirty; git stash finds nothing to save. That is the
    gap _stash used to fall into.
    """
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    commit(inner, "lib.txt", "v1\n")
    repo = workspace / "repo"
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    git(repo, "commit", "-q", "-m", "add submodule")
    (repo / "vendor" / "lib.txt").write_text("edited\n", encoding="utf-8")
    return repo


def test_stash_reports_whether_it_actually_stashed(workspace: Path, tmp_path: Path):
    """git stash exits 0 saying "No local changes to save"."""
    repo = submodule_repo(workspace, tmp_path)
    assert gt.is_dirty(gt.Git(repo)), "git status calls this dirty"

    stashed, problem = gt._stash(gt.Git(repo), "repo")
    assert problem is None
    assert stashed is False, "nothing was saved, so nothing may be claimed"


def test_a_failed_move_never_pops_a_stash_it_did_not_make(workspace: Path, tmp_path: Path):
    """_unstash used to pop whatever was on top — which is the user's."""
    repo = submodule_repo(workspace, tmp_path)
    (repo / "mine.txt").write_text("my important wip\n", encoding="utf-8")
    git(repo, "add", "mine.txt")
    git(repo, "stash", "push", "-q", "-m", "my important wip")
    assert "my important wip" in git(repo, "stash", "list")

    git(repo, "switch", "-q", "-c", "side")
    gt.sync_repo(repo, "repo", gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force"), run())

    assert "my important wip" in git(repo, "stash", "list"), "somebody else's stash"


def test_a_move_that_stashed_nothing_does_not_say_it_did(workspace: Path, tmp_path: Path):
    repo = submodule_repo(workspace, tmp_path)
    git(repo, "switch", "-q", "-c", "side")

    actions = gt.sync_repo(repo, "repo", gt._merge(gt.DEFAULTS, gt.FORCE_OVERRIDES, "force"), run())
    moved = [a for a in actions if a.applied and "switch" in a.kind]
    assert moved and "git stash pop" not in moved[0].detail


def test_a_kept_directorys_contents_are_still_offered(workspace: Path):
    """It is not going anywhere, so what is inside it must still be considered."""
    old = workspace / "oldproj"
    clone = old / "clone"
    clone.mkdir(parents=True)
    git(clone, "init", "-q", "-b", "main")
    (old / "note.log").write_text("junk", encoding="utf-8")
    age(old, 30)
    age(old / "note.log", 30)

    cfg = config(
        trash={
            "enabled": True,
            "scope": "workspace",
            "dirs": True,
            "min_age_days": 7,
            "patterns": ["*.log", "oldproj"],
        }
    )
    actions = gt.sweep_trash(workspace, cfg, run(gt.DRY), _holding(workspace), [])
    assert any("note.log" in a.target for a in actions), "the file matched the user's own glob"
    assert any(a.skipped and "git repository" in a.detail for a in actions)


@pytest.mark.parametrize(
    "text",
    ["exclude: [a, , b]\n", "sync:\n  remote:\n", "exclude: [,]\n"],
)
def test_an_empty_scalar_does_not_crash_the_parser(text):
    """ "" is `in` every string, so the indicator test indexed an empty token."""
    with contextlib.suppress(gt.Failure):
        gt._parse_yaml_subset(text, "<t>")  # a Failure is fine; an IndexError is not


def test_an_unterminated_quote_is_refused(workspace: Path):
    """Read as a plain scalar it becomes a pattern that can never match."""
    with pytest.raises(gt.Failure, match="unterminated quote"):
        gt._parse_yaml_subset('clean:\n  keep:\n    - "docs/*\n', "<t>")


def test_home_is_refused_even_when_it_is_a_symlink(tmp_path: Path, monkeypatch):
    """Comparing a resolved path against an unresolved one let it through."""
    real = tmp_path / "realhome"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: link))

    with pytest.raises(gt.Failure, match="refusing to work on"):
        gt.resolve_workspace(str(real))
    with pytest.raises(gt.Failure, match="refusing to work on"):
        gt.resolve_workspace(str(link))


def test_the_summary_does_not_call_a_quarantined_path_removed(workspace: Path, capsys):
    """*.tfplan is disposable, but this one is named like a credential."""
    repo = workspace / "repo"
    (repo / "api-token.tfplan").write_text("KEY", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    out = capsys.readouterr().out
    assert "artefacts quarantined" in out
    assert "artefacts removed" not in out


def test_a_cache_holding_a_credential_is_removed_and_the_credential_kept(workspace: Path, capsys):
    """Both halves in one summary: the space back, the key still there."""
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "deploy.pem").write_text("KEY", encoding="utf-8")
    (repo / "__pycache__" / "m.pyc").write_bytes(b"0" * 4096)

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert "stayed in place" in capsys.readouterr().out
    assert not (repo / "__pycache__" / "m.pyc").exists(), "the disposable part goes"
    assert (repo / "__pycache__" / "deploy.pem").read_text(encoding="utf-8") == "KEY"


@pytest.mark.parametrize(
    "detail",
    [
        "uncommitted work in libs/lib",
        "main tracks .env, which is ignored here and would be replaced",
        "kept: contains .env",
        "kept: contains a git repository",
    ],
)
def test_these_messages_have_a_category(detail):
    assert gt._reason_of(detail) != "other, see the lines marked -"


def test_quitting_a_restore_leaves_an_honest_manifest(workspace: Path):
    """Otherwise the next restore reports a file already back as a failure."""
    for name in ("one", "two", "three"):
        (workspace / f"{name}.junk").write_text(name, encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    for name in ("one", "two", "three"):
        holding.take(workspace / f"{name}.junk")
    holding.write_manifest()

    replies = iter(["y", "q"])
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: next(replies))
    with pytest.raises(gt.Quit):
        gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", decider)

    left = gt._read_manifest(workspace / gt.QUARANTINE_DIRNAME / "stamp")["entries"]
    back = [p.name for p in workspace.glob("*.junk")]
    assert len(back) == 1
    assert not any(Path(e["from"]).name in back for e in left), "no claim on what is back"

    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert not [a for a in actions if a.error], "and the rest restore without a false failure"


def test_up_to_date_is_not_asserted_after_a_declined_fetch(
    workspace: Path, remote: Path, tmp_path: Path
):
    """Declining the fetch means this run checked nothing."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")

    repo = workspace / "repo"
    decider = gt.Decider(gt.ASK, stream=io.StringIO(), prompt_input=lambda _: "n")
    actions = gt.sync_repo(repo, "repo", config(), decider)
    update = [a for a in actions if a.kind == "update"]
    assert update and "as of the last fetch" in update[0].detail


def test_the_summary_does_not_count_a_stash_that_never_happened(
    workspace: Path, tmp_path: Path, capsys
):
    """The line said "switched"; the summary said "stashed and switched"."""
    repo = submodule_repo(workspace, tmp_path)
    git(repo, "switch", "-q", "-c", "side")
    (workspace / ".git-tidy.yaml").write_text(
        "sync:\n  stash: true\n  switch: always\n", encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "sync", "--apply"])
    out = capsys.readouterr().out
    assert "stashed and switched" not in out
    assert "branches switched" in out


# --------------------------------------------------------------------------- #
# Claude review, round four
# --------------------------------------------------------------------------- #


def test_a_tag_of_the_same_name_does_not_shadow_the_branch(
    workspace: Path, remote: Path, tmp_path: Path
):
    """git shortens refs/heads/main to "heads/main" once refs/tags/main exists."""
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")

    repo = workspace / "repo"
    git(repo, "tag", "main")

    assert gt.current_branch(gt.Git(repo)) == "main"
    assert [b.name for b in gt.list_branches(gt.Git(repo))] == ["main"]

    gt.sync_repo(repo, "repo", config(), run())
    assert (repo / "theirs.txt").is_file(), "it was fast-forwarded, not merely 'switched'"


def test_a_tag_of_the_same_name_does_not_defeat_branches_keep(workspace: Path, remote: Path):
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "release/1.0")
    git(repo, "tag", "release/1.0")

    actions = gt.prune_branches(repo, "repo", config(), run(), fetched=True)
    assert actions == [], "keep still protects it"
    # %(refname), not the short form — which is exactly the trap being tested.
    assert "refs/heads/release/1.0" in git(repo, "branch", "--format=%(refname)").split()


def test_a_default_sweep_is_counted(workspace: Path, capsys):
    """trash.quarantine defaults to true, so this is the ordinary case."""
    for name in ("aaaaaaaaaa.log", "qwrtplkjhgf.txt"):
        junk = workspace / name
        junk.write_text("x", encoding="utf-8")
        age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text("trash:\n  enabled: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "trash", "--apply"])
    assert "loose files quarantined" in capsys.readouterr().out


def test_quiet_prints_the_summary_it_promises(workspace: Path, capsys):
    """--quiet says "only print the summary", and printed nothing at all."""
    junk = workspace / "lalalalala.log"
    junk.write_text("x", encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text("trash:\n  enabled: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "trash", "--apply", "-q"])
    out = capsys.readouterr().out
    assert "Summary" in out
    assert "lalalalala.log" not in out, "and only the summary"


def test_clean_ignored_leaves_dependency_and_build_trees(workspace: Path):
    """.gitignore covers node_modules in nearly every repository."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("node_modules/\nbuild/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    for name in ("node_modules", "build"):
        (repo / name).mkdir()
        (repo / name / "x.bin").write_bytes(b"0" * 16)

    cfg = config(clean={"ignored": True})
    gt.clean_ignored(repo, "repo", cfg, run(), None, gt.Git(repo))
    assert (repo / "node_modules" / "x.bin").is_file()
    assert (repo / "build" / "x.bin").is_file()

    on = config(clean={"ignored": True, "dependencies": True, "builds": True})
    gt.clean_ignored(repo, "repo", on, run(), None, gt.Git(repo))
    assert not (repo / "node_modules").exists()
    assert not (repo / "build").exists()


def test_a_rebase_that_stashed_nothing_is_not_counted_as_one(
    workspace: Path, remote: Path, tmp_path: Path
):
    repo = submodule_repo(workspace, tmp_path)
    git(tmp_path, "clone", "-q", str(remote), "other")
    commit(tmp_path / "other", "theirs.txt")
    git(tmp_path / "other", "push", "-q")
    commit(repo, "mine.txt")
    git(repo, "fetch", "-q", "origin")

    cfg = config(sync={"diverged": "rebase", "stash": True})
    actions = gt._diverged(
        gt.Git(repo), "repo", "main", "origin/main", "1", "1", cfg["sync"], run()
    )
    done = [a for a in actions if a.applied]
    assert done and done[0].kind == "rebase", "no stash was made, so not stash+rebase"
    assert "git stash pop" not in done[0].detail


def test_a_torn_record_is_not_deleted_after_being_reported_as_kept(workspace: Path):
    """restore said "left in the quarantine" and then removed the directory."""
    for name in ("a.log", "torn.log"):
        (workspace / name).write_text(name, encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")
    holding.take(workspace / "a.log")
    holding.take(workspace / "torn.log")
    stamp = workspace / gt.QUARANTINE_DIRNAME / "stamp"
    # Only the first record survives the kill; the second line is half written.
    lines = (stamp / gt.JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    (stamp / gt.JOURNAL_NAME).write_text(lines[0] + '\n{"from": "/half', encoding="utf-8")

    actions = gt.restore(workspace / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert any("unreadable" in a.detail for a in actions)
    assert (stamp / gt.CONTENT_DIRNAME / "torn.log").is_file(), "left, as it was reported"


def test_a_byte_order_mark_does_not_change_what_a_config_means(tmp_path: Path):
    """An editor on Windows writes it; PyYAML strips it; this did not."""
    yaml = pytest.importorskip("yaml")
    text = "﻿jobs: 4\n"
    assert gt._parse_yaml_subset(text, "<t>") == yaml.safe_load(text)
    (tmp_path / ".git-tidy.yaml").write_text(text, encoding="utf-8")
    assert gt.ConfigResolver(tmp_path).for_path(tmp_path)["jobs"] == 4


def test_no_message_the_tool_can_produce_lands_in_other():
    """Swept out of the source, so the next uncategorised one fails here first.

    "other, see the lines marked -" is true and useless: it tells a person
    something needs them without telling them what.
    """
    source = (Path(__file__).resolve().parent.parent / "git_tidy.py").read_text(encoding="utf-8")
    category_names = {reason for _, reason in gt.REASONS} | {"other, see the lines marked -"}
    interesting = (
        "kept",
        "no ",
        "cannot",
        "already",
        "declined",
        "up to date",
        "checked out",
        "linked worktree",
        "staying on",
        "detached",
        "diverged",
        "uncommitted",
        "unreadable",
        "orphaned",
        "would be replaced",
        "not pushed",
        "no longer exists",
        "commits not in",
        "needs a fetch",
        "credential in",
        "in use by",
        "not a quarantine",
    )
    messages = {
        found
        for found in re.findall(r'(?:detail\s*=|,)\s*f?"([^"]{8,95})"', source)
        if found not in category_names
        and " " in found  # a message is a sentence; "kept_size" is a dict key
        and found != "{why}, contains {buried}"  # only ever on an applied action
        # git's own words, matched against a fetch error — never a detail line.
        and found not in gt.OFFLINE_SIGNS
        and any(word in found for word in interesting)
    }
    assert messages, "the sweep found nothing, so it is not testing anything"
    uncategorised = [m for m in messages if gt._reason_of(m) == "other, see the lines marked -"]
    assert uncategorised == []


# --------------------------------------------------------------------------- #
# Claude review, round five
# --------------------------------------------------------------------------- #


def test_a_commit_on_no_branch_is_not_walked_away_from(workspace: Path):
    """git warns on stderr and exits 0, so the warning was being discarded."""
    repo = workspace / "repo"
    git(repo, "checkout", "-q", "--detach")
    commit(repo, "only-here.txt", "on no branch")
    stranded = git(repo, "rev-parse", "HEAD")

    actions = gt.sync_repo(repo, "repo", config(), run())
    assert (repo / "only-here.txt").is_file()
    assert git(repo, "rev-parse", "HEAD") == stranded
    held = [a for a in actions if "on no branch" in a.detail]
    assert held and held[0].skipped
    assert gt._reason_of(held[0].detail) == "commits on a detached HEAD and no branch"


def test_a_detached_head_on_a_branch_still_switches(workspace: Path):
    """Detached at a commit a branch already reaches loses nothing."""
    repo = workspace / "repo"
    git(repo, "checkout", "-q", "--detach")
    gt.sync_repo(repo, "repo", config(), run())
    assert gt.current_branch(gt.Git(repo)) == "main"


def test_a_bare_repository_is_a_repository(workspace: Path, tmp_path: Path):
    """clone --bare leaves no .git entry, so every guard walked past it."""
    bare = workspace / "backup.old"
    git(workspace, "clone", "-q", "--bare", str(workspace / "repo"), "backup.old")
    age(bare, 30)

    assert gt.holds_git_data(bare)
    assert not gt.is_repo(bare), "it is not a checkout, but it is still a repository"

    cfg = config(trash={"enabled": True, "dirs": True, "quarantine": False, "min_age_days": 7})
    actions = gt.sweep_trash(workspace, cfg, run(), _holding(workspace), [])
    assert (bare / "objects").is_dir(), "the only copy of those commits"
    # Excluded when candidates are chosen, as every other repository is, so it
    # never becomes an action at all.
    assert not [a for a in actions if "backup.old" in a.target]


def test_a_bare_repository_inside_an_artefact_directory_survives(workspace: Path):
    repo = workspace / "repo"
    buried = repo / "node_modules" / "mirror"
    buried.mkdir(parents=True)
    git(repo / "node_modules", "clone", "-q", "--bare", str(repo), "mirror")

    cfg = config(clean={"dependencies": True})
    actions = gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert (buried / "objects").is_dir()
    assert any(a.skipped and "git repository" in a.detail for a in actions)


def test_doctor_reports_a_detached_head_without_a_remote(tmp_path: Path):
    """A repository with no remote is where an unpushed commit matters most."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    git(lonely, "init", "-q", "-b", "main")
    commit(lonely, "a.txt")
    git(lonely, "checkout", "-q", "--detach")

    actions = gt.doctor_repo(lonely, "lonely", config())
    assert any("detached" in a.detail for a in actions)
    assert any("no remote" in a.detail for a in actions)


def test_doctor_does_not_report_a_detached_head_twice(workspace: Path):
    repo = workspace / "repo"
    git(repo, "checkout", "-q", "--detach")
    actions = gt.doctor_repo(repo, "repo", config())
    assert len([a for a in actions if "detached" in a.detail]) == 1


def test_each_keep_rule_says_which_rule_it_was(workspace: Path):
    """Pointing at ignored_keep for something node_modules kept is a dead end."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("node_modules/\ndist/\n.env\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    for name in ("node_modules", "dist"):
        (repo / name).mkdir()
        (repo / name / "x").write_text("x", encoding="utf-8")
    (repo / ".env").write_text("SECRET", encoding="utf-8")

    actions = gt.clean_ignored(
        repo, "repo", config(clean={"ignored": True}), run(), None, gt.Git(repo)
    )
    by_target = {a.target: a.detail for a in actions}
    assert by_target[".env"] == "kept by ignored_keep"
    assert "dependency tree" in by_target["node_modules"]
    assert "build output" in by_target["dist"]
    assert (
        gt._reason_of(by_target["node_modules"]) == "dependency trees — clean.dependencies is off"
    )


def test_clean_ignored_does_not_shield_a_path_from_clean_dirs(workspace: Path):
    """Turning it on made the tool clean less, and blamed ignored_keep for it."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore build")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_bytes(b"0" * 8)

    (workspace / ".git-tidy.yaml").write_text(
        'clean:\n  ignored: true\n  extra_dirs: ["build"]\n', encoding="utf-8"
    )
    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (repo / "build").exists(), "clean.dirs still names it"


def test_a_dependency_name_in_clean_dirs_is_honoured(workspace: Path):
    repo = workspace / "repo"
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x").write_text("x", encoding="utf-8")

    cfg = config(clean={"extra_dirs": ["node_modules"]})
    actions = gt.clean_tree(repo, "repo", cfg, run(), gt.Git(repo), None)
    assert any(a.target == "node_modules" and a.applied for a in actions)


def test_declining_is_reported_as_declining(workspace: Path):
    """ "declined: switch from detached HEAD" is about the answer, not the HEAD."""
    assert gt._reason_of("declined: switch from detached HEAD") == "declined at the prompt"
    assert gt._reason_of("detached at abc1234") == "detached HEAD"


def test_a_remote_less_repository_is_counted_once(tmp_path: Path, capsys):
    lonely = tmp_path / "space" / "lonely"
    lonely.mkdir(parents=True)
    git(lonely, "init", "-q", "-b", "main")
    commit(lonely, "a.txt")

    gt.main(["-C", str(tmp_path / "space"), "run"])
    out = capsys.readouterr().out
    assert "1  no remote configured" in out
    assert "2  no remote configured" not in out


def test_config_merges_everything_above_the_path(tmp_path: Path, capsys):
    outer = tmp_path / "outer"
    (outer / "b" / "c").mkdir(parents=True)
    (outer / ".git-tidy.yaml").write_text("jobs: 7\nclean:\n  ignored: true\n", encoding="utf-8")
    (outer / "b" / ".git-tidy.yaml").write_text("clean:\n  builds: true\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    gt.main(["-C", str(elsewhere), "config", str(outer / "b" / "c")])
    merged = json.loads(capsys.readouterr().out)
    assert merged["jobs"] == 7, "the config two levels up still counts"
    assert merged["clean"]["ignored"] is True
    assert merged["clean"]["builds"] is True


def test_config_can_be_asked_from_inside_a_repository(workspace: Path, capsys):
    """It reads nothing and writes nothing; the checkout is where people ask."""
    assert gt.main(["-C", str(workspace / "repo"), "config"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == gt.DEFAULTS["jobs"]


def test_a_home_that_does_not_exist_is_a_message_not_a_traceback():
    with pytest.raises(gt.Failure, match="cannot work out what"):
        gt.resolve_workspace("~nosuchuser-git-tidy/git")


@pytest.mark.parametrize(
    "message,expected",
    [
        # Each of these matches more than one needle in REASONS, so the answer
        # depends on the order of the table. One of them was wrong for three
        # rounds; this pins the rest.
        ("declined: switch from detached HEAD", "declined at the prompt"),
        ("declined: remove directory", "declined at the prompt"),
        (
            "detached HEAD at abc1234, on no branch — switching would leave those "
            "commits reachable only through the reflog",
            "commits on a detached HEAD and no branch",
        ),
        ("detached at abc1234", "detached HEAD"),
        ("kept: contains a git repository", "directories holding a git repository"),
        ("kept: contains .env", "directories holding something that must not go"),
        (
            "orphaned worktree: /nowhere no longer exists, so git cannot work here.",
            "orphaned worktrees — the parent pruned them away",
        ),
        ("upstream origin/x no longer exists", "on a branch whose upstream was deleted"),
    ],
)
def test_an_ambiguous_message_resolves_to_the_right_category(message, expected):
    assert gt._reason_of(message) == expected


# --------------------------------------------------------------------------- #
# Claude review, round six
# --------------------------------------------------------------------------- #


def test_local_state_is_never_deleted_by_the_pattern_walk(workspace: Path):
    """clean.ignored refused this directory; clean.dirs took it minutes later.

    htmlcov, not .terraform: a path the user listed in clean.regenerable is
    declared rebuildable and its terraform.tfstate really is the backend
    pointer — see test_a_regenerable_cache_is_reclaimed_not_moved. Everywhere
    else clean.ignored_keep holds, and both walks have to honour it.
    """
    repo = workspace / "repo"
    cache = repo / "htmlcov"
    cache.mkdir()
    (cache / "provider.bin").write_bytes(b"0" * 32)
    (cache / "terraform.tfstate").write_text("the only copy", encoding="utf-8")
    (cache / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    (cache / "secrets.py").write_text("AWS_SECRET = 1", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    gt.clean_tree(
        repo,
        "repo",
        config(),
        run(),
        gt.Git(repo),
        None,
        sensitive=gt.DEFAULTS["trash"]["sensitive"],
        local_state=gt.DEFAULTS["clean"]["ignored_keep"],
        holding=holding,
    )
    assert not (cache / "provider.bin").exists(), "the disposable part is reclaimed"
    assert (cache / "terraform.tfstate").read_text(encoding="utf-8") == "the only copy"
    assert (cache / "id_rsa").is_file()
    # secrets.* is an ignored_keep entry, and the source exemption belongs to
    # trash.sensitive alone: a gitignored secrets.py is not committed source.
    assert (cache / "secrets.py").read_text(encoding="utf-8") == "AWS_SECRET = 1"


def test_a_regenerable_cache_is_reclaimed_not_moved(workspace: Path):
    """The whole point of the tool is the space, and .terraform is regenerable.

    Every .terraform holds a terraform.tfstate — the backend pointer, which
    `terraform init` writes again in seconds, not the state itself. Treating it
    as irreplaceable turned every cache into a rename into a directory in the
    same workspace, so a run that reported 40 GB reclaimed freed nothing.
    """
    repo = workspace / "repo"
    (repo / ".terraform").mkdir()
    (repo / ".terraform" / "terraform.tfstate").write_text("state", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (repo / ".terraform").exists()
    assert not list((workspace / gt.QUARANTINE_DIRNAME).glob("*/files/repo/.terraform/*"))


def test_a_tfstate_outside_a_regenerable_cache_is_still_quarantined(workspace: Path):
    """htmlcov is disposable, but it is not something a tool rebuilds state into."""
    repo = workspace / "repo"
    (repo / "htmlcov").mkdir()
    (repo / "htmlcov" / "terraform.tfstate").write_text("state", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    kept = repo / "htmlcov" / "terraform.tfstate"
    assert kept.read_text(encoding="utf-8") == "state", "left exactly where it was"


def test_a_private_key_is_never_hard_deleted_even_inside_a_regenerable_cache(workspace: Path):
    """trash.sensitive holds everywhere: nothing rebuilds a private key."""
    repo = workspace / "repo"
    (repo / ".terraform").mkdir()
    (repo / ".terraform" / "id_rsa").write_text("PRIVATE", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert (repo / ".terraform" / "id_rsa").read_text(encoding="utf-8") == "PRIVATE"


def test_a_switch_sees_an_env_inside_an_ignored_directory(workspace: Path):
    """--directory collapses the parent, so the guard never looked inside."""
    repo = workspace / "repo"
    (repo / "config").mkdir()
    (repo / "config" / ".env").write_text("MAIN_VERSION", encoding="utf-8")
    git(repo, "add", "-f", "config/.env")
    git(repo, "commit", "-q", "-m", "track it on main")
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "rm", "-q", "--cached", "config/.env")
    (repo / ".gitignore").write_text("config/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore it here")
    (repo / "config" / ".env").write_text("MY-ONLY-LOCAL-SECRET", encoding="utf-8")

    actions = gt.sync_repo(repo, "repo", config(), run())
    assert (repo / "config" / ".env").read_text(encoding="utf-8") == "MY-ONLY-LOCAL-SECRET"
    assert any("would be replaced" in a.detail for a in actions)


def test_the_clobber_guard_uses_the_ref_the_switch_will_take(workspace: Path, remote: Path):
    """With no local trunk, `cat-file -e main:.env` fails and waved it through."""
    repo = workspace / "repo"
    (repo / ".env").write_text("FROM_MAIN", encoding="utf-8")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-q", "-m", "track .env")
    git(repo, "push", "-q")
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "rm", "-q", "--cached", ".env")
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    git(repo, "branch", "-D", "main")  # only origin/main is left
    (repo / ".env").write_text("MY-ONLY-LOCAL-SECRET", encoding="utf-8")

    actions = gt.sync_repo(repo, "repo", config(), run())
    assert (repo / ".env").read_text(encoding="utf-8") == "MY-ONLY-LOCAL-SECRET"
    assert any("would be replaced" in a.detail for a in actions)


def test_doctor_reports_unpushed_commits_on_a_gone_branch(workspace: Path, remote: Path):
    """The one case where they exist nowhere else."""
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature", extra_commit=True)
    actions = gt.doctor_repo(repo, "repo", config())
    assert any("not pushed" in a.detail for a in actions)


def test_clean_ignored_matches_git_clean_xd(workspace: Path):
    """--directory hides ignored files inside an untracked directory."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore logs")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("untracked", encoding="utf-8")
    (repo / "build" / "x.log").write_text("ignored", encoding="utf-8")

    assert "build/x.log" in gt.ignored_paths(gt.Git(repo))
    gt.clean_ignored(repo, "repo", config(clean={"ignored": True}), run(), None, gt.Git(repo))
    assert not (repo / "build" / "x.log").exists(), "git clean -Xd would take it"
    assert (repo / "build" / "out.o").is_file(), "and would leave this"


def test_a_closed_pipe_is_not_a_traceback(workspace: Path):
    """`| head` closes stdout while the run is still printing."""
    (workspace / "repo" / "__pycache__").mkdir()
    result = subprocess.run(
        [sys.executable, str(Path(gt.__file__)), "-C", str(workspace), "clean", "-v"],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        stdin=subprocess.DEVNULL,
    )
    assert "Traceback" not in result.stderr


def test_config_with_no_path_inside_a_checkout_sees_the_configs_above(
    workspace: Path, capsys, monkeypatch
):
    (workspace / ".git-tidy.yaml").write_text("jobs: 7\n", encoding="utf-8")
    monkeypatch.chdir(workspace / "repo")
    gt.main(["config"])
    assert json.loads(capsys.readouterr().out)["jobs"] == 7


def test_a_bad_setting_after_the_fetch_still_reports_the_fetch(workspace: Path):
    repo = workspace / "repo"
    actions = gt.sync_repo(repo, "repo", config(sync={"submodules": "updat"}), run())
    assert any(a.kind == "fetch" and a.applied for a in actions)
    assert any(a.error for a in actions)


def test_two_yaml_documents_are_refused():
    """Merging them silently dropped whichever key came first."""
    with pytest.raises(gt.Failure, match="more than one YAML document"):
        gt._parse_yaml_subset("clean:\n  ignored: true\n---\nclean:\n  builds: true\n", "<t>")


def test_a_date_shaped_value_is_refused_the_way_pyyaml_reads_it():
    with pytest.raises(gt.Failure, match="reads as a date"):
        gt._parse_yaml_subset("exclude:\n  - 2024-01-01\n", "<t>")
    assert gt._parse_yaml_subset('exclude:\n  - "2024-01-01"\n', "<t>") == {
        "exclude": ["2024-01-01"]
    }


def test_a_rolled_up_line_keeps_the_reason_it_was_grouped_by(workspace: Path):
    stream = io.StringIO()
    printer = gt.Printer(stream, quiet=False, color=False)
    printer.batch(
        [
            gt.Action(
                "ignored", "r", "a", "kept: a dependency tree, clean.dependencies", skipped=True
            ),
            gt.Action(
                "ignored", "r", "b", "kept: a dependency tree, clean.dependencies", skipped=True
            ),
            gt.Action("ignored", "r", "c", "kept: build output, clean.builds", skipped=True),
            gt.Action("ignored", "r", "d", "kept: build output, clean.builds", skipped=True),
        ]
    )
    out = stream.getvalue()
    assert "dependency trees" in out and "build output" in out
    assert out.count("2 paths") == 2


def test_one_unpushed_commit_against_a_non_origin_trunk_is_categorised():
    assert gt._reason_of("kept: 1 commit not in upstream/main") == (
        "branches with commits not in the trunk"
    )
    assert gt._reason_of("kept: 1 commit not in main") == ("branches with commits not in the trunk")


def test_init_refuses_a_jobs_value_the_tool_would_reject(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    answers = iter(["-3"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    with pytest.raises(gt.Failure, match="negative"):
        gt.main(["-C", str(tmp_path), "init", "--ask", "--path", str(tmp_path)])
    assert not (tmp_path / ".git-tidy.yaml").exists()


def test_quiet_still_says_where_the_quarantine_is(workspace: Path, capsys):
    junk = workspace / "lalalalala.log"
    junk.write_text("x", encoding="utf-8")
    age(junk, 30)
    (workspace / ".git-tidy.yaml").write_text("trash:\n  enabled: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "trash", "--apply", "-q"])
    out = capsys.readouterr().out
    assert "Quarantined files are under" in out
    assert "restore" in out


def test_restore_list_honours_json(workspace: Path, capsys):
    root = workspace / gt.QUARANTINE_DIRNAME / "stamp"
    root.mkdir(parents=True)
    (root / gt.MANIFEST_NAME).write_text(
        '{"entries": [{"from": "/a", "to": "/b"}]}', encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "restore", "--list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["quarantines"] == [{"stamp": "stamp", "entries": 1, "complete": True}]


# --------------------------------------------------------------------------- #
# Claude review, round seven
# --------------------------------------------------------------------------- #


def test_a_directory_named_like_a_credential_is_quarantined(workspace: Path):
    """A file called api-token.tfplan was; a directory called tokens/ was not."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("creds/\ntokens/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    for name in ("creds", "tokens"):
        (repo / name).mkdir()
        (repo / name / "data").write_text("the only copy", encoding="utf-8")
    holding = gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp")

    gt.clean_ignored(
        repo, "repo", config(clean={"ignored": True}), run(), None, gt.Git(repo), holding
    )
    for name in ("creds", "tokens"):
        kept = holding.dir / gt.CONTENT_DIRNAME / "repo" / name / "data"
        assert kept.read_text(encoding="utf-8") == "the only copy", name


def test_a_merge_in_progress_is_never_stashed(workspace: Path, tmp_path: Path):
    """A stash carries the content and not the parentage, and clears MERGE_HEAD."""
    repo = workspace / "repo"
    commit(repo, "base.txt", "base\n")
    git(repo, "switch", "-q", "-c", "topic")
    commit(repo, "topic.txt", "topic\n")
    git(repo, "switch", "-q", "main")
    commit(repo, "other.txt", "other\n")
    git(repo, "merge", "--no-ff", "--no-commit", "topic")
    assert (repo / ".git" / "MERGE_HEAD").is_file()

    stashed, problem = gt._stash(gt.Git(repo), "repo")
    assert stashed is False
    assert problem is not None and "merge is in progress" in problem.error
    assert (repo / ".git" / "MERGE_HEAD").is_file(), "still there to finish or abort"


def test_doctor_uses_the_configured_remote_and_trunk(workspace: Path, remote: Path):
    """It was building its own sync config and discarding the real one."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "develop")
    git(repo, "push", "-q", "-u", "origin", "develop")
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "push", "-q", "-u", "origin", "feature")
    commit(repo, "only-here.txt")
    git(remote, "branch", "-D", "feature")
    git(repo, "fetch", "-q", "--prune", "origin")

    cfg = config(sync={"default_branch": "develop"})
    actions = gt.doctor_repo(repo, "repo", cfg)
    assert any("not pushed" in a.detail for a in actions)


def test_an_orphaned_worktree_is_counted_once(workspace: Path, capsys):
    repo = workspace / "repo"
    side = workspace / "side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))
    shutil.rmtree(repo / ".git" / "worktrees" / "side")

    gt.main(["-C", str(workspace), "run"])
    out = capsys.readouterr().out
    assert "1  orphaned worktrees" in out
    assert "4  orphaned worktrees" not in out


def test_submodule_update_says_nothing_when_there_is_nothing_to_do(workspace: Path, tmp_path: Path):
    """`git submodule update` exits 0 whether or not it moved anything."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    commit(inner, "lib.txt")
    repo = workspace / "repo"
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor")
    git(repo, "commit", "-q", "-m", "add submodule")

    cfg = config(sync={"submodules": "update"})["sync"]
    assert gt._sync_submodules(gt.Git(repo), "repo", cfg, run(gt.DRY)) == []
    assert gt._sync_submodules(gt.Git(repo), "repo", cfg, run()) == []


def test_a_deeper_config_can_switch_a_step_on(workspace: Path, capsys):
    """It could turn one off but never on, while config said it was on."""
    loose = workspace / "proj"
    loose.mkdir()
    junk = loose / "zzzxxxvvvbbb.log"
    junk.write_text("x", encoding="utf-8")
    age(junk, 30)
    (loose / ".git-tidy.yaml").write_text(
        "trash:\n  enabled: true\n  scope: workspace\n", encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "trash", "--apply"])
    assert not junk.exists(), "the deeper config asked for it"


@pytest.mark.parametrize("value", ["2024-1-2", "2024-01-2", "1999-1-1", "2024-01-02Tfoo"])
def test_a_date_shaped_string_matches_pyyaml(value):
    """An over-eager rule refused what PyYAML reads as an ordinary string."""
    yaml = pytest.importorskip("yaml")
    text = f"exclude:\n  - {value}\n"
    try:
        mine = gt._parse_yaml_subset(text, "<t>")
    except gt.Failure:
        mine = "REJECTED"
    assert repr(mine) == repr(yaml.safe_load(text))


def test_init_writes_by_default_and_prints_under_dry_run(tmp_path: Path, capsys, monkeypatch):
    """-n was never passed, and it said "run without -n"."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    target = tmp_path / ".git-tidy.yaml"

    gt.main(["-C", str(tmp_path), "init", "--path", str(tmp_path)])
    assert target.is_file(), "no flag means write it"

    target.unlink()
    gt.main(["-C", str(tmp_path), "init", "-n", "--path", str(tmp_path)])
    assert not target.exists(), "-n means print it"
    assert "git-tidy configuration" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Claude review, round eight
# --------------------------------------------------------------------------- #


def test_unstash_puts_the_staging_back(workspace: Path):
    """A plain pop flattens the staged/unstaged split and then drops the stash."""
    repo = workspace / "repo"
    commit(repo, "b.txt", "one\n")
    commit(repo, "c.txt", "one\n")
    (repo / "b.txt").write_text("staged\n", encoding="utf-8")
    (repo / "c.txt").write_text("unstaged\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    before = git(repo, "status", "--porcelain")

    stashed, problem = gt._stash(gt.Git(repo), "repo")
    assert stashed and problem is None
    assert gt._unstash(gt.Git(repo)) == "nothing changed"
    assert git(repo, "status", "--porcelain") == before, "staged is staged again"


def test_a_never_pushed_branch_is_reported(workspace: Path):
    """Its commits are on no remote at all, and nothing else mentions it."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "scratch/local-only")
    commit(repo, "only-copy.txt")
    git(repo, "switch", "-q", "main")

    actions = gt.doctor_repo(repo, "repo", config())
    reported = [a for a in actions if a.target == "scratch/local-only"]
    assert reported and "not pushed" in reported[0].detail
    assert gt._reason_of(reported[0].detail) == "on a local-only branch, never pushed"


def test_a_deeper_config_can_switch_clean_on(workspace: Path):
    """The walk pruned at the root, so the widening could never reach anything."""
    sub = workspace / "sub"
    (sub / "__pycache__").mkdir(parents=True)
    (sub / "__pycache__" / "x.pyc").write_bytes(b"0" * 64)
    (workspace / ".git-tidy.yaml").write_text("clean:\n  enabled: false\n", encoding="utf-8")
    (sub / ".git-tidy.yaml").write_text("clean:\n  enabled: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (sub / "__pycache__").exists(), "the deeper config asked for it"


@pytest.mark.parametrize("argv", [["init", "-n"], ["init", "-qn"], ["init", "-nq"]])
def test_clustered_short_flags_still_mean_dry_run(argv):
    assert gt.parse_args(argv).explicit_dry is True


def test_dry_run_init_prints_even_on_a_terminal(tmp_path: Path, capsys, monkeypatch):
    """-n was honoured only when stdin was not a tty, which is the rarer case."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    target = tmp_path / ".git-tidy.yaml"
    gt.main(["-C", str(tmp_path), "init", "-n", "--path", str(tmp_path)])
    assert not target.exists()
    assert "git-tidy configuration" in capsys.readouterr().out


@needs_permissions
def test_init_reports_a_target_it_cannot_write(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        with pytest.raises(gt.Failure, match="cannot write"):
            gt.cmd_init(locked / ".git-tidy.yaml", gt.AUTO, force=False, printer=quiet_printer())
    finally:
        locked.chmod(0o755)


def test_a_missing_named_remote_is_not_called_no_remote(workspace: Path):
    repo = workspace / "repo"
    git(repo, "remote", "rename", "origin", "upstream")
    actions = gt.sync_repo(repo, "repo", config(), run())
    assert actions[0].detail == "no such remote"
    assert gt._reason_of(actions[0].detail) == "the configured remote is not there"


def test_a_url_in_a_list_is_not_a_mapping():
    """YAML starts a mapping on ": ", not on any colon at all."""
    yaml = pytest.importorskip("yaml")
    for text in (
        "exclude:\n  - https://internal/mirror\n",
        'clean:\n  keep:\n    - "C:/build/*"\n',
        "exclude:\n  - a:b\n",
    ):
        assert gt._parse_yaml_subset(text, "<t>") == yaml.safe_load(text), text


def test_jobs_zero_means_the_same_on_the_command_line(workspace: Path):
    """The config comment says 0 = one per core, and init writes that verbatim."""
    assert gt.main(["-C", str(workspace), "-j", "0", "doctor"]) == 0
    with pytest.raises(gt.Failure, match="cannot be negative"):
        gt.main(["-C", str(workspace), "-j", "-2", "doctor"])


# --------------------------------------------------------------------------- #
# The fallback parser against PyYAML, systematically
# --------------------------------------------------------------------------- #

AMBIGUOUS = [
    "jobs: -\n",
    "clean:\n  enabled: a: b\n",
    "outer:\n  inner:\n    deep: -\n",
    "exclude: [#notacomment]\n",
    "exclude:\n  - -\n",
    "a: 2024-01-02T03:04:05Z\n",
]


@pytest.mark.parametrize("text", AMBIGUOUS)
def test_the_fallback_parser_refuses_what_pyyaml_refuses(text: str):
    """Being *looser* than PyYAML is the same defect as being tighter.

    Most of these are a ScannerError or ParserError in PyYAML and were quietly
    read as a string here, so the file meant one thing from a checkout and
    another from a shipped build, which has no PyYAML in it. The timestamp is
    not an error for PyYAML but a datetime, which is not a valid value for any
    setting: refusing it with a sentence beats a schema error further on.
    """
    with pytest.raises(gt.Failure):
        gt._parse_yaml_subset(text, "<t>")


DIFFERENTIAL = [
    "exclude:\n  - https://internal/mirror\n",
    "exclude:\n  - a:b\n",
    "exclude:\n  - a # trailing\n",
    "exclude:\n  - 'a: b'\n",
    'exclude:\n  - "a: b"\n',
    "clean:\n  keep:\n    - 'C:/build/*'\n",
    "clean:\n  keep:\n  - flush\n  - style\n",
    "jobs: 007\n",
    "a: 2024-1-2\nb: 12:30\nc: 1:2:3\n",
    "a: .inf\nb: -.Inf\nc: 1E-3\nd: .5\n",
    "a: yes\nb: no\nc: on\nd: off\ne: n\nf: y\n",
    "a: ~\nb: null\nc: NULL\nd:\n",
    "map:\n  key with spaces: 1\n",
    "top: 1\nnested:\n    deep: 2\n",
    "trash:\n  keep:\n    - README*\n    - '*.md'\n",
    "jobs: 4\n\n# comment\nexclude:\n  - a\n",
    "clean:\n  enabled: true\n  keep: []\n",
    "exclude: [a, b]\nkeep: {a: 1}\n",
]


@pytest.mark.parametrize("text", DIFFERENTIAL)
def test_whole_documents_parse_the_same_as_pyyaml(text: str):
    """A value that means two things depending on the environment is a bug.

    The shipped binaries carry no PyYAML, so every one of these is a case of
    "loads from a checkout, fails on every build" if the two ever disagree.
    """
    yaml = pytest.importorskip("yaml")
    assert gt._parse_yaml_subset(text, "<t>") == yaml.safe_load(text), text


# --------------------------------------------------------------------------- #
# Claude review, round nine
# --------------------------------------------------------------------------- #


def test_a_tag_named_like_a_branch_does_not_hide_unpushed_commits(workspace: Path):
    """git resolves a bare name as a tag first, so every rev-walk read the tag."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "feature")
    commit(repo, "only-here.txt")
    git(repo, "switch", "-q", "main")
    git(repo, "tag", "feature", "main")  # the tag now shadows the branch

    reported = [a for a in gt.doctor_repo(repo, "repo", config()) if a.target == "feature"]
    assert reported and "not pushed" in reported[0].detail


def test_a_tag_named_like_a_branch_does_not_break_prune(workspace: Path, remote: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "push", "-q", "-u", "origin", "feature")
    commit(repo, "only-here.txt")
    git(repo, "switch", "-q", "main")
    git(repo, "tag", "feature", "main")
    git(remote, "branch", "-D", "feature")
    git(repo, "fetch", "-q", "--prune", "origin")

    kept = [a for a in gt.prune_branches(repo, "repo", config(), run()) if a.target == "feature"]
    assert kept and "not in" in kept[0].detail, kept
    assert git(repo, "branch", "--list", "feature").strip()


def test_a_bisect_in_progress_survives_sync(workspace: Path):
    """Switching away resets HEAD, and `git bisect reset` is the only way back."""
    repo = workspace / "repo"
    for number in range(5):
        commit(repo, f"c{number}.txt")
    oldest = git(repo, "rev-parse", "HEAD~4").strip()
    git(repo, "bisect", "start")
    git(repo, "bisect", "bad")
    git(repo, "bisect", "good", oldest)
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD", "detached"

    before = git(repo, "rev-parse", "HEAD").strip()
    actions = gt.sync_repo(repo, "repo", config(sync={"switch": "always"}), run())
    assert any("in progress" in a.detail for a in actions), actions
    assert git(repo, "rev-parse", "HEAD").strip() == before
    assert (repo / ".git" / "BISECT_LOG").exists()


@needs_permissions
def test_an_unreadable_subtree_does_not_read_as_an_empty_one(workspace: Path):
    """os.walk swallows PermissionError, so the guards answered "nothing here"."""
    repo = workspace / "repo"
    hidden = repo / "htmlcov" / "keep"
    hidden.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(hidden / "vendored")], check=True)
    hidden.chmod(0o000)
    try:
        assert gt.main(["-C", str(workspace), "clean", "--apply"]) == 0
        assert (repo / "htmlcov").exists(), "a repository may be buried in there"
    finally:
        hidden.chmod(0o755)


@pytest.mark.parametrize(
    "text", ["clean:\n  ignored:true\n", "jobs:2\n", "trash:\n  enabled:true\n"]
)
def test_a_colon_with_no_space_is_refused(text: str):
    """PyYAML reads it as a plain scalar, so it was fatal from a checkout only."""
    with pytest.raises(gt.Failure, match="needs a space"):
        gt._parse_yaml_subset(text, "<t>")


@pytest.mark.parametrize(
    ("quoted", "want"),
    [('"a\\tb"', "a\tb"), ('"a\\nb"', "a\nb"), ('"\\u00e9"', "é"), ('"a\\\\b"', "a\\b")],
)
def test_double_quoted_escapes_resolve_as_pyyaml_resolves_them(quoted: str, want: str):
    """Left literal, "a\\tb" was a five-character glob that matched nothing."""
    assert gt._parse_yaml_subset(f"clean:\n  keep:\n    - {quoted}\n", "<t>") == {
        "clean": {"keep": [want]}
    }


def test_an_unknown_escape_is_refused_rather_than_guessed():
    with pytest.raises(gt.Failure, match="unknown escape"):
        gt._parse_yaml_subset('clean:\n  keep:\n    - "x\\q"\n', "<t>")


@pytest.mark.parametrize(
    ("argv", "want"),
    [
        (["-C/x/notes", "init"], False),
        (["-C", "/x/notes", "init"], False),
        (["-j4", "clean"], False),
        (["-n", "clean"], True),
        (["-qn", "clean"], True),
        (["-nq", "clean"], True),
        (["--dry-run", "clean"], True),
    ],
)
def test_only_a_real_flag_counts_as_a_dry_run(argv: list[str], want: bool):
    """A short option's attached value is not a cluster of flags."""
    assert gt._asked_for_dry_run(argv) is want


def test_a_deeper_config_can_widen_the_trash_scope(workspace: Path):
    """`git-tidy config sub` said scope: workspace while the run swept nothing."""
    sub = workspace / "sub"
    sub.mkdir()
    old = sub / "junk.tmp"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (0, 0))
    (workspace / ".git-tidy.yaml").write_text(
        "trash:\n  enabled: true\n  scope: root\n", encoding="utf-8"
    )
    (sub / ".git-tidy.yaml").write_text("trash:\n  scope: workspace\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "trash", "--apply"])
    assert not old.exists(), "the deepest config wins, as the README says"


def expire_now(root: Path, only: str | None = None) -> list[gt.Action]:
    return gt.expire_quarantines(root, gt.DEFAULTS["trash"]["retention_days"], run(), only)


def test_expire_says_so_when_a_named_quarantine_is_too_young(tmp_path: Path):
    root = tmp_path / gt.QUARANTINE_DIRNAME
    (root / "20260101T000000Z").mkdir(parents=True)
    (root / "20260101T000000Z" / gt.MANIFEST_NAME).write_text("{}", encoding="utf-8")

    actions = expire_now(root, only="20260101T000000Z")
    assert actions and actions[0].skipped and "not yet" in actions[0].detail
    assert gt._reason_of(actions[0].detail) != "other, see the lines marked -"


def test_expire_refuses_a_stamp_that_is_not_there(tmp_path: Path):
    root = tmp_path / gt.QUARANTINE_DIRNAME
    root.mkdir()
    with pytest.raises(gt.Failure, match="no quarantine 'NOPE'"):
        expire_now(root, only="NOPE")


@needs_permissions
def test_a_repository_probe_answers_yes_when_it_cannot_tell(tmp_path: Path):
    """Path.is_dir() raises before 3.12 and returns False from 3.12 on.

    Both are wrong for a guard that decides whether deleting something would
    destroy a repository: the same workspace behaved differently depending on
    which interpreter the tool happened to be running under.
    """
    locked = tmp_path / "locked"
    (locked / "maybe").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        assert gt.holds_git_data(locked / "maybe") is True
        assert gt.cannot_look(locked / "maybe") is True
        # is_repo stays strict: a checkout that cannot be read is not one to sync.
        assert gt.is_repo(locked / "maybe") is False
    finally:
        locked.chmod(0o755)


# --------------------------------------------------------------------------- #
# Claude review, round ten
# --------------------------------------------------------------------------- #


def test_a_rebase_does_not_replace_an_ignored_local_file(workspace: Path, remote: Path):
    """The switch and the fast-forward both guard this; the rebase did not.

    A rebase checks the upstream out as surely as they do, so an uncommitted
    .env that the incoming commits happen to track was replaced — never
    committed, never stashed, never quarantined.
    """
    repo = workspace / "repo"
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore .env")
    git(repo, "push", "-q", "origin", "main")

    other = workspace.parent / "other"
    git(workspace.parent, "clone", "-q", str(remote), str(other))
    (other / ".env").write_text("UPSTREAM=theirs", encoding="utf-8")
    git(other, "add", "-f", ".env")
    git(other, "commit", "-q", "-m", "track .env upstream")
    git(other, "push", "-q", "origin", "main")

    commit(repo, "mine.txt")  # local commit as well: diverged
    (repo / ".env").write_text("MY_SECRET=do-not-lose-me", encoding="utf-8")
    git(repo, "fetch", "-q", "origin")

    actions = gt.sync_repo(repo, "repo", config(sync={"diverged": "rebase"}), run())
    assert any("would be replaced" in a.detail for a in actions), actions
    assert (repo / ".env").read_text(encoding="utf-8") == "MY_SECRET=do-not-lose-me"


def test_a_dependency_tree_is_reclaimed_and_its_credential_lifted_out(workspace: Path):
    """Renaming 400 MB into the workspace's own quarantine reclaims nothing."""
    repo = workspace / "repo"
    modules = repo / "node_modules"
    (modules / "acorn" / "dist").mkdir(parents=True)
    (modules / "acorn" / "dist" / "tokenizer.js").write_bytes(b"0" * 40960)
    (modules / "id_rsa").write_text("PRIVATE", encoding="utf-8")

    (workspace / ".git-tidy.yaml").write_text("clean:\n  dependencies: true\n", encoding="utf-8")
    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (modules / "acorn").exists(), "the tree is really gone"
    assert (modules / "id_rsa").read_text(encoding="utf-8") == "PRIVATE"


@pytest.mark.parametrize(
    ("name", "protected"),
    [
        ("tokenizer.js", False),
        ("token.py", False),
        ("phystokens.py", False),
        ("tokens.pyc", False),
        ("id_rsa", True),
        ("cacert.pem", True),
        ("api-token.txt", True),
        ("creds.json", True),
        ("deploy.key", True),
    ],
)
def test_source_code_is_not_mistaken_for_a_credential(name: str, protected: bool):
    """trash.sensitive casts a wide net, and *token* caught half of every venv."""
    assert gt._protects(name, gt.DEFAULTS["trash"]["sensitive"]) is protected


def test_clean_ignored_does_not_keep_and_remove_the_same_path(workspace: Path, capsys):
    """One run said "kept: contains terraform.tfstate" and "removed", of one path."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text(".terraform/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / ".terraform").mkdir()
    (repo / ".terraform" / "terraform.tfstate").write_text("backend", encoding="utf-8")

    (workspace / ".git-tidy.yaml").write_text(
        "clean:\n  ignored: true\n  dirs: []\n", encoding="utf-8"
    )
    gt.main(["-C", str(workspace), "clean", "--apply", "-v"])
    lines = [line for line in capsys.readouterr().out.splitlines() if ".terraform" in line]
    assert len(lines) == 1, lines
    assert not (repo / ".terraform").exists()


@needs_permissions
def test_an_unreadable_subtree_is_not_called_a_git_repository(workspace: Path, capsys):
    """It said "contains a git repository" for a tree with no git anywhere in it."""
    loose = workspace / "project.old"
    (loose / "locked").mkdir(parents=True)
    (loose / "locked").chmod(0o000)
    (workspace / ".git-tidy.yaml").write_text(
        "trash:\n  enabled: true\n  scope: workspace\n  dirs: true\n  min_age_days: 0\n",
        encoding="utf-8",
    )
    try:
        gt.main(["-C", str(workspace), "trash", "-v"])
        out = capsys.readouterr().out
        assert "cannot be read" in out
        assert "contains a git repository" not in out
    finally:
        (loose / "locked").chmod(0o755)


def test_a_rebase_in_progress_is_an_operation_in_progress(workspace: Path):
    """git sets none of the *_HEAD markers during a rebase, only a directory."""
    repo = workspace / "repo"
    commit(repo, "a.txt", "one\n")
    git(repo, "switch", "-q", "-c", "side")
    commit(repo, "a.txt", "side\n")
    git(repo, "switch", "-q", "main")
    commit(repo, "a.txt", "main\n")
    subprocess.run(["git", "-C", str(repo), "rebase", "side"], capture_output=True, check=False)

    assert gt._operation_in_progress(gt.Git(repo)) == "a rebase"
    actions = gt.sync_repo(repo, "repo", config(sync={"switch": "always"}), run())
    assert any("in progress" in a.detail for a in actions), actions
    assert not any(a.error for a in actions), actions


def test_a_character_class_in_a_pattern_is_not_a_broken_flow_list():
    """fixtures/[0-9] is legal fnmatch and a plain string to PyYAML."""
    yaml = pytest.importorskip("yaml")
    text = "clean:\n  keep:\n    - fixtures/[0-9]\n    - a{b\n"
    assert gt._parse_yaml_subset(text, "<t>") == yaml.safe_load(text)


def test_init_dry_run_keeps_its_advice_off_stdout(tmp_path: Path, capsys):
    """`git-tidy init -n > .git-tidy.yaml` is the documented recipe."""
    target = tmp_path / ".git-tidy.yaml"
    target.write_text("", encoding="utf-8")  # the redirect creates it first
    assert gt.main(["-C", str(tmp_path), "init", "-n", "--path", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "Not written" in captured.err
    assert "Not written" not in captured.out
    # The template is all comments, so it parses to nothing — but it parses,
    # which the advice line on stdout used to stop it doing.
    assert gt.load_yaml(captured.out, "<stdout>") is None


def test_a_clone_of_an_empty_repository_is_fast_forwarded(tmp_path: Path):
    """An unborn HEAD has nothing to count, and it was left behind for ever."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    space = tmp_path / "space"
    space.mkdir()
    git(space, "clone", "-q", str(origin), "fresh")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    commit(seed, "README.md", "hello\n")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")

    gt.main(["-C", str(space), "sync", "--apply"])
    assert (space / "fresh" / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_the_generated_config_documents_the_real_defaults(tmp_path: Path):
    """`git-tidy init` writes documentation, so it has to be true documentation.

    Three separate review rounds found a default in this template that the code
    did not use, or a setting missing from it. Uncomment every setting the
    template ships and the result must be the defaults, exactly, and must merge
    against the schema.
    """
    assert gt.main(["-C", str(tmp_path), "init", "-q", "--path", str(tmp_path)]) == 0
    body = (tmp_path / gt.CONFIG_NAMES[0]).read_text(encoding="utf-8")

    keys = set(gt.DEFAULTS)
    for value in gt.DEFAULTS.values():
        if isinstance(value, dict):
            keys |= set(value)
    setting = re.compile(r"^(\s*)# ([a-z_]+):(.*)$")
    item = re.compile(r"^(\s*)#(\s+- .*)$")

    lines: list[str] = []
    in_setting = False
    for line in body.splitlines():
        if (m := setting.match(line)) and m.group(2) in keys:
            lines.append(f"{m.group(1)}{m.group(2)}:{m.group(3)}")
            in_setting = True
        elif in_setting and (m := item.match(line)):
            lines.append(m.group(1) + m.group(2))
        else:
            in_setting = False
            if not line.lstrip().startswith("#"):
                lines.append(line)

    parsed = gt.load_yaml("\n".join(lines) + "\n", "<generated>")
    assert parsed == gt.DEFAULTS
    gt._merge(gt.DEFAULTS, parsed, "<generated>")


def test_no_comment_line_reads_like_a_setting_it_is_not():
    """A prose line starting `switch:` sat four lines from the real `switch:`.

    The template is a file people edit by uncommenting lines, so two lines that
    look the same and mean different things is a trap.
    """
    keys = set(gt.DEFAULTS)
    for value in gt.DEFAULTS.values():
        if isinstance(value, dict):
            keys |= set(value)
    collisions = [
        f"{name}: {line.strip()}"
        for name, text in gt.COMMENTS.items()
        for line in text.splitlines()
        if (m := re.match(r"^\s*([a-z_]+):", line)) and m.group(1) in keys
    ]
    assert collisions == []


# --------------------------------------------------------------------------- #
# The exit-code contract, as the man page states it
# --------------------------------------------------------------------------- #

USAGE_OR_CONFIG = [
    (["nosuchcommand"], "usage"),
    (["-j", "-2", "clean"], "cannot be negative"),
    (["restore", "NOPE", "--apply"], "no quarantine"),
]


@pytest.mark.parametrize(("argv", "expected"), USAGE_OR_CONFIG)
def test_a_usage_or_configuration_error_exits_two(
    workspace: Path, capsys, argv: list[str], expected: str
):
    """man/git-tidy.1: "0 on success, 1 if anything failed, 2 on a usage or
    configuration error". Scripts branch on this."""
    with pytest.raises(SystemExit) as exit_info:
        gt.entrypoint(["-C", str(workspace), *argv])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert expected in (captured.err + captured.out).lower()


BROKEN_CONFIGS = [
    ("clean:\n  ignored_keep:\n", "nothing after the colon"),
    ("nonsense: 1\n", "unknown setting"),
    ("clean:\n  enabled: maybe\n", "clean"),
]


@pytest.mark.parametrize(("body", "expected"), BROKEN_CONFIGS)
def test_a_broken_config_exits_two_with_the_file_named(
    workspace: Path, capsys, body: str, expected: str
):
    (workspace / gt.CONFIG_NAMES[0]).write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        gt.entrypoint(["-C", str(workspace), "clean", "-n"])
    assert exit_info.value.code == 2
    message = capsys.readouterr().err
    assert gt.CONFIG_NAMES[0] in message
    assert expected in message


@pytest.mark.parametrize("command", ["sync", "prune", "clean", "trash", "doctor", "run"])
@pytest.mark.parametrize("mode", ["-n", "--apply"])
def test_the_json_report_has_the_same_shape_for_every_command(
    workspace: Path, capsys, command: str, mode: str
):
    """--json is the scripting contract, so every command must honour it."""
    repo = workspace / "repo"
    (repo / "__pycache__").mkdir(exist_ok=True)
    (repo / "__pycache__" / "a.pyc").write_bytes(b"0" * 32)

    assert gt.main(["-C", str(workspace), command, mode, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) >= {"version", "mode", "interrupted", "actions"}
    assert report["version"] == gt.__version__
    for action in report["actions"]:
        assert set(action) >= {"kind", "scope", "target", "detail"}
        assert isinstance(action.get("size", 0), int)


def test_a_protected_symlink_keeps_its_directory_instead_of_failing(workspace: Path, capsys):
    """The quarantine refuses a symlink out of the workspace, as it should.

    The refusal used to surface as a raw relative_to() message on an action
    whose target was "-", so the run reported a failure naming neither the file
    nor the directory — and the whole point of a symlink is that deleting it
    cannot destroy what it points at.
    """
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "real_id_rsa").write_text("REAL", encoding="utf-8")
    repo = workspace / "repo"
    cache = repo / "__pycache__"
    cache.mkdir()
    (cache / "a.pyc").write_bytes(b"0" * 64)
    (cache / "id_rsa").symlink_to(outside / "real_id_rsa")

    assert gt.main(["-C", str(workspace), "clean", "--apply", "-v"]) == 0
    out = capsys.readouterr().out
    assert "kept: holds a protected symlink" in out
    assert "relative_to" not in out and "subpath" not in out
    assert "1 failed" not in out
    assert cache.exists(), "kept whole rather than half-emptied"
    assert (outside / "real_id_rsa").read_text(encoding="utf-8") == "REAL"
    assert gt._reason_of("kept: holds a protected symlink") != "other, see the lines marked -"


# --------------------------------------------------------------------------- #
# Claude review, round eleven
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["secrets.py", "secrets.sh", ".env.ts"])
def test_the_source_exemption_does_not_reach_ignored_keep(name: str):
    """A gitignored secrets.py is by definition not committed source.

    Round ten exempted source-code extensions from trash.sensitive, which is
    right — a tokenizer.js is not a secret. Applying the same exemption to
    clean.ignored_keep deleted exactly the files that list exists to name.
    """
    sensitive = gt.DEFAULTS["trash"]["sensitive"]
    local_state = gt.DEFAULTS["clean"]["ignored_keep"]
    assert gt._protects(name, sensitive) is False, "not a credential by name"
    assert gt._protects(name, sensitive, local_state) is True


def test_a_fast_forward_will_not_replace_an_ignored_source_file(
    workspace: Path, remote: Path, tmp_path: Path
):
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("secrets.*\n", encoding="utf-8")
    (repo / "config").mkdir()
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore secrets")
    git(repo, "push", "-q", "origin", "main")

    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "config").mkdir(exist_ok=True)
    (other / "config" / "secrets.py").write_text("UPSTREAM", encoding="utf-8")
    git(other, "add", "-f", "config/secrets.py")
    git(other, "commit", "-q", "-m", "track it upstream")
    git(other, "push", "-q", "origin", "main")

    (repo / "config" / "secrets.py").write_text("MY-ONLY-COPY", encoding="utf-8")
    git(repo, "fetch", "-q", "origin")

    actions = gt.sync_repo(repo, "repo", config(), run())
    assert any("would be replaced" in a.detail for a in actions), actions
    assert (repo / "config" / "secrets.py").read_text(encoding="utf-8") == "MY-ONLY-COPY"


def test_every_ignored_keep_match_is_lifted_out_not_just_the_first(workspace: Path):
    """One ignored_keep entry, two files: one was rescued and one destroyed."""
    repo = workspace / "repo"
    build = repo / "build"
    build.mkdir()
    (build / "out.bin").write_bytes(b"0" * 4096)
    (build / ".env").write_text("P", encoding="utf-8")
    (build / ".env.sh").write_text("export AWS_SECRET=hunter2", encoding="utf-8")
    (workspace / gt.CONFIG_NAMES[0]).write_text("clean:\n  builds: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (build / "out.bin").exists()
    assert (build / ".env").is_file()
    assert (build / ".env.sh").read_text(encoding="utf-8") == "export AWS_SECRET=hunter2"


def test_a_path_ignored_keep_protects_by_its_path_is_lifted_out(workspace: Path):
    """_holds_protected matches the relative path and says so; the rescue did not."""
    repo = workspace / "repo"
    (repo / "build" / "config").mkdir(parents=True)
    (repo / "build" / "out.bin").write_bytes(b"0" * 4096)
    (repo / "build" / "config" / "prod.json").write_text("THE ONLY COPY", encoding="utf-8")
    (workspace / gt.CONFIG_NAMES[0]).write_text(
        'clean:\n  builds: true\n  ignored_keep:\n    - "build/config/*.json"\n', encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (repo / "build" / "out.bin").exists()
    kept = repo / "build" / "config" / "prod.json"
    assert kept.read_text(encoding="utf-8") == "THE ONLY COPY"


def test_a_path_is_decided_once_even_when_both_clean_steps_name_it(workspace: Path, capsys):
    """One run printed "kept: contains …", counted it, and deleted it next line.

    clean.ignored decides the path — here by emptying it out around the file
    ignored_keep names — and clean.dirs must not look at it again.
    """
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore build")
    (repo / "build" / "config").mkdir(parents=True)
    (repo / "build" / "out.bin").write_bytes(b"0" * 4096)
    (repo / "build" / "config" / "prod.json").write_text("THE ONLY COPY", encoding="utf-8")
    (workspace / gt.CONFIG_NAMES[0]).write_text(
        'clean:\n  ignored: true\n  builds: true\n  ignored_keep:\n    - "build/config/*.json"\n',
        encoding="utf-8",
    )

    gt.main(["-C", str(workspace), "clean", "--apply", "-v"])
    lines = [line for line in capsys.readouterr().out.splitlines() if "build" in line]
    assert len(lines) == 1, lines
    assert (repo / "build" / "config" / "prod.json").read_text(encoding="utf-8") == "THE ONLY COPY"
    assert not (repo / "build" / "out.bin").exists()


def test_bytes_left_in_place_are_not_called_freed(workspace: Path, capsys):
    """The next `df` would expose it, and reclaimed space is the headline."""
    repo = workspace / "repo"
    cache = repo / "__pycache__"
    cache.mkdir()
    (cache / "big.pyc").write_bytes(b"0" * 100_000)
    (cache / "id_rsa").write_bytes(b"k" * 50_000)

    gt.main(["-C", str(workspace), "clean", "--apply"])
    out = capsys.readouterr().out
    assert "stayed in place" in out
    freed = next(line for line in out.splitlines() if "freed" in line)
    assert "150" not in freed, freed  # not the whole directory
    assert (cache / "id_rsa").stat().st_size == 50_000


@needs_permissions
def test_an_unreadable_subtree_is_not_reported_as_a_symlink(workspace: Path, capsys):
    """Round ten fixed this wording in _measure; the rescue reintroduced it."""
    repo = workspace / "repo"
    cache = repo / ".terraform"
    (cache / "locked").mkdir(parents=True)
    (cache / "locked").chmod(0o000)
    try:
        gt.main(["-C", str(workspace), "clean", "-v"])
        out = capsys.readouterr().out
        assert "cannot be read" in out
        assert "symlink" not in out
    finally:
        (cache / "locked").chmod(0o755)


def test_saying_all_to_a_quarantine_does_not_consent_to_deletes(workspace: Path):
    """Both were kind "remove", so one `a` covered the irreversible one too."""
    repo = workspace / "repo"
    for name, holds in (("cache-creds", True), ("cache-plain", False)):
        (repo / name).mkdir()
        (repo / name / ("id_rsa" if holds else "a.bin")).write_bytes(b"0" * 64)
    (workspace / gt.CONFIG_NAMES[0]).write_text(
        'clean:\n  extra_dirs: ["cache-creds", "cache-plain"]\n', encoding="utf-8"
    )
    answers = iter(["a"])  # "all of these" to whichever comes first
    decider = gt.Decider(gt.ASK, prompt_input=lambda _: next(answers, "n"))

    actions = gt.clean_tree(
        repo,
        "repo",
        config(clean={"extra_dirs": ["cache-creds", "cache-plain"]}),
        decider,
        gt.Git(repo),
        None,
        sensitive=gt.DEFAULTS["trash"]["sensitive"],
        local_state=gt.DEFAULTS["clean"]["ignored_keep"],
        holding=gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp"),
    )
    kinds = {a.consent_key for a in actions if a.applied}
    assert kinds <= {"remove+quarantined"}, [(a.target, a.consent_key, a.applied) for a in actions]
    assert (repo / "cache-plain").exists(), "never consented to, so never deleted"


@needs_permissions
def test_an_artefact_directory_with_an_unreadable_subtree_is_reported(workspace: Path, capsys):
    """It was descended into instead of removed, and no line was printed."""
    repo = workspace / "repo"
    cache = repo / "htmlcov"
    (cache / "locked").mkdir(parents=True)
    (cache / "locked").chmod(0o000)
    try:
        gt.main(["-C", str(workspace), "clean", "-v"])
        assert "htmlcov" in capsys.readouterr().out
    finally:
        (cache / "locked").chmod(0o755)


FLOW_AGREEMENT = [
    "clean:\n  keep: [conf?.yaml]\n",
    "clean:\n  keep: [fixtures/[0-9]]\n",
    "sync:\n  remote: upstream:\n",
]


@pytest.mark.parametrize("text", FLOW_AGREEMENT)
def test_the_fallback_parser_refuses_what_pyyaml_refuses_in_flow(text: str):
    """Accepted here and a ParserError with PyYAML installed is the same defect
    as the other way round: the config depends on the environment."""
    yaml = pytest.importorskip("yaml")
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)
    with pytest.raises(gt.Failure):
        gt._parse_yaml_subset(text, "<t>")


def test_skipping_a_quarantine_kind_does_not_skip_the_deletes(workspace: Path):
    """The mirror of the `a` case: `s` must not silence a different decision.

    Answering "skip these" to a reversible quarantine kept the irreversible
    deletes from ever being asked about — the same conflation, in the direction
    that quietly does less rather than more.
    """
    repo = workspace / "repo"
    for name, holds in (("cache-creds", True), ("cache-plain", False)):
        (repo / name).mkdir()
        (repo / name / ("id_rsa" if holds else "a.bin")).write_bytes(b"0" * 64)
    asked: list[str] = []

    def answer(question: str) -> str:
        asked.append(question)
        return "s" if len(asked) == 1 else "y"

    decider = gt.Decider(gt.ASK, prompt_input=answer)
    gt.clean_tree(
        repo,
        "repo",
        config(clean={"extra_dirs": ["cache-creds", "cache-plain"]}),
        decider,
        gt.Git(repo),
        None,
        sensitive=gt.DEFAULTS["trash"]["sensitive"],
        local_state=gt.DEFAULTS["clean"]["ignored_keep"],
        holding=gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp"),
    )
    assert len(asked) == 2, "the second kind was still asked about"
    assert (repo / "cache-creds").exists(), "skipped"
    assert not (repo / "cache-plain").exists(), "consented to separately"


def test_yes_to_all_is_asked_once_and_covers_everything(workspace: Path):
    """Y is the one answer that spans kinds, and the prompt says so."""
    repo = workspace / "repo"
    for name in ("cache-creds", "cache-plain"):
        (repo / name).mkdir()
        (repo / name / ("id_rsa" if name.endswith("creds") else "a.bin")).write_bytes(b"0" * 64)
    asked: list[str] = []

    def answer(question: str) -> str:
        asked.append(question)
        return "Y"

    decider = gt.Decider(gt.ASK, prompt_input=answer)
    gt.clean_tree(
        repo,
        "repo",
        config(clean={"extra_dirs": ["cache-creds", "cache-plain"]}),
        decider,
        gt.Git(repo),
        None,
        sensitive=gt.DEFAULTS["trash"]["sensitive"],
        local_state=gt.DEFAULTS["clean"]["ignored_keep"],
        holding=gt.Quarantine(workspace / gt.QUARANTINE_DIRNAME, workspace, stamp="stamp"),
    )
    assert len(asked) == 1
    assert not (repo / "cache-creds").exists()
    assert not (repo / "cache-plain").exists()
    kept = workspace / gt.QUARANTINE_DIRNAME / "stamp" / gt.CONTENT_DIRNAME
    assert (kept / "repo" / "cache-creds" / "id_rsa").is_file(), "still never hard-deleted"


# --------------------------------------------------------------------------- #
# Claude review, round twelve
# --------------------------------------------------------------------------- #


def test_clean_ignored_does_not_stop_the_space_being_reclaimed(workspace: Path, capsys):
    """Turning on the setting the README calls the fastest way to reclaim space
    made the tool reclaim nothing for that directory."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    modules = repo / "node_modules" / "pkg"
    modules.mkdir(parents=True)
    (modules / "big.bin").write_bytes(b"0" * 200_000)
    (modules / "server.key").write_text("PRIVATE", encoding="utf-8")
    (workspace / gt.CONFIG_NAMES[0]).write_text(
        "clean:\n  ignored: true\n  dependencies: true\n", encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert not (modules / "big.bin").exists(), "the space came back"
    assert (modules / "server.key").read_text(encoding="utf-8") == "PRIVATE"
    assert "freed" in capsys.readouterr().out


def test_protection_does_not_need_a_quarantine_to_exist(workspace: Path):
    """It stays where it is, so there is nothing to fall back on."""
    repo = workspace / "repo"
    cache = repo / "htmlcov"
    cache.mkdir()
    (cache / "junk.bin").write_bytes(b"0" * 4096)
    (cache / ".env").write_text("DB=1", encoding="utf-8")

    gt.clean_ignored(repo, "repo", config(clean={"ignored": True}), run(), None, gt.Git(repo))
    gt.clean_tree(
        repo,
        "repo",
        config(),
        run(),
        gt.Git(repo),
        None,  # no quarantine
        sensitive=gt.DEFAULTS["trash"]["sensitive"],
        local_state=gt.DEFAULTS["clean"]["ignored_keep"],
        holding=None,  # and nowhere to put anything
    )
    assert (cache / ".env").read_text(encoding="utf-8") == "DB=1"
    assert not (cache / "junk.bin").exists()


def test_a_thinned_directory_is_not_called_quarantined(workspace: Path, capsys):
    """ "removed, keeping id_rsa in quarantine" contains the word quarantine."""
    repo = workspace / "repo"
    for name in ("one", "two", "three"):
        (repo / name).mkdir()
        (repo / name / "big.bin").write_bytes(b"0" * 100_000)
        (repo / name / "id_rsa").write_text("K", encoding="utf-8")
    (workspace / gt.CONFIG_NAMES[0]).write_text(
        'clean:\n  extra_dirs: ["one", "two", "three"]\n', encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "clean", "--apply"])
    out = capsys.readouterr().out
    assert "quarantined" not in out
    assert "emptied out" in out


def test_a_dry_run_predicts_both_totals(workspace: Path, capsys):
    """The dry run under-reported the bytes that would stay behind."""
    repo = workspace / "repo"
    cache = repo / "htmlcov"
    cache.mkdir()
    (cache / "big.bin").write_bytes(b"0" * 300_000)
    (cache / "id_rsa").write_bytes(b"k" * 200_000)

    gt.main(["-C", str(workspace), "clean"])
    dry = capsys.readouterr().out
    gt.main(["-C", str(workspace), "clean", "--apply"])
    applied = capsys.readouterr().out

    def totals(text: str) -> list[str]:
        after = text.split("Summary", 1)[-1]
        return re.findall(r"([\d.]+ [KMG]?B)\s+(?:to free|freed|would stay|stayed)", after)

    assert totals(dry) == totals(applied) != [], (dry, applied)


def test_a_directory_held_back_is_reported_once(workspace: Path, capsys):
    """One directory, two lines, and a held-back count of two."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("htmlcov/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    cache = repo / "htmlcov"
    cache.mkdir()
    (cache / "mytoken").symlink_to(workspace.parent / "elsewhere")
    (workspace / gt.CONFIG_NAMES[0]).write_text("clean:\n  ignored: true\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "-v"])
    out = capsys.readouterr().out
    assert len([line for line in out.splitlines() if "htmlcov" in line]) == 1
    assert "       1  directories" in out or "1  directories" in out


COMMENT_AGREEMENT = [
    "a: x 'y # z\n",
    'a: 5 " # z\n',
    "exclude:\n  - a 'b # c\n",
    "a: it's fine  # note\n",
    'a: "q" # c\n',
    "a: 'q' # c\n",
    'clean:\n  keep: ["a\\"b", c]\n',
]


@pytest.mark.parametrize("text", COMMENT_AGREEMENT)
def test_a_quote_only_opens_where_a_value_can_begin(text: str):
    """ "after a space" swallowed the comment in `a: x 'y # z`, silently."""
    yaml = pytest.importorskip("yaml")
    assert gt._parse_yaml_subset(text, "<t>") == yaml.safe_load(text), text


@pytest.mark.parametrize("text", ["--- {jobs: 4}\n", "---jobs: 4\n"])
def test_content_on_a_document_marker_line_is_refused(text: str):
    """The line was dropped whole, so the config silently became empty."""
    with pytest.raises(gt.Failure, match="not after ---"):
        gt._parse_yaml_subset(text, "<t>")


@pytest.mark.parametrize("text", ["---\njobs: 4\n", "jobs: 4\n...\n"])
def test_a_bare_document_marker_is_still_fine(text: str):
    assert gt._parse_yaml_subset(text, "<t>") == {"jobs": 4}


# --------------------------------------------------------------------------- #
# _thin_out, over every shape of tree it can be handed
# --------------------------------------------------------------------------- #

THIN_SHAPES: list[tuple[str, list[str], list[str]]] = [
    # name, files to create, which of them are protected
    ("flat", ["a.bin", "b.bin", "id_rsa"], ["id_rsa"]),
    ("nested", ["x/a.bin", "x/id_rsa", "y/b.bin"], ["x/id_rsa"]),
    ("deep", ["a/b/c/d/.env", "a/b/junk.bin", "top.bin"], ["a/b/c/d/.env"]),
    ("two kept, far apart", ["p/.env", "q/r/id_rsa", "s/j.bin"], ["p/.env", "q/r/id_rsa"]),
    ("everything kept", ["id_rsa", "x/.env"], ["id_rsa", "x/.env"]),
    ("kept beside its own junk", ["k/.env", "k/j.bin"], ["k/.env"]),
    ("only junk under a kept path", ["k/.env", "k/deep/j.bin"], ["k/.env"]),
    ("unicode and spaces", ["a b/ü.bin", "a b/id_rsa"], ["a b/id_rsa"]),
]


@pytest.mark.parametrize(
    ("name", "files", "protected"), THIN_SHAPES, ids=[s[0] for s in THIN_SHAPES]
)
def test_thinning_keeps_exactly_what_it_was_told_to(
    tmp_path: Path, name: str, files: list[str], protected: list[str]
):
    """Every protected path survives, nothing else does, and the total adds up."""
    root = tmp_path / "tree"
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"0" * (1024 if relative in protected else 4096))
    keep = [root / relative for relative in protected]
    junk_bytes = sum(4096 for relative in files if relative not in protected)

    freed, _ = gt._thin_out(root, keep)

    for relative in protected:
        assert (root / relative).is_file(), f"{name}: {relative} was deleted"
    for relative in files:
        if relative not in protected:
            assert not (root / relative).exists(), f"{name}: {relative} survived"
    assert freed == junk_bytes, name
    assert root.is_dir(), f"{name}: the directory itself must remain"


def test_thinning_keeps_a_protected_directory_whole(tmp_path: Path):
    root = tmp_path / "tree"
    (root / "creds" / "inner").mkdir(parents=True)
    (root / "creds" / "inner" / "a").write_bytes(b"0" * 16)
    (root / "creds" / "b").write_bytes(b"0" * 16)
    (root / "junk.bin").write_bytes(b"0" * 4096)

    freed, _ = gt._thin_out(root, [root / "creds"])
    assert (root / "creds" / "inner" / "a").is_file()
    assert (root / "creds" / "b").is_file()
    assert not (root / "junk.bin").exists()
    assert freed == 4096


def test_thinning_does_not_follow_a_symlink_out_of_the_tree(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("REAL", encoding="utf-8")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "link").symlink_to(outside)
    (root / "junk.bin").write_bytes(b"0" * 4096)

    gt._thin_out(root, [])
    assert (outside / "precious").read_text(encoding="utf-8") == "REAL"
    assert not (root / "junk.bin").exists()


@needs_permissions
def test_thinning_reports_what_it_could_not_remove(tmp_path: Path):
    """A read-only parent stops one unlink; the rest still goes and nothing lies."""
    root = tmp_path / "tree"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "stuck.bin").write_bytes(b"0" * 4096)
    (root / "free.bin").write_bytes(b"0" * 2048)
    locked.chmod(0o555)
    try:
        freed, problem = gt._thin_out(root, [])
        assert not (root / "free.bin").exists()
        assert (locked / "stuck.bin").is_file(), "could not be removed"
        assert freed == 2048, "and was not counted as freed"
        assert problem, "and the caller is told, rather than reporting it emptied"
    finally:
        locked.chmod(0o755)


# --------------------------------------------------------------------------- #
# The quarantine round trip, over every shape of thing it can be handed
# --------------------------------------------------------------------------- #

QUARANTINE_SHAPES: list[tuple[str, list[str]]] = [
    ("one file", ["a.bin"]),
    ("several files", ["a.bin", "b.bin", "c.bin"]),
    ("a directory", ["d/one.bin", "d/two.bin"]),
    ("nested directories", ["d/e/f/deep.bin", "d/e/shallow.bin"]),
    ("spaces in the name", ["a file.bin", "a dir/inside.bin"]),
    ("unicode", ["ümlaut.bin", "日本/語.bin"]),
    ("a newline in the name", ["we\nird.bin"]),
    ("a dotfile", [".env", ".config/x.bin"]),
    ("an empty file", ["empty.bin"]),
    ("names that collide across directories", ["x/same.bin", "y/same.bin"]),
]


@pytest.mark.parametrize(
    ("name", "files"), QUARANTINE_SHAPES, ids=[s[0] for s in QUARANTINE_SHAPES]
)
def test_taking_then_restoring_is_the_identity(tmp_path: Path, name: str, files: list[str]):
    """Whatever went in comes back, at the same path with the same bytes.

    This is the promise the whole quarantine exists to make, and it is the one
    thing a user cannot check for themselves after the fact.
    """
    space = tmp_path / "space"
    contents = {}
    for index, relative in enumerate(files):
        path = space / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        body = b"" if relative.endswith("empty.bin") else bytes([index % 256]) * (index * 7 + 1)
        path.write_bytes(body)
        contents[relative] = body

    holding = gt.Quarantine(space / gt.QUARANTINE_DIRNAME, space, stamp="stamp")
    # Top-level entries only, as the sweep hands them over.
    for entry in sorted({Path(relative).parts[0] for relative in files}):
        holding.take(space / entry)
    holding.write_manifest()

    for relative in files:
        assert not (space / relative).exists(), f"{name}: {relative} did not move"

    actions = gt.restore(space / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert not [a for a in actions if a.error], actions
    for relative, body in contents.items():
        assert (space / relative).read_bytes() == body, f"{name}: {relative} came back wrong"


def test_restoring_twice_is_not_an_error_the_second_time(tmp_path: Path):
    """The second one has nothing to do, and must not claim it did something."""
    space = tmp_path / "space"
    space.mkdir()
    (space / "a.bin").write_bytes(b"x")
    holding = gt.Quarantine(space / gt.QUARANTINE_DIRNAME, space, stamp="stamp")
    holding.take(space / "a.bin")
    holding.write_manifest()

    first = gt.restore(space / gt.QUARANTINE_DIRNAME, "stamp", run())
    assert [a for a in first if a.applied]
    # A named stamp that is no longer there is worth an error naming it.
    with pytest.raises(gt.Failure, match="no quarantine 'stamp'"):
        gt.restore(space / gt.QUARANTINE_DIRNAME, "stamp", run())
    # Asking for "whatever is there" when nothing is, is not an error at all.
    second = gt.restore(space / gt.QUARANTINE_DIRNAME, None, run())
    assert not [a for a in second if a.applied]
    assert gt._reason_of(second[0].detail) != "other, see the lines marked -"
    assert (space / "a.bin").read_bytes() == b"x"


def test_a_run_clears_quarantines_past_their_retention(workspace: Path, capsys):
    """Expiring only ever happened by hand, so a daily run grew without bound."""
    old = workspace / gt.QUARANTINE_DIRNAME / "20240101T000000Z"
    (old / gt.CONTENT_DIRNAME).mkdir(parents=True)
    (old / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    (old / gt.CONTENT_DIRNAME / "old.bin").write_bytes(b"0" * 4096)
    os.utime(old, (0, 0))

    assert gt.main(["-C", str(workspace), "run", "--apply"]) == 0
    assert not old.exists()
    assert "quarantines deleted" in capsys.readouterr().out


def test_a_fresh_quarantine_is_left_alone_by_a_run(workspace: Path):
    fresh = workspace / gt.QUARANTINE_DIRNAME / "20990101T000000Z"
    (fresh / gt.CONTENT_DIRNAME).mkdir(parents=True)
    (fresh / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")

    gt.main(["-C", str(workspace), "run", "--apply"])
    assert fresh.exists()


def test_doctor_never_expires_anything(workspace: Path):
    """It reports; it does not remove, and that has to stay true."""
    old = workspace / gt.QUARANTINE_DIRNAME / "20240101T000000Z"
    (old / gt.CONTENT_DIRNAME).mkdir(parents=True)
    (old / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    os.utime(old, (0, 0))

    gt.main(["-C", str(workspace), "doctor", "--apply"])
    assert old.exists()


def test_retention_days_zero_switches_the_sweep_off(workspace: Path):
    old = workspace / gt.QUARANTINE_DIRNAME / "20240101T000000Z"
    (old / gt.CONTENT_DIRNAME).mkdir(parents=True)
    (old / gt.MANIFEST_NAME).write_text('{"entries": []}', encoding="utf-8")
    os.utime(old, (0, 0))
    (workspace / gt.CONFIG_NAMES[0]).write_text("trash:\n  retention_days: 0\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "run", "--apply"])
    assert old.exists(), "0 means never, not immediately"


# --------------------------------------------------------------------------- #
# Stopping when it is the network
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("message", "network"),
    [
        ("fatal: unable to access 'https://x/': Could not resolve host: x", True),
        ("ssh: connect to host x port 22: Operation timed out", True),
        ("fatal: unable to access 'https://x/': Failed to connect to proxy", True),
        ("ssh: connect to host x port 22: Connection refused", True),
        ("fatal: repository 'https://x/y.git/' not found", False),
        ("remote: Permission to x denied to y", False),
        ("fatal: Authentication failed for 'https://x/'", False),
        ("error: cannot lock ref 'refs/remotes/origin/main'", False),
    ],
)
def test_only_network_errors_count_as_unreachable(message: str, network: bool):
    """A wrong guess here abandons a whole run over one broken remote URL."""
    assert gt.looks_unreachable(message) is network


def _offline_workspace(root: Path, count: int) -> Path:
    origin = root / "origin.git"
    git(root, "init", "--bare", "-q", "-b", "main", str(origin))
    seed = root / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    commit(seed, "README.md", "hello\n")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")
    space = root / "space"
    space.mkdir()
    for number in range(count):
        git(space, "clone", "-q", str(origin), f"r{number}")
        # A distinct host each: the give-up counts unreachable *remotes*, and
        # one dead URL cloned six times is one dead URL.
        git(space / f"r{number}", "remote", "set-url", "origin", f"https://x{number}.invalid/r")
    return space


def test_a_run_stops_once_the_network_is_clearly_the_problem(tmp_path: Path, capsys):
    """256 repositories times a 120-second timeout is most of a working day."""
    space = _offline_workspace(tmp_path, 6)

    assert gt.main(["-C", str(space), "sync", "--apply", "-j", "1"]) == 1
    out = capsys.readouterr().out
    assert "could not reach 3 remotes in a row" in out
    assert "VPN" in out and "proxy" in out
    attempted = [line for line in out.splitlines() if line.lstrip().startswith("! r")]
    assert len(attempted) == gt.OFFLINE_AFTER, attempted


def test_clean_still_works_with_no_network(tmp_path: Path):
    """The message says so, so it had better be true."""
    space = _offline_workspace(tmp_path, 4)
    (space / "r0" / "__pycache__").mkdir()
    (space / "r0" / "__pycache__" / "m.pyc").write_bytes(b"0" * 64)

    assert gt.main(["-C", str(space), "clean", "--apply"]) == 0
    assert not (space / "r0" / "__pycache__").exists()


def test_one_broken_remote_does_not_abandon_the_run(tmp_path: Path, capsys):
    space = _offline_workspace(tmp_path, 1)
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(space / "fine"))

    assert gt.main(["-C", str(space), "sync", "--apply", "-j", "1"]) == 1
    out = capsys.readouterr().out
    assert "could not reach the remote for" not in out
    assert "fine" in out


def test_clean_keep_protects_a_file_inside_an_ignored_directory(workspace: Path):
    """clean.keep named it by hand, and it was hard-deleted anyway.

    switched_off_rules leaves ignored_keep and clean.keep to _remove on purpose
    — refusing the whole directory for them is what made turning clean.ignored
    on reclaim nothing — but only ignored_keep was ever handed over.
    """
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore scratch")
    (repo / "scratch" / "tmpfiles").mkdir(parents=True)
    (repo / "scratch" / "tmpfiles" / "big.bin").write_bytes(b"0" * 4096)
    kept = repo / "scratch" / "notes-i-need.md"
    kept.write_text("HAND WRITTEN", encoding="utf-8")
    (workspace / gt.CONFIG_NAMES[0]).write_text(
        'clean:\n  ignored: true\n  keep:\n    - "notes-i-need.md"\n', encoding="utf-8"
    )

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert kept.read_text(encoding="utf-8") == "HAND WRITTEN"
    assert not (repo / "scratch" / "tmpfiles").exists(), "the junk around it still goes"


def test_local_state_covers_both_lists():
    both = gt.local_state_of(gt.DEFAULTS["clean"])
    assert set(gt.DEFAULTS["clean"]["ignored_keep"]) <= set(both)
    assert set(gt.DEFAULTS["clean"]["keep"]) <= set(both)


# --------------------------------------------------------------------------- #
# Claude review, round thirteen
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("days", [0, -1])
def test_expire_never_runs_without_a_positive_retention(tmp_path: Path, days: int):
    """0 meant "cutoff = now", so `restore --expire` deleted everything.

    A negative one moved the cutoff into the future and took the quarantine the
    running command had just written, before its manifest was even flushed.
    """
    root = tmp_path / gt.QUARANTINE_DIRNAME
    fresh = root / "20990101T000000Z"
    fresh.mkdir(parents=True)
    (fresh / gt.MANIFEST_NAME).write_text("{}", encoding="utf-8")

    assert gt.expire_quarantines(root, days, run()) == []
    assert fresh.exists()


@pytest.mark.parametrize("key", ["retention_days", "min_age_days"])
def test_a_negative_count_is_refused_where_it_is_written(key: str):
    with pytest.raises(gt.Failure, match="cannot be negative"):
        gt._merge(gt.DEFAULTS, {"trash": {key: -1}}, "test")


def test_a_403_is_that_repositorys_problem_not_the_networks():
    """git prefixes every HTTP failure with "unable to access", 403 included."""
    forbidden = "fatal: unable to access 'https://x/y.git/': The requested URL returned error: 403"
    assert gt.looks_unreachable(forbidden) is False
    assert gt.looks_unreachable("fatal: unable to access 'https://x/': Could not resolve host: x")


def test_a_successful_fetch_resets_the_offline_count(tmp_path: Path):
    """ "Three in a row" was "three ever", so three dead remotes anywhere in a
    256-repository workspace abandoned every run from then on."""
    context = gt.Context(
        workspace=tmp_path,
        resolver=gt.ConfigResolver(tmp_path, {}),
        decider=run(),
        printer=quiet_printer(),
        repos=[],
        quarantine=gt.Quarantine(tmp_path / gt.QUARANTINE_DIRNAME, tmp_path),
    )
    for number in range(gt.OFFLINE_AFTER - 1):
        context.note_unreachable(f"https://dead{number}/", "Could not resolve host")
    assert not context.giving_up
    context.note_reachable()
    context.note_unreachable("https://dead9/", "Could not resolve host")
    assert not context.giving_up, "the count starts again after one that worked"


def test_one_unreachable_remote_is_counted_once(tmp_path: Path):
    """A repository and its linked worktrees all fetch the same remote."""
    context = gt.Context(
        workspace=tmp_path,
        resolver=gt.ConfigResolver(tmp_path, {}),
        decider=run(),
        printer=quiet_printer(),
        repos=[],
        quarantine=gt.Quarantine(tmp_path / gt.QUARANTINE_DIRNAME, tmp_path),
    )
    for _ in range(gt.OFFLINE_AFTER + 2):
        context.note_unreachable("https://one-remote/", "Could not resolve host")
    assert not context.giving_up, "one remote, however many checkouts of it"


def test_a_partly_finished_run_does_not_claim_nothing_changed(tmp_path: Path, capsys):
    """It printed "Nothing was changed" two lines above "3 fast-forwarded"."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    commit(seed, "README.md", "hello\n")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")
    space = tmp_path / "space"
    space.mkdir()
    # a- before z-, so the working ones are reached first and there is
    # something to have been changed by the time the network gives out.
    for number in range(gt.OFFLINE_AFTER + 1):
        git(space, "clone", "-q", str(origin), f"a-ok{number}")
    for number in range(gt.OFFLINE_AFTER):
        git(space, "clone", "-q", str(origin), f"z-bad{number}")
        git(space / f"z-bad{number}", "remote", "set-url", "origin", f"https://x{number}.invalid/r")

    gt.main(["-C", str(space), "sync", "--apply", "-j", "1"])
    out = capsys.readouterr().out
    assert "Nothing was changed" not in out
    assert "already been fetched" in out


@needs_permissions
def test_a_thin_out_that_could_not_finish_says_so(workspace: Path, capsys):
    """It reported "emptied out" and the full size freed, with 97 KB still there."""
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    scratch = repo / "scratch"
    (scratch / "locked").mkdir(parents=True)
    (scratch / "locked" / "big.bin").write_bytes(b"0" * 40960)
    (scratch / ".env").write_text("SECRET", encoding="utf-8")
    (workspace / gt.CONFIG_NAMES[0]).write_text("clean:\n  ignored: true\n", encoding="utf-8")
    (scratch / "locked").chmod(0o555)
    try:
        gt.main(["-C", str(workspace), "clean", "--apply"])
        out = capsys.readouterr().out
        assert "failed" in out
        assert (scratch / "locked" / "big.bin").exists()
        assert (scratch / ".env").read_text(encoding="utf-8") == "SECRET"
    finally:
        (scratch / "locked").chmod(0o755)


def test_nothing_expires_when_the_run_applied_nothing(workspace: Path):
    """ "a clean that applies anything", says the README and the man page."""
    old = workspace / gt.QUARANTINE_DIRNAME / "20240101T000000Z"
    old.mkdir(parents=True)
    (old / gt.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    os.utime(old, (0, 0))
    (workspace / gt.CONFIG_NAMES[0]).write_text("clean:\n  enabled: false\n", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    assert old.exists()


@pytest.mark.parametrize("text", ["jobs: 4\t\n", "jobs: 4\x00\n", "[a]: 1\n", "{a: b}: 1\n"])
def test_the_parser_refuses_what_pyyaml_refuses_here_too(text: str):
    """A tab outside the indentation ran fine on every shipped binary.

    A flow collection as a key was worse: an unhashable dict key is a bare
    TypeError rather than anything a person can act on.
    """
    yaml = pytest.importorskip("yaml")
    with pytest.raises(Exception):  # noqa: B017 - the point is that it does not parse
        yaml.safe_load(text)
    with pytest.raises(gt.Failure):
        gt._parse_yaml_subset(text, "<t>")


# --------------------------------------------------------------------------- #
# doctor --fix
# --------------------------------------------------------------------------- #


def test_doctor_still_changes_nothing_without_fix(workspace: Path):
    """It has reported and only reported since the first version."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "--detach", "HEAD")
    git(repo, "remote", "set-url", "origin", "https://a:tok@example.invalid/r.git")

    gt.main(["-C", str(workspace), "doctor", "--apply"])
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert "tok" in git(repo, "remote", "get-url", "origin")


def test_fix_puts_a_detached_head_back_on_the_trunk(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "--detach", "HEAD")

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_fix_refuses_a_detached_head_that_is_holding_the_work(workspace: Path):
    """Switching away would leave those commits reachable from the reflog only."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "--detach", "HEAD")
    commit(repo, "only-here.txt")
    detached = git(repo, "rev-parse", "HEAD").strip()

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(repo, "rev-parse", "HEAD").strip() == detached
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"


def test_fix_refuses_a_detached_head_with_uncommitted_changes(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "--detach", "HEAD")
    (repo / "README.md").write_text("edited", encoding="utf-8")

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert (repo / "README.md").read_text(encoding="utf-8") == "edited"


def test_fix_takes_the_credential_out_of_the_url(workspace: Path):
    repo = workspace / "repo"
    git(repo, "remote", "set-url", "origin", "https://alice:ghp_tok@example.invalid/r.git")

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(repo, "remote", "get-url", "origin").strip() == "https://example.invalid/r.git"


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://a:tok@host/r.git", "https://host/r.git"),
        ("https://a:tok@host/deep/path/r.git", "https://host/deep/path/r.git"),
        ("http://u:p@host:8443/r.git", "http://host:8443/r.git"),
        ("ssh://git@host/r.git", None),
        ("https://host/r.git", None),
        ("git@host:org/r.git", None),
    ],
)
def test_a_url_without_its_credential_is_still_a_url(url: str, want: str | None):
    """sub(r"\\1", url) looked right and ate the :// with the credential."""
    assert gt.without_credential(url) == want


def test_fix_leaves_unpushed_work_alone(workspace: Path, capsys):
    """The whole line between what --fix does and what it only reports."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "-c", "feature/mine")
    commit(repo, "only-here.txt")

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(repo, "branch", "--list", "feature/mine").strip()
    assert "not pushed" in capsys.readouterr().out


def test_fix_is_a_dry_run_like_everything_else(workspace: Path, capsys):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "--detach", "HEAD")

    gt.main(["-C", str(workspace), "doctor", "--fix"])
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert "would switch back to main" in capsys.readouterr().out


def test_run_takes_fix_too(workspace: Path):
    repo = workspace / "repo"
    git(repo, "switch", "-q", "--detach", "HEAD")

    assert gt.main(["-C", str(workspace), "run", "--fix", "--apply"]) == 0
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


# --------------------------------------------------------------------------- #
# Claude review, round fourteen — doctor --fix, an hour after it shipped
# --------------------------------------------------------------------------- #


def test_fix_does_not_replace_an_ignored_local_file(workspace: Path, remote: Path):
    """is_dirty ignores ignored files, so a local .env was invisible to it.

    In one `run --fix --apply` the tool printed sync's refusal, did the thing it
    had refused, and then summarised it as held back.
    """
    repo = workspace / "repo"
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore .env")
    git(repo, "add", "-f", ".env") if False else None
    (repo / ".env").write_text("PLACEHOLDER", encoding="utf-8")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-q", "-m", "track a template")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "switch", "-q", "--detach", "HEAD~1")
    (repo / ".env").write_text("SECRET_TOKEN=real", encoding="utf-8")

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert (repo / ".env").read_text(encoding="utf-8") == "SECRET_TOKEN=real"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"


def test_fix_leaves_a_bisect_alone(workspace: Path):
    """The BISECT_* files survive a switch, so the next `good` marks the trunk."""
    repo = workspace / "repo"
    for number in range(5):
        commit(repo, f"c{number}.txt")
    git(repo, "push", "-q", "origin", "main")
    oldest = git(repo, "rev-parse", "HEAD~4").strip()
    git(repo, "bisect", "start")
    git(repo, "bisect", "bad")
    git(repo, "bisect", "good", oldest)

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert (repo / ".git" / "BISECT_LOG").exists()


def test_fix_leaves_a_linked_worktree_on_its_own_head(workspace: Path):
    """Holding its own HEAD is the entire reason a linked worktree exists."""
    repo = workspace / "repo"
    commit(repo, "second.txt")
    linked = workspace / "wt"
    git(repo, "worktree", "add", "--detach", "-q", str(linked), "HEAD~1")

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(linked, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"


def test_a_quit_keeps_the_fixes_already_made(workspace: Path):
    """ "everything already done is kept" has to be true of doctor too."""
    repo = workspace / "repo"
    git(repo, "switch", "-q", "--detach", "HEAD")
    git(repo, "remote", "set-url", "origin", "https://a:tok@example.invalid/r.git")
    answers = iter(["y", "q"])

    decider = gt.Decider(gt.ASK, prompt_input=lambda _: next(answers))
    try:
        gt.doctor_repo(repo, "repo", config(), decider)
    except gt.Quit as quit_now:
        assert any(a.applied for a in quit_now.done), quit_now.done
    else:  # pragma: no cover - the second answer is q
        pytest.fail("q should raise Quit")


def test_each_remedy_asks_for_its_own_consent(workspace: Path):
    """One `a` to a HEAD switch used to rewrite remote URLs workspace-wide."""
    kinds = {"switch back", "strip credential", "pack"}
    seen = {gt.Action(kind, "repo", "target", "detail").consent_key for kind in kinds}
    assert len(seen) == len(kinds), seen


def test_a_credential_from_insteadof_is_not_this_repositorys(workspace: Path, tmp_path: Path):
    """`git remote get-url` expands insteadOf; .git/config was already clean."""
    repo = workspace / "repo"
    git(repo, "remote", "set-url", "origin", "https://example.invalid/org/r.git")
    git(
        repo,
        "config",
        "url.https://u:s3cr3t@example.invalid/.insteadOf",
        "https://example.invalid/",
    )

    actions = gt.doctor_repo(repo, "repo", config())
    assert not [a for a in actions if "credential" in a.detail], actions


def test_a_credential_in_a_pushurl_is_found(workspace: Path):
    """get-url returns the first fetch URL only, so this sat there unmentioned."""
    repo = workspace / "repo"
    git(repo, "config", "remote.origin.pushurl", "https://a:pushtok@example.invalid/r.git")

    actions = gt.doctor_repo(repo, "repo", config())
    assert [a for a in actions if "credential" in a.detail], actions

    gt.main(["-C", str(workspace), "doctor", "--fix", "--apply"])
    assert git(repo, "config", "--get", "remote.origin.pushurl").strip() == (
        "https://example.invalid/r.git"
    )


@pytest.mark.parametrize(
    ("url", "credential"),
    [
        ("https://a:tok@host/r.git", True),
        ("https://ghp_abc123@github.com/o/r.git", True),
        ("https://:tok@host/r.git", True),
        ("HTTPS://ghp_x@host/r.git", True),
        ("ssh://u:p@host/r.git", True),
        ("ssh://git@host/r.git", False),
        ("ssh://git@host:7999/x.git", False),
        ("git@host:o/r.git", False),
        ("https://host/r.git", False),
    ],
)
def test_what_counts_as_a_credential_in_a_url(url: str, credential: bool):
    """A bare token@ is a PAT over https and an ordinary username over ssh."""
    assert bool(gt.CREDENTIAL_IN_URL.match(url)) is credential


@pytest.mark.parametrize(
    "text",
    [
        "jobs: 4  # workers\there\n",
        "# a\tcomment\njobs: 4\n",
        'clean:\n  keep:\n    - "a\tb"\n',
        "jobs: 4\n",
        "a: b   \n",
    ],
)
def test_a_tab_where_yaml_allows_one_is_allowed(text: str):
    """The check scanned the raw line, so a tab in a comment killed the config.

    `git-tidy init` writes a comment-heavy file that people then edit, and it
    loaded from a checkout while refusing on every shipped binary.
    """
    yaml = pytest.importorskip("yaml")
    assert gt._parse_yaml_subset(text, "<t>") == yaml.safe_load(text)


@pytest.mark.parametrize("text", ["jobs: 4\t\n", "a: b\x0c\n", "a: 4\x0cclean:\n  ignored: true\n"])
def test_a_tab_where_yaml_forbids_one_is_refused(text: str):
    with pytest.raises(gt.Failure):
        gt._parse_yaml_subset(text, "<t>")


def test_a_hanging_fetch_counts_as_unreachable(tmp_path: Path, capsys):
    """A dropped VPN hangs rather than refusing, which is the case it was for.

    Git.run *raises* on a timeout, so it never became a fetch action with an
    error on it, and the give-up never saw the one shape it was written for:
    256 repositories each waited the full sync.timeout.
    """
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    space = tmp_path / "space"
    space.mkdir()
    for number in range(gt.OFFLINE_AFTER + 3):
        git(space, "clone", "-q", str(origin), f"r{number}")
        git(space / f"r{number}", "remote", "set-url", "origin", f"ssh://git@h{number}.invalid/r")
    slow = tmp_path / "slow-ssh"
    slow.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    slow.chmod(0o755)
    (space / gt.CONFIG_NAMES[0]).write_text("sync:\n  timeout: 2\n", encoding="utf-8")

    os.environ["GIT_SSH_COMMAND"] = str(slow)
    try:
        gt.main(["-C", str(space), "sync", "--apply", "-j", "1"])
    finally:
        os.environ.pop("GIT_SSH_COMMAND", None)
    out = capsys.readouterr().out
    assert "could not reach 3 remotes in a row" in out
    attempted = [line for line in out.splitlines() if line.lstrip().startswith("! r")]
    assert len(attempted) == gt.OFFLINE_AFTER, attempted


def test_a_failed_repository_is_named(workspace: Path, capsys):
    """`! -: - — git fetch timed out` tells you nothing in a workspace of 256."""
    actions: list[gt.Action] = []
    with gt.reporting(actions, "the-repo"):
        raise gt.Failure("something went wrong")
    assert actions[0].scope == "the-repo"
