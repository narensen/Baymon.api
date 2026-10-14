"""HTTP client for the Baymon API. Handles sign-in, token refresh and errors."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx

from baymon.cli.config import Config, Credentials


class ApiError(Exception):
    def __init__(self, message: str, code: str = "api_error", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _error_from(resp: httpx.Response) -> ApiError:
    try:
        err = resp.json().get("error", {})
        return ApiError(err.get("message") or resp.reason_phrase, err.get("code", "api_error"), resp.status_code)
    except ValueError:
        return ApiError(f"Server error {resp.status_code}: {resp.text[:200]}", "api_error", resp.status_code)


class Client:
    def __init__(self, config: Config, creds: Credentials | None = None) -> None:
        self.config = config
        self.creds = creds if creds and creds.api_url in ("", config.api_url) else None
        # Generous read timeout: Render free instances can take ~a minute to wake.
        self.http = httpx.Client(base_url=config.api_url, timeout=httpx.Timeout(90.0, connect=15.0))

    def set_api_url(self, url: str) -> None:
        self.config.api_url = url.rstrip("/")
        self.http = httpx.Client(base_url=self.config.api_url, timeout=self.http.timeout)
        if self.creds and self.creds.api_url != self.config.api_url:
            self.creds = None

    # ---------------------------------------------------------------- auth

    @property
    def signed_in(self) -> bool:
        return self.creds is not None

    def _store(self, data: dict[str, Any]) -> Credentials:
        self.creds = Credentials(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data.get("expires_at") or time.time() + 3600),
            user_id=data["user"]["id"],
            email=data["user"].get("email") or "",
            api_url=self.config.api_url,
        )
        self.creds.save()
        return self.creds

    def login(self, email: str, password: str) -> Credentials:
        resp = self._send("POST", "/v1/auth/login", json={"email": email, "password": password}, auth=False)
        return self._store(resp.json())

    def logout(self) -> None:
        self.creds = None
        Credentials.clear()

    def _refresh(self) -> None:
        if not self.creds:
            raise ApiError("Not signed in. Run `baymon login`.", "unauthorized", 401)
        try:
            resp = self._send("POST", "/v1/auth/refresh", json={"refresh_token": self.creds.refresh_token}, auth=False)
        except ApiError as exc:
            if exc.status == 401:
                self.logout()
                raise ApiError("Your session expired. Run `baymon login`.", "unauthorized", 401) from exc
            raise
        self._store(resp.json())

    def _headers(self) -> dict[str, str]:
        if not self.creds:
            raise ApiError(f"Not signed in to {self.config.api_url}. Run `baymon login`.", "unauthorized", 401)
        if self.creds.expires_at - time.time() < 60:
            self._refresh()
        assert self.creds is not None
        return {"Authorization": f"Bearer {self.creds.access_token}"}

    # ------------------------------------------------------------- requests

    def _send(self, method: str, path: str, auth: bool = True, **kwargs: Any) -> httpx.Response:
        for attempt in (1, 2):
            headers = self._headers() if auth else {}
            try:
                resp = self.http.request(method, path, headers=headers, **kwargs)
            except httpx.ConnectError as exc:
                raise ApiError(
                    f"Can't reach the Baymon server at {self.config.api_url}. "
                    "Is it running? Change it with `baymon config api_url <url>`.",
                    "unreachable",
                ) from exc
            except httpx.TimeoutException as exc:
                raise ApiError("The Baymon server took too long to respond.", "timeout") from exc
            if resp.status_code == 401 and auth and attempt == 1:
                self._refresh()
                continue
            if resp.status_code >= 400:
                raise _error_from(resp)
            return resp
        raise ApiError("Not authorized.", "unauthorized", 401)

    def me(self) -> dict[str, Any]:
        return self._send("GET", "/v1/me").json()

    def models(self) -> list[dict[str, Any]]:
        return self._send("GET", "/v1/models").json()

    def health(self) -> bool:
        try:
            return self.http.get("/health", timeout=10).status_code == 200
        except httpx.HTTPError:
            return False

    def stream_agent(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield NDJSON events from /v1/agent/complete."""
        for attempt in (1, 2):
            headers = self._headers()
            try:
                with self.http.stream("POST", "/v1/agent/complete", json=payload, headers=headers,
                                      timeout=httpx.Timeout(300.0, connect=15.0)) as resp:
                    if resp.status_code == 401 and attempt == 1:
                        self._refresh()
                        continue
                    if resp.status_code >= 400:
                        resp.read()
                        raise _error_from(resp)
                    for line in resp.iter_lines():
                        if line.strip():
                            yield json.loads(line)
                    return
            except httpx.ConnectError as exc:
                raise ApiError(f"Can't reach the Baymon server at {self.config.api_url}.", "unreachable") from exc
            except httpx.TimeoutException as exc:
                raise ApiError("The model took too long to respond.", "timeout") from exc
            except httpx.RemoteProtocolError as exc:
                raise ApiError("The connection to the server dropped mid-reply.", "disconnected") from exc

    # --------------------------------------------------------------- memory

    def memory_list(self) -> list[dict[str, Any]]:
        return self._send("GET", "/v1/memory").json()["items"]

    def memory_add(self, content: str, category: str = "other") -> dict[str, Any]:
        return self._send("POST", "/v1/memory", json={"content": content, "category": category}).json()

    def memory_delete(self, memory_id: str) -> None:
        self._send("DELETE", f"/v1/memory/{memory_id}")

    def memory_import(self, text: str) -> list[dict[str, Any]]:
        return self._send("POST", "/v1/memory/import", json={"text": text}).json()["items"]

    def memory_observe(self, user_message: str, reply: str) -> None:
        self._send("POST", "/v1/memory/observe", json={"user_message": user_message[:20_000], "reply": reply[:40_000]})

    # ---------------------------------------------------------------- voice

    def transcribe(self, wav: bytes, prompt: str | None = None) -> str:
        data = {"prompt": prompt[-896:]} if prompt else None
        resp = self._send("POST", "/v1/transcribe", files={"file": ("speech.wav", wav, "audio/wav")}, data=data)
        return resp.json()["text"]

    def speak_reply(self, text: str, voice: str | None = None) -> tuple[str, bytes | None, list[dict[str, str]]]:
        body = self._send("POST", "/v1/speak/reply", json={"text": text, **({"voice": voice} if voice else {})}).json()
        audio = base64.b64decode(body["audio_base64"]) if body.get("audio_base64") else None
        return body["caption"], audio, body.get("warnings", [])
