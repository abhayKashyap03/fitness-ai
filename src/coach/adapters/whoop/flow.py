"""Interactive authorization-code capture for the WHOOP OAuth login.

Two flows, one exchange path:

* :func:`run_login` — the laptop flow. Opens a browser and binds a one-shot
  localhost server to catch the redirect.
* :func:`run_login_headless` — the **host** flow. Prints the URL, and the
  operator pastes the redirect back. Needed because
  [ADR-0019](../../../../docs/adr/0019-hosting-the-owners-instance.md) authorises
  WHOOP *on the host* against a public ``https://<domain>/callback``, and the
  laptop flow cannot run there: ``webbrowser.open`` has no browser to open and
  binding port 8080 for a callback that arrives at port 443 catches nothing.

Isolated from :mod:`auth` because these touch the browser, a socket, and stdout
— none of which belong in the testable OAuth core. The parts that *can* be
tested without a real WHOOP login (state verification, redirect parsing) are
split out below and are unit-tested; the end-to-end flows are not.
"""

from __future__ import annotations

import secrets
import webbrowser
from collections.abc import Callable
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


def parse_redirect(pasted: str) -> dict[str, str]:
    """Pull the OAuth parameters out of a pasted redirect URL.

    Accepts a full URL or a bare query string, because an operator copying from
    a browser address bar reasonably produces either. Tolerant of surrounding
    whitespace for the same reason.

    A **bare code** is deliberately not accepted. The ``state`` parameter is the
    only defence against a code injected by someone else, and a flow that
    accepts input with no state to check has quietly dropped that defence — the
    error message tells the operator to paste the whole URL instead.
    """
    text = pasted.strip().strip("'\"")
    if not text:
        raise RuntimeError("nothing pasted")
    query = urlparse(text).query or (text if "=" in text else "")
    params = {k: v[0] for k, v in parse_qs(query).items()}
    if not params:
        raise RuntimeError(
            "could not find any OAuth parameters in that input — paste the FULL "
            "redirect URL from the browser's address bar, including everything "
            "after the '?'"
        )
    if "state" not in params:
        raise RuntimeError(
            "that URL has no 'state' parameter. Paste the complete redirect URL: "
            "state is what proves the code came from the login you started, and "
            "this flow will not skip it."
        )
    return params


def _exchange(
    oauth: WhoopOAuth, store: TokenStore, captured: dict[str, str], expected_state: str
) -> TokenSet:
    """Verify the callback and turn the code into stored tokens.

    Shared by both flows so the security checks cannot differ between them —
    a headless path that forgot the state check would be a real vulnerability
    that no test of the laptop path would notice.
    """
    if captured.get("error"):
        # WHOOP's own refusal (user declined, bad client). Surfaced verbatim
        # rather than flattened into "no code received", which sends the
        # operator hunting for a network problem that isn't there.
        raise RuntimeError(
            f"WHOOP refused the authorization: {captured['error']}"
            + (f" — {captured['error_description']}" if "error_description" in captured else "")
        )
    if captured.get("state") != expected_state:
        raise RuntimeError("OAuth state mismatch — aborting (possible CSRF).")
    if "code" not in captured:
        raise RuntimeError(f"No authorization code received: {captured}")
    tokens = oauth.exchange_code(captured["code"])
    store.save(tokens)
    return tokens


def run_login(oauth: WhoopOAuth, store: TokenStore, redirect_uri: str) -> TokenSet:
    """Open the browser, capture the callback, exchange the code, persist tokens.

    The laptop flow. Requires a desktop browser and the ability to bind the
    redirect's port locally — see :func:`run_login_headless` for a host.
    """
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

    return _exchange(oauth, store, _CallbackHandler.captured, state)


def run_login_headless(
    oauth: WhoopOAuth,
    store: TokenStore,
    *,
    prompt: Callable[[str], str] = input,
    echo: Callable[[str], None] = print,
) -> TokenSet:
    """Authorize WHOOP with no browser and no listening socket.

    The host flow (ADR-0019 §3). The operator opens the printed URL on whatever
    machine has a browser, completes the WHOOP login, and pastes back the URL
    the browser ended up at. Nothing needs to be listening on the host for this
    to work, which matters because it must run *before* the reverse proxy and
    TLS are finished.

    ``prompt``/``echo`` are injected so the flow is testable without stdin.
    """
    state = secrets.token_urlsafe(24)
    auth_url = oauth.authorize_url(state=state)

    echo("Open this URL in any browser and authorize WHOOP:\n")
    echo(f"  {auth_url}\n")
    echo(
        "You will be redirected to your callback URL. That page may fail to load —\n"
        "that is fine and expected; the part we need is in the address bar.\n"
        "Copy the ENTIRE resulting URL and paste it here.\n"
    )
    pasted = prompt("Redirect URL: ")
    return _exchange(oauth, store, parse_redirect(pasted), state)
