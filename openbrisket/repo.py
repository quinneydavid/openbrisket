"""Everything you make, in one git repository, with an optional copy on a git host of your own.

    <data folder>/              the repository ("your book")
        recipes/                your recipes, rubs (rubs/) and stacks (stacks/)
        cooks/                  one folder per cook
        .gitignore              keeps everything else out: settings, tokens and working files never enter git

Every save and every cook event is a commit. Give the app the address of an empty private repository
(Settings > Backup) and each commit is pushed there within a minute. Changes made in that repository are
pulled in, and a new install pointed at it gets everything back.

Older versions kept recipes and cooks in two separate repositories; `unify` joins them into one and keeps
the history of both.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import threading
from datetime import datetime, timezone

GIT_ID = ["-c", "user.name=OpenBrisket", "-c", "user.email=openbrisket@localhost"]
LOCK = threading.RLock()                      # one writer at a time: recipes, cooks and the backup share an index
REMOTE = "backup"
BRANCH = "main"
KEPT = ("recipes", "cooks")                   # the folders that are in the book
ALLOWED_TOP = {".gitignore", "README.md", *KEPT}
SCHEMES = ("https://",)
NET_TIMEOUT_S = 90
IGNORE = """\
# OpenBrisket keeps only recipes/ and cooks/ in git. Settings and tokens stay out.
/*
!/.gitignore
!/README.md
!/recipes/
!/cooks/
state.json
*.tmp
"""
README = """\
# My barbecue

Written by OpenBrisket. `recipes/` holds my recipes, rubs and stacks, `cooks/` holds one folder per cook.
Every save and every cook event is a commit.
"""


# git refuses a repository whose folder belongs to another user. A mounted data volume is often owned by root
# while the app runs as an ordinary user, so tell git that the folders this app works in are trusted.
if "GIT_CONFIG_COUNT" not in os.environ:
    os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0="*")


class RepoError(Exception):
    pass


def git(root: str, *args: str, env: dict | None = None, timeout: float | None = None, check: bool = True) -> str:
    try:
        r = subprocess.run(["git", "-C", root, *GIT_ID, *args], capture_output=True, text=True,
                           env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RepoError("the repository did not answer in time") from None
    if check and r.returncode != 0:
        raise RepoError(r.stderr.strip() or r.stdout.strip() or f"git {args[0]} failed")
    return r.stdout


def _ok(root: str, *args: str) -> bool:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).returncode == 0


# --- the one repository -------------------------------------------------------------------------------
def is_book(home: str) -> bool:
    """True when `home` is a repository this app made for recipes and cooks together."""
    return (os.path.isdir(os.path.join(home, ".git"))
            and git(home, "config", "--get", "openbrisket.book", check=False).strip() == "true")


def covered(root: str) -> bool:
    """True when `root` (a recipes or cooks folder) lives in the book, so it needs no repository of its own."""
    if os.path.isdir(os.path.join(root, ".git")):
        return False
    parent = os.path.dirname(os.path.abspath(root))
    return os.path.basename(os.path.abspath(root)) in KEPT and is_book(parent)


def has_repo(root: str) -> bool:
    return os.path.isdir(os.path.join(root, ".git")) or covered(root)


def _write_if_changed(path: str, text: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _history(nested: str) -> list[dict]:
    """The commits of an old separate repository, oldest first."""
    if not _ok(nested, "rev-parse", "-q", "--verify", "HEAD"):
        return []
    raw = git(nested, "log", "--reverse", "--first-parent", "--format=%H%x1f%T%x1f%ct%x1f%aI%x1f%B%x1e", "HEAD")
    out = []
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        sha, tree, ctime, date, message = rec.strip("\n").split("\x1f", 4)
        out.append({"sha": sha, "tree": tree, "time": int(ctime), "date": date, "message": message.strip()})
    return out


def _join(home: str, nested: dict[str, str]) -> None:
    """Replay the commits of the old separate repositories into the book, each under its own folder, in the
    order they happened. Dates and messages are kept, so a recipe's history reads as before."""
    queues = {}
    for name, path in nested.items():
        commits = _history(path)
        if commits:
            git(home, "fetch", "-q", path, "HEAD")                     # bring the objects over
            queues[name] = commits
    blobs = {}
    for fname, text in ((".gitignore", IGNORE), ("README.md", README)):
        r = subprocess.run(["git", "-C", home, "hash-object", "-w", "--stdin"], input=text, capture_output=True,
                           text=True, check=True)
        blobs[fname] = r.stdout.strip()
    trees: dict[str, str] = {}
    parent = None
    while queues:
        name = min(queues, key=lambda n: queues[n][0]["time"])          # merge by time, order within each kept
        c = queues[name].pop(0)
        if not queues[name]:
            del queues[name]
        trees[name] = c["tree"]
        listing = "".join(f"100644 blob {sha}\t{fname}\n" for fname, sha in blobs.items())
        listing += "".join(f"040000 tree {sha}\t{folder}\n" for folder, sha in trees.items())
        root_tree = subprocess.run(["git", "-C", home, "mktree"], input=listing, capture_output=True, text=True,
                                   check=True).stdout.strip()
        env = dict(os.environ, GIT_AUTHOR_DATE=c["date"], GIT_COMMITTER_DATE=c["date"])
        args = ["commit-tree", root_tree, "-m", c["message"] or "change"]
        if parent:
            args += ["-p", parent]
        parent = git(home, *args, env=env).strip()
    if parent:
        git(home, "update-ref", f"refs/heads/{BRANCH}", parent)
        git(home, "reset", "-q")                                        # the files are already in place
    keep = os.path.join(home, ".migrated")
    os.makedirs(keep, mode=0o700, exist_ok=True)
    for name, path in nested.items():                                   # the old repositories are kept, out of the way
        target = os.path.join(keep, f"{name}.git")
        if os.path.exists(target):
            shutil.rmtree(target)
        os.replace(os.path.join(path, ".git"), target)


def unify(home: str, library_root: str, cooks_root: str) -> bool:
    """Make the data folder the one repository for recipes and cooks, joining older separate repositories
    into it. Returns False (and changes nothing) when the folders are somewhere else or `home` is already a
    repository of another kind."""
    a = os.path.abspath
    if a(library_root) != os.path.join(a(home), "recipes") or a(cooks_root) != os.path.join(a(home), "cooks"):
        return False
    with LOCK:
        if os.path.isdir(os.path.join(home, ".git")):
            if not is_book(home):
                return False
            _write_if_changed(os.path.join(home, ".gitignore"), IGNORE)
            return True
        os.makedirs(home, mode=0o700, exist_ok=True)
        nested = {name: os.path.join(home, name) for name in KEPT if os.path.isdir(os.path.join(home, name, ".git"))}
        subprocess.run(["git", "init", "-q", "-b", BRANCH, home], check=True, capture_output=True)
        try:
            _write_if_changed(os.path.join(home, ".gitignore"), IGNORE)
            if not os.path.exists(os.path.join(home, "README.md")):
                _write_if_changed(os.path.join(home, "README.md"), README)
            if nested:
                _join(home, nested)
            git(home, "add", "-A")
            if git(home, "status", "--porcelain").strip() or not _ok(home, "rev-parse", "-q", "--verify", "HEAD"):
                git(home, "commit", "-q", "--allow-empty", "-m",
                    "one repository for recipes and cooks" if nested
                    else f"start the book {secrets.token_hex(4)}")      # unique, so two installs never share a start
            git(home, "config", "openbrisket.book", "true")
        except Exception:
            if not nested or all(os.path.isdir(os.path.join(p, ".git")) for p in nested.values()):
                shutil.rmtree(os.path.join(home, ".git"), ignore_errors=True)     # nothing moved yet: start over next time
            raise
        return True


# --- the copy on a git host ---------------------------------------------------------------------------
def check_url(url: str) -> str:
    url = str(url or "").strip()
    if not url.startswith(SCHEMES):
        raise RepoError("the address must start with https://, e.g. https://github.com/you/my-barbecue.git")
    if url.startswith("https://") and "@" in url.split("/", 3)[2]:
        raise RepoError("leave the user name and token out of the address; the token has its own field")
    if any(ch.isspace() for ch in url):
        raise RepoError("the address has a space in it")
    return url


def _env(token: str | None) -> dict:
    """Environment for a git call over the network: never ask on a terminal, use no stored logins, and send the
    token as a header (kept out of the command line and out of the saved address)."""
    extra = [("credential.helper", "")]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        extra.append(("http.extraHeader", f"Authorization: Basic {basic}"))
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    n = int(env.get("GIT_CONFIG_COUNT") or 0)             # keep what is already set (safe.directory)
    for i, (key, value) in enumerate(extra, start=n):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(n + len(extra))
    return env


def _plain(error: str) -> str:
    """A git network error, said in a way that tells you what to fix."""
    low = error.lower()
    if "authentication failed" in low or "could not read username" in low or "403" in low or "401" in low:
        return "the repository refused the token; it needs to read and write the contents of that repository"
    if "not found" in low or "does not appear to be a git repository" in low:
        return "repository not found; check the address, and that the token can see it"
    if "could not resolve host" in low or "failed to connect" in low or "timed out" in low or "in time" in low:
        return "could not reach the git host"
    return error.splitlines()[-1][:300] if error else "the push failed"


def is_public(url: str) -> bool:
    """True when anyone can read the repository without logging in."""
    r = subprocess.run(["git", "ls-remote", "--", url], capture_output=True, text=True, env=_env(None),
                       timeout=30)
    return r.returncode == 0


def has_content(home: str) -> bool:
    """True once there is a cook or a recipe, rub or stack of your own."""
    cooks = os.path.join(home, "cooks")
    if os.path.isdir(cooks) and any(os.path.isfile(os.path.join(cooks, d, "cook.yaml")) for d in os.listdir(cooks)):
        return True
    for _dir, _sub, files in os.walk(os.path.join(home, "recipes")):
        if any(f.endswith((".yaml", ".yml")) for f in files):
            return True
    return False


def _top(home: str, ref: str) -> set[str]:
    return set(git(home, "ls-tree", "--name-only", ref).split())


def _foreign(names: set[str]) -> str:
    return ", ".join(sorted(names - ALLOWED_TOP)[:5])


def waiting(home: str) -> int | None:
    """How many commits have not reached the copy yet. None before the first exchange."""
    if not _ok(home, "rev-parse", "-q", "--verify", f"refs/remotes/{REMOTE}/{BRANCH}"):
        return None
    return int(git(home, "rev-list", "--count", f"{REMOTE}/{BRANCH}..HEAD").strip() or 0)


def sync(home: str, url: str, token: str | None) -> dict:
    """Bring the book and its copy in line: pull what changed there, push what changed here. On a new install
    with nothing in it yet, everything in the copy is brought down. Returns what was done."""
    url = check_url(url)
    if not is_book(home):
        raise RepoError("recipes and cooks are not in the app's own repository, so there is nothing to copy")
    env = _env(token)
    with LOCK:
        if git(home, "remote", check=False).split().count(REMOTE):
            git(home, "remote", "set-url", REMOTE, url)
        else:
            git(home, "remote", "add", REMOTE, url)
    try:
        git(home, "fetch", "-q", "--prune", REMOTE, env=env, timeout=NET_TIMEOUT_S)
    except RepoError as e:
        raise RepoError(_plain(str(e))) from None
    done = {"pulled": 0, "pushed": 0, "restored": False}
    remote_ref = f"refs/remotes/{REMOTE}/{BRANCH}"
    with LOCK:
        _write_if_changed(os.path.join(home, ".gitignore"), IGNORE)
        git(home, "add", "-A")
        if git(home, "status", "--porcelain").strip():
            git(home, "commit", "-q", "-m", "changes on disk")
        if _ok(home, "rev-parse", "-q", "--verify", remote_ref):
            other = _foreign(_top(home, remote_ref))
            if other:
                raise RepoError(f"that repository holds other files ({other}); give OpenBrisket a repository of its own")
            ahead = int(git(home, "rev-list", "--count", f"{remote_ref}..HEAD").strip())
            behind = int(git(home, "rev-list", "--count", f"HEAD..{remote_ref}").strip())
            related = _ok(home, "merge-base", "HEAD", remote_ref)
            if behind and not related:
                if has_content(home):
                    raise RepoError("that repository already holds a different book; use an empty repository, "
                                    "or point a new install at it to bring that book back")
                git(home, "reset", "-q", "--hard", remote_ref)
                done.update(restored=True, pulled=behind)
            elif behind and not ahead:
                git(home, "merge", "-q", "--ff-only", remote_ref)
                done["pulled"] = behind
            elif behind:
                r = subprocess.run(["git", "-C", home, *GIT_ID, "merge", "-q", "--no-edit", remote_ref],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    files = git(home, "diff", "--name-only", "--diff-filter=U", check=False).split()
                    git(home, "merge", "--abort", check=False)
                    raise RepoError("the same file was changed here and in the repository: "
                                    + (", ".join(files[:5]) or "see the repository")
                                    + ". Undo the change in the repository, then try again")
                done["pulled"] = behind
        other = _foreign(_top(home, "HEAD"))
        if other:
            raise RepoError(f"refusing to push: files outside recipes and cooks are in the book ({other})")
        head = git(home, "rev-parse", "HEAD").strip()
        need_push = not _ok(home, "rev-parse", "-q", "--verify", remote_ref) or \
            git(home, "rev-parse", remote_ref).strip() != head
        if need_push:
            done["pushed"] = int(git(home, "rev-list", "--count",
                                     *( [f"{remote_ref}..{head}"] if _ok(home, "rev-parse", "-q", "--verify", remote_ref)
                                        else [head] )).strip())
    if need_push:
        try:
            git(home, "push", "-q", REMOTE, f"{head}:refs/heads/{BRANCH}", env=env, timeout=NET_TIMEOUT_S)
        except RepoError as e:
            raise RepoError(_plain(str(e))) from None
    return done


def forget(home: str) -> None:
    """Stop copying: drop the saved address. The repository on the git host is not touched."""
    with LOCK:
        if is_book(home) and git(home, "remote", check=False).split().count(REMOTE):
            git(home, "remote", "remove", REMOTE)
    try:
        os.remove(state_path(home))
    except OSError:
        pass


# --- what the Settings page shows ---------------------------------------------------------------------
def state_path(home: str) -> str:
    return os.path.join(home, ".git", "openbrisket-backup.json")


def load_state(home: str) -> dict:
    try:
        with open(state_path(home), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(home: str, **changes) -> dict:
    state = {**load_state(home), **changes}
    try:
        with open(state_path(home), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass
    return state


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
