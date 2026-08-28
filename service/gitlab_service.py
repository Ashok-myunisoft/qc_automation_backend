import base64
import logging
import os
import time

import gitlab
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TREE_CACHE_SECONDS = 45

# repository_tree(recursive=True, all=True) isn't one API call for a repo of
# any real size — python-gitlab walks it page by page under the hood, and
# for the real source repo (7000+ files) that's several HTTP round-trips in
# a row. Any single page hiccuping surfaces as a GitlabGetError even though
# the repo/branch/token are all fine — a large recursive listing is
# structurally more failure-prone than any other single call this class
# makes. This retry absorbs exactly that kind of transient failure instead
# of failing the whole Generate/Fetch over one bad page.
TREE_FETCH_RETRIES = 3
TREE_FETCH_BACKOFF_SECONDS = 2  # doubles each retry: 2s, 4s


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

    def _fetch_tree_with_retry(self) -> list[dict]:
        """See TREE_FETCH_RETRIES comment above for why this specific call
        gets a retry and nothing else in this class does. Uses plain
        time.sleep — this class is already called synchronously from inside
        async handlers elsewhere in the app (blocking the event loop for
        the duration of the underlying HTTP call regardless), so this
        doesn't introduce a new blocking pattern, just extends an existing
        one across up to TREE_FETCH_RETRIES attempts instead of one."""
        last_error: Exception | None = None
        for attempt in range(1, TREE_FETCH_RETRIES + 1):
            try:
                return self._project.repository_tree(
                    recursive=True, all=True, ref=self._branch
                )
            except gitlab.exceptions.GitlabGetError as e:
                last_error = e
                if attempt < TREE_FETCH_RETRIES:
                    wait = TREE_FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "%s: repository_tree attempt %d/%d failed (%s) — retrying in %ss",
                        self._env_prefix, attempt, TREE_FETCH_RETRIES, e, wait,
                    )
                    time.sleep(wait)
        raise last_error

    def get_repo_tree(self, force_refresh: bool = False) -> list[str]:
        now = time.time()
        if (
            not force_refresh
            and self._tree_cache is not None
            and (now - self._tree_cache_at) < TREE_CACHE_SECONDS
        ):
            return self._tree_cache

        items = self._fetch_tree_with_retry()
        paths = [item["path"] for item in items if item["type"] == "blob"]

        self._tree_cache = paths
        self._tree_cache_at = now
        return paths

    def get_module_screen_map(self, force_refresh: bool = True) -> dict[str, list[str]]:
        """Live module -> [screen, ...] map, derived from the QC repo's real
        folder structure: Regression-Testing/{Module}_Module/{Screen}/...

        Module names are returned WITHOUT the "_Module" suffix (e.g.
        "SkillManagement", not "SkillManagement_Module") to match what
        architecture_resolver's `_pascal(module) + "_Module"` already
        expects as input elsewhere in this app — so a name picked from
        this map can be passed straight into the existing fetch/generate
        flow unchanged.

        Screens are every distinct folder found directly under a module
        folder, regardless of whether that screen folder currently has a
        complete .feature + .js pair — an in-progress/partial folder is
        still a real screen someone is working on and should be pickable.

        force_refresh defaults to True (bypassing get_repo_tree's 45s TTL
        cache) since this powers a live dropdown that's meant to reflect
        the repo's current state at the moment it's opened, not whatever
        happened to be cached from an earlier, unrelated call."""
        tree = self.get_repo_tree(force_refresh=force_refresh)

        modules: dict[str, set[str]] = {}
        for path in tree:
            parts = path.split("/")
            # Regression-Testing / {Module}_Module / {Screen} / ...
            if len(parts) < 3 or not parts[1].endswith("_Module"):
                continue
            module_name = parts[1][: -len("_Module")]
            screen_name = parts[2]
            if not module_name or not screen_name:
                continue
            modules.setdefault(module_name, set()).add(screen_name)

        return {
            module: sorted(screens)
            for module, screens in sorted(modules.items())
        }

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