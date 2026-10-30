"""Connecting a channel: the OAuth sign-in in the user's browser (a loopback redirect with PKCE),
then the choice of the channel when the account has several."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from publishing import channels, youtube_api

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = " ".join(f"https://www.googleapis.com/auth/{scope}" for scope in ("youtube", "youtube.upload", "yt-analytics.readonly"))
SIGN_IN_TIMEOUT_S = 600
IDLE_CONNECTION_S = 10     # browsers open spare connections that never send a request
PAGE = "<html><body style='font-family:sans-serif;padding:3em'><h2>{}</h2><p>You can close this tab; ReCapper shows the result.</p></body></html>"


def load_client(file: str) -> dict:
    """The OAuth client in a Google Cloud 'client_secret' JSON; ValueError says what is wrong with it."""
    try:
        raw = json.loads(Path(file.strip().strip('"')).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"Could not read the OAuth client file: {e}") from e
    section = raw.get("installed") if isinstance(raw, dict) else None
    if not section or not section.get("client_id") or not section.get("client_secret"):
        raise ValueError("This is not a Desktop-app OAuth client: create one of type 'Desktop app' in Google Cloud and download its JSON.")
    return {"id": section["client_id"], "secret": section["client_secret"],
            "auth_uri": section.get("auth_uri", AUTH_URI), "token_uri": section.get("token_uri", youtube_api.TOKEN_URI)}


class SignIn:
    """One sign-in. `state` is waiting (for the browser), working, choose (several channels), done or error."""

    def __init__(self, client: dict):
        self.client, self.state, self.message, self.options = client, "waiting", "Waiting for you to sign in in the browser...", []
        self._verifier, self._csrf = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
        self._code: str | None = None
        self._error: str | None = None
        self._token: dict | None = None
        self._answered = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_port}"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
        self.url = f"{client['auth_uri']}?" + urlencode({
            "client_id": client["id"], "redirect_uri": self.redirect_uri, "response_type": "code", "scope": SCOPES,
            "state": self._csrf, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent select_account"})

    def _handler(self):
        sign_in = self

        class Handler(BaseHTTPRequestHandler):
            timeout = IDLE_CONNECTION_S

            def do_GET(self):
                query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                if query.get("state") != sign_in._csrf or not ("code" in query or "error" in query):
                    self.send_response(404)
                    self.end_headers()
                    return
                if "code" in query:
                    sign_in._code = query["code"]
                else:
                    sign_in._error = f"Google sign-in was cancelled ({query['error']})."
                body = PAGE.format("Signed in" if sign_in._code else "Sign-in cancelled").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                sign_in._answered.set()

            def log_message(self, *args):
                pass

        return Handler

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()
        webbrowser.open(self.url)

    def _fail(self, message: str) -> None:
        self.state, self.message = "error", message

    def close(self) -> None:
        """Stop waiting for the browser; a sign-in that is already past that point carries on."""
        self._answered.set()

    def _run(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        answered = self._answered.wait(SIGN_IN_TIMEOUT_S)
        self._server.shutdown()
        self._server.server_close()
        if self._code is None:
            self._fail(self._error or ("Sign-in was replaced by a new one." if answered else "Sign-in timed out; try again."))
            return
        self.state, self.message = "working", "Connecting the channel..."
        try:
            self._finish()
        except (youtube_api.ApiError, requests.RequestException, ValueError, KeyError) as e:
            self._fail(f"Could not connect the channel: {e}")

    def _finish(self) -> None:
        resp = requests.post(self.client["token_uri"], timeout=30, data={
            "client_id": self.client["id"], "client_secret": self.client["secret"], "code": self._code,
            "code_verifier": self._verifier, "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"})
        data = resp.json()
        if "access_token" not in data:
            raise ValueError(data.get("error_description") or data.get("error") or "Google refused the sign-in.")
        if "refresh_token" not in data:
            raise ValueError("Google gave no refresh token: remove ReCapper at myaccount.google.com/permissions and connect again.")
        self._token = youtube_api.token_from(data)
        found = youtube_api.Client(self.client, self._token, lambda token: None).my_channels()
        if not found:
            raise ValueError("This Google account has no YouTube channel.")
        if len(found) == 1:
            self._save(found[0])
        else:
            self.options, self.state, self.message = found, "choose", "Choose the channel to connect."

    def _save(self, info: dict) -> None:
        channels.save(channels.new(info, self.client, self._token))
        self.state, self.message, self.options = "done", f"Connected {info['title']}.", []

    def choose(self, channel_id: str) -> None:
        picked = next((o for o in self.options if o["id"] == channel_id), None)
        if self.state != "choose" or picked is None:
            raise ValueError("There is no such channel to choose.")
        self._save(picked)

    def view(self) -> dict:
        return {"state": self.state, "message": self.message, "options": self.options}


_current: SignIn | None = None


def begin(client_file: str) -> SignIn:
    global _current
    client = load_client(client_file)
    if _current:
        _current.close()
    _current = SignIn(client)
    _current.start()
    return _current


def current() -> SignIn | None:
    return _current
