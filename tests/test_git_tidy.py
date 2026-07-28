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


def test_init_asks_and_records_the_answers(tmp_path: Path):
    target = tmp_path / ".git-tidy.yaml"
    answers = iter(["4", "y", "n", "n", "y", "y", "y"])
    gt.cmd_init(
        target, gt.ASK, force=False, printer=quiet_printer(), prompt_input=lambda _: next(answers)
    )
    parsed = gt._parse_yaml_subset(target.read_text(encoding="utf-8"), "<init>")
    assert parsed["jobs"] == 4
    assert parsed["clean"]["ignored"] is True
    assert parsed["trash"]["enabled"] is True


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
    actions = gt.prune_branches(repo, "repo", config(branches={"require_merged": False}), run())
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
    repo = workspace / "repo"
    make_gone_branch(repo, remote, "feature")
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "feature")

    actions = gt.prune_branches(repo, "repo", config(), run())
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").split()
    assert any(a.error for a in actions if a.target == "feature")


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
