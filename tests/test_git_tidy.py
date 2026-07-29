"""Tests for git-tidy.

Almost everything here runs against real git repositories in a temp directory —
a bare repo standing in for the remote, and clones of it for the work trees.
Mocking git would only prove that the mock behaves the way the test author
imagined, and the interesting cases (a branch whose upstream is gone, a repo that
has diverged, a path that .gitignore covers) are precisely the ones where that
imagination tends to be wrong.
"""

from __future__ import annotations

import io
import json
import os
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


def test_yaml_empty_list_value_is_a_list_not_none():
    """`dirs:` with nothing after it must not leave None where a list is iterated."""
    merged = gt._merge(gt.DEFAULTS, {"clean": {"dirs": None}}, "test")
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
    assert gt.cmd_init(target, gt.DRY, force=False, printer=quiet_printer()) == 0
    assert target.is_file()
    gt.ConfigResolver(tmp_path).for_path(tmp_path)  # must not raise


def test_init_refuses_to_overwrite(tmp_path: Path):
    target = tmp_path / ".git-tidy.yaml"
    target.write_text("jobs: 1\n", encoding="utf-8")
    with pytest.raises(gt.Failure, match="already exists"):
        gt.cmd_init(target, gt.DRY, force=False, printer=quiet_printer())
    assert gt.cmd_init(target, gt.DRY, force=True, printer=quiet_printer()) == 0


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
    assert actions[0].detail == "no remote configured"


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
    with pytest.raises(gt.Failure, match=r"sync\.switch"):
        gt.sync_repo(repo, "repo", config(sync={"switch": "sideways"}), run())


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
    (repo / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_bytes(b"0" * 100)
    (repo / "debug.log").write_text("noise", encoding="utf-8")
    (repo / "src.txt").write_text("kept", encoding="utf-8")

    gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert not (repo / "build").exists()
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

    assert (holding.dir / "repo" / "__pycache__" / "m.pyc").read_text(encoding="utf-8") == "first"
    assert (holding.dir / "second" / "__pycache__" / "m.pyc").read_text(
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
    assert held and held[0].skipped and "checked out in" in held[0].detail
    assert not any(a.error for a in actions)


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
    assert (workspace / gt.QUARANTINE_DIRNAME / "stamp" / junk.name).is_file()


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
    assert (holding.dir / "splunk_token.pw").is_file(), "a token must never be hard-deleted"


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


def test_main_rejects_zero_jobs(workspace: Path):
    with pytest.raises(gt.Failure, match=r"--jobs"):
        gt.main(["-C", str(workspace), "-j", "0", "clean"])


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
    assert gt.main(["-C", str(workspace), "init"]) == 0
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
    answers = iter(["4", "y", "n", "n", "y", "n", "y", "30"])
    gt.cmd_init(
        target, gt.AUTO, force=False, printer=quiet_printer(), prompt_input=lambda _: next(answers)
    )
    parsed = gt._parse_yaml_subset(target.read_text(encoding="utf-8"), "<init>")
    assert parsed["jobs"] == 4
    assert parsed["clean"]["ignored"] is True
    assert parsed["trash"] == {"enabled": True, "min_age_days": 30}


def test_init_dry_run_writes_the_plain_template(tmp_path: Path):
    target = tmp_path / ".git-tidy.yaml"
    gt.cmd_init(target, gt.DRY, force=False, printer=quiet_printer())
    assert gt._parse_yaml_subset(target.read_text(encoding="utf-8"), "<init>") is None


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
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore build")
    deep = repo / "build" / "config"
    deep.mkdir(parents=True)
    (deep / "server.pem").write_text("PRIVATE KEY", encoding="utf-8")
    (repo / "build" / "out.bin").write_bytes(b"0" * 64)

    actions = gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert (deep / "server.pem").is_file(), "the key must survive its parent"
    assert actions and actions[0].skipped and "server.pem" in actions[0].detail


def test_clean_ignored_still_removes_a_directory_with_nothing_protected(workspace: Path):
    repo = workspace / "repo"
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore build")
    (repo / "build" / "sub").mkdir(parents=True)
    (repo / "build" / "sub" / "out.bin").write_bytes(b"0" * 64)

    gt.clean_ignored(repo, "repo", config(), run(), None, gt.Git(repo))
    assert not (repo / "build").exists()


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
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore build")
    deep = repo / "build" / "config"
    deep.mkdir(parents=True)
    (deep / "server.crt").write_text("CERT", encoding="utf-8")

    cfg = config(clean={"ignored_keep": ["build/config/*.crt"]})
    actions = gt.clean_ignored(repo, "repo", cfg, run(), None, gt.Git(repo))
    assert (deep / "server.crt").is_file()
    assert actions and actions[0].skipped


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
    with pytest.raises(gt.Failure, match=r"sync\.worktrees"):
        gt.sync_repo(repo, "repo", config(sync={"worktrees": "skpi"}), run())


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
        p.read_text(encoding="utf-8") for p in (holding.dir / "a").iterdir() if p.is_file()
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
    assert (holding.dir / "repo" / "__pycache__").exists()


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
    quarantined = list((workspace / gt.QUARANTINE_DIRNAME).glob("*/careful/__pycache__/m.pyc"))
    assert quarantined, "the deeper config asked for this one to be recoverable"
    assert not list((workspace / gt.QUARANTINE_DIRNAME).glob("*/plain/*")), "and only that one"


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


def test_a_credential_inside_a_disposable_directory_is_moved_not_deleted(workspace: Path):
    """The promise trash makes, kept by clean too."""
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
    assert not cache.exists()
    assert (holding.dir / "repo" / ".terraform" / "client.pem").is_file()
    assert any("contains" in a.detail and a.quarantined for a in actions)


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
    assert (holding.dir / "repo" / "api-token.txt").read_text(encoding="utf-8") == "the only copy"
    assert any(a.quarantined and "api-token" in a.target for a in actions)


# --------------------------------------------------------------------------- #
# Round twenty-eight of review
# --------------------------------------------------------------------------- #


def test_a_credential_matched_by_a_file_pattern_is_quarantined(workspace: Path):
    """The guarantee cannot depend on which mechanism found the file."""
    repo = workspace / "repo"
    (repo / "api-token.pyc").write_text("the only copy", encoding="utf-8")
    (repo / "module.pyc").write_text("disposable", encoding="utf-8")
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
    assert not (repo / "module.pyc").exists()
    assert (holding.dir / "repo" / "api-token.pyc").read_text(encoding="utf-8") == "the only copy"


def test_a_loose_credential_is_quarantined_too(workspace: Path):
    loose = workspace / "loose"
    loose.mkdir()
    (loose / "api-token.pyc").write_text("the only copy", encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    found = list((workspace / gt.QUARANTINE_DIRNAME).glob("*/loose/api-token.pyc"))
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
    assert (holding.dir / "loose" / "lalalalala" / "id_rsa.pem").is_file()
    assert actions and "contains" in actions[0].detail


# --------------------------------------------------------------------------- #
# Round thirty-three of review
# --------------------------------------------------------------------------- #


def test_a_deeper_sensitive_list_protects_a_loose_credential(workspace: Path):
    """The guarantee has to follow the config that governs the path."""
    loose = workspace / "loose"
    loose.mkdir()
    (loose / "vault.pyc").write_text("the only copy", encoding="utf-8")
    (loose / ".git-tidy.yaml").write_text('trash:\n  sensitive: ["vault*"]\n', encoding="utf-8")

    gt.main(["-C", str(workspace), "clean", "--apply"])
    found = list((workspace / gt.QUARANTINE_DIRNAME).glob("*/loose/vault.pyc"))
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
    (loose / "api-token.pyc").write_text("the only copy", encoding="utf-8")

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
