# git-tidy

Keep a directory full of git checkouts clean. One command fetches every repo,
puts each back on its default branch, deletes the branches whose upstream is
gone, and reclaims the tens of gigabytes that build tools left behind.

```bash
git-tidy run              # show what it would do, change nothing
git-tidy run --ask        # confirm each change: y/n, a/s for all of a kind, q to stop
git-tidy run --apply      # do all of it
```

One file, standard library only. Run it straight from a checkout with
`./git_tidy.py`, install it as a `git-tidy` command, or download a self-contained
binary — see [Install](#install).

Nothing is modified without `--ask` or `--apply`.

## Why

A machine that works across a lot of repositories accumulates three problems at
once, and they are usually dealt with by hand, badly:

- **Stale checkouts.** Two hundred clones, each on whatever branch you left it,
  most of them behind. `for d in */; do (cd "$d" && git pull); done` merges when
  it should not, and stops at the first repo that asks for a password.
- **Dead branches.** The pull request merged three months ago; the local branch
  is still there. So are forty others. Some of them still hold work you never
  pushed, and telling those apart is the whole difficulty.
- **Build output.** `.terraform`, `node_modules`, `__pycache__`, `.gradle`,
  scanner caches. On a working laptop this is routinely **tens of gigabytes** —
  in the workspace this was written for, 40 GB of `.terraform` alone.

git-tidy does the safe parts automatically, and reports the parts that need you.

## What each command does

| Command            | Does                                                                              |
| ------------------ | --------------------------------------------------------------------------------- |
| `git-tidy sync`    | Fetch, prune remote refs, switch to the default branch, fast-forward              |
| `git-tidy prune`   | Delete local branches whose upstream is gone, once their commits are in the trunk |
| `git-tidy clean`   | Remove build output and caches, including everything `.gitignore` already covers  |
| `git-tidy trash`   | Sweep loose junk files into a quarantine you can undo                             |
| `git-tidy doctor`  | Report what needs a human, and change nothing                                     |
| `git-tidy run`     | All of the above, in that order                                                   |
| `git-tidy init`    | Write a commented config file, globally or for one directory                      |
| `git-tidy config`  | Print the effective config for any path                                           |
| `git-tidy restore` | Put quarantined files back                                                        |

### sync

Fetches, then **fast-forwards only**. A repo that has diverged from its upstream
is reported and left alone — no merge, no rebase, no `--force-with-lease`, no
surprises in the reflog.

The one exception is opt-in and off by default: `sync.diverged: rebase` replays
the local commits on top of the upstream ones. It aborts and reports if that
conflicts, rather than leaving a half-applied rebase behind, but it does rewrite
commit ids — the originals stay reachable through the reflog.

The default branch is whatever the remote's own `HEAD` points at, not a hardcoded
`main`. A repo whose worktree has uncommitted changes stays on its branch.

### prune

A local branch is deleted only when **both** are true:

1. its upstream is gone from the remote (`[gone]` after a pruning fetch), and
2. its commits are already contained in the default branch.

A branch that fails the second test is *reported*, with the number of commits
that would be lost:

```text
  - platform: feature/SIG-4912-subnet-az — kept: 3 commits not in origin/main
```

`branches.keep` protects names outright; `main`, `master`, `trunk`, `develop` and
`release/*` are on that list by default.

**Squash and rebase merges look unmerged.** The containment test is
`git merge-base --is-ancestor`, and a squash merge produces a *new* commit with
different parents, so the original commits are genuinely not ancestors of the
trunk. git-tidy therefore keeps those branches and tells you why. That is
deliberate: the alternative is guessing that content which merely looks similar
is the same work, and guessing wrong here deletes commits.

If your team squash-merges everything, the way to clear them out is
`branches.require_merged: false`, or the `--force` that turns it on. That skips
the containment test, so it is the one setting here that can lose work.

It also only takes effect where a fetch succeeded in the same run — the `[gone]`
mark is a cached observation, and one from last week is not evidence the branch
is still gone. `prune` on its own does not fetch, so use `run`:

```bash
git-tidy run --force --ask     # fetches, then asks about each unmerged branch
```

`prune --ask` will not offer them: a branch the containment test keeps is
reported, not proposed, so there is nothing to answer.

### clean

Two mechanisms, and the first is usually all you need:

- **`clean.ignored`** — remove everything `.gitignore` already calls disposable,
  the same set as `git clean -Xd`. Off by default, because "ignored" also covers
  local-only files that are ignored *on purpose*: `.env`, `*.tfstate`, `*.pem`.
  Those are listed in `clean.ignored_keep` and are never removed. A directory
  holding one is **emptied out around it**: the file stays exactly where it is,
  because an application reads it from that path, and everything else in the
  directory is still reclaimed.

  So a 400 MB `node_modules` with one `id_rsa` in it gives back 400 MB and
  leaves the key where it was. Inside a path listed in `clean.regenerable`
  (`.terraform`, `.gradle`, `.next` …) `ignored_keep` does not apply at all,
  because those are caches a tool rebuilds — every `.terraform` holds a
  `terraform.tfstate`, which is the backend pointer, not your state.

  `trash.sensitive` holds everywhere, but not for source code: a `tokenizer.js`
  or a `pygments/token.py` matches `*token*` and is not a secret, so anything
  with a source-code extension is exempt. If it really is source, it is
  committed, and `clean` does not touch tracked files.
- **`clean.dirs` / `clean.files`** — names removed wherever they appear, ignored
  or not: `.terraform`, `.terragrunt-cache`, `__pycache__`, `.pytest_cache`,
  `.scannerwork`, `*.pyc`, `.coverage`, and so on.

Dependency trees (`node_modules`, `.venv`, `venv`, `vendor`, `.bundle`) and
build directories (`dist`, `build`, `target`, `out`) are **off by default** —
including under `clean.ignored`, which would otherwise take them, since
`.gitignore` covers them in practically every repository. Dependency trees are
expensive to restore without a network, and every one of those build directory
names is also a perfectly ordinary source directory name. Turn them on with
`clean.dependencies` and `clean.builds`.

Inside a repository, a **tracked file is never deleted**, however much it looks
like an artefact — unless `clean.tracked: true` says so, which is off by default
and the only way `clean` will touch committed content. Symlinks are never followed. Nested repositories are left for
their own pass.

### trash

The one part that cannot be inferred from git, so it is off until you turn it on.
It looks for loose files in the workspace — not inside repositories — that are

- older than `trash.min_age_days` (7 by default), **and**
- match a configured glob, or one of the heuristics: `mash` (keyboard-mash names
  like `asjfoisjdgipfdspigfjdpi.txt` or `lalalalala.log`), `empty` (zero bytes),
  `temp` (`*~`, `*.swp`, `*.swo`, `*.orig`, `*.rej`, `*.bak`, `*.tmp`, `*.old`).
  Note the last two: with `trash.dirs: true`, which is off by default, a
  `project.old/` somebody parked is swept whole — to quarantine.

Everything swept is **moved to a quarantine**, not deleted, with a manifest:

```bash
git-tidy restore --list          # what is in there
git-tidy restore --apply         # put the newest sweep back
git-tidy restore --expire --apply  # drop them now, without waiting for a run
```

A `clean`, `trash` or `run` that applies anything also drops the quarantines
older than `trash.retention_days` (30 by default; `0` keeps them for ever), so a
daily run does not leave the workspace growing a second copy of everything it
ever removed. A run that applied nothing expires nothing, and `doctor` and
`config` still change nothing at all.

Files that look like credentials — `*.pw`, `*.secret`, `*.pem`, `*creds*`,
`*token*` — are always quarantined rather than deleted, even with quarantine
switched off. A token on disk may be the only copy.

### when the network is the problem

If three *remotes* in a row cannot be reached, the run stops instead of working
through the rest on the same timeout — 256 repositories at the default 120
seconds is most of a working day — and says to check the VPN, the proxy
(`http_proxy`, `https_proxy`, and git's own `http.proxy`), DNS and the SSH
agent. Remotes, not checkouts: a repository and its linked worktrees fetch the
same URL, and one dead URL is one dead URL. In a row, too — one fetch that
works starts the count again.

Only errors that are about the network count. A repository that is simply gone,
or a permission denial (git writes `unable to access …: The requested URL
returned error: 403` for both a missing repository and one you cannot read), is
that repository's problem and the run carries on. Whatever was fetched or
fast-forwarded before the network went is done, and the message says so rather
than claiming nothing changed. `clean` and `trash` do not need the network at
all.

### doctor

Changes nothing. Reports:

- **credentials embedded in a remote URL** — `https://user:token@host/repo.git`
  leaves that token in plain text in `.git/config`, and it is easy to miss for
  years. The report redacts it.
- detached HEADs, commits that exist only locally, repositories with no remote,
  and a `.git` big enough to be worth a `git gc`.

`doctor --fix` (also `run --fix`) carries out the three of those that cannot
cost a commit: it puts a detached HEAD back on the trunk, takes the credential
out of a remote URL, and packs an oversized `.git`. Like everything else it is a
dry run until `--apply`.

It stops short wherever the answer is a decision rather than a command. A
detached HEAD is only moved when every one of these holds: the commit it sits on
is already contained in the trunk (otherwise that HEAD *is* the work, and
switching away would leave it reachable from nothing but the reflog); the trunk
exists locally; nothing is uncommitted; no merge, rebase, cherry-pick or bisect
is in progress; the trunk is not checked out in another worktree; this is not a
linked worktree, which `sync.worktrees` keeps out of it; and the trunk does not
track a file that is gitignored here and would be replaced — the same guard
`sync` uses, because a local `.env` is invisible to "is the tree clean".

Unpushed commits, branches that exist only locally and repositories with no
remote are never touched by `--fix`: only you know whether those should be
pushed or dropped.

Credentials are read out of `.git/config` directly rather than through
`git remote get-url`, which expands `insteadOf` — a token in your `~/.gitconfig`
is not this repository's problem. `pushurl` and any second `url` are checked
too, and a bare `https://token@host/…`, which is how a personal access token is
usually pasted, counts as much as `user:secret@`. An `ssh://git@host` username
does not.

`--ask` asks once per remedy per repository; answering `a` covers that one
remedy, not the other two.

## Modes

| Flag        | Behaviour                                                                                                |
| ----------- | -------------------------------------------------------------------------------------------------------- |
| `--dry-run` | The default. Print what would happen, change nothing.                                                    |
| `--ask`     | Prompt per change: `y`, `n`, `a` (all of this kind), `s` (skip this kind), `Y` (everything), `q` (stop). |
| `--apply`   | Do everything without asking.                                                                            |

`--ask` runs single-threaded so the prompts do not interleave; the other two use
all cores.

### `--force`

A separate flag, and the only one in this tool that can lose work. It does two
things, and nothing else:

- **`branches.require_merged: false`** — deletes a branch whose upstream is gone
  even when its commits are not in the trunk. Still refuses unless a fetch
  succeeded in the same run, so the `[gone]` mark it acts on was observed now
  rather than cached from some earlier day.
- **`sync.switch: always` and `sync.stash: true`** — switches and fast-forwards
  a repository with uncommitted changes, putting them in a stash first. Nothing
  is discarded; the report tells you which stash and how to get it back.

What `--force` deliberately cannot do:

- delete a directory with a git repository inside it — a vendored or forgotten
  checkout holds commits that exist nowhere else. The one exception is
  `clean.regenerable`, the short list of caches whose nested repository is
  itself a tool's clone: `terraform init` puts one under `.terraform/modules`
  for every module, and keeping those would make a gigabyte unreclaimable
- hard-delete anything matching `trash.sensitive` or `clean.ignored_keep`.
  A directory holding one is emptied out around it: the file stays at its path
  and the rest is reclaimed. Two carve-outs, both documented above —
  `clean.ignored_keep` does not apply inside `clean.regenerable`, and the
  source-code exemption applies to `trash.sensitive` alone
- remove a tracked file, follow a symlink, or reach outside the workspace

Pair it with `--ask` the first time, so you see what it selects before it acts.

## Speed

Everything here waits on either the network or the disk, so the work is spread
across a thread pool. `jobs: 0` — the default — means **one worker per CPU core**.

A serial pass over a workspace of a few hundred repositories and 40 GB of build
output takes minutes; across all cores it takes seconds. Raise `jobs` above the
core count if your repositories are all remote and slow to fetch, since git
spends that time waiting rather than computing.

```bash
git-tidy -j 32 sync --apply   # a lot of slow remotes
```

## Configuration

YAML, in two places, and both are optional:

- **global** — `~/.config/git-tidy/config.yaml` (or `$XDG_CONFIG_HOME`)
- **per directory** — `.git-tidy.yaml`, in the workspace root or in any directory
  down to an individual repository

They are merged in that order and **the deepest wins**, so one repository can opt
out of a rule the workspace sets:

```yaml
# ~/git/some-repo/.git-tidy.yaml — this one has its own release process
sync:
  enabled: false
clean:
  keep: ["fixtures/**"]
```

Write a starting point, with every setting documented and commented out:

```bash
git-tidy init                # ./.git-tidy.yaml
git-tidy init --global       # ~/.config/git-tidy/config.yaml
git-tidy init --global --ask # ...and answer a few questions first
```

`git-tidy config <path>` prints the result of the merge for that path, which is
the fastest way to find out why a rule did or did not apply.

An unknown key is an error, not a shrug — a typo fails loudly instead of silently
doing nothing.

### A worked example

```yaml
jobs: 0 # one per core

exclude:
  - "archive/*" # never touch these clones

sync:
  submodules: init # keep submodules checked out

branches:
  keep: ["main", "release/*", "spike/*"]

clean:
  ignored: true # everything .gitignore covers
  dependencies: true # ...including node_modules and .venv
  ignored_keep: # ...except these, which are local state
    - ".env"
    - "*.tfstate"

trash:
  enabled: true
  patterns: ["*.log", "rank-snapshot-*.json"]
  min_age_days: 14
```

## Install

Homebrew:

```bash
brew install sapn95/tap/git-tidy
```

A self-contained binary from the [releases](https://github.com/sapn95/git-tidy/releases)
(compiled with Nuitka; no Python needed):

```bash
tar -xzf git-tidy-macos-arm64.tar.gz
./git-tidy.dist/git-tidy --version
```

From PyPI-style tooling, or straight from a checkout:

```bash
uv tool install git-tidy
# or
git clone https://github.com/sapn95/git-tidy && ./git-tidy/git_tidy.py --help
```

PyYAML is used when it happens to be installed; when it is not, a strict parser
for the documented config subset stands in, so there is nothing to install
alongside the script. The two agree — that is a test, not a hope — and where
they cannot, both refuse rather than guess. The released binaries are built
without PyYAML on purpose, so a config means the same thing on every one of
them regardless of the machine that built it.

Named `git-tidy`, so git finds it as a subcommand too:

```bash
git tidy run --apply
```

## Safety

- Dry run by default. `--apply` is always explicit.
- Fast-forward only by default. Never merges, never force-pushes. Rebases only
  where `sync.diverged: rebase` explicitly asks for it.
- A branch with unpushed commits is reported, never deleted — unless `--force`
  or `branches.require_merged: false` asks, which is what that flag is for.
- Tracked files are never removed by `clean`, unless `clean.tracked: true`
  explicitly asks — it is off by default and the one setting `clean` has that
  can touch committed content.
- Symlinks are never followed; nothing outside the workspace is ever touched.
- Swept files go to a quarantine with a manifest, and `restore` undoes it.
- Anything that looks like a credential is quarantined, never deleted.
- It refuses to run on `$HOME` or a filesystem root.

## Development

```bash
uv run pytest                 # tests, with coverage (min 80%)
uvx ruff check . && uvx ruff format --check .
```

Tests run against real git repositories in a temp directory — a bare repo
standing in for the remote — rather than against a mock, because the interesting
cases are exactly the ones a mock gets wrong.

## Licence

MIT
