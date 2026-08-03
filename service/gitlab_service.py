import base64
import os
import time

import gitlab
from dotenv import load_dotenv

load_dotenv()

TREE_CACHE_SECONDS = 45


class GitLabService:
    """
    Thin wrapper around a single GitLab project (repo).

    By default this reads the original GITLAB_* env vars, so existing
    callers (fetch/approve/push against the `qc_test` repo) are unaffected.

    Pass `env_prefix="SOURCE_GITLAB"` to point an instance at a *second*,
    independently-configured repo (its own URL/token/project id/branch) —
    used for Generate's source-code lookup against the real app repo.
    """

    def __init__(self, env_prefix: str = "GITLAB"):
        self._env_prefix = env_prefix

        url        = os.getenv(f"{env_prefix}_URL", "https://gitlab.com")
        token      = os.getenv(f"{env_prefix}_TOKEN")
        project_id = os.getenv(f"{env_prefix}_PROJECT_ID")
        self._branch = os.getenv(f"{env_prefix}_BRANCH", "main")
        self._token  = token

        if not token or not project_id:
            raise RuntimeError(
                f"{env_prefix}_TOKEN and {env_prefix}_PROJECT_ID must be set "
                "in .env (see .env.example)."
            )

        self._gl = gitlab.Gitlab(url, private_token=token)
        self._project = self._gl.projects.get(project_id)

        self._tree_cache: list[str] | None = None
        self._tree_cache_at: float = 0.0

    @property
    def branch(self) -> str:
        return self._branch

    def get_clone_url(self) -> str:
        base_url = self._project.http_url_to_repo
        scheme, rest = base_url.split("://", 1)
        return f"{scheme}://oauth2:{self._token}@{rest}"

    def get_repo_tree(self, force_refresh: bool = False) -> list[str]:
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
        try:
            f = self._project.files.get(file_path=path, ref=self._branch)
        except gitlab.exceptions.GitlabGetError as e:
            if e.response_code == 404:
                return None
            raise
        return base64.b64decode(f.content).decode("utf-8")

    def create_or_update_file(self, path: str, content: str, commit_message: str) -> None:
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