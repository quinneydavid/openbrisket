"""One repository for recipes and cooks, and its copy on a git host (here: a bare repository on disk)."""
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from openbrisket import library, repo, settings, sources
from openbrisket.service import Service, ServiceError
from openbrisket.store import Store
from tests.helpers import ROOT
from tests.test_service import FakeSource

RUB = {"name": "coffee-chilli", "ingredients": [{"name": "ground coffee", "parts": 2},
                                                 {"name": "kosher salt", "parts": 2, "kind": "salt"}]}
ME = ["-c", "user.name=Me", "-c", "user.email=me@localhost"]


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "data")
        self.remote = os.path.join(self.tmp.name, "remote.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.remote)
        self.use(self.home)
        for p in (mock.patch.object(sources, "from_settings", lambda cfg: FakeSource()),
                  mock.patch.object(repo, "SCHEMES", ("https://", "/"))):
            p.start()
            self.addCleanup(p.stop)

    def use(self, home):
        env = mock.patch.dict(os.environ, {"OPENBRISKET_HOME": home})
        env.start()
        self.addCleanup(env.stop)

    def service(self, home=None):
        home = home or self.home
        return Service([os.path.join(ROOT, "recipes"), os.path.join(home, "recipes")])

    def clone(self, name="clone"):
        path = os.path.join(self.tmp.name, name)
        sh("git", "clone", "-q", self.remote, path)
        return path


class BookTests(Base):
    def test_new_install_is_one_repository_without_secrets(self):
        s = self.service()
        self.assertTrue(s.book)
        s.access_token()                                   # writes settings.yaml into the data folder
        s.save_rub(RUB)
        self.assertFalse(os.path.isdir(os.path.join(self.home, "recipes", ".git")))
        tracked = sh("git", "-C", self.home, "ls-files").split()
        self.assertIn("recipes/rubs/coffee-chilli.yaml", tracked)
        self.assertNotIn("settings.yaml", tracked)
        self.assertEqual(len(s.item_history("rub", "coffee-chilli")), 1)

    def test_older_separate_repositories_are_joined_with_their_history(self):
        lib, cooks = os.path.join(self.home, "recipes"), os.path.join(self.home, "cooks")
        library.save_rub(lib, RUB)                          # the layout before 0.12: two repositories
        library.save_rub(lib, dict(RUB, ingredients=RUB["ingredients"] + [{"name": "chilli", "parts": 1}],
                                   change="more heat"))
        store = Store(cooks)
        store.ensure()
        self.assertTrue(os.path.isdir(os.path.join(lib, ".git")) and os.path.isdir(os.path.join(cooks, ".git")))
        before = len(sh("git", "-C", lib, "log", "--format=%H").split()) + \
            len(sh("git", "-C", cooks, "log", "--format=%H").split())

        s = self.service()
        self.assertTrue(s.book)
        self.assertFalse(os.path.exists(os.path.join(lib, ".git")))
        self.assertFalse(os.path.exists(os.path.join(cooks, ".git")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".migrated", "recipes.git")))
        self.assertGreaterEqual(len(sh("git", "-C", self.home, "log", "--format=%H").split()), before)
        history = s.item_history("rub", "coffee-chilli")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["changes"][0]["field"], "chilli")
        self.assertEqual(sh("git", "-C", self.home, "status", "--porcelain").strip(), "")
        s.save_rub(dict(RUB, change="back to plain"))       # and it keeps working afterwards
        self.assertEqual(len(s.item_history("rub", "coffee-chilli")), 3)

    def test_a_folder_that_is_someone_elses_repository_is_left_alone(self):
        os.makedirs(self.home)
        sh("git", "init", "-q", self.home)
        s = self.service()
        self.assertFalse(s.book)
        self.assertFalse(s.backup_view()["available"])
        s.save_rub(RUB)                                     # still works, in a repository of its own
        self.assertTrue(os.path.isdir(os.path.join(self.home, "recipes", ".git")))
        with self.assertRaises(ServiceError):
            s.save_backup(self.remote)


class BackupTests(Base):
    def test_push_then_restore_on_a_new_install(self):
        s = self.service()
        s.access_token()
        s.save_rub(RUB)
        out = s.save_backup(self.remote, "tok")
        self.assertTrue(out["ok"], out)
        self.assertGreater(out["done"]["pushed"], 0)
        self.assertEqual(out["waiting"], 0)
        self.assertTrue(out["token_set"])
        self.assertNotIn("token", {k for k in out if k != "token_set"})
        files = sh("git", "-C", self.remote, "ls-tree", "-r", "--name-only", "main").split("\n")
        self.assertIn("recipes/rubs/coffee-chilli.yaml", files)
        self.assertFalse([f for f in files if "settings" in f])
        self.assertNotIn("tok", sh("git", "-C", self.home, "config", "--list"))     # the token is not kept in git

        s.save_rub(dict(RUB, ingredients=RUB["ingredients"] + [{"name": "chilli", "parts": 1}]))
        self.assertEqual(s.backup_view()["waiting"], 1)
        self.assertEqual(s.backup_now()["done"]["pushed"], 1)

        home2 = os.path.join(self.tmp.name, "data2")       # a new install, pointed at the same repository
        self.use(home2)
        s2 = self.service(home2)
        out = s2.save_backup(self.remote)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["done"]["restored"])
        self.assertIn("coffee-chilli", [r["id"] for r in s2.list_rubs()])
        self.assertEqual(len(s2.item_history("rub", "coffee-chilli")), 2)

    def test_a_change_made_in_the_repository_is_pulled(self):
        s = self.service()
        s.save_rub(RUB)
        s.save_backup(self.remote)
        clone = self.clone()
        path = os.path.join(clone, "recipes", "rubs", "coffee-chilli.yaml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("ground coffee", "espresso powder"))
        sh("git", *ME, "commit", "-qam", "espresso", cwd=clone)
        sh("git", "push", "-q", cwd=clone)
        out = s.backup_now()
        self.assertEqual(out["done"]["pulled"], 1)
        rub = next(r for r in s.list_rubs() if r["id"] == "coffee-chilli")
        self.assertIn("espresso powder", [i["name"] for i in rub["ingredients"]])

    def test_the_same_file_changed_on_both_sides_is_reported_not_merged(self):
        s = self.service()
        s.save_rub(RUB)
        s.save_backup(self.remote)
        clone = self.clone()
        path = os.path.join(clone, "recipes", "rubs", "coffee-chilli.yaml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("ground coffee", "espresso powder"))
        sh("git", *ME, "commit", "-qam", "espresso", cwd=clone)
        sh("git", "push", "-q", cwd=clone)
        s.save_rub(dict(RUB, ingredients=[{"name": "instant coffee", "parts": 2}, RUB["ingredients"][1]]))
        out = s.backup_now()
        self.assertFalse(out["ok"])
        self.assertIn("coffee-chilli.yaml", out["error"])
        self.assertEqual(sh("git", "-C", self.home, "status", "--porcelain").strip(), "")
        rub = next(r for r in s.list_rubs() if r["id"] == "coffee-chilli")
        self.assertIn("instant coffee", [i["name"] for i in rub["ingredients"]])

    def test_a_repository_holding_other_things_is_refused(self):
        clone = self.clone()
        with open(os.path.join(clone, "settings.yaml"), "w", encoding="utf-8") as f:
            f.write("access: {token: stolen}\n")
        sh("git", "add", "-A", cwd=clone)
        sh("git", *ME, "commit", "-qm", "not a book", cwd=clone)
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=clone)
        s = self.service()
        token = s.access_token()
        out = s.save_backup(self.remote)
        self.assertFalse(out["ok"])
        self.assertIn("settings.yaml", out["error"])
        self.assertEqual(settings.load()["access"]["token"], token)

    def test_a_second_book_is_not_mixed_into_this_one(self):
        s = self.service()
        s.save_rub(RUB)
        s.save_backup(self.remote)
        home2 = os.path.join(self.tmp.name, "data2")
        self.use(home2)
        s2 = self.service(home2)
        s2.save_rub(dict(RUB, name="other"))
        out = s2.save_backup(self.remote)
        self.assertFalse(out["ok"])
        self.assertIn("different book", out["error"])

    def test_new_commits_are_pushed_in_the_background(self):
        s = self.service()
        s.save_backup(self.remote)
        t = threading.Thread(target=s.backup_forever, kwargs={"every_s": 0.05}, daemon=True)
        t.start()
        self.addCleanup(s.stop)
        s.save_rub(RUB)
        deadline = time.monotonic() + 10
        while s.backup_view()["waiting"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(s.backup_view()["waiting"], 0)
        self.assertIn("recipes/rubs/coffee-chilli.yaml",
                      sh("git", "-C", self.remote, "ls-tree", "-r", "--name-only", "main"))

    def test_removing_the_backup_and_bad_addresses(self):
        s = self.service()
        for bad in ("http://example.com/x.git", "git@github.com:me/x.git", "https://me:secret@github.com/me/x.git",
                    "--upload-pack=x"):
            with self.assertRaises(ServiceError):
                s.save_backup(bad)
        s.save_backup(self.remote, "tok")
        view = s.save_backup("")
        self.assertFalse(view["configured"])
        self.assertFalse(view["token_set"])
        self.assertEqual(sh("git", "-C", self.home, "remote").strip(), "")
        with self.assertRaises(ServiceError):
            s.backup_now()


if __name__ == "__main__":
    unittest.main()
