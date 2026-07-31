import base64
import os
import time

import gitlab
from dotenv import load_dotenv

load_dotenv()

# Repo tree rarely changes second-to-second; cache it briefly so a burst of
# fetch/generate/approve calls in one demo session doesn't refetch the whole
# tree every time. force_refresh=True bypasses this when it matters (e.g.
# right after a push, before resolving where to run).
TREE_CACHE_SECONDS = 45


class GitLabService:
    def __init__(self):
        url = os.getenv("GITLAB_URL", "https://gitlab.com")
        token = os.getenv("GITLAB_TOKEN")
        project_id = os.getenv("GITLAB_PROJECT_ID")
        self._branch = os.getenv("GITLAB_BRANCH", "main")

        if not token or not project_id:
            raise RuntimeError(
                "GITLAB_TOKEN and GITLAB_PROJECT_ID must be set in .env "
                "(see .env.example)."
            )

        self._gl = gitlab.Gitlab(url, private_token=token)
        self._project = self._gl.projects.get(project_id)

        self._tree_cache: list[str] | None = None
        self._tree_cache_at: float = 0.0

    @property
    def branch(self) -> str:
        return self._branch

    def get_clone_url(self) -> str:
        """
        Returns an authenticated HTTPS clone URL (token embedded), so
        cypress_runner.py can `git clone`/`git pull` without needing its
        own separate GitLab credentials or config.
        """
        token = os.getenv("GITLAB_TOKEN")
        base_url = self._project.http_url_to_repo  # e.g. https://gitlab.com/group/repo.git
        scheme, rest = base_url.split("://", 1)
        return f"{scheme}://oauth2:{token}@{rest}"

    def get_repo_tree(self, force_refresh: bool = False) -> list[str]:
        """Returns every file path (blobs only, not directories) in the repo,
        at self._branch. This is the whole "architecture" — one call, no
        hardcoded module/screen list."""
        now = time.time()
        if (
            not force_refresh
            and self._tree_cache is not None
            and (now - self._tree_cache_at) < TREE_CACHE_SECONDS
        ):
            return self._tree_cache

        items = self._project.repository_tree(
            recursive=True, all=True, ref=self._branch
        )
        paths = [item["path"] for item in items if item["type"] == "blob"]

        self._tree_cache = paths
        self._tree_cache_at = now
        return paths

    def fetch_file(self, path: str) -> str | None:
        """Returns file content as text, or None if it doesn't exist."""
        try:
            f = self._project.files.get(file_path=path, ref=self._branch)
        except gitlab.exceptions.GitlabGetError as e:
            if e.response_code == 404:
                return None
            raise
        return base64.b64decode(f.content).decode("utf-8")

    def create_or_update_file(self, path: str, content: str, commit_message: str) -> None:
        """Creates the file if it doesn't exist, updates it (with its real
        blob sha, via python-gitlab's file object) if it does. Also
        invalidates the tree cache so a subsequent resolve sees the new file."""
        try:
            f = self._project.files.get(file_path=path, ref=self._branch)
            f.content = content
            f.save(branch=self._branch, commit_message=commit_message)
        except gitlab.exceptions.GitlabGetError as e:
            if e.response_code == 404:
                self._project.files.create(
                    {
                        "file_path": path,
                        "branch": self._branch,
                        "content": content,
                        "commit_message": commit_message,
                    }
                )
            else:
                raise

        self._tree_cache = None