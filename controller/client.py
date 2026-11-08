"""HTTP client for the simulator. The only way the controller learns anything about the world.

- Transport errors and 5xx are retried with backoff for idempotent calls.
- POST /approvals and PUT /config are idempotent by proposal_id on the server (409 duplicate_proposal_id),
  so they are retried too; the caller reconciles a duplicate.
- POST /sim/advance is NOT retried: the caller checks /sim/state to see whether time moved.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import settings

log = logging.getLogger("controller.client")


class SimError(Exception):
    def __init__(self, status: int, code: str, detail=None):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class SimUnavailable(Exception):
    pass


class SimClient:
    def __init__(self, url: str, timeout: float = settings.HTTP_TIMEOUT, retries: int = settings.HTTP_RETRIES):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout)
        self.retries = retries
        self.calls = 0

    def _send(self, method: str, path: str, *, json=None, params=None, retry=True, raw=False):
        attempt = 0
        while True:
            self.calls += 1
            try:
                r = self.http.request(method, path, json=json, params=params)
            except httpx.TransportError as e:
                if not retry or attempt >= self.retries:
                    raise SimUnavailable(f"{method} {path}: {e}") from e
                self._sleep(attempt, f"{method} {path} transport error {e!r}")
                attempt += 1
                continue
            if r.status_code >= 500 and retry and attempt < self.retries:
                self._sleep(attempt, f"{method} {path} -> {r.status_code}")
                attempt += 1
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = {"detail": r.text}
                code = body.get("error")
                if not code:
                    code = body.get("detail") if isinstance(body.get("detail"), str) else f"http_{r.status_code}"
                raise SimError(r.status_code, code, body.get("detail"))
            return r.text if raw else r.json()

    @staticmethod
    def _sleep(attempt: int, why: str):
        d = min(0.25 * 2 ** attempt, 10.0)
        log.warning("retrying in %.2fs: %s", d, why)
        time.sleep(d)

    # ---- world
    def health(self):
        return self._send("GET", "/health")

    def state(self):
        return self._send("GET", "/sim/state")

    def advance(self, minutes: int):
        return self._send("POST", "/sim/advance", json={"minutes": minutes}, retry=False)

    def chunks(self, kind: str, after: int = -1):
        return self._send("GET", "/events/chunks", params={"kind": kind, "after": after})

    def chunk_text(self, n: int) -> str:
        return self._send("GET", f"/events/chunks/{n}", raw=True)

    # ---- workflows
    def workflows(self):
        return self._send("GET", "/workflows")

    def config(self, wid: str):
        return self._send("GET", f"/workflows/{wid}/config")

    def config_history(self, wid: str):
        return self._send("GET", f"/workflows/{wid}/config/history")

    def auto_apply(self, wid: str, body: dict):
        return self._send("PUT", f"/workflows/{wid}/config", json=body)

    # ---- governance
    def submit(self, body: dict):
        return self._send("POST", "/approvals", json=body)

    def approval(self, aid: str):
        return self._send("GET", f"/approvals/{aid}")

    def approvals(self, workflow_id: str | None = None, status: str | None = None):
        p = {k: v for k, v in (("workflow_id", workflow_id), ("status", status)) if v}
        return self._send("GET", "/approvals", params=p)

    def apply(self, wid: str, approval_id: str):
        return self._send("POST", f"/workflows/{wid}/apply", json={"approval_id": approval_id})

    # ---- credits
    def suggest_fixes(self, wid: str, problem: str):
        return self._send("POST", "/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem})

    def validate(self, wid: str, prompt_version: str, n: int):
        return self._send("POST", "/validation/runs", json={"workflow_id": wid, "prompt_version": prompt_version,
                                                            "n": n})
