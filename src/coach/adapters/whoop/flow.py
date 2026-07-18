"""Interactive authorization-code capture via a one-shot localhost server.

Isolated from :mod:`auth` because it touches the browser, a socket, and stdout —
none of which belong in the testable OAuth core. Runs only under
``coach auth whoop`` and needs a real WHOOP login, so it is not unit-tested
(marked live-verification-pending).
"""

from __future__ import annotations

import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from .auth import TokenSet, TokenStore, WhoopOAuth


class _CallbackHandler(BaseHTTPRequestHandler):
    captured: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        q = parse_qs(urlparse(self.path).query)
        _CallbackHandler.captured = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in _CallbackHandler.captured
        msg = "Authorized — you can close this tab." if ok else "Authorization failed."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *_args) -> None:  # silence default logging
        return


def run_login(oauth: WhoopOAuth, store: TokenStore, redirect_uri: str) -> TokenSet:
    """Open the browser, capture the callback, exchange the code, persist tokens."""
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080

    state = secrets.token_urlsafe(24)
    auth_url = oauth.authorize_url(state=state)

    _CallbackHandler.captured = {}
    server = HTTPServer((host, port), _CallbackHandler)

    print("Opening your browser to authorize WHOOP…")
    print(f"If it doesn't open, visit:\n  {auth_url}")
    webbrowser.open(auth_url)

    server.handle_request()  # blocks until the single callback arrives
    server.server_close()

    captured = _CallbackHandler.captured
    if captured.get("state") != state:
        raise RuntimeError("OAuth state mismatch — aborting (possible CSRF).")
    if "code" not in captured:
        raise RuntimeError(f"No authorization code received: {captured}")

    tokens = oauth.exchange_code(captured["code"])
    store.save(tokens)
    return tokens
