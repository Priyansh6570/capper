"""The YouTube Data and Analytics APIs over plain HTTPS, for one OAuth client and token."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import requests

from publishing import channels, quota

API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
TOKEN_URI = "https://oauth2.googleapis.com/token"

COST_LIST, COST_WRITE, COST_UPLOAD = 1, 50, 1600
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}
WRITABLE_STATUS = ("privacyStatus", "embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids")
CHUNK_BYTES = 16 * 1024 * 1024          # a multiple of 256 KiB, as resumable uploads require
TRANSIENT_RETRIES = 5
EXPIRY_MARGIN_S = 60


class ApiError(Exception):
    def __init__(self, status: int, reason: str | None, message: str | None):
        super().__init__(message or reason or f"HTTP {status}")
        self.status, self.reason = status, reason


class QuotaExceeded(ApiError):
    pass


class AuthError(ApiError):
    pass


class SessionExpired(Exception):
    pass


def _error(resp: requests.Response) -> ApiError:
    try:
        body = resp.json()["error"]
        reason = (body.get("errors") or [{}])[0].get("reason") or body.get("status")
        message = body.get("message")
    except (ValueError, KeyError, AttributeError, TypeError):
        reason, message = None, resp.text[:200]
    return (QuotaExceeded if reason in QUOTA_REASONS else ApiError)(resp.status_code, reason, message)


def token_from(data: dict, refresh: str | None = None) -> dict:
    return {"access": data["access_token"], "refresh": data.get("refresh_token", refresh),
            "expires_at": time.time() + data["expires_in"]}


def refreshed(client: dict, refresh: str) -> dict:
    resp = requests.post(client["token_uri"], timeout=30, data={
        "client_id": client["id"], "client_secret": client["secret"],
        "refresh_token": refresh, "grant_type": "refresh_token"})
    data = resp.json() if resp.content else {}
    if "access_token" not in data:
        raise AuthError(resp.status_code, data.get("error"), data.get("error_description") or "Google refused the token.")
    return token_from(data, refresh)


def _avatar(snippet: dict) -> str:
    thumbs = snippet.get("thumbnails") or {}
    return (thumbs.get("default") or next(iter(thumbs.values()), {})).get("url", "")


class Client:
    def __init__(self, client: dict, token: dict, on_token: Callable[[dict], None]):
        self.client, self.token, self.on_token = client, token, on_token

    @classmethod
    def for_channel(cls, channel_id: str) -> "Client":
        channel = channels.get(channel_id)
        if channel is None:
            raise AuthError(404, "channelNotConnected", "This channel is not connected.")
        return cls(channel["client"], channel["token"], lambda token: channels.update(channel_id, token=token))

    def _access(self, force: bool = False) -> str:
        if force or self.token["expires_at"] - time.time() < EXPIRY_MARGIN_S:
            self.token = refreshed(self.client, self.token["refresh"])
            self.on_token(self.token)
        return self.token["access"]

    def _send(self, method: str, url: str, cost: int = 0, *, reauthorize: bool = True, **kwargs) -> requests.Response:
        if cost:
            quota.spend(self.client["id"], cost)
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._access()}"}
        resp = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 60), **kwargs)
        if resp.status_code == 401 and reauthorize:
            self._access(force=True)
            return self._send(method, url, 0, reauthorize=False, headers=headers, **kwargs)
        if not resp.ok:
            error = _error(resp)
            if isinstance(error, QuotaExceeded):
                quota.exhaust(self.client["id"])
            raise error
        return resp

    def my_channels(self) -> list[dict]:
        items = self._send("GET", f"{API}/channels", COST_LIST, params={"part": "snippet", "mine": "true"}).json().get("items", [])
        return [{"id": i["id"], "title": i["snippet"]["title"], "avatar": _avatar(i["snippet"])} for i in items]

    def playlist_id(self, title: str) -> str:
        """The channel's playlist called `title`, created when it does not exist."""
        page = None
        while True:
            params = {"part": "snippet", "mine": "true", "maxResults": 50, **({"pageToken": page} if page else {})}
            data = self._send("GET", f"{API}/playlists", COST_LIST, params=params).json()
            for playlist in data.get("items", []):
                if playlist["snippet"]["title"] == title:
                    return playlist["id"]
            page = data.get("nextPageToken")
            if not page:
                break
        created = self._send("POST", f"{API}/playlists", COST_WRITE, params={"part": "snippet,status"},
                             json={"snippet": {"title": title}, "status": {"privacyStatus": "public"}})
        return created.json()["id"]

    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        self._send("POST", f"{API}/playlistItems", COST_WRITE, params={"part": "snippet"}, json={"snippet": {
            "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}})

    def comment(self, video_id: str, text: str) -> None:
        """Post a top-level comment on a video as the channel. (The API cannot pin it: that is done in Studio.)"""
        self._send("POST", f"{API}/commentThreads", COST_WRITE, params={"part": "snippet"}, json={"snippet": {
            "videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}})

    def set_thumbnail(self, video_id: str, jpeg: bytes) -> None:
        self._send("POST", f"{UPLOAD}/thumbnails/set", COST_WRITE, params={"videoId": video_id}, data=jpeg,
                   headers={"Content-Type": "image/jpeg"})

    def videos(self, ids: list[str], part: str = "snippet,status,statistics") -> list[dict]:
        found = []
        for start in range(0, len(ids), 50):
            found += self._send("GET", f"{API}/videos", COST_LIST,
                                params={"part": part, "id": ",".join(ids[start:start + 50])}).json().get("items", [])
        return found

    def set_privacy(self, video_id: str, privacy: str) -> str:
        """Change a video's visibility; returns the visibility it really has afterwards."""
        current = self.videos([video_id], "status")
        if not current:
            raise ApiError(404, "videoNotFound", "The video no longer exists on YouTube.")
        status = {k: v for k, v in current[0]["status"].items() if k in WRITABLE_STATUS}
        done = self._send("PUT", f"{API}/videos", COST_WRITE, params={"part": "status"},
                          json={"id": video_id, "status": {**status, "privacyStatus": privacy}})
        return done.json()["status"]["privacyStatus"]

    def upload(self, path: Path, snippet: dict, status: dict, *, session_url: str | None,
               on_session: Callable[[str], None], progress: Callable[[float], None]) -> dict:
        """Upload `path` as a new video, continuing the resumable session `session_url` when it has one."""
        if not session_url:
            started = self._send("POST", f"{UPLOAD}/videos", COST_UPLOAD,
                                 params={"uploadType": "resumable", "part": "snippet,status"},
                                 json={"snippet": snippet, "status": status},
                                 headers={"X-Upload-Content-Length": str(path.stat().st_size),
                                          "X-Upload-Content-Type": "video/*"})
            session_url = started.headers["Location"]
            on_session(session_url)
        return self._send_file(session_url, path, progress)

    @staticmethod
    def _resume_point(resp: requests.Response) -> int:
        sent = resp.headers.get("Range")
        return int(sent.rsplit("-", 1)[1]) + 1 if sent else 0

    def _send_file(self, session_url: str, path: Path, progress: Callable[[float], None]) -> dict:
        size, failures, offset = path.stat().st_size, 0, None
        with open(path, "rb") as source:
            while True:
                try:
                    if offset is None:
                        resp = requests.put(session_url, headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"}, timeout=60)
                    else:
                        source.seek(offset)
                        chunk = source.read(CHUNK_BYTES)
                        resp = requests.put(session_url, data=chunk, timeout=300, headers={
                            "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}"})
                except requests.RequestException:
                    resp = None
                if resp is None or resp.status_code >= 500:
                    failures += 1
                    if failures > TRANSIENT_RETRIES:
                        raise ApiError(resp.status_code if resp else 0, "uploadInterrupted", "The connection to YouTube kept failing.")
                    time.sleep(2 ** failures)
                    offset = None
                elif resp.status_code in (200, 201):
                    return resp.json()
                elif resp.status_code == 308:
                    offset = self._resume_point(resp)
                    progress(offset / size)
                elif resp.status_code == 404:
                    raise SessionExpired()
                else:
                    raise _error(resp)

    def report(self, start: str, end: str, metrics: str, *, dimensions: str | None = None,
               sort: str | None = None, limit: int | None = None) -> list[dict]:
        """Analytics rows as dicts keyed by column name."""
        params = {"ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": metrics,
                  **({"dimensions": dimensions} if dimensions else {}), **({"sort": sort} if sort else {}),
                  **({"maxResults": limit} if limit else {})}
        data = self._send("GET", ANALYTICS, 0, params=params).json()
        names = [c["name"] for c in data.get("columnHeaders", [])]
        return [dict(zip(names, row)) for row in data.get("rows", [])]
