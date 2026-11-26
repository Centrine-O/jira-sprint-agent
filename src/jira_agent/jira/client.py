"""Thin HTTP client for Jira Cloud (REST API v3 and Agile API 1.0).

Handles authentication, retries with backoff, rate limits (HTTP 429),
pagination, and plain-language error messages.
"""

from __future__ import annotations

import random
import time
from typing import Any, Iterator

import httpx

from jira_agent.config import Secrets, get_secrets

# Status codes that mean "try again later" rather than "your request is wrong".
RETRYABLE_STATUS = {429, 502, 503, 504}

# Methods that are safe to repeat. POST is not, because repeating a create
# could make duplicate issues. A 429 is the exception: Jira rejected the
# request without processing it, so retrying is always safe.
IDEMPOTENT_METHODS = {"GET", "PUT", "DELETE", "HEAD", "OPTIONS"}


class JiraError(Exception):
    """A Jira request failed. `message` is written for humans."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or []

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} Details: {'; '.join(self.details)}"
        return self.message


def _error_details(response: httpx.Response) -> list[str]:
    """Pull Jira's own error text out of a failed response."""
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return [text[:300]] if text else []
    details: list[str] = []
    if isinstance(data, dict):
        details.extend(str(m) for m in data.get("errorMessages", []) or [])
        for field, msg in (data.get("errors") or {}).items():
            details.append(f"{field}: {msg}")
        if not details and data.get("message"):
            details.append(str(data["message"]))
    return details


def _friendly_message(response: httpx.Response) -> str:
    code = response.status_code
    if code == 401:
        return (
            "Jira rejected the login. Check JIRA_EMAIL and JIRA_API_TOKEN in .env. "
            "The token may have expired or been revoked."
        )
    if code == 403:
        return "Your Jira account doesn't have permission to do this."
    if code == 404:
        return (
            "Jira couldn't find that item. It may not exist, or your account "
            "may not be able to see it."
        )
    if code == 429:
        return "Jira is rate-limiting requests. Wait a minute and try again."
    if 500 <= code < 600:
        return f"Jira had a server problem (HTTP {code}). Try again shortly."
    return f"Jira returned an error (HTTP {code})."


class JiraClient:
    """Use as a context manager:

        with JiraClient() as jira:
            me = jira.get("/rest/api/3/myself")
    """

    def __init__(
        self,
        secrets: Secrets | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
    ) -> None:
        secrets = secrets or get_secrets()
        self.base_url = secrets.jira_base_url
        self.max_retries = max_retries
        self._http = httpx.Client(
            base_url=self.base_url,
            auth=(secrets.jira_email, secrets.jira_api_token.get_secret_value()),
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

    # ---- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- core request ----------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        retry: bool | None = None,
    ) -> Any:
        """Send a request. Returns parsed JSON, text, or None (for empty replies).

        `retry` overrides the default: idempotent methods retry, POST doesn't.
        """
        method = method.upper()
        can_retry = retry if retry is not None else method in IDEMPOTENT_METHODS
        attempt = 0

        while True:
            try:
                response = self._http.request(
                    method, path, params=params, json=json, files=files, headers=headers
                )
            except httpx.TransportError as exc:
                if can_retry and attempt < self.max_retries:
                    self._wait(attempt, None)
                    attempt += 1
                    continue
                raise JiraError(
                    f"Couldn't reach Jira at {self.base_url}. "
                    "Check your internet connection and JIRA_BASE_URL."
                ) from exc

            status = response.status_code
            should_retry = status in RETRYABLE_STATUS and (can_retry or status == 429)
            if should_retry and attempt < self.max_retries:
                self._wait(attempt, response.headers.get("Retry-After"))
                attempt += 1
                continue

            if status >= 400:
                raise JiraError(_friendly_message(response), status, _error_details(response))

            if status == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    @staticmethod
    def _wait(attempt: int, retry_after: str | None) -> None:
        """Sleep before a retry: Jira's Retry-After if given, else exponential backoff."""
        delay: float | None = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(2**attempt, 30) + random.uniform(0, 1)
        time.sleep(delay)

    # ---- pagination ------------------------------------------------------

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        items_key: str = "values",
        page_size: int = 50,
    ) -> Iterator[dict[str, Any]]:
        """Walk endpoints that page with startAt/maxResults (Agile API and most v3 lists).

        Handles both shapes Jira uses: a dict with a list under `items_key`,
        or a bare list.
        """
        start = 0
        while True:
            page_params = dict(params or {})
            page_params.update(startAt=start, maxResults=page_size)
            data = self.get(path, params=page_params)

            items = data if isinstance(data, list) else (data or {}).get(items_key, [])
            yield from items

            if not items:
                return
            if isinstance(data, dict):
                if data.get("isLast") is True:
                    return
                total = data.get("total")
                if total is not None and start + len(items) >= total:
                    return
            if len(items) < page_size:
                return
            start += len(items)

    def search_jql(
        self,
        jql: str,
        fields: list[str] | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Search issues with JQL using the current endpoint (/rest/api/3/search/jql),
        which pages with nextPageToken instead of startAt."""
        token: str | None = None
        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": fields or ["*navigable"],
            }
            if token:
                body["nextPageToken"] = token
            # Search is read-only, so retrying the POST is safe.
            data = self.post("/rest/api/3/search/jql", json=body, retry=True) or {}
            yield from data.get("issues", [])
            token = data.get("nextPageToken")
            if not token or data.get("isLast") is True:
                return