import os
import shutil
import tempfile
import unittest
from pathlib import Path

import pygit2
from core.git_api import GitRepository


class TestGitApi(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.path = Path(self.test_dir)
        self.signature = pygit2.Signature("Test User", "test@example.com")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _create_commit(self, repo, filename, content, message="Commit"):
        # Ensure user config exists for operations that rely on it implicitely (like stash)
        try:
            repo.config["user.name"]
        except KeyError:
            repo.config["user.name"] = "Test User"
            repo.config["user.email"] = "test@example.com"
            
        file_path = os.path.join(repo.workdir, filename)
        with open(file_path, 'w') as f:
            f.write(content)
        
        index = repo.index
        index.add(filename)
        index.write()
        tree = index.write_tree()
        
        parents = []
        if not repo.head_is_unborn:
            parents = [repo.head.target]
            
        repo.create_commit(
            "HEAD",
            self.signature,
            self.signature,
            message,
            tree,
            parents
        )
        return str(repo.head.target)

    def test_init_repository(self):
        api = GitRepository.init_repository(self.path, origin_url="https://example.com/repo.git")
        self.assertTrue(api.is_valid)
        self.assertTrue(api.repo.remotes['origin'] is not None)
        self.assertEqual(api.repo.remotes['origin'].url, "https://example.com/repo.git")

    def test_basic_status(self):
        api = GitRepository.init_repository(self.path)
        
        # Initially empty/unborn
        with self.assertRaises(Exception):
             # head.shorthand raises if unborn usually, or returns None? 
             # pygit2: raises GitError if no head
             print(api.get_current_branch())

        # Create a commit
        self._create_commit(api.repo, "test.txt", "hello")
        
        self.assertEqual(api.get_current_branch(), "master") # default is usually master or main depending on git config, pygit2 default is master usually
        self.assertFalse(api.is_dirty())
        
        # Make dirty
        with open(self.path / "test.txt", "w") as f:
            f.write("changed")
            
        self.assertTrue(api.is_dirty())

    def test_checkout_and_branch(self):
        api = GitRepository.init_repository(self.path)
        commit1 = self._create_commit(api.repo, "f1", "c1")
        
        # Create new branch
        branch_name = "feature"
        # Create branch pointing to HEAD
        commit_obj = api.repo.revparse_single("HEAD")
        api.repo.branches.create(branch_name, commit_obj)
        
        api.checkout(branch_name)
        self.assertEqual(api.get_current_branch(), branch_name)
        
        # Commit on feature
        commit2 = self._create_commit(api.repo, "f2", "c2")
        self.assertNotEqual(commit1, commit2)
        
        # Checkout master
        api.checkout("master")
        self.assertEqual(api.get_current_branch(), "master")
        self.assertEqual(api.get_head_commit(), commit1)

    def test_fast_forward_merge(self):
        api = GitRepository.init_repository(self.path)
        commit1 = self._create_commit(api.repo, "f1", "c1")
        
        # Create 'feature' branch
        commit_obj = api.repo.revparse_single("HEAD")
        api.repo.branches.create("feature", commit_obj)
        
        # Switch to feature and add commit
        api.checkout("feature")
        commit2 = self._create_commit(api.repo, "f2", "c2")
        
        # Switch back to master
        api.checkout("master")
        self.assertEqual(api.get_head_commit(), commit1)
        
        # Merge feature (fast-forward)
        api.merge_fast_forward(commit2)
        
        self.assertEqual(api.get_head_commit(), commit2)
        self.assertTrue((self.path / "f2").exists())

    def test_stash(self):
        api = GitRepository.init_repository(self.path)
        # Configure user for stash
        api.repo.config["user.name"] = "Test User"
        api.repo.config["user.email"] = "test@example.com"
        
        self._create_commit(api.repo, "f1", "c1")
        
        # Modify file
        with open(self.path / "f1", "w") as f:
            f.write("modified")
            
        self.assertTrue(api.is_dirty())
        
        # Stash
        saved = api.stash_save("WIP")
        self.assertTrue(saved)
        self.assertFalse(api.is_dirty())
        with open(self.path / "f1", "r") as f:
            self.assertEqual(f.read(), "c1")
            
        # Pop
        api.stash_pop()
        self.assertTrue(api.is_dirty())
        with open(self.path / "f1", "r") as f:
            self.assertEqual(f.read(), "modified")

    def test_dry_run(self):
        api = GitRepository.init_repository(self.path)
        self._create_commit(api.repo, "test", "content")
        
        # Test dry-run checkout
        api.checkout("non-existent-branch", dry_run=True)
        # Should not raise exception
        
        # Test dry-run stash
        with open(self.path / "test", "w") as f:
            f.write("modified")
        
        api.stash_save("WIP", dry_run=True)
        self.assertTrue(api.is_dirty()) # Should still be dirty

if __name__ == "__main__":
    unittest.main()
