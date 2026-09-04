"""Transport-level REST client for the qTest API.

`RestClient` owns connection concerns only: base URL resolution, authentication
headers, timeouts, retries and session lifetime. It deliberately knows nothing
about qTest endpoints, payloads or artifacts -- extraction and injection objects
consume an instance of this class and issue their own requests through it.

Authentication uses a long-lived API token read from `config/qtest.env`: one
token per instance (`SOURCE_QTEST_API_TOKEN` for HS, `TARGET_QTEST_API_TOKEN`
for HCSC). The token does not expire, so it is applied to the session headers
once at construction time and never re-negotiated per request.

Credentials are never logged: only the last characters of the token are shown.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

#: Default location of the connection settings file (project root: config/).
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[3] / "config" / "qtest.env"

#: Prefix of the HS instance variables, the migration origin.
SOURCE_PREFIX = "SOURCE"
#: Prefix of the HCSC instance variables, the migration destination.
TARGET_PREFIX = "TARGET"

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 3
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


class MissingConfigurationError(RuntimeError):
    """Raised when a required connection variable cannot be resolved."""


class RestClient:
    """A configured REST client for a single qTest instance.

    The client exposes generic, endpoint-agnostic HTTP verbs. Deciding *which*
    endpoint to call and *how* to interpret the response is the responsibility
    of the objects that consume the client.

    Example:
        >>> client = RestClient.from_env(SOURCE_PREFIX)
        >>> response = client.get("/api/v3/projects")
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: requests.Session | None = None,
    ) -> None:
        if not base_url:
            logger.error("Cannot build a client: base_url is missing")
            raise ValueError("base_url is required")
        if not api_token:
            logger.error("Cannot build a client for %s: api_token is missing", base_url)
            raise ValueError("api_token is required")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_token = api_token
        self._session = session if session is not None else self._build_session(max_retries)
        self._session.headers.update(self._default_headers(api_token))

        logger.info(
            "REST client ready for %s (timeout=%ss, retries=%s)",
            self.base_url,
            timeout,
            max_retries,
        )

    # -- construction --------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        prefix: str = SOURCE_PREFIX,
        env_file: Path | str = DEFAULT_ENV_FILE,
        **overrides: Any,
    ) -> RestClient:
        """Build a client from the `.env` connection settings.

        Args:
            prefix: variable prefix identifying the instance, `SOURCE` (HS) or
                `TARGET` (HCSC).
            env_file: path to the `.env` file holding the settings.
            **overrides: forwarded to `__init__` (e.g. `timeout=60`), so callers
                can tune transport without touching the file.

        Raises:
            MissingConfigurationError: if a required variable is absent or empty.
        """
        env_file = Path(env_file)
        logger.info("Loading %s connection settings from %s", prefix, env_file)

        if not env_file.exists():
            logger.warning(
                "%s does not exist: falling back to environment variables", env_file
            )

        # Real environment variables win over the file, so CI can inject values.
        values = {**dotenv_values(env_file), **os.environ}

        return cls(
            base_url=cls._required(values, f"{prefix}_QTEST_BASE_URL", env_file),
            api_token=cls._required(values, f"{prefix}_QTEST_API_TOKEN", env_file),
            **overrides,
        )

    @classmethod
    def for_source(cls, env_file: Path | str = DEFAULT_ENV_FILE, **overrides: Any) -> RestClient:
        """Client for the HS instance, built from the `SOURCE_QTEST_*` settings."""
        logger.debug("Building the source (HS) client")
        return cls.from_env(SOURCE_PREFIX, env_file, **overrides)

    @classmethod
    def for_target(cls, env_file: Path | str = DEFAULT_ENV_FILE, **overrides: Any) -> RestClient:
        """Client for the HCSC instance, built from the `TARGET_QTEST_*` settings."""
        logger.debug("Building the target (HCSC) client")
        return cls.from_env(TARGET_PREFIX, env_file, **overrides)

    @staticmethod
    def _required(values: dict[str, str | None], name: str, env_file: Path) -> str:
        value = (values.get(name) or "").strip()
        if not value:
            logger.error("Required setting %s is missing from %s", name, env_file)
            raise MissingConfigurationError(
                f"{name} is not set. Define it in {env_file} "
                f"(see config/qtest.example.env) or as an environment variable."
            )
        logger.debug("Resolved setting %s", name)
        return value

    @staticmethod
    def _default_headers(api_token: str) -> dict[str, str]:
        # qTest expects a lowercase "bearer" scheme; tolerate a token that
        # already carries the prefix.
        token = api_token if api_token.lower().startswith("bearer ") else f"bearer {api_token}"
        logger.debug("Building default headers with a bearer token (value not logged)")
        return {
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _build_session(max_retries: int) -> requests.Session:
        logger.debug(
            "Building session: %s retries on statuses %s", max_retries, RETRY_STATUS_CODES
        )
        session = requests.Session()
        retry = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=RETRY_STATUS_CODES,
            allowed_methods=frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    # -- exposed client ------------------------------------------------------

    @property
    def session(self) -> requests.Session:
        """The underlying session, for consumers needing streaming or hooks."""
        return self._session

    def url(self, path: str) -> str:
        """Resolve `path` against the base URL.

        Absolute URLs are returned untouched, so paginated responses can be
        followed by passing their `next` link straight back in.
        """
        if path.startswith(("http://", "https://")):
            logger.debug("Path %s is already absolute", path)
            return path
        resolved = f"{self.base_url}/{path.lstrip('/')}"
        logger.debug("Resolved %s to %s", path, resolved)
        return resolved

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Issue a request and return the raw response.

        The status code is *not* validated here: consumers decide whether a
        given code is an error for their use case. Request bodies are not
        logged, to keep credentials and test data out of the log.
        """
        kwargs.setdefault("timeout", self.timeout)
        url = self.url(path)
        logger.info("%s %s", method, url)

        started = time.monotonic()
        try:
            response = self._session.request(method, url, **kwargs)
        except requests.RequestException:
            logger.exception(
                "%s %s failed after %.2fs", method, url, time.monotonic() - started
            )
            raise

        elapsed = time.monotonic() - started
        log = logger.info if response.ok else logger.warning
        log("%s %s -> %s in %.2fs", method, url, response.status_code, elapsed)
        return response

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        logger.debug("GET requested for %s", path)
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        logger.debug("POST requested for %s", path)
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        logger.debug("PUT requested for %s", path)
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        logger.debug("PATCH requested for %s", path)
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        logger.debug("DELETE requested for %s", path)
        return self.request("DELETE", path, **kwargs)

    # -- lifetime ------------------------------------------------------------

    def close(self) -> None:
        logger.debug("Closing session for %s", self.base_url)
        self._session.close()

    def __enter__(self) -> RestClient:
        logger.debug("Entering client context for %s", self.base_url)
        return self

    def __exit__(self, *exc_info: object) -> None:
        logger.debug("Leaving client context for %s", self.base_url)
        self.close()

    def __repr__(self) -> str:
        # Not logged: logging calls repr() on its arguments, which would recurse.
        # Never expose the token: only its last characters, to tell tokens apart.
        tail = self._api_token[-4:] if len(self._api_token) > 4 else "****"
        return f"RestClient(base_url={self.base_url!r}, api_token='...{tail}')"
