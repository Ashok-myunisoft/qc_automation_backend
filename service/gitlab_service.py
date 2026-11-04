import base64
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import gitlab
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TREE_CACHE_SECONDS = 3600  # 1 hour

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
TREE_PARALLEL_PAGES = 8         # pages requested at once by the parallel tree read
TREE_MAX_PAGES = 5000           # safety stop for the parallel read (500,000 entries)


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

        # No timeout here previously — on a silently-dropping network (some
        # firewall/proxy misconfigurations behave this way rather than
        # cleanly refusing), a GitLab call could hang indefinitely with no
        # error ever surfacing, which looks identical to the process just
        # being slow. 30s is generous given this environment's observed
        # tree-fetch latency (large repos have taken minutes legitimately)
        # but still guarantees SOME error eventually instead of an
        # unbounded hang.
        self._gl = gitlab.Gitlab(url, private_token=token, timeout=30)
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
                    recursive=True, all=True, ref=self._branch, per_page=100
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

    def _fetch_tree_parallel(self) -> list[dict]:
        """Same listing as _fetch_tree_with_retry, but several pages are requested at once.
        The sequential walk is one HTTP round-trip after another (70+ for a 7000-file repo), which is
        what makes the big source repo take minutes; here TREE_PARALLEL_PAGES pages are in flight together."""
        per_page = 100

        def get_page(n: int) -> list[dict]:
            last: Exception | None = None
            for attempt in range(1, TREE_FETCH_RETRIES + 1):
                try:
                    return self._project.repository_tree(
                        recursive=True, ref=self._branch, per_page=per_page, page=n
                    )
                except gitlab.exceptions.GitlabGetError as e:
                    last = e
                    if attempt < TREE_FETCH_RETRIES:
                        time.sleep(TREE_FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            raise last

        items: dict[str, dict] = {}
        page = 1
        with ThreadPoolExecutor(max_workers=TREE_PARALLEL_PAGES) as pool:
            while page <= TREE_MAX_PAGES:
                batch = list(pool.map(get_page, range(page, page + TREE_PARALLEL_PAGES)))
                for rows in batch:
                    for it in rows:
                        items[it["path"] + "|" + it["type"]] = it
                if any(len(rows) < per_page for rows in batch):   # a short page is the last one
                    break
                page += TREE_PARALLEL_PAGES
        return list(items.values())

    def get_repo_tree(self, force_refresh: bool = False, parallel: bool | None = None) -> list[str]:
        """parallel=None: on for the source repo (SOURCE_GITLAB), off for the others. If the parallel read
        fails for any reason it falls back to the sequential one, so it can only be faster, never worse."""
        now = time.time()
        if (
            not force_refresh
            and self._tree_cache is not None
            and (now - self._tree_cache_at) < TREE_CACHE_SECONDS
        ):
            return self._tree_cache

        if parallel is None:
            parallel = self._env_prefix == "SOURCE_GITLAB"
        started = time.time()
        items = None
        if parallel:
            try:
                items = self._fetch_tree_parallel()
            except Exception as e:
                logger.warning("%s: parallel tree read failed (%s) - falling back to the sequential read", self._env_prefix, e)
        if items is None:
            items = self._fetch_tree_with_retry()
        logger.info("%s: read the repo tree (%d entries) in %.1fs%s", self._env_prefix, len(items), time.time() - started,
                    " [parallel]" if parallel and items is not None else "")
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