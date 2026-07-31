#!/usr/bin/env bash
# Every scenario in which this tool has, at some point, destroyed something —
# replayed end to end against the working tree, through main(), on real git
# repositories.
#
# The unit tests cover each of these individually. This exists because that was
# not enough: twice, a fix for one of them reintroduced another. It is cheap to
# run and it is the check worth running after any change to the removal, sync or
# protection paths.
#
#   bash scripts/regression_sweep.sh
#
# Needs git and python3, nothing else. Works entirely under /tmp.
set -u
GT="${GT:-$(cd "$(dirname "$0")/.." && pwd)/git_tidy.py}"
ROOT=/tmp/regsweep
rm -rf "$ROOT"; mkdir -p "$ROOT"
export GIT_CONFIG_GLOBAL="$ROOT/.gitconfig"
git config --global user.email t@e.invalid
git config --global user.name T
git config --global init.defaultBranch main

pass=0; fail=0
ok()   { printf "  ok    %s\n" "$1"; pass=$((pass + 1)); }
bad()  { printf "  FAIL  %s\n" "$1"; fail=$((fail + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', wanted '$3')"; fi }

export GT_DIR
GT_DIR="$(dirname "$GT")"

newspace() {  # $1 = name -> $ROOT/$1 with one clone in it
  local w="$ROOT/$1"
  mkdir -p "$w"
  git init -q --bare "$w/o.git"
  git clone -q "$w/o.git" "$w/repo" 2>/dev/null
  ( cd "$w/repo" && echo hi > README.md && git add -A && git commit -qm f &&
    git push -q -u origin main )
  printf "%s" "$w"
}

echo "1. unpushed commits survive prune (round 1)"
w=$(newspace unpushed)
( cd "$w/repo" && git switch -qc feature && git push -q -u origin feature &&
  echo x > only.txt && git add -A && git commit -qm "not pushed" && git switch -q main )
git -C "$w/o.git" branch -D feature >/dev/null 2>&1
( cd "$w/repo" && git fetch -q --prune origin )
python3 "$GT" -C "$w" prune --apply >/dev/null 2>&1
check "the branch is kept" "$(git -C "$w/repo" branch --list feature | tr -d ' *')" "feature"

echo "2. a tag shadowing a branch does not hide it (round 9)"
w=$(newspace tagshadow)
( cd "$w/repo" && git switch -qc feature && echo x > o.txt && git add -A &&
  git commit -qm local && git switch -q main && git tag feature main )
out=$(python3 "$GT" -C "$w" doctor -v 2>&1)
case "$out" in
  *feature*"not pushed"*) ok "doctor still reports it" ;;
  *) bad "doctor is silent" ;;
esac

echo "3. a gitignored .env is not replaced by a fast-forward (rounds 6, 11)"
w=$(newspace clobber)
( cd "$w/repo" && printf 'secrets.*\n' > .gitignore && mkdir -p config &&
  git add -A && git commit -qm ign && git push -q origin main )
git clone -q "$w/o.git" "$w/other" 2>/dev/null
( cd "$w/other" && mkdir -p config && echo UPSTREAM > config/secrets.py &&
  git add -f config/secrets.py && git commit -qm up && git push -q origin main )
( cd "$w/repo" && echo MINE > config/secrets.py && git fetch -q origin )
python3 "$GT" -C "$w" sync --apply --include repo >/dev/null 2>&1
check "the local copy survives" "$(cat "$w/repo/config/secrets.py")" "MINE"

echo "4. a rebase does not replace it either (round 10)"
w=$(newspace rebaseclobber)
( cd "$w/repo" && printf '.env\n' > .gitignore && git add -A && git commit -qm ign &&
  git push -q origin main )
git clone -q "$w/o.git" "$w/other" 2>/dev/null
( cd "$w/other" && echo THEIRS > .env && git add -f .env && git commit -qm up &&
  git push -q origin main )
( cd "$w/repo" && echo MINE > .env && echo m > mine.txt && git add mine.txt &&
  git commit -qm mine && git fetch -q origin )
printf 'sync:\n  diverged: rebase\n' > "$w/.git-tidy.yaml"
python3 "$GT" -C "$w" sync --apply --include repo >/dev/null 2>&1
check "the local copy survives a rebase" "$(cat "$w/repo/.env")" "MINE"

echo "5. a nested repository is never deleted with its parent (round 3)"
w=$(newspace nested)
mkdir -p "$w/repo/htmlcov"
git init -q "$w/repo/htmlcov/vendored"
( cd "$w/repo/htmlcov/vendored" && echo a > a.txt && git add -A && git commit -qm unpushed )
python3 "$GT" -C "$w" clean --apply --force >/dev/null 2>&1
if [ -d "$w/repo/htmlcov/vendored/.git" ]
then ok "the vendored repo survives --force"
else bad "the vendored repo was deleted"
fi

echo "6. a regenerable cache is reclaimed, not renamed (round 9)"
w=$(newspace reclaim)
mkdir -p "$w/repo/.terraform/providers"
head -c 400000 /dev/urandom > "$w/repo/.terraform/providers/p.bin"
echo backend > "$w/repo/.terraform/terraform.tfstate"
before=$(du -sk "$w" | cut -f1)
python3 "$GT" -C "$w" clean --apply >/dev/null 2>&1
after=$(du -sk "$w" | cut -f1)
if [ "$((before - after))" -gt 300 ]
then ok "the space came back ($before -> $after KB)"
else bad "nothing reclaimed ($before -> $after KB)"
fi

echo "7. a credential is lifted out and the tree still goes (rounds 10, 11)"
w=$(newspace rescue)
mkdir -p "$w/repo/node_modules/acorn/dist"
head -c 300000 /dev/urandom > "$w/repo/node_modules/acorn/dist/tokenizer.js"
echo PRIVATE > "$w/repo/node_modules/id_rsa"
echo 'export AWS_SECRET=1' > "$w/repo/node_modules/.env.sh"
printf 'clean:\n  dependencies: true\n' > "$w/.git-tidy.yaml"
python3 "$GT" -C "$w" clean --apply >/dev/null 2>&1
if [ -d "$w/repo/node_modules" ]; then bad "the tree was kept"; else ok "the tree went"; fi
if [ "$(find "$w/.git-tidy-trash" -path "*/node_modules/id_rsa" | wc -l)" -gt 0 ]
then ok "id_rsa rescued"; else bad "id_rsa lost"; fi
if [ "$(find "$w/.git-tidy-trash" -path "*/node_modules/.env.sh" | wc -l)" -gt 0 ]
then ok ".env.sh rescued"; else bad ".env.sh lost"; fi
if [ "$(find "$w/.git-tidy-trash" -path "*/node_modules/acorn/dist/tokenizer.js" | wc -l)" -gt 0 ]
then bad "tokenizer.js was treated as a secret"; else ok "tokenizer.js went with the tree"; fi

echo "8. a stash round-trips with its staging (round 8)"
w=$(newspace stash)
( cd "$w/repo" && echo b > b.txt && echo c > c.txt && git add -A && git commit -qm two &&
  echo staged > b.txt && echo unstaged > c.txt && git add b.txt )
before=$(git -C "$w/repo" status --porcelain)
python3 - "$w/repo" <<'PY'
import sys, pathlib
sys.path.insert(0, __import__("os").environ["GT_DIR"])
import git_tidy as gt
g = gt.Git(pathlib.Path(sys.argv[1]))
gt._stash(g, "repo")
gt._unstash(g)
PY
check "staged is staged again" "$(git -C "$w/repo" status --porcelain)" "$before"

echo "9. an interrupted sweep loses nothing (round 4)"
w=$(newspace journal)
for i in $(seq 1 400); do printf x > "$w/j$i.bak"; done
touch -t 202501010000 "$w"/*.bak
printf 'trash:\n  enabled: true\n  scope: workspace\n' > "$w/.git-tidy.yaml"
python3 "$GT" -C "$w" trash --apply -j 1 >/dev/null 2>&1 &
sleep 0.4; kill -9 $! 2>/dev/null; wait $! 2>/dev/null
python3 "$GT" -C "$w" restore --apply >/dev/null 2>&1
check "every file is back" "$(find "$w" -maxdepth 1 -name '*.bak' | wc -l | tr -d ' ')" "400"

echo ""
echo "$pass passed, $fail failed"
exit $(( fail > 0 ))
