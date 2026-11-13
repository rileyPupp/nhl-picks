"""Sign in with Google or GitHub (OAuth 2.0 authorization-code flow). Standard library only.

A provider is turned on by setting its client ID and secret in the environment:
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET
PUBLIC_URL is the site's address (e.g. https://nhlpicks.example.com); callbacks go to
PUBLIC_URL/auth/<provider>/callback.
"""
import json
import os
import urllib.request
from urllib.parse import urlencode

PROVIDERS = {
    "google": {
        "label": "Google",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "user_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "extra": {"prompt": "select_account"},
    },
    "github": {
        "label": "GitHub",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "user_url": "https://api.github.com/user",
        "scope": "read:user",
        "extra": {"allow_signup": "true"},
    },
}


class OAuthError(Exception):
    pass


def _credentials(name):
    prefix = name.upper()
    cid, secret = os.environ.get(f"{prefix}_CLIENT_ID"), os.environ.get(f"{prefix}_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


def enabled():
    """Providers with credentials set, in display order."""
    return [{"id": name, "label": p["label"]} for name, p in PROVIDERS.items() if _credentials(name)]


def is_enabled(name):
    return name in PROVIDERS and _credentials(name) is not None


def redirect_uri(public_url, name):
    return f"{public_url.rstrip('/')}/auth/{name}/callback"


def authorize_url(name, public_url, state):
    p = PROVIDERS[name]
    cid, _ = _credentials(name)
    params = {"client_id": cid, "redirect_uri": redirect_uri(public_url, name), "response_type": "code",
              "scope": p["scope"], "state": state, **p["extra"]}
    return f'{p["authorize_url"]}?{urlencode(params)}'


def _request(url, data=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": "nhl-picks"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except (OSError, ValueError) as e:
        raise OAuthError(f"Couldn't reach {url.split('/')[2]}.") from e


def fetch_identity(name, public_url, code):
    """Swap the callback code for the person's identity: {subject, name, email}."""
    p = PROVIDERS[name]
    cid, secret = _credentials(name)
    token = _request(p["token_url"], {
        "client_id": cid, "client_secret": secret, "code": code,
        "redirect_uri": redirect_uri(public_url, name), "grant_type": "authorization_code",
    })
    access = token.get("access_token")
    if not access:
        raise OAuthError(token.get("error_description") or token.get("error") or "Sign-in was not completed.")
    info = _request(p["user_url"], token=access)
    if name == "google":
        if not info.get("sub"):
            raise OAuthError("Google didn't return an account id.")
        return {"subject": str(info["sub"]), "name": info.get("given_name") or info.get("name") or "",
                "email": info.get("email")}
    if not info.get("id"):
        raise OAuthError("GitHub didn't return an account id.")
    return {"subject": str(info["id"]), "name": info.get("login") or info.get("name") or "",
            "email": info.get("email")}
