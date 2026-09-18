#!/usr/bin/env python3
"""
HLS (m3u8) Downloader
Features: master playlist quality selection, alternate audio track selection
(listed interactively; the DEFAULT rendition is auto-selected), WebVTT
subtitle track selection and muxing, AES-128 decryption (explicit and
implicit IVs, per-segment key rotation), SAMPLE-AES / SAMPLE-AES-CTR
per-sample decryption for fMP4/CMAF (cenc/cbcs, clearkey), byte-range
segments, fMP4 init-segment handling, ad-break skipping (CUE-OUT/CUE-IN),
live playlist polling, concurrent downloads with resume (including HTTP-Range
resume of interrupted segments), retries with backoff, optional request-rate
and bandwidth throttling, optional proxy, per-host remembered header profiles,
cookie support, cookie-necessity detection (signed-URL/JWT awareness with
expiry checking; graceful continuation when browser-cookie extraction fails,
with per-browser profile-location diagnosis covering XDG/Snap/Flatpak
installs), 401/403 diagnostics with curl-replay output, a run history log,
probe/dry-run mode with size estimation and JSON output, batch mode, a
doctor/self-check command, per-output locking, post-merge duration and
silent-output verification, clean Ctrl-C handling (finalize-the-partial or
abort, always exit code 130, never a shutdown hang), an optional aria2c
backend, and a Tokyo Night-themed progress display.

Usage:
    python hls_downloader.py <m3u8_url> [-o output.mp4] [-q 720] [-w 8]
                              [--rate 1] [--limit-rate 2M] [--proxy URL]
                              [--referer URL] [--origin URL]
                              [--user-agent UA] [--page-url URL]
                              [--cookie "name=value; ..."] [--audio N]
                              [--subs N] [--skip-ads] [--no-live-poll]
                              [--probe] [--estimate] [--json]
                              [--batch FILE] [--doctor] [--quiet]
"""

import argparse
import base64
import json
import os
import re
import shlex
import struct
import sys
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse, parse_qs
from threading import Lock, Thread

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    import urllib3
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests")

try:
    import Crypto as _Crypto
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import browser_cookie3
    HAS_BROWSER_COOKIE3 = True
except ImportError:
    HAS_BROWSER_COOKIE3 = False


# Segment count above which an available aria2c is used automatically
# (roadmap: "optional aria2c backend for very large segment counts").
ARIA2C_AUTO_THRESHOLD = 300

# browser-cookie3 function name per --cookies-from-browser choice. Some of
# these only work on the platform the browser actually ships on (e.g. Safari
# is macOS-only); browser_cookie3 raises if the store isn't found there.
BROWSER_COOKIE_LOADERS = {
    "firefox": "firefox",
    "chrome": "chrome",
    "chromium": "chromium",
    "edge": "edge",
    "brave": "brave",
    "opera": "opera",
    "vivaldi": "vivaldi",
    "safari": "safari",
}


# --------------------------------------------------------------------------
# Tokyo Night theming — stderr for UI/status, stdout stays clean for data
# --------------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GRAY = "\033[38;2;86;95;137m"
    BLUE = "\033[38;2;122;162;247m"
    CYAN = "\033[38;2;125;207;255m"
    GREEN = "\033[38;2;158;206;106m"
    YELLOW = "\033[38;2;224;175;104m"
    RED = "\033[38;2;247;118;142m"
    MAGENTA = "\033[38;2;187;154;247m"


QUIET = False  # set from --quiet; suppresses routine status/spinner chatter


def status(msg, color=C.CYAN):
    if QUIET:
        return
    print(f"{color}›{C.RESET} {msg}", file=sys.stderr)


def warn(msg):
    print(f"{C.YELLOW}⚠{C.RESET} {msg}", file=sys.stderr)


def error(msg):
    print(f"{C.RED}✗{C.RESET} {msg}", file=sys.stderr)


def success(msg):
    print(f"{C.GREEN}✓{C.RESET} {msg}", file=sys.stderr)


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Spinner:
    """Background braille spinner for indeterminate waits (key fetch, merge).
    A no-op under --quiet."""

    def __init__(self, message):
        self.message = message
        self._stop = False
        self._thread = None

    def __enter__(self):
        if QUIET:
            return self
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        i = 0
        while not self._stop:
            frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
            print(f"\r{C.MAGENTA}{frame}{C.RESET} {self.message}",
                  end="", file=sys.stderr, flush=True)
            time.sleep(0.08)
            i += 1
        print("\r" + " " * (len(self.message) + 4) + "\r",
              end="", file=sys.stderr, flush=True)

    def __exit__(self, *exc):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.5)


def fmt_eta(seconds):
    if seconds is None or seconds != seconds or seconds in (float("inf"), float("-inf")):
        return "?"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def fmt_size(n):
    """1234567 → '1.2 MB'"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _hard_exit(code=130):
    """Exit without interpreter shutdown. Skipping shutdown means skipping
    the atexit hook that joins lingering ThreadPoolExecutor worker threads —
    that join blocks for as long as an in-flight segment read (up to the
    socket timeout), and a Ctrl-C during it is what prints the un-catchable
    'Exception ignored on threading shutdown' traceback. Callers must have
    already flushed anything they care about to disk; stdio is flushed here."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


# --------------------------------------------------------------------------
# Global rate limiters — honored by every fetch() from any worker thread
# --------------------------------------------------------------------------

_RATE_LIMITER = None   # requests/sec pacing (RateLimiter)
_BYTE_LIMITER = None   # bytes/sec pacing (ByteRateLimiter, --limit-rate)
_TENC_CACHE = {}       # init-segment URL → {track_id: tenc defaults} (SAMPLE-AES)
_SGBP_WARNED = False
_INTERRUPTED = False   # Ctrl-C was caught mid-download; partial file was finalized


class RateLimiter:
    """Thread-safe pacing: guarantees a minimum interval between successive
    HTTP requests across all worker threads. For sources that publish limits
    like 'no more than 1 request per second'."""

    def __init__(self, rps):
        self.min_interval = (1.0 / rps) if rps and rps > 0 else 0.0
        self._lock = Lock()
        self._next_ok = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_ok - now
            self._next_ok = max(now, self._next_ok) + self.min_interval
        if delay > 0:
            time.sleep(delay)


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)[bB]?\s*$")


def _parse_size(text):
    """argparse type for --limit-rate: '500K', '2M', '1G', or plain bytes."""
    m = _SIZE_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"bad size {text!r} — use e.g. 500K, 2M, 1G")
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


class ByteRateLimiter:
    """Token-bucket bytes/sec cap shared across all worker threads. Unlike
    --rate (requests/sec), this paces actual bandwidth for sources that cap
    throughput rather than request count."""

    def __init__(self, bps):
        self.bps = max(1, int(bps))
        self._lock = Lock()
        self._tokens = float(self.bps)
        self._last = time.monotonic()

    def consume(self, n):
        with self._lock:
            now = time.monotonic()
            self._tokens = min(float(self.bps),
                               self._tokens + (now - self._last) * self.bps)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                wait = 0.0
            else:
                wait = (n - self._tokens) / self.bps
                self._tokens = 0.0
        if wait > 0:
            time.sleep(wait)


# --------------------------------------------------------------------------
# URL auth analysis & cookie-necessity detection
#
# Most HLS sources authenticate with a credential embedded in the URL
# (signed tokens) plus a Referer — not session cookies. A browser never
# sends the embedding site's cookies to the media host, so cookie problems
# (extraction failures, empty jars) usually cost nothing. These helpers say
# so up front instead of sending the user chasing a non-problem — and flag
# the one thing that really dooms a run: an expired token.
# --------------------------------------------------------------------------

_DIAGNOSIS_SHOWN = False      # 401/403 diagnosis printed once
_COOKIE_EXTRACT_ERROR = None  # message from a failed --cookies-from-browser load
_COOKIE_UNNEEDED_NOTED = False
_URL_SIGNED = False           # advise_cookie_necessity() saw a self-authenticating URL


class CookieExtractError(Exception):
    """--cookies-from-browser couldn't produce cookies. Deliberately not
    fatal at the call site: whether cookies matter depends entirely on how
    the source authenticates, which we only learn from the first request."""


# Query parameters that carry a signed-URL credential rather than routing or
# display data. Their presence means the URL itself is the credential — and a
# browser would send none of the embedding site's cookies to the media host.
_SIGNED_PARAM_NAMES = {
    "token", "jwt", "signature", "sig", "hmac", "hash", "auth",
    "hdnts",                   # Akamai token auth
    "policy", "key-pair-id",   # CloudFront signed URLs (with 'Signature')
    "e", "s",                  # generic expiry+signature pair ('?e=...&s=...')
}


def _b64url_decode(seg):
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _decode_jwt(value):
    """Decoded payload dict if `value` looks like a JWT (three base64url
    segments, header carrying alg/typ). The signature is NOT verified — only
    claims like exp are read, to explain the URL's auth to the user."""
    parts = value.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception:
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    return payload if {"alg", "typ"} & set(header) else None


def analyze_url_auth(url):
    """Returns (signals, jwt_payload): `signals` describes any self-contained
    credentials in the URL's query string (empty list = none); `jwt_payload`
    is the decoded JWT payload when one of the values is a JWT."""
    signals, jwt_payload = [], None
    qs = parse_qs(urlparse(url).query, keep_blank_values=True)
    for name, values in qs.items():
        if name.lower() not in _SIGNED_PARAM_NAMES:
            continue
        for v in values:
            payload = _decode_jwt(v)
            if payload:
                jwt_payload = payload
                signals.append(f"{name}=<JWT>")
                break
        else:
            signals.append(name)
    return signals, jwt_payload


def advise_cookie_necessity(url, page_url=None, cookies_requested=False):
    """Printed once before the first request. Explains when cookies can't be
    the missing piece (the credential is in the URL itself), and checks a
    JWT's exp claim against the local clock — an expired token dooms the run
    no matter what headers or cookies are supplied. True if URL is signed."""
    global _URL_SIGNED
    signals, jwt_payload = analyze_url_auth(url)
    if not signals:
        return False
    _URL_SIGNED = True
    host = urlparse(url).hostname
    detail = ", ".join(signals)
    if cookies_requested:
        who = f"your {urlparse(page_url).hostname} session" if page_url \
            else "the site's session"
        warn(f"This URL is self-authenticating ({detail}): the credential is the "
             f"link itself, and a browser sends none of {who}'s cookies to "
             f"{host}. Cookies are unlikely to be the missing piece here — "
             f"--page-url/Referer matters more. Loading them anyway since you asked.")
    else:
        status(f"Signed URL ({detail}) — auth rides on the link + Referer, "
               f"not cookies", C.GRAY)
    exp = jwt_payload.get("exp") if jwt_payload else None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        try:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))
        except (ValueError, OverflowError, OSError):
            when = None
        if when:
            if exp < time.time():
                warn(f"The URL's token expired {when} (per your system clock) — "
                     f"re-copy a FRESH url from the page; nothing else fixes the "
                     f"401/403 you'll get")
            else:
                status(f"Token valid until {when} (per your system clock)", C.GRAY)
    return True


def _handle_cookie_extract_error(browser, host, err):
    """Extraction failed. Not fatal by design: most HLS sources authenticate
    via a signed URL and/or Referer and need no cookies at all. Record the
    failure so a later 401/403 diagnosis can connect the dots, and name the
    fallback."""
    global _COOKIE_EXTRACT_ERROR
    _COOKIE_EXTRACT_ERROR = err
    warn(f"Cookies not loaded from {browser} — {err}")
    if _URL_SIGNED:
        status("Continuing without cookies — as noted above, this URL's auth is "
               "self-contained; nothing was lost", C.GRAY)
    else:
        status("Continuing without them — most HLS auth is a signed URL and/or "
               "Referer rather than cookies, so this may be fine. If a 401/403 "
               "follows, retry with --cookie and the raw Cookie: header from "
               "DevTools.", C.GRAY)


def _note_no_host_cookies(browser, host, page_url):
    """Extraction worked but the browser holds zero cookies for the media
    host. Usually not an error at all — site sessions don't leak across
    hosts — so say what actually carries the auth instead."""
    src = urlparse(page_url).hostname if page_url else None
    scope = ""
    if src and src != host:
        scope = f" — expected: browsers never send {src} session cookies to {host}"
    warn(f"No {browser} cookies for {host}{scope}. If the video plays on the site "
         f"without a separate media-host login, the credential is the Referer "
         f"or a signed URL token — not cookies.")


def _note_cookies_not_needed():
    """Called after the first manifest fetch succeeded. If cookie extraction
    had failed earlier and no 401/403 has surfaced, that failure provably
    cost nothing — say so once, so the user doesn't go fixing a non-problem."""
    global _COOKIE_UNNEEDED_NOTED
    if (_COOKIE_EXTRACT_ERROR and not _URL_SIGNED
            and not _DIAGNOSIS_SHOWN and not _COOKIE_UNNEEDED_NOTED):
        _COOKIE_UNNEEDED_NOTED = True
        status("The manifest loads fine without cookies — the earlier extraction "
               "failure cost nothing; they weren't needed for this source", C.GRAY)


# --------------------------------------------------------------------------
# Remembered per-host header profiles
# --------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "hls_downloader"
PROFILE_PATH = CONFIG_DIR / "profiles.json"
DATA_DIR = Path.home() / ".local" / "share" / "hls_downloader"
HISTORY_PATH = DATA_DIR / "history.jsonl"


def _load_profiles():
    try:
        return json.loads(PROFILE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_profiles(profiles):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(json.dumps(profiles, indent=2))
    except OSError:
        pass


def get_cached_profile(host):
    if not host:
        return None
    return _load_profiles().get(host)


def remember_profile(host, referer=None, origin=None, user_agent=None, rate=None,
                     page_url=None):
    """Only called with explicitly user-supplied values — never with
    auto-derived guesses — so the cache only ever holds confirmed-working
    headers. Cookies are deliberately NOT remembered here (credentials).
    `rate` (requests/sec) and `page_url` are remembered the same way, so a
    host's polite pace and embedding page only have to be discovered once."""
    if not host or not any([referer, origin, user_agent, rate, page_url]):
        return
    profiles = _load_profiles()
    entry = profiles.get(host, {})
    if referer:
        entry["referer"] = referer
    if origin:
        entry["origin"] = origin
    if user_agent:
        entry["user_agent"] = user_agent
    if rate:
        entry["rate"] = rate
    if page_url:
        entry["page_url"] = page_url
    profiles[host] = entry
    _save_profiles(profiles)


def log_run(entry):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def derive_output_name(url):
    """Pull a friendly filename out of a proxy's own query params (e.g.
    '?embed=campeones-cup/2026-09-17/mia-caz') when present."""
    qs = parse_qs(urlparse(url).query)
    embed = qs.get("embed", [None])[0]
    if embed:
        return embed.replace("/", "-") + ".mp4"
    return "output.mp4"


# --------------------------------------------------------------------------
# HTTP session
# --------------------------------------------------------------------------

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def _parse_cookie_header(raw):
    """'a=1; b=2' → {'a': '1', 'b': '2'}"""
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            name, _, value = part.partition("=")
            out[name.strip()] = value.strip()
    return out


# Candidate profile directories per browser on Linux, in search order:
# the classic location first, then the XDG layout (Firefox 121+ / Fedora,
# ~/.config/mozilla/...), then Snap/Flatpak sandbox overrides. Entries after
# the first carry a ready-made symlink fix for browser-cookie3 builds that
# only search the classic path, plus a short label explaining WHY the profile
# lives where it does — the error message uses it so the diagnosis is a fact
# about this machine, not a guess. Snap Firefox is the odd one out: its data
# is in the revision-independent common/ dir, not current/.config/. Only
# firefox/chromium/opera have official snaps, so the rest list Flatpak only.
# profiles.ini and cache dirs aren't listed: the cookie DB is inside the
# profile dir, and the symlink fixes link the whole parent tree anyway.
_PROFILE_PATHS = {
    "firefox": [
        ("~/.mozilla/firefox", None, "standard location"),
        ("~/.config/mozilla/firefox",
         "ln -s ~/.config/mozilla ~/.mozilla",
         "XDG profile location (Firefox 121+ / Fedora)"),
        ("~/snap/firefox/common/.mozilla/firefox",
         "ln -s ~/snap/firefox/common/.mozilla ~/.mozilla",
         "Snap install"),
        ("~/.var/app/org.mozilla.firefox/.mozilla/firefox",
         "ln -s ~/.var/app/org.mozilla.firefox/.mozilla ~/.mozilla",
         "Flatpak install"),
    ],
    "chrome": [
        ("~/.config/google-chrome", None, "standard location"),
        ("~/.var/app/com.google.Chrome/config/google-chrome",
         "ln -s ~/.var/app/com.google.Chrome/config/google-chrome ~/.config/google-chrome",
         "Flatpak install"),
    ],
    "chromium": [
        ("~/.config/chromium", None, "standard location"),
        ("~/snap/chromium/current/.config/chromium",
         "ln -s ~/snap/chromium/current/.config/chromium ~/.config/chromium",
         "Snap install"),
        ("~/.var/app/org.chromium.Chromium/config/chromium",
         "ln -s ~/.var/app/org.chromium.Chromium/config/chromium ~/.config/chromium",
         "Flatpak install"),
    ],
    "edge": [
        ("~/.config/microsoft-edge", None, "standard location"),
        ("~/.var/app/com.microsoft.Edge/config/microsoft-edge",
         "ln -s ~/.var/app/com.microsoft.Edge/config/microsoft-edge ~/.config/microsoft-edge",
         "Flatpak install"),
    ],
    "brave": [
        ("~/.config/BraveSoftware/Brave-Browser", None, "standard location"),
        ("~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
         "ln -s ~/.var/app/com.brave.Browser/config/BraveSoftware ~/.config/BraveSoftware",
         "Flatpak install"),
    ],
    "vivaldi": [
        ("~/.config/vivaldi", None, "standard location"),
        ("~/.var/app/com.vivaldi.Vivaldi/config/vivaldi",
         "ln -s ~/.var/app/com.vivaldi.Vivaldi/config/vivaldi ~/.config/vivaldi",
         "Flatpak install"),
    ],
    "opera": [
        ("~/.config/opera", None, "standard location"),
        ("~/snap/opera/current/.config/opera",
         "ln -s ~/snap/opera/current/.config/opera ~/.config/opera",
         "Snap install"),
        ("~/.var/app/com.opera.Opera/config/opera",
         "ln -s ~/.var/app/com.opera.Opera/config/opera ~/.config/opera",
         "Flatpak install"),
    ],
    "safari": [
        ("~/Library/Cookies", None, "standard location (macOS: Cookies.binarycookies)"),
    ],
}


def _profile_candidates(browser):
    """[(path, symlink_fix_or_None, label), ...] for one browser, in the
    order locations are most likely to be the active one."""
    return list(_PROFILE_PATHS.get(browser, []))


def _profile_diagnosis(browser):
    """Check the filesystem for a browser's candidate profile directories.
    Returns (found, missing): `found` is [(path, fix, label), ...] for dirs
    that exist; `missing` is [path, ...] for those that don't."""
    found, missing = [], []
    for path, fix, label in _profile_candidates(browser):
        if Path(path).expanduser().is_dir():
            found.append((path, fix, label))
        else:
            missing.append(path)
    return found, missing


def load_browser_cookies(browser, host):
    """Read cookies for `host` straight out of an installed browser's cookie
    store via browser-cookie3, keyed the same way --cookie is: {name: value}.
    Raises CookieExtractError (with fix-it guidance diagnosed against the
    real filesystem) instead of exiting — see _handle_cookie_extract_error
    for why."""
    if not HAS_BROWSER_COOKIE3:
        raise CookieExtractError(
            "the browser-cookie3 package isn't installed — pip install browser-cookie3")
    loader_name = BROWSER_COOKIE_LOADERS.get(browser)
    loader = getattr(browser_cookie3, loader_name, None) if loader_name else None
    if loader is None:
        raise CookieExtractError(f"unsupported browser '{browser}'")
    try:
        jar = loader(domain_name=host) if host else loader()
    except Exception as e:
        msg = f"couldn't read {browser} cookies: {e}"
        low = str(e).lower()
        if ("profile" in low or "cookies file" in low or "cookie file" in low
                or "no such file" in low):
            # Missing-profile class of failure — diagnose against the real
            # filesystem before advising anything.
            found, missing = _profile_diagnosis(browser)
            off_standard = [(p, f, l) for p, f, l in found if f]
            if off_standard:
                # The profile is there, but only where this browser_cookie3
                # build never looks — say exactly where and hand over a fix.
                path, fix, label = off_standard[0]
                others = ""
                if len(off_standard) > 1:
                    others = ("\n  (also present: "
                              + "; ".join(p for p, _f, _l in off_standard[1:]) + ")")
                msg += (f"\n  {browser}'s profile exists as a {label} at {path}, "
                        f"but this browser-cookie3 build only searches the standard "
                        f"location.{others}"
                        f"\n  Fix: 'pip install -U browser-cookie3' (newer releases "
                        f"check more locations), or: {fix}")
            elif not found:
                msg += (f"\n  None of {browser}'s usual profile directories exist "
                        f"on this machine ({'; '.join(missing)}). Is {browser} "
                        f"installed, and has it been run at least once here?")
            else:
                msg += ("\n  Profile directories present: "
                        + "; ".join(p for p, _f, _l in found)
                        + " — check their permissions, or the error above names "
                        f"the actual failure.")
        elif "locked" in low or "readonly" in low or "read-only" in low:
            msg += (f"\n  The cookie database is locked — {browser} is probably "
                    f"running. Close it and retry, or use --cookie with the raw "
                    f"Cookie: header from DevTools.")
        elif any(w in low for w in ("decrypt", "keyring", "secret", "unlock")):
            msg += ("\n  The browser's cookie-encryption key couldn't be retrieved "
                    "(keyring locked, or the browser is open and holds the DB).")
        raise CookieExtractError(msg) from e
    return {c.name: c.value for c in jar}


def _is_local_host(url):
    """True if the URL's host is localhost or a private-network address —
    i.e. almost certainly your own proxy/gateway, not a public CDN."""
    import ipaddress

    host = urlparse(url).hostname
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def derive_headers(m3u8_url, page_url=None, referer=None, origin=None):
    """Fill in Referer/Origin automatically when not given explicitly.

    Priority: explicit flag > page_url's origin > the manifest URL's own
    origin. The page_url case is the strong signal (many sites check for
    the actual embedding page); the manifest-origin fallback is a weaker
    heuristic that only helps when the CDN and player share a domain — and
    is skipped entirely when the manifest URL is your own local proxy,
    since sending headers built from a private address to a proxy that
    forwards them upstream can trip the *upstream's* Referer checks.
    """

    def origin_of(u):
        p = urlparse(u)
        return f"{p.scheme}://{p.netloc}"

    if page_url:
        return referer or page_url, origin or origin_of(page_url)

    if _is_local_host(m3u8_url):
        return referer, origin  # no page_url and a local host: send nothing extra

    fallback = origin_of(m3u8_url)
    return referer or fallback, origin or fallback


def build_session(m3u8_url=None, page_url=None, referer=None, origin=None,
                   user_agent=None, cookie=None, cookies_from_browser=None,
                   proxy=None, retries=3):
    """Scoped session: retries+backoff, auto-derived Referer/Origin (with
    explicit overrides), an optional raw Cookie header, an optional proxy,
    and SSL warnings silenced only for this session rather than globally."""
    session = requests.Session()

    if m3u8_url and (referer is None or origin is None):
        auto_referer, auto_origin = derive_headers(m3u8_url, page_url, referer, origin)
        referer = referer or auto_referer
        origin = origin or auto_origin

    headers = {"User-Agent": user_agent or DEFAULT_UA}
    if referer:
        headers["Referer"] = referer
    if origin:
        headers["Origin"] = origin
    session.headers.update(headers)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        status(f"Proxying through {proxy}", C.GRAY)
    # HTTP_PROXY / HTTPS_PROXY / ALL_PROXY env vars are honored automatically
    # by requests (trust_env=True) — --proxy exists only to override per run.
    if cookies_from_browser:
        host = urlparse(m3u8_url).hostname if m3u8_url else None
        try:
            browser_cookies = load_browser_cookies(cookies_from_browser, host)
        except CookieExtractError as e:
            _handle_cookie_extract_error(cookies_from_browser, host, str(e))
        else:
            if browser_cookies:
                session.cookies.update(browser_cookies)
                status(f"Loaded {len(browser_cookies)} cookie(s) from "
                       f"{cookies_from_browser} for {host}", C.GRAY)
            else:
                _note_no_host_cookies(cookies_from_browser, host, page_url)
    if cookie:
        parsed = _parse_cookie_header(cookie)
        if parsed:
            session.cookies.update(parsed)
            status(f"Applied {len(parsed)} cookie(s): {', '.join(sorted(parsed))}", C.GRAY)
        else:
            warn("--cookie ignored: no 'name=value' pairs found. Copy the FULL "
                 "'cookie:' request-header line from DevTools (Network → the .m3u8 "
                 "request → Headers). Do NOT rebuild it by hand — HttpOnly cookies "
                 "like __ddg2 won't show in document.cookie or the Storage-filtered view.")
    session.verify = False

    retry = Retry(
        total=retries,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=64, pool_connections=64)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


class _PacedResponse:
    """Minimal stand-in for a requests.Response after a paced/streaming read,
    exposing what fetch() callers use: .content/.text/.status_code/.headers."""

    def __init__(self, resp, content):
        self.status_code = resp.status_code
        self.headers = resp.headers
        self.url = resp.url
        self.content = content

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code} for {self.url}")


def _curl_replay(session, url, byte_range=None):
    """Build a shell-quoted curl command reproducing this request's headers
    and cookies (deduplicated by name, last value wins), for isolating
    header problems from client-fingerprint ones."""
    parts = ["curl", "-v", "--insecure"]
    for k, v in session.headers.items():
        parts.append(f"-H {shlex.quote(f'{k}: {v}')}")
    jar = {}
    for n, v in session.cookies.items():
        jar[n] = v
    if jar:
        cookie_line = "; ".join(f"{n}={v}" for n, v in jar.items())
        parts.append(f"-H {shlex.quote(f'Cookie: {cookie_line}')}")
    if byte_range:
        length, offset = byte_range
        parts.append(f"-H {shlex.quote(f'Range: bytes={offset}-{offset + length - 1}')}")
    if session.proxies:
        p = session.proxies.get("https") or session.proxies.get("http")
        if p:
            parts.append(f"--proxy {shlex.quote(p)}")
    parts.append(shlex.quote(url))
    return " ".join(parts)


def _diagnose_auth(r, session, url, body, byte_range=None):
    """One-shot 401/403 diagnostics: names the blocker, prints targeted
    hints, and emits a curl replay of the exact failing request."""
    global _DIAGNOSIS_SHOWN
    if _DIAGNOSIS_SHOWN or r.status_code not in (401, 403):
        return
    _DIAGNOSIS_SHOWN = True
    server = r.headers.get("Server", "?")
    warn(f"HTTP {r.status_code} — Server: {server}")
    if body:
        warn(f"Response body: {body}")
    if _COOKIE_EXTRACT_ERROR and not _URL_SIGNED:
        first = _COOKIE_EXTRACT_ERROR.splitlines()[0]
        warn(f"This run is cookieless (--cookies-from-browser failed earlier: "
             f"{first}). If the source is session-gated, fix that or paste the "
             f"raw header with --cookie")
    if "ddos" in server.lower() or "ddos" in body.lower():
        warn("DDoS-Guard rejected this request. Checklist:")
        warn("  1. Complete __ddg* cookie set — especially __ddg2 (clearance). "
             "Copy the raw cookie: request header verbatim from DevTools.")
        warn("  2. Exact browser User-Agent (copy the user-agent: header, "
             "don't retype it).")
        warn("  3. Terminal public IP must equal the IP in __ddg9_ "
             "(check: curl -s https://api.ipify.org).")
        warn("  4. Fresh token URL — e=/s= pairs may be short-lived or single-use.")
    if "identifiable" in body.lower() or "user agent" in body.lower():
        warn("This site publishes a bot policy: identified tools are welcome, "
             "browser impersonation is blocked. Try an honest UA with contact "
             "info and pacing, e.g.:")
        warn('  --user-agent "my-archiver/1.0 (personal backup; contact: you@example.com)" '
             "--rate 1 -w 3")
        warn("  — and DROP the __ddg* cookies for that attempt (they're bound to "
             "your browser's UA and will mismatch).")
    warn("Reproduce this exact request with:")
    print(f"  {_curl_replay(session, url, byte_range)}", file=sys.stderr)


def _build_range_header(byte_range, resume_offset=0):
    """Combined Range for a playlist byte-range plus a .part resume offset."""
    length, offset = byte_range if byte_range else (None, 0)
    if length is not None:
        return f"bytes={offset + resume_offset}-{offset + length - 1}"
    if resume_offset:
        return f"bytes={resume_offset}-"
    return None


def fetch_stream(session, url, byte_range=None, resume_offset=0, timeout=30):
    """Streaming GET used for segment bodies (chunked disk writes, Range
    resume, byte-rate pacing). Honors _RATE_LIMITER and the 401/403
    diagnostics exactly like fetch(). Returns (response, resume_offset) —
    the offset comes back as 0 if the server ignored a requested Range
    (i.e. a full 200 body is coming instead of a 206)."""
    if _RATE_LIMITER:
        _RATE_LIMITER.wait()
    headers = {}
    rng = _build_range_header(byte_range, resume_offset)
    if rng:
        headers["Range"] = rng
    r = session.get(url, headers=headers, timeout=timeout, stream=True)
    if r.status_code in (401, 403):
        try:
            body = r.raw.read(2048, decode_content=True).decode("utf-8", "replace")
        except Exception:
            body = ""
        _diagnose_auth(r, session, url,
                       re.sub(r"\s+", " ", body).strip()[:600], byte_range)
    r.raise_for_status()
    if resume_offset and r.status_code != 206:
        return r, 0
    return r, resume_offset


def fetch(session, url, byte_range=None, timeout=30):
    global _DIAGNOSIS_SHOWN
    if _RATE_LIMITER:
        _RATE_LIMITER.wait()
    if _BYTE_LIMITER:
        r, _ = fetch_stream(session, url, byte_range=byte_range, timeout=timeout)
        chunks = []
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                chunks.append(chunk)
                _BYTE_LIMITER.consume(len(chunk))
        r.close()
        return _PacedResponse(r, b"".join(chunks))
    headers = {}
    rng = _build_range_header(byte_range)
    if rng:
        headers["Range"] = rng
    r = session.get(url, headers=headers, timeout=timeout)
    if r.status_code in (401, 403):
        body = re.sub(r"\s+", " ", r.text or "").strip()[:600]
        _diagnose_auth(r, session, url, body, byte_range)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# Playlist parsing
# --------------------------------------------------------------------------

class PlaylistInfo(NamedTuple):
    """parse_playlist's result. `.duration` is the summed EXTINF time of the
    media segments (0.0 for master playlists) — used by --estimate and the
    post-merge duration check."""
    variants: list
    segments: list
    is_live: bool
    target_duration: float
    audio_tracks: list
    discontinuity_count: int
    part_count: int
    subtitle_tracks: list
    iframes: list

    @property
    def duration(self):
        return sum(s.duration or 0.0 for s in self.segments)


class Segment:
    __slots__ = ("url", "seq", "key_info", "byte_range", "map_info", "duration",
                 "is_ad", "is_gap")

    def __init__(self, url, seq, key_info, byte_range, map_info, duration, is_ad,
                 is_gap=False):
        self.url = url
        self.seq = seq
        self.key_info = key_info
        self.byte_range = byte_range
        self.map_info = map_info
        self.duration = duration
        self.is_ad = is_ad
        self.is_gap = is_gap


def _attr(line, name, quoted=True):
    pattern = rf'{name}="([^"]*)"' if quoted else rf'{name}=([^,\s]*)'
    m = re.search(pattern, line)
    return m.group(1) if m else None


def _parse_byterange(spec, url, last_end):
    """Parse an EXT-X-BYTERANGE 'n[@o]' spec, tracking implicit offsets
    per source URL when the offset is omitted (per HLS spec: contiguous
    with the previous range on the same resource)."""
    if "@" in spec:
        length_s, offset_s = spec.split("@", 1)
        length, offset = int(length_s), int(offset_s)
    else:
        length = int(spec)
        offset = last_end.get(url, 0)
    last_end[url] = offset + length
    return (length, offset)


def parse_playlist(session, m3u8_url):
    """
    Parse an m3u8 file.
    Returns a PlaylistInfo:
      - variants: list of (url, resolution, bandwidth) if a master playlist
      - segments: list of Segment if a media playlist. Segments tagged
        #EXT-X-GAP have is_gap=True (the origin doesn't actually have that
        segment — HLS players skip it rather than stall on it, and so do we)
      - is_live: True if no #EXT-X-ENDLIST was found (media playlists only)
      - target_duration: float, from #EXT-X-TARGETDURATION
      - audio_tracks / subtitle_tracks: lists of {name, language, url,
        default[, forced]} from #EXT-X-MEDIA:TYPE=AUDIO / TYPE=SUBTITLES
        entries (master playlists only)
      - iframes: list of (url, bandwidth) from #EXT-X-I-FRAME-STREAM-INF —
        preview/thumbnail renditions, listed but not downloadable as media
      - discontinuity_count / part_count: informational counters
      - .duration property: summed EXTINF seconds of the media segments
    """
    text = fetch(session, m3u8_url).text
    _note_cookies_not_needed()   # manifest fetched ⇒ any earlier cookie failure was harmless
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    variants = []
    segments = []
    audio_tracks = []
    subtitle_tracks = []
    iframes = []
    pending_variant = None
    pending_byte_range = None
    pending_duration = None

    current_key = None      # dict describing the active #EXT-X-KEY, or None
    current_map = None      # (map_url, byte_range) from #EXT-X-MAP, or None
    last_byte_range_end = {}
    media_seq = 0
    is_live = True
    target_duration = 6.0
    discontinuity_count = 0
    part_count = 0
    pending_gap = False      # set by a preceding #EXT-X-GAP tag
    cue_state = False       # True while inside a CUE-OUT/CUE-IN ad break

    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r'RESOLUTION=(\d+x\d+)', line)
            bw = re.search(r'BANDWIDTH=(\d+)', line)
            pending_variant = {
                "resolution": res.group(1) if res else "unknown",
                "bandwidth": int(bw.group(1)) if bw else 0,
            }

        elif line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            # Preview/thumbnail renditions — listed for completeness, they're
            # not selectable media playlists.
            uri = re.search(r'URI="([^"]+)"', line)
            bw = re.search(r'BANDWIDTH=(\d+)', line)
            if uri:
                iframes.append((urljoin(m3u8_url, uri.group(1)),
                                int(bw.group(1)) if bw else 0))

        elif line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            m = re.search(r':(\d+)', line)
            if m:
                media_seq = int(m.group(1))

        elif line.startswith("#EXT-X-TARGETDURATION"):
            m = re.search(r':([\d.]+)', line)
            if m:
                target_duration = float(m.group(1))

        elif line.startswith("#EXT-X-ENDLIST"):
            is_live = False

        elif line.startswith("#EXTINF"):
            m = re.search(r':([\d.]+)', line)
            pending_duration = float(m.group(1)) if m else None

        elif line.startswith("#EXT-X-DISCONTINUITY"):
            discontinuity_count += 1

        elif line.startswith("#EXT-X-GAP"):
            # Applies to the very next segment URI: the origin doesn't
            # actually have it, so it must be skipped rather than fetched.
            pending_gap = True

        elif line.startswith("#EXT-X-PART:"):
            part_count += 1

        elif line.startswith("#EXT-X-CUE-OUT"):
            cue_state = True

        elif line.startswith("#EXT-X-CUE-IN"):
            cue_state = False

        elif line.startswith("#EXT-X-MEDIA"):
            mtype = _attr(line, "TYPE", quoted=False)
            uri = _attr(line, "URI")
            if uri and mtype in ("AUDIO", "SUBTITLES"):
                entry = {
                    "name": _attr(line, "NAME"),
                    "language": _attr(line, "LANGUAGE"),
                    "url": urljoin(m3u8_url, uri),
                    "default": (_attr(line, "DEFAULT", quoted=False) or "").upper() == "YES",
                }
                if mtype == "SUBTITLES":
                    entry["forced"] = (_attr(line, "FORCED", quoted=False) or "").upper() == "YES"
                    subtitle_tracks.append(entry)
                else:
                    audio_tracks.append(entry)

        elif line.startswith("#EXT-X-KEY"):
            # Keys can change mid-playlist; each segment carries a snapshot
            # of whatever key was active when it was listed.
            method = re.search(r'METHOD=([A-Za-z0-9-]+)', line)
            uri = re.search(r'URI="([^"]+)"', line)
            iv_match = re.search(r'IV=0[xX]([0-9a-fA-F]+)', line)
            kf = re.search(r'KEYFORMAT="([^"]+)"', line)
            if method and method.group(1) == "NONE":
                current_key = None
            elif method and uri:
                current_key = {
                    "method": method.group(1),
                    "key_url": urljoin(m3u8_url, uri.group(1)),
                    # None here means "use the implicit (sequence-number) IV"
                    "explicit_iv": bytes.fromhex(iv_match.group(1)) if iv_match else None,
                    # Missing KEYFORMAT means 'identity' (raw 16-byte key at
                    # the URI) per the HLS spec — the only kind we can use.
                    "keyformat": kf.group(1) if kf else "identity",
                }
            elif method:
                warn(f"#EXT-X-KEY METHOD={method.group(1)} without a key URI — "
                     f"treating this playlist section as unencrypted")

        elif line.startswith("#EXT-X-MAP"):
            uri = re.search(r'URI="([^"]+)"', line)
            br = re.search(r'BYTERANGE="([^"]+)"', line)
            if uri:
                map_url = urljoin(m3u8_url, uri.group(1))
                map_range = _parse_byterange(br.group(1), map_url, last_byte_range_end) if br else None
                current_map = (map_url, map_range)

        elif line.startswith("#EXT-X-BYTERANGE"):
            m = re.search(r':(.+)', line)
            if m:
                pending_byte_range = m.group(1)

        elif not line.startswith("#"):
            if pending_variant:
                variants.append((urljoin(m3u8_url, line),
                                  pending_variant["resolution"],
                                  pending_variant["bandwidth"]))
                pending_variant = None
            else:
                seg_url = urljoin(m3u8_url, line)
                byte_range = None
                if pending_byte_range:
                    byte_range = _parse_byterange(pending_byte_range, seg_url, last_byte_range_end)
                    pending_byte_range = None
                segments.append(Segment(
                    url=seg_url,
                    seq=media_seq,
                    key_info=current_key,
                    byte_range=byte_range,
                    map_info=current_map,
                    duration=pending_duration,
                    is_ad=cue_state,
                    is_gap=pending_gap,
                ))
                media_seq += 1
                pending_duration = None
                pending_gap = False

    return PlaylistInfo(variants=variants, segments=segments, is_live=is_live,
                        target_duration=target_duration, audio_tracks=audio_tracks,
                        discontinuity_count=discontinuity_count, part_count=part_count,
                        subtitle_tracks=subtitle_tracks, iframes=iframes)


def select_variant(session, m3u8_url, preferred_height=None):
    """Follow a chain of master playlists to reach a media playlist.
    Iterative (not recursive) with cycle detection. Returns
    (media_url, audio_tracks, subtitle_tracks, chosen_bandwidth) — the track
    lists come from the first (master) level, if any. EXT-X-I-FRAME-STREAM-INF
    renditions are displayed but aren't selectable media."""
    seen = set()
    audio_tracks, subtitle_tracks = [], []
    chosen_bw = 0
    first = True
    while True:
        if m3u8_url in seen:
            raise RuntimeError("Circular master playlist reference detected.")
        seen.add(m3u8_url)

        pl = parse_playlist(session, m3u8_url)
        if first:
            audio_tracks = pl.audio_tracks
            subtitle_tracks = pl.subtitle_tracks
            first = False
        if not pl.variants:
            return m3u8_url, audio_tracks, subtitle_tracks, chosen_bw

        status(f"Master playlist — {len(pl.variants)} quality levels found:")
        for i, (url, res, bw) in enumerate(pl.variants):
            print(f"  {C.GRAY}[{i}]{C.RESET} {res}  ({bw // 1000} kbps)", file=sys.stderr)
        for _url, bw in pl.iframes:
            print(f"  {C.GRAY}(i) I-frame preview, {bw // 1000} kbps — not selectable{C.RESET}",
                  file=sys.stderr)
        if audio_tracks:
            print(f"  {C.GRAY}audio: "
                  + ", ".join(t.get("name") or t.get("language") or "?"
                              for t in audio_tracks)
                  + f" — pick with --audio 0..{len(audio_tracks) - 1}{C.RESET}",
                  file=sys.stderr)
        if subtitle_tracks:
            print(f"  {C.GRAY}subs: "
                  + ", ".join(t.get("name") or t.get("language") or "?"
                              for t in subtitle_tracks)
                  + f" — add with --subs 0..{len(subtitle_tracks) - 1}{C.RESET}",
                  file=sys.stderr)

        if preferred_height:
            best = None
            for url, res, bw in pl.variants:
                try:
                    h = int(res.split("x")[1])
                except (ValueError, IndexError):
                    continue
                if h <= preferred_height and (best is None or h > best[1]):
                    best = (url, h, bw)
            if best:
                chosen, chosen_bw = best[0], best[2]
                status(f"Auto-selected: {best[1]}p", C.GREEN)
            else:
                chosen, chosen_bw = pl.variants[0][0], pl.variants[0][2]
                status(f"No rendition ≤ {preferred_height}p — using highest available", C.GREEN)
        else:
            choice = input(f"{C.BOLD}Select quality [default 0]: {C.RESET}").strip()
            idx = int(choice) if choice.isdigit() and int(choice) < len(pl.variants) else 0
            chosen = pl.variants[idx][0]
            chosen_bw = pl.variants[idx][2]
            status(f"Selected: {pl.variants[idx][1]}", C.GREEN)

        m3u8_url = chosen


def probe_playlist(session, url, as_json=False, estimate=False):
    """Dry-run: report variants, audio/subtitle tracks, I-frame renditions,
    encryption, live/VOD status, and marker counts without downloading.
    --estimate adds a size estimate (first variant's BANDWIDTH × summed
    EXTINF duration); --json prints the report as JSON on stdout."""
    report = {"url": url}
    pl = parse_playlist(session, url)

    est_bw = 0
    if pl.variants:
        report["type"] = "master"
        report["variants"] = [{"index": i, "resolution": res, "bandwidth": bw}
                              for i, (_u, res, bw) in enumerate(pl.variants)]
        if pl.iframes:
            report["iframe_renditions"] = [{"bandwidth": bw, "url": u}
                                           for u, bw in pl.iframes]
        est_bw = pl.variants[0][2]
        if not as_json:
            status(f"Master playlist — {len(pl.variants)} video quality level(s):")
            for i, (u, res, bw) in enumerate(pl.variants):
                print(f"  {C.GRAY}[{i}]{C.RESET} {res}  ({bw // 1000} kbps)", file=sys.stderr)
            for _u, bw in pl.iframes:
                print(f"  {C.GRAY}(i) I-frame preview, {bw // 1000} kbps — preview/thumbnail "
                      f"rendition, not downloadable as media{C.RESET}", file=sys.stderr)
        # Descend one level into the first variant for media-level info.
        pl = parse_playlist(session, pl.variants[0][0])
    else:
        report["type"] = "media"

    methods = sorted({s.key_info["method"] for s in pl.segments if s.key_info})
    drm = any(s.key_info and s.key_info.get("keyformat", "identity") != "identity"
              for s in pl.segments)
    ad_count = sum(1 for s in pl.segments if s.is_ad)
    gap_count = sum(1 for s in pl.segments if s.is_gap)

    report["live"] = pl.is_live
    report["segments"] = len(pl.segments)
    report["duration_sec"] = round(pl.duration, 3)
    report["target_duration_sec"] = pl.target_duration
    report["encryption_methods"] = methods
    report["drm_protected"] = drm
    report["audio_tracks"] = [{"name": t.get("name"), "language": t.get("language"),
                               "default": t.get("default")} for t in pl.audio_tracks]
    report["subtitle_tracks"] = [{"name": t.get("name"), "language": t.get("language"),
                                  "default": t.get("default")} for t in pl.subtitle_tracks]
    report["ad_segments"] = ad_count
    report["gap_segments"] = gap_count
    report["discontinuities"] = pl.discontinuity_count
    report["partial_segment_tags"] = pl.part_count

    estimated = None
    if estimate and est_bw and pl.duration:
        estimated = int(pl.duration * est_bw / 8)
        report["estimated_bytes"] = estimated
        report["estimate_basis"] = ("first listed variant's BANDWIDTH × summed EXTINF "
                                    "duration; separate audio/subtitle tracks extra")

    if as_json:
        print(json.dumps(report, indent=2))
        return

    status(f"{'LIVE' if pl.is_live else 'VOD'} playlist — {len(pl.segments)} segment(s), "
           f"~{pl.target_duration:.1f}s target duration", C.GREEN)
    if methods:
        if drm:
            warn(f"Encryption: {'+'.join(methods)} with a DRM key format — not decryptable "
                 f"by this tool (clearkey HLS only)")
        elif "SAMPLE-AES" in methods or "SAMPLE-AES-CTR" in methods:
            status(f"Encryption: {'+'.join(methods)} — per-sample CENC/CBCS on fMP4, "
                   f"decrypted in-process", C.GREEN)
        else:
            status(f"Encryption: {'+'.join(methods)}")
    else:
        status("Encryption: none detected")
    if pl.audio_tracks:
        status(f"{len(pl.audio_tracks)} audio track(s) — add with --audio N:")
        for i, t in enumerate(pl.audio_tracks):
            tag = " (default)" if t.get("default") else ""
            print(f"  {C.GRAY}[{i}]{C.RESET} {t.get('name') or '?'} "
                  f"[{t.get('language') or '?'}]{tag}", file=sys.stderr)
    if pl.subtitle_tracks:
        status(f"{len(pl.subtitle_tracks)} subtitle track(s) — add with --subs N:")
        for i, t in enumerate(pl.subtitle_tracks):
            tag = " (default)" if t.get("default") else ""
            tag += " (forced)" if t.get("forced") else ""
            print(f"  {C.GRAY}[{i}]{C.RESET} {t.get('name') or '?'} "
                  f"[{t.get('language') or '?'}]{tag}", file=sys.stderr)
    if ad_count:
        status(f"{ad_count} segment(s) inside CUE-OUT/CUE-IN ad breaks — use --skip-ads to drop them", C.YELLOW)
    if gap_count:
        status(f"{gap_count} segment(s) marked #EXT-X-GAP — origin doesn't have them, "
               f"skipped automatically", C.YELLOW)
    if pl.discontinuity_count:
        status(f"{pl.discontinuity_count} discontinuity marker(s) found (not necessarily ads)", C.GRAY)
    if pl.part_count:
        status(f"{pl.part_count} #EXT-X-PART partial-segment tag(s) — low-latency HLS source; "
               f"parts aren't fetched separately, only their finalized full segments", C.GRAY)
    if estimated:
        status(f"Estimated size: ~{fmt_size(estimated)} (bandwidth × {fmt_eta(pl.duration)}; "
               f"audio/subtitles extra)", C.GREEN)


# --------------------------------------------------------------------------
# Decryption — AES-128 whole-segment, and CENC/CBCS per-sample (SAMPLE-AES)
# --------------------------------------------------------------------------

_key_cache = {}
_key_cache_lock = Lock()


def get_key_bytes(session, key_url):
    with _key_cache_lock:
        cached = _key_cache.get(key_url)
    if cached is not None:
        return cached
    data = fetch(session, key_url).content
    with _key_cache_lock:
        _key_cache[key_url] = data
    return data


def resolve_iv(key_info, seq):
    """Explicit IV from the playlist if given; otherwise the HLS-spec
    fallback of the segment's media sequence number, big-endian, 16 bytes."""
    if key_info.get("explicit_iv") is not None:
        return key_info["explicit_iv"]
    return seq.to_bytes(16, "big")


# ---- CENC / CBCS box parsing and per-sample decryption for fMP4 (CMAF) ----

def _iter_boxes(data, start, end):
    """Yield (box_type_bytes, body_start, body_end) for each child box in
    data[start:end]."""
    off = start
    while off + 8 <= end:
        size = struct.unpack_from(">I", data, off)[0]
        typ = data[off + 4:off + 8]
        hdr = 8
        if size == 1:
            if off + 16 > end:
                break
            size = struct.unpack_from(">Q", data, off + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            break
        yield typ, off + hdr, off + size
        off += size


def _find_boxes(data, start, end, *types):
    return [b for b in _iter_boxes(data, start, end) if b[0] in types]


def _flags(data, body_start):
    """24-bit flags of a fullbox whose body starts at body_start."""
    return int.from_bytes(data[body_start + 1:body_start + 4], "big")


def _parse_tenc(data, s, _e):
    """tenc body → {iv_size, kid, constant_iv, crypt, skip}. v0 has a
    reserved byte where v1 carries the cbcs pattern as two 4-bit
    (crypt, skip) block counts; both then carry IsEncrypted + IV_Size."""
    ver = data[s]
    off = s + 4
    crypt = skip = None
    if ver != 0:
        crypt = data[off] >> 4          # default_crypt_byte_block
        skip = data[off] & 0x0F         # default_skip_byte_block
    off += 1        # reserved (v0) or default crypt/skip pattern (v1)
    off += 1        # IsEncrypted (always 1 inside an encrypted track)
    iv_size = data[off]; off += 1
    kid = bytes(data[off:off + 16]); off += 16
    constant_iv = None
    if iv_size == 0 and off < len(data):    # cbcs constant-IV case
        cisz = data[off]; off += 1
        constant_iv = bytes(data[off:off + cisz])
    return {"iv_size": iv_size, "kid": kid, "constant_iv": constant_iv,
            "crypt": crypt, "skip": skip}


def _parse_init_tenc(init_data):
    """Parse each track's encryption defaults from an fMP4 init segment:
    moov→trak→(tkhd track id, mdia→minf→stbl→stsd→entry→sinf→(schm scheme,
    schi→tenc)). Returns {track_id: {...tenc, scheme}}. Cached per init URL
    because fragments decrypt individually but the moov only ever appears
    in the init."""
    out = {}
    for _moov, ms, me in _find_boxes(init_data, 0, len(init_data), b"moov"):
        for _trak, ts, te in _find_boxes(init_data, ms, me, b"trak"):
            track_id, scheme, tenc = None, None, None
            for _tkhd, ks, _ke in _find_boxes(init_data, ts, te, b"tkhd"):
                off = ks + 4 + (8 if init_data[ks] == 0 else 16)
                track_id = struct.unpack_from(">I", init_data, off)[0]
            for _mdia, ds, de in _find_boxes(init_data, ts, te, b"mdia"):
                for _minf, fs, fe in _find_boxes(init_data, ds, de, b"minf"):
                    for _stbl, gs, ge in _find_boxes(init_data, fs, fe, b"stbl"):
                        for _stsd, hs, he in _find_boxes(init_data, gs, ge, b"stsd"):
                            # stsd body: version/flags + entry_count, then sample
                            # entry boxes; each entry's own body starts with
                            # 6 reserved + 2 data-ref bytes before its children.
                            for _entry, es, ee in _iter_boxes(init_data, hs + 8, he):
                                for _sinf, s2, e2 in _find_boxes(init_data, es + 8, ee, b"sinf"):
                                    for typ, cs, ce in _iter_boxes(init_data, s2, e2):
                                        if typ == b"schm":
                                            scheme = init_data[cs + 4:cs + 8].decode("latin-1", "replace")
                                        elif typ == b"schi":
                                            for t2, c2, e3 in _iter_boxes(init_data, cs, ce):
                                                if t2 == b"tenc":
                                                    tenc = _parse_tenc(init_data, c2, e3)
            if track_id is not None and tenc is not None:
                out[track_id] = {"scheme": scheme, **tenc}
    return out


def _ctr_xor_into(out, start, length, ecb, counter):
    """XOR out[start:start+length] with AES-CTR keystream whose 128-bit
    big-endian counter starts at `counter`. Returns the counter positioned
    after the last block (CENC increments once per block, including a final
    partial one)."""
    off = 0
    while off < length:
        ks = ecb.encrypt((counter & ((1 << 128) - 1)).to_bytes(16, "big"))
        n = min(16, length - off)
        seg = int.from_bytes(bytes(out[start + off:start + off + n]), "big")
        out[start + off:start + off + n] = (seg ^ int.from_bytes(ks[:n], "big")).to_bytes(n, "big")
        off += n
        counter = (counter + 1) & ((1 << 128) - 1)
    return counter


def _cbc_pattern(out, start, length, ecb, iv, crypt_n, skip_n):
    """cbcs: repeating pattern of crypt_n encrypted 16-byte blocks and skip_n
    clear blocks from the sample start. The CBC chain runs through the whole
    sample — clear blocks still chain (their raw bytes act as ciphertext)."""
    period = max(1, crypt_n + skip_n)
    chain = bytes(iv)
    for b in range(length // 16):
        pos = start + b * 16
        block = bytes(out[pos:pos + 16])
        if b % period < crypt_n:
            dec = int.from_bytes(ecb.decrypt(block), "big") ^ int.from_bytes(chain, "big")
            out[pos:pos + 16] = dec.to_bytes(16, "big")
        chain = block          # chaining always uses raw (ciphertext) bytes
    # trailing <16 bytes stay clear, per CENC


def _cbc_full(out, start, length, ecb, iv):
    """Full-block AES-CBC over a sample (cbc1, or cbcs with a 0:0 pattern)."""
    chain = bytes(iv)
    for pos in range(start, start + (length // 16) * 16, 16):
        block = bytes(out[pos:pos + 16])
        dec = int.from_bytes(ecb.decrypt(block), "big") ^ int.from_bytes(chain, "big")
        out[pos:pos + 16] = dec.to_bytes(16, "big")
        chain = block


def _decrypt_traf(out, data, traf_s, traf_e, moof_start, key_bytes, tenc_by_track):
    global _SGBP_WARNED
    track_id, base_offset, default_base_is_moof = None, None, False
    default_size, senc_box, has_sbgp = None, None, False
    truns = []      # (data_offset_or_None, [sample_sizes])

    for typ, s, e in _iter_boxes(data, traf_s, traf_e):
        if typ == b"tfhd":
            flags = _flags(data, s)
            off = s + 4
            track_id = struct.unpack_from(">I", data, off)[0]; off += 4
            if flags & 0x000001:
                base_offset = struct.unpack_from(">Q", data, off)[0]; off += 8
            if flags & 0x000002:
                off += 4                    # sample description index
            if flags & 0x000008:
                off += 4                    # default sample duration
            if flags & 0x000010:
                default_size = struct.unpack_from(">I", data, off)[0]; off += 4
            if flags & 0x000020:
                off += 4                    # default sample flags
            default_base_is_moof = bool(flags & 0x020000)
        elif typ == b"trun":
            flags = _flags(data, s)
            count = struct.unpack_from(">I", data, s + 4)[0]
            off = s + 8
            data_offset = None
            if flags & 0x000001:
                data_offset = struct.unpack_from(">i", data, off)[0]; off += 4
            if flags & 0x000004:
                off += 4                    # first-sample-flags
            sizes = []
            for _ in range(count):
                if flags & 0x000100:
                    off += 4
                if flags & 0x000200:
                    sizes.append(struct.unpack_from(">I", data, off)[0]); off += 4
                if flags & 0x000400:
                    off += 4
                if flags & 0x000800:
                    off += 4
            if not (flags & 0x000200) and default_size is not None:
                sizes = [default_size] * count
            truns.append((data_offset, sizes))
        elif typ == b"senc":
            senc_box = (s, e)
        elif typ == b"sbgp":
            has_sbgp = True
        # tfdt/sdtp/sgpd carry nothing we need

    t = tenc_by_track.get(track_id) if track_id else None
    if not t:
        # No tenc == a genuinely clear track inside an encrypted CMAF set —
        # leave it alone (warn once in case it's a parse miss instead).
        if not _SGBP_WARNED:
            _SGBP_WARNED = True
            warn(f"traf track {track_id} has no tenc in the init segment — left clear")
        return
    if has_sbgp and not _SGBP_WARNED:
        _SGBP_WARNED = True
        warn("fMP4 fragment carries sample-group (sbgp) crypt overrides — "
             "decrypting with tenc defaults; verify the output plays")
    if t["iv_size"] == 0 and not t["constant_iv"]:
        raise RuntimeError("SAMPLE-AES: in-band per-sample IVs (saiz/saio) aren't supported")

    scheme = t["scheme"]
    crypt_n, skip_n = t["crypt"] or 0, t["skip"] or 0
    if scheme not in ("cenc", "cbcs", "cbc1"):
        if scheme:
            raise RuntimeError(f"unsupported CENC scheme '{scheme}' (supported: cenc, cbcs, cbc1)")
        scheme = "cbcs" if (crypt_n or skip_n) else "cenc"
    if scheme == "cbc1":
        crypt_n, skip_n = 1, 0
    ecb = AES.new(key_bytes, AES.MODE_ECB)

    if base_offset is None:
        base_offset = moof_start if default_base_is_moof else 0

    # Per-sample IVs from senc, else the tenc constant IV
    ivs, subs = None, None
    if senc_box is not None:
        ss, _se = senc_box
        if t["iv_size"] == 0:
            raise RuntimeError("senc present but tenc declares no per-sample IV size")
        count = struct.unpack_from(">I", data, ss + 4)[0]
        off = ss + 8
        has_subs = bool(_flags(data, ss) & 0x000002)
        ivs, subs = [], []
        for _ in range(count):
            ivs.append(bytes(data[off:off + t["iv_size"]])); off += t["iv_size"]
            if has_subs:
                n = struct.unpack_from(">H", data, off)[0]; off += 2
                entries = []
                for _i in range(n):
                    clear, enc = struct.unpack_from(">HI", data, off)
                    off += 6
                    entries.append((clear, enc))
                subs.append(entries)
            else:
                subs.append(None)

    total = sum(len(sz) for _, sz in truns)
    if ivs is not None and len(ivs) != total:
        raise RuntimeError(f"senc has {len(ivs)} IV(s) for {total} sample(s) — "
                           f"fragment layout not understood")

    cursor = base_offset
    sample_i = 0
    for data_offset, sizes in truns:
        if data_offset is not None:
            cursor = base_offset + data_offset
        for size in sizes:
            iv = ivs[sample_i] if ivs is not None else t["constant_iv"]
            if not iv:
                raise RuntimeError("no IV available for an encrypted sample "
                                   "(no senc entry, no constant IV)")
            start = cursor
            if scheme == "cenc":
                ranges = [(0, size)]
                if ivs is not None and subs[sample_i]:
                    ranges = subs[sample_i]
                counter = int.from_bytes(iv, "big")
                pos = start
                for clear_len, enc_len in ranges:
                    pos += clear_len
                    counter = _ctr_xor_into(out, pos, enc_len, ecb, counter)
                    pos += enc_len
            elif crypt_n == 0 and skip_n == 0:
                _cbc_full(out, start, size, ecb, iv)
            else:
                _cbc_pattern(out, start, size, ecb, iv, crypt_n, skip_n)
            cursor += size
            sample_i += 1


def decrypt_cenc_fragment(data, key_bytes, tenc_by_track):
    """Decrypt one fMP4 fragment (one or more moof+mdat pairs) per CENC:
    'cenc' = AES-CTR, 'cbcs' = AES-CBC with a crypt/skip byte pattern.
    Handles tenc defaults (including constant IV) and per-sample senc IVs.
    Anything it can't handle raises — ciphertext is never silently passed
    through as garbage. Limitations: FairPlay-style 32-byte clear prefixes
    (DRM-only anyway), saiz/saio in-band IVs, and cens are not supported."""
    if len(key_bytes) != 16:
        raise RuntimeError(f"SAMPLE-AES key must be 16 bytes, got {len(key_bytes)}")
    out = bytearray(data)
    decrypted = 0
    for moof_typ, moof_body, moof_end in _iter_boxes(data, 0, len(data)):
        if moof_typ != b"moof":
            continue
        moof_start = moof_body - 8
        for traf_typ, ts, te in _iter_boxes(data, moof_body, moof_end):
            if traf_typ == b"traf":
                _decrypt_traf(out, data, ts, te, moof_start, key_bytes, tenc_by_track)
                decrypted += 1
    if not decrypted:
        raise RuntimeError("SAMPLE-AES: no moof/traf found — not an fMP4 fragment")
    return bytes(out)


def decrypt_segment(data, key_info, seq, session, seg=None):
    if not key_info:
        return data
    if not HAS_CRYPTO:
        raise RuntimeError("AES decryption needed. Run: pip install pycryptodome")
    method = key_info.get("method")
    if method == "AES-128":
        key_bytes = get_key_bytes(session, key_info["key_url"])
        iv = resolve_iv(key_info, seq)
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
        plaintext = cipher.decrypt(data)          # decrypt FIRST
        pad_len = plaintext[-1]                   # then read padding from plaintext
        return plaintext[:-pad_len] if 1 <= pad_len <= 16 else plaintext
    if method in ("SAMPLE-AES", "SAMPLE-AES-CTR"):
        if key_info.get("keyformat", "identity") != "identity":
            raise RuntimeError(
                f"{method} with KEYFORMAT '{key_info['keyformat']}' is DRM "
                f"(FairPlay/Widevine/PlayReady) — this tool decrypts clearkey HLS only")
        if seg is None or seg.map_info is None:
            raise RuntimeError(
                f"{method} on a non-fMP4 (TS) segment isn't supported — "
                f"per-sample TS decryption isn't implemented; fMP4/CMAF is")
        tenc = _TENC_CACHE.get(seg.map_info[0])
        if not tenc:
            raise RuntimeError("no parsed init segment (tenc) available for "
                               "SAMPLE-AES decryption")
        return decrypt_cenc_fragment(data, get_key_bytes(session, key_info["key_url"]), tenc)
    raise RuntimeError(f"unsupported encryption method: {method}")


# --------------------------------------------------------------------------
# Segment download
# --------------------------------------------------------------------------

def download_segment(session, seg, tmp_dir, ext, prefix="seg"):
    """Download (and decrypt) one segment.

    Unencrypted segments stream straight to '<name>.part' and interrupted
    downloads resume from their existing bytes via an HTTP Range request
    (worthwhile for long-GOP fMP4 segments). Encrypted segments need the
    whole ciphertext in memory for the cipher, so they restart from zero.
    The '.done' marker is written only after a full, verified write, so a
    killed run always retries rather than trusting a partial file."""
    out_path = os.path.join(tmp_dir, f"{prefix}_{seg.seq:08d}{ext}")
    marker = out_path + ".done"
    if os.path.exists(marker) and os.path.exists(out_path):
        return out_path, os.path.getsize(out_path)

    encrypted = seg.key_info is not None
    part_path = out_path + ".part"
    resume = 0
    if os.path.exists(part_path):
        if encrypted:
            os.remove(part_path)      # cipher needs the whole segment anyway
        else:
            resume = os.path.getsize(part_path)

    try:
        r, resume = fetch_stream(session, seg.url, byte_range=seg.byte_range,
                                 resume_offset=resume)
    except requests.exceptions.HTTPError as e:
        if resume and "416" in str(e):
            os.remove(part_path)      # stale beyond EOF — start over
            r, resume = fetch_stream(session, seg.url, byte_range=seg.byte_range,
                                     resume_offset=0)
        else:
            raise

    written = resume
    with open(part_path, "wb" if not resume else "ab") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            if _BYTE_LIMITER:
                _BYTE_LIMITER.consume(len(chunk))
            written += len(chunk)
    r.close()

    if seg.byte_range and written != seg.byte_range[0]:
        os.remove(part_path)
        raise RuntimeError(
            f"expected {seg.byte_range[0]} bytes for this segment, got {written} "
            f"(server may be ignoring the Range header)")

    if encrypted:
        with open(part_path, "rb") as f:
            data = f.read()
        data = decrypt_segment(data, seg.key_info, seg.seq, session, seg)
        with open(out_path, "wb") as f:
            f.write(data)
        os.remove(part_path)
        size = len(data)
    else:
        os.replace(part_path, out_path)
        size = written
    open(marker, "w").close()
    return out_path, size


# --------------------------------------------------------------------------
# aria2c backend (optional, for very large segment counts)
# --------------------------------------------------------------------------

def _aria2c_header_lines(session):
    lines = []
    ua = session.headers.get("User-Agent")
    if ua:
        lines.append(f"  header=User-Agent: {ua}")
    ref = session.headers.get("Referer")
    if ref:
        lines.append(f"  header=Referer: {ref}")
    org = session.headers.get("Origin")
    if org:
        lines.append(f"  header=Origin: {org}")
    if session.cookies:
        cookie_line = "; ".join(f"{n}={v}" for n, v in session.cookies.items())
        lines.append(f"  header=Cookie: {cookie_line}")
    return lines


def download_batch_aria2c(session, candidates, tmp_dir, ext, prefix, workers):
    """Fetch a batch of segments with an external aria2c process instead of
    the built-in per-thread requests downloader — much faster once a
    playlist runs into the thousands of segments. Segments are always
    fetched raw here (aria2c doesn't know about HLS decryption) and then
    decrypted in-process afterward, same as the requests backend. Returns
    (succeeded, failed) where succeeded is a list of (Segment, byte_size)
    and failed a list of (Segment, error_str) — or None if aria2c itself
    couldn't be run at all, signaling the caller to fall back entirely."""
    input_lines = []
    header_lines = _aria2c_header_lines(session)
    entries = {}
    for s in candidates:
        out_name = f"{prefix}_{s.seq:08d}{ext}.part"
        entries[out_name] = s
        input_lines.append(s.url)
        input_lines.append(f"  out={out_name}")
        input_lines.append(f"  dir={tmp_dir}")
        input_lines.extend(header_lines)
        if s.byte_range:
            length, offset = s.byte_range
            input_lines.append(f"  header=Range: bytes={offset}-{offset + length - 1}")

    input_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("\n".join(input_lines) + "\n")
            input_path = f.name

        cmd = ["aria2c", "-i", input_path,
               "-j", str(max(1, workers)), "-x", "4", "-s", "1",
               "--auto-file-renaming=false", "--allow-overwrite=true",
               "--continue=true", "--summary-interval=0",
               "--console-log-level=warn", "--check-certificate=false"]
        if _RATE_LIMITER and _RATE_LIMITER.min_interval > 0:
            # aria2c has no cross-request pacing knob; the closest honest
            # approximation of "one request at a time" is one connection.
            cmd[cmd.index("-j") + 1] = "1"
            cmd[cmd.index("-x") + 1] = "1"
        if _BYTE_LIMITER:
            cmd.append(f"--max-overall-download-limit={_BYTE_LIMITER.bps}")
        _proxy = (session.proxies.get("https") or session.proxies.get("http")) \
            if session.proxies else None
        if _proxy:
            cmd.append(f"--all-proxy={_proxy}")
        try:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return None
    finally:
        if input_path:
            try:
                os.remove(input_path)
            except OSError:
                pass

    succeeded, failed = [], []
    for out_name, s in entries.items():
        raw_path = os.path.join(tmp_dir, out_name)
        final_path = os.path.join(tmp_dir, f"{prefix}_{s.seq:08d}{ext}")
        if os.path.exists(raw_path) and os.path.getsize(raw_path) > 0:
            try:
                with open(raw_path, "rb") as f:
                    data = decrypt_segment(f.read(), s.key_info, s.seq, session, s)
                with open(final_path, "wb") as f:
                    f.write(data)
                open(final_path + ".done", "w").close()
                os.remove(raw_path)
                succeeded.append((s, len(data)))
            except Exception as e:
                failed.append((s, str(e)))
        else:
            failed.append((s, "aria2c produced no output for this segment"))

    if result.returncode != 0 and not succeeded:
        return None
    return succeeded, failed


# --------------------------------------------------------------------------
# One track's worth of download (video OR a selected audio track)
# --------------------------------------------------------------------------

class TrackResult(NamedTuple):
    raw_path: str
    ext: str
    bytes: int
    elapsed_sec: float
    expected_duration: float   # summed EXTINF of successfully downloaded segments
    failed_segments: int


def run_download(session, media_url, tmp_dir, workers, live_poll, skip_ads, prefix,
                 aria2c=False, no_aria2c=False, parallel=False, label=None):
    """Download every segment of one media playlist (polling if live), merge
    them, and return a TrackResult. `parallel`/`label` switch the progress
    readout to throttled full lines so video and audio can download
    concurrently without fighting over one \\r line. Ctrl-C is caught here:
    the pool is cancelled without a blocking join and the partial download
    is finalized (marking the run interrupted so batch mode stops)."""
    global _INTERRUPTED
    seen_seqs = set()
    skipped_seqs = set()
    state = {"ext": ".ts", "init_path": None}
    stats = {"bytes": 0, "start": time.time()}
    expected_dur = 0.0
    permanent_failures = []
    aria2c_available = (not no_aria2c) and shutil.which("aria2c") is not None
    if aria2c and not aria2c_available and not no_aria2c:
        warn("--aria2c given but aria2c isn't on PATH — using the built-in downloader instead.")

    def note_done(seg, size):
        nonlocal expected_dur
        seen_seqs.add(seg.seq)
        stats["bytes"] += size
        expected_dur += seg.duration or 0.0

    def process_batch(segments):
        candidates = [s for s in segments if s.seq not in seen_seqs and s.seq not in skipped_seqs]

        gaps = [s for s in candidates if s.is_gap]
        if gaps:
            for s in gaps:
                skipped_seqs.add(s.seq)
            candidates = [s for s in candidates if not s.is_gap]
            warn(f"Skipping {len(gaps)} {prefix} segment(s) marked #EXT-X-GAP "
                 f"(not present at the origin)")

        if skip_ads:
            ads = [s for s in candidates if s.is_ad]
            for s in ads:
                skipped_seqs.add(s.seq)
            candidates = [s for s in candidates if not s.is_ad]
        if not candidates:
            return

        if state["init_path"] is None:
            mapped = next((s.map_info for s in candidates if s.map_info), None)
            if mapped:
                state["ext"] = ".mp4"
                map_url, map_range = mapped
                spin = Spinner(f"Fetching {prefix} init segment (fMP4)...") \
                    if not parallel else nullcontext()
                with spin:
                    r = fetch(session, map_url, byte_range=map_range)
                state["init_path"] = os.path.join(tmp_dir, f"{prefix}_init{state['ext']}")
                with open(state["init_path"], "wb") as f:
                    f.write(r.content)
                # fMP4 fragments decrypt individually, but their tenc defaults
                # (IV size, constant IV, cbcs pattern, scheme) live only in the
                # init's moov — parse and remember them once.
                _TENC_CACHE.setdefault(map_url, _parse_init_tenc(r.content))
                if not _TENC_CACHE[map_url]:
                    warn(f"{prefix} init segment declares no encrypted tracks — "
                         f"SAMPLE-AES decryption won't be possible")

        ext = state["ext"]
        total = len(candidates)

        use_aria2c = aria2c_available and (aria2c or total >= ARIA2C_AUTO_THRESHOLD)
        if use_aria2c:
            status(f"{total} {prefix} segment(s) to download — using aria2c backend", C.GRAY)
            spin = Spinner(f"aria2c downloading {prefix} segments...") \
                if not parallel else nullcontext()
            with spin:
                result = download_batch_aria2c(session, candidates, tmp_dir, ext, prefix, workers)
            if result is None:
                warn("aria2c backend failed entirely — falling back to the built-in downloader.")
                use_aria2c = False
            else:
                succeeded, failed = result
                for s, size in succeeded:
                    note_done(s, size)
                success(f"{len(succeeded)}/{total} {prefix} segment(s) fetched via aria2c")
                if failed:
                    warn(f"{len(failed)} {prefix} segment(s) failed via aria2c, "
                         f"retrying with the built-in downloader...")
                    candidates = [s for s, _err in failed]
                else:
                    candidates = []

        if candidates:
            total = len(candidates)
            status(f"{total} {prefix} segment(s) to download (workers: {workers})")

            done = 0
            lock = Lock()
            failed = []
            last_shown = 0.0

            pool = ThreadPoolExecutor(max_workers=workers)
            futures = {pool.submit(download_segment, session, s, tmp_dir, ext, prefix): s
                       for s in candidates}
            try:
                for future in as_completed(futures):
                    s = futures[future]
                    try:
                        _, size = future.result()
                        with lock:
                            note_done(s, size)
                    except Exception as e:
                        failed.append((s, str(e)))
                    if not QUIET:
                        with lock:
                            done += 1
                            now = time.time()
                            if parallel and done < total and now - last_shown < 1.0:
                                continue
                            last_shown = now
                            pct = done * 100 // total
                            bar = "#" * (pct // 4) + "-" * (25 - pct // 4)
                            elapsed = time.time() - stats["start"]
                            speed_mb = stats["bytes"] / elapsed / (1024 * 1024) if elapsed > 0 else 0.0
                            avg = elapsed / done if done else 0
                            eta = fmt_eta(avg * (total - done)) if avg else "?"
                            if parallel:
                                print(f"  {C.GRAY}[{label}]{C.RESET} [{bar}] {pct}% "
                                      f"({done}/{total})  {speed_mb:.2f} MB/s  ETA {eta}   ",
                                      file=sys.stderr, flush=True)
                            else:
                                print(f"\r  {C.BLUE}[{bar}]{C.RESET} {pct}% ({done}/{total})"
                                      f"  {speed_mb:.2f} MB/s  ETA {eta}",
                                      end="", file=sys.stderr, flush=True)
            except KeyboardInterrupt:
                # A blocking join here is exactly what turns a second Ctrl-C
                # into a broken pool and an un-joined worker at exit. Cancel
                # everything not yet started and leave; in-flight segments
                # finish (or die) in the background — their .done markers are
                # honored by the next run, and the merge below only copies
                # seqs it already recorded.
                for f in futures:
                    f.cancel()
                pool.shutdown(wait=False)
                raise
            else:
                pool.shutdown(wait=True)
            if not QUIET:
                print(file=sys.stderr)

            if failed:
                warn(f"{len(failed)} {prefix} segment(s) failed, retrying sequentially...")
                for s, err in failed:
                    try:
                        _, size = download_segment(session, s, tmp_dir, ext, prefix)
                        note_done(s, size)
                    except Exception as e:
                        error(f"Segment {s.seq} failed permanently: {e}")
                        permanent_failures.append(s.seq)

    try:
        pl = parse_playlist(session, media_url)
        if not pl.segments:
            raise RuntimeError(f"No segments found in {prefix} playlist.")
        process_batch(pl.segments)
        is_live = pl.is_live
        if is_live and live_poll:
            status(f"Live {prefix} playlist — polling for new segments (Ctrl-C to stop and finalize)", C.YELLOW)
            while is_live:
                time.sleep(max(pl.target_duration / 2, 1.0))
                try:
                    pl = parse_playlist(session, media_url)
                except Exception as e:
                    warn(f"Playlist refresh failed, retrying: {e}")
                    continue
                process_batch(pl.segments)
                is_live = pl.is_live
    except KeyboardInterrupt:
        _INTERRUPTED = True
        warn("Interrupted — finalizing what's downloaded so far.")

    status(f"Merging {prefix} segments...")
    ext = state["ext"]
    raw_path = os.path.join(tmp_dir, f"{prefix}_merged{ext}")
    with open(raw_path, "wb") as out:
        if state["init_path"] and os.path.exists(state["init_path"]):
            with open(state["init_path"], "rb") as f:
                shutil.copyfileobj(f, out)
        for seq in sorted(seen_seqs):
            seg_path = os.path.join(tmp_dir, f"{prefix}_{seq:08d}{ext}")
            if os.path.exists(seg_path):
                with open(seg_path, "rb") as f:
                    shutil.copyfileobj(f, out)

    return TrackResult(raw_path, ext, stats["bytes"], time.time() - stats["start"],
                       expected_dur, len(permanent_failures))


# --------------------------------------------------------------------------
# Subtitles — WebVTT-in-HLS download and assembly
# --------------------------------------------------------------------------

def _clean_vtt(text, keep_header=False):
    """Strip HLS-specific WebVTT lines: X-TIMESTAMP-MAP (MPEGTS alignment
    metadata, meaningless once re-muxed — VOD LOCAL timestamps are already
    wall-clock correct) and any per-segment repeated 'WEBVTT' headers."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("X-TIMESTAMP-MAP"):
            continue
        if not keep_header and s.startswith("WEBVTT"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def run_subtitle_download(session, sub_url, tmp_dir, workers=4):
    """Fetch a WebVTT-in-HLS subtitle playlist and assemble a plain .vtt:
    the EXT-X-MAP header block (styles, if any) followed by every segment's
    cues in sequence. AES-128 subtitle segments are decrypted like any other.
    Returns the .vtt path."""
    pl = parse_playlist(session, sub_url)
    if not pl.segments:
        raise RuntimeError("subtitle playlist has no segments")
    parts = []
    mapped = next((s.map_info for s in pl.segments if s.map_info), None)
    if mapped:
        r = fetch(session, mapped[0], byte_range=mapped[1])
        header = _clean_vtt(r.content.decode("utf-8", "replace"), keep_header=True)
        if header:
            parts.append(header)
    for seg in pl.segments:
        if seg.is_gap:
            continue
        r = fetch(session, seg.url, byte_range=seg.byte_range)
        data = decrypt_segment(r.content, seg.key_info, seg.seq, session, seg)
        body = _clean_vtt(data.decode("utf-8", "replace"))
        if body:
            parts.append(body)
    text = "\n\n".join(parts)
    if not text.startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
    out_path = os.path.join(tmp_dir, "subs.vtt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return out_path


# --------------------------------------------------------------------------
# Interrupted-merge recovery — rebuild an output file from an existing
# `<output>_parts/` directory without refetching anything.
# --------------------------------------------------------------------------

_SEG_DONE_RE = re.compile(r"^(video|audio)_(\d{8})(\.ts|\.mp4)\.done$")


def merge_dir_to_file(tmp_dir, prefix):
    """Rebuild the merged raw stream for `prefix` ('video' or 'audio')
    purely from what's already on disk in tmp_dir — every segment that has
    a completed '.done' marker, in sequence order, plus the init segment if
    one was captured. This is exactly what run_download's own merge step
    does at the end of a normal run, factored out so --resume-merge can
    call it directly on a `_parts/` directory left over from an interrupted
    or crashed run, with no network access at all. Returns (path, ext) or
    (None, None) if nothing usable is present for this prefix."""
    if not os.path.isdir(tmp_dir):
        return None, None

    init_path = os.path.join(tmp_dir, f"{prefix}_init.mp4")
    has_init = os.path.exists(init_path)
    ext = ".mp4" if has_init else None

    seqs = {}
    for name in os.listdir(tmp_dir):
        m = _SEG_DONE_RE.match(name)
        if not m or m.group(1) != prefix:
            continue
        seq, found_ext = int(m.group(2)), m.group(3)
        seg_path = os.path.join(tmp_dir, f"{prefix}_{seq:08d}{found_ext}")
        if os.path.exists(seg_path):
            seqs[seq] = found_ext
            ext = ext or found_ext

    if not seqs and not has_init:
        return None, None
    ext = ext or ".ts"

    merged_path = os.path.join(tmp_dir, f"{prefix}_merged{ext}")
    with open(merged_path, "wb") as out:
        if has_init:
            with open(init_path, "rb") as f:
                shutil.copyfileobj(f, out)
        for seq in sorted(seqs):
            seg_path = os.path.join(tmp_dir, f"{prefix}_{seq:08d}{seqs[seq]}")
            with open(seg_path, "rb") as f:
                shutil.copyfileobj(f, out)
    return merged_path, ext


# --------------------------------------------------------------------------
# Per-output lock — one writer per <output>_parts/ directory
# --------------------------------------------------------------------------

def _read_pid(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) would *terminate* the process on Windows — probe
        # via tasklist instead. If we can't tell, assume alive (fail safe).
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class OutputLock:
    """Advisory lock (<output>.lock) so two concurrent runs can't share one
    <output>_parts/ directory and corrupt each other's .done markers. A lock
    whose owning process is no longer running is stale and taken over."""

    def __init__(self, output):
        self.path = output + ".lock"
        self._fd = None

    def __enter__(self):
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                pid = _read_pid(self.path)
                if pid and _pid_alive(pid):
                    sys.exit(f"Another run (pid {pid}) is already using "
                             f"{self.path[:-len('.lock')]} — pick another -o, or "
                             f"remove {self.path} if that process is gone.")
                warn(f"Removing stale lock {self.path} (pid {pid} isn't running)")
                try:
                    os.remove(self.path)
                except OSError:
                    pass

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
            try:
                os.remove(self.path)
            except OSError:
                pass


def resume_merge(output):
    """--resume-merge entry point: no network activity at all. Rebuilds
    video (and audio/subs, if present) purely from '<output>_parts/', muxes
    with ffmpeg, and finalizes to `output` — for when a run finished (or
    nearly finished) downloading but the merge/mux step itself was
    interrupted or crashed."""
    tmp_dir = output + "_parts"
    if not os.path.isdir(tmp_dir):
        sys.exit(f"No {tmp_dir} directory found — nothing to recover.")

    with OutputLock(output):
        status(f"Rebuilding from {tmp_dir} — no network access, using what's on disk...")
        video_path, _vext = merge_dir_to_file(tmp_dir, "video")
        if video_path is None:
            sys.exit(f"No completed video segments found in {tmp_dir}.")
        audio_path, _aext = merge_dir_to_file(tmp_dir, "audio")
        subs_path = os.path.join(tmp_dir, "subs.vtt")
        if not os.path.exists(subs_path):
            subs_path = None

        if shutil.which("ffmpeg"):
            status("Muxing recovered track(s) (ffmpeg)...")
            cmd = ["ffmpeg", "-y", "-i", video_path]
            if audio_path:
                cmd += ["-i", audio_path]
            if subs_path:
                cmd += ["-i", subs_path]
            cmd += ["-map", "0:v:0"]
            if audio_path:
                cmd += ["-map", "1:a:0"]
            else:
                # Keep audio muxed inside the video track (see download_hls).
                cmd += ["-map", "0:a?"]
            if subs_path:
                cmd += ["-map", f"{2 if audio_path else 1}:s:0"]
            cmd += ["-c", "copy"]
            if subs_path:
                cmd += ["-c:s", "mov_text"]   # after -c copy — last match wins
            cmd += [output]
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode != 0 or not os.path.exists(output) or os.path.getsize(output) == 0:
                warn("Mux failed — falling back to video-only output.")
                _finalize_video_only(video_path, output)
        else:
            if audio_path or subs_path:
                warn("ffmpeg not found — can't mux audio/subtitles; keeping video-only output.")
            _finalize_video_only(video_path, output)

        for p in (video_path, audio_path, subs_path):
            if p and os.path.exists(p) and os.path.abspath(p) != os.path.abspath(output):
                os.remove(p)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    size_mb = os.path.getsize(output) / (1024 * 1024)
    success(f"Recovered: {output} ({size_mb:.1f} MB)")


def _finalize_video_only(raw_path, output):
    """Move the raw concatenated stream to `output`, remuxing with ffmpeg
    into a clean container when available."""
    if os.path.abspath(raw_path) != os.path.abspath(output):
        shutil.move(raw_path, output)
    if shutil.which("ffmpeg"):
        status("Remuxing to MP4 with ffmpeg...")
        tmp_out = output + ".tmp.mp4"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", output, "-c", "copy", tmp_out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            os.replace(tmp_out, output)
        else:
            warn("Remux failed — keeping raw concatenated stream.")
            if os.path.exists(tmp_out):
                os.remove(tmp_out)


# --------------------------------------------------------------------------
# Post-merge integrity checks
# --------------------------------------------------------------------------

def verify_duration(output, expected_sec, tolerance=0.10, min_abs=2.0):
    """Post-merge sanity check: ffprobe the final file and compare its
    duration against the playlist's EXTINF sum — catches silent partial-
    download corruption that still muxes cleanly. Skips silently when
    ffprobe is unavailable; a broken check never fails a download."""
    if not expected_sec or not shutil.which("ffprobe") or not os.path.exists(output):
        return
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", output],
            capture_output=True, text=True, timeout=30)
        actual = float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return
    if abs(actual - expected_sec) > max(min_abs, expected_sec * tolerance):
        warn(f"Output duration {fmt_eta(actual)} differs from the playlist's "
             f"expected {fmt_eta(expected_sec)} — segments may be missing or "
             f"the source changed under us")


def verify_streams(output):
    """Post-merge stream check: warn when the output has no audio track —
    the signature of audio living in a separate rendition we didn't fetch
    (or a source that's genuinely video-only). Never fails a download."""
    if not shutil.which("ffprobe") or not os.path.exists(output):
        return
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "json", output],
            capture_output=True, text=True, timeout=30)
        types = {s.get("codec_type") for s in json.loads(r.stdout).get("streams", [])}
    except Exception:
        return
    if "audio" not in types:
        warn("Output has NO audio stream. If this source serves audio as a "
             "separate rendition, run --probe and retry with --audio N; "
             "video-only masters are also possible")


# --------------------------------------------------------------------------
# Doctor / self-check
# --------------------------------------------------------------------------

def run_doctor():
    """--doctor: report every dependency the tool can silently work around,
    plus config paths — turning 'failed mid-run' surprises into a one-command
    check. Exits non-zero only if something *required* is missing."""
    status("Doctor — runtime & optional components", C.CYAN)
    ok = True

    def check(name, present, detail, impact, required=False):
        nonlocal ok
        if required and not present:
            ok = False
        mark = f"{C.GREEN}✓{C.RESET}" if present else f"{C.RED}✗{C.RESET}"
        print(f" {mark} {C.BOLD}{name:<22}{C.RESET} {detail}\n   {C.GRAY}↳ {impact}{C.RESET}",
              file=sys.stderr)

    check("python", True, sys.version.split()[0], "required", required=True)
    check("requests", True, getattr(requests, "__version__", "present"),
          "required", required=True)
    check("pycryptodome", HAS_CRYPTO,
          getattr(_Crypto, "__version__", "present") if HAS_CRYPTO
          else "missing — pip install pycryptodome",
          "AES-128 and SAMPLE-AES (CENC/CBCS) decryption")
    ff = shutil.which("ffmpeg")
    check("ffmpeg", bool(ff), ff or "not on PATH",
          "remux to clean MP4; muxing alternate audio and subtitle tracks")
    check("ffprobe", bool(shutil.which("ffprobe")),
          shutil.which("ffprobe") or "not on PATH",
          "post-merge duration/integrity check (ships with ffmpeg)")
    check("aria2c", bool(shutil.which("aria2c")),
          shutil.which("aria2c") or "not on PATH",
          "optional fast backend for very large playlists (--aria2c)")
    check("browser-cookie3", HAS_BROWSER_COOKIE3,
          "present" if HAS_BROWSER_COOKIE3 else "missing — pip install browser-cookie3",
          "--cookies-from-browser")
    if HAS_BROWSER_COOKIE3 and sys.platform.startswith("linux"):
        usable = [b for b in sorted(_PROFILE_PATHS) if _profile_diagnosis(b)[0]]
        if usable:
            status(f"Browser profiles found for: {', '.join(usable)} "
                   f"(usable with --cookies-from-browser)", C.GRAY)
        else:
            status("No browser profile directories found — --cookies-from-browser "
                   "has nothing to read on this machine", C.GRAY)
    for label, path in (("profiles store", PROFILE_PATH), ("history store", HISTORY_PATH)):
        check(label, True, f"{path}" + ("" if path.exists() else " (not created yet)"),
              "info")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        v = os.environ.get(var) or os.environ.get(var.lower())
        if v:
            status(f"{var} is set — requests will proxy through it", C.GRAY)
    if ok:
        success("Everything required for a basic download is present.")
    else:
        error("Required components are missing — downloads will fail.")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def download_hls(m3u8_url, output="output.mp4", workers=8, preferred_height=None,
                 referer=None, origin=None, user_agent=None, page_url=None,
                 cookie=None, cookies_from_browser=None, rate=None,
                 limit_rate=None, proxy=None,
                 live_poll=True, skip_ads=False, audio_index=None, subs_index=None,
                 quiet=False, probe=False, estimate=False, as_json=False,
                 aria2c=False, no_aria2c=False):
    """Orchestrate one download end-to-end. Returns a result dict."""
    global QUIET, _RATE_LIMITER, _BYTE_LIMITER
    QUIET = quiet

    host = urlparse(m3u8_url).hostname
    # Only saves headers/rate/page-url that were explicitly given — never
    # auto-derived guesses — so the cache only ever holds confirmed values.
    remember_profile(host, referer, origin, user_agent, rate, page_url)

    cached = get_cached_profile(host) or {}
    if not any((referer, origin, user_agent)) and any(
            cached.get(k) for k in ("referer", "origin", "user_agent")):
        referer = cached.get("referer")
        origin = cached.get("origin")
        user_agent = cached.get("user_agent")
        status(f"Using remembered header profile for {host}", C.GRAY)
    if page_url is None and referer is None and origin is None and cached.get("page_url"):
        page_url = cached["page_url"]
        status(f"Using remembered page URL for {host}: {page_url}", C.GRAY)
    if rate is None and cached.get("rate"):
        rate = cached["rate"]
        status(f"Using remembered rate limit for {host}: {rate:g} req/s", C.GRAY)

    _RATE_LIMITER = RateLimiter(rate) if rate else None
    if _RATE_LIMITER:
        status(f"Rate limit: {rate:g} request(s)/second across all workers", C.GRAY)
    _BYTE_LIMITER = ByteRateLimiter(limit_rate) if limit_rate else None
    if _BYTE_LIMITER:
        status(f"Bandwidth limit: {fmt_size(limit_rate)}/s", C.GRAY)

    # Explain this URL's auth model before the first request: signed tokens
    # mean cookies can't be the missing piece, and an expired JWT dooms the
    # run no matter what headers are sent.
    advise_cookie_necessity(m3u8_url, page_url, bool(cookie or cookies_from_browser))

    session = build_session(m3u8_url=m3u8_url, page_url=page_url, referer=referer,
                            origin=origin, user_agent=user_agent, cookie=cookie,
                            cookies_from_browser=cookies_from_browser, proxy=proxy)
    if not referer and not page_url and not cached:
        auto_ref = session.headers.get("Referer")
        status(f"Auto Referer/Origin: {auto_ref}" if auto_ref else
               "Local/proxy URL detected — sending no extra Referer/Origin", C.GRAY)

    if probe:
        probe_playlist(session, m3u8_url, as_json=as_json, estimate=estimate)
        return None

    media_url, audio_tracks, subtitle_tracks, _bw = \
        select_variant(session, m3u8_url, preferred_height)

    tmp_dir = output + "_parts"
    os.makedirs(tmp_dir, exist_ok=True)

    t0 = time.time()
    with OutputLock(output):
        audio_track = None
        if audio_index is not None:
            if not audio_tracks:
                warn("No alternate audio tracks found in the master playlist; ignoring --audio")
            elif not (0 <= audio_index < len(audio_tracks)):
                warn(f"--audio {audio_index} is out of range (0-{len(audio_tracks) - 1}); ignoring")
            else:
                audio_track = audio_tracks[audio_index]
        elif audio_tracks:
            # Player behavior: the DEFAULT=YES rendition is what a viewer
            # hears, so record it unless --audio said otherwise.
            auto = next((i for i, t in enumerate(audio_tracks) if t.get("default")), 0)
            audio_track = audio_tracks[auto]
            status(f"No --audio given — auto-selecting audio track [{auto}] "
                   f"{audio_track.get('name') or audio_track.get('language') or '?'} "
                   f"(--probe lists all)", C.GRAY)

        # Video and audio download concurrently — audio no longer waits for
        # video; both share the same session and rate limiters.
        if audio_track:
            status(f"Downloading video and audio in parallel "
                   f"({audio_track.get('name') or audio_track.get('language') or 'track'})...")
            pool = ThreadPoolExecutor(max_workers=2)
            fv = pool.submit(run_download, session, media_url, tmp_dir, workers,
                             live_poll, skip_ads, "video", aria2c, no_aria2c,
                             True, "video")
            fa = pool.submit(run_download, session, audio_track["url"], tmp_dir,
                             workers, live_poll, skip_ads, "audio", aria2c,
                             no_aria2c, True, "audio")
            try:
                video = fv.result()
                try:
                    audio = fa.result()
                except Exception as e:
                    warn(f"Audio track failed: {e}")
                    audio = None
            except KeyboardInterrupt:
                # Signals only reach the main thread, so the run_download
                # workers can't finalize themselves — abandon the pool and
                # let the top-level handler exit hard; _parts/ stays resumable.
                pool.shutdown(wait=False)
                raise
            pool.shutdown(wait=True)
        else:
            video = run_download(session, media_url, tmp_dir, workers, live_poll,
                                 skip_ads, "video", aria2c=aria2c, no_aria2c=no_aria2c)
            audio = None

        subs_path = None
        if subs_index is not None:
            if not subtitle_tracks:
                warn("No subtitle tracks found in the master playlist; ignoring --subs")
            elif not (0 <= subs_index < len(subtitle_tracks)):
                warn(f"--subs {subs_index} is out of range (0-{len(subtitle_tracks) - 1}); ignoring")
            else:
                track = subtitle_tracks[subs_index]
                status(f"Downloading subtitle track: "
                       f"{track.get('name') or track.get('language') or '?'}")
                try:
                    subs_path = run_subtitle_download(session, track["url"], tmp_dir)
                except Exception as e:
                    warn(f"Subtitle download failed: {e}")

        failed_total = video.failed_segments + (audio.failed_segments if audio else 0)
        if failed_total:
            warn(f"{failed_total} segment(s) failed permanently — output is INCOMPLETE; "
                 f"re-running the same command resumes the missing ones")

        if shutil.which("ffmpeg"):
            status("Muxing final output (ffmpeg)...")
            cmd = ["ffmpeg", "-y", "-i", video.raw_path]
            if audio:
                cmd += ["-i", audio.raw_path]
            if subs_path:
                cmd += ["-i", subs_path]
            cmd += ["-map", "0:v:0"]
            if audio:
                cmd += ["-map", "1:a:0"]
            else:
                # No separate audio input — keep any audio stream muxed
                # inside the video segments (single-variant live restreams
                # usually carry A/V together). '?' = don't fail if absent.
                cmd += ["-map", "0:a?"]
            if subs_path:
                cmd += ["-map", f"{2 if audio else 1}:s:0"]
            cmd += ["-c", "copy"]
            if subs_path:
                cmd += ["-c:s", "mov_text"]   # after -c copy — last match wins
            cmd += [output]
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode != 0 or not os.path.exists(output) or os.path.getsize(output) == 0:
                warn("Mux failed — falling back to video-only output.")
                _finalize_video_only(video.raw_path, output)
        else:
            if audio or subs_path:
                warn("ffmpeg not found — can't mux audio/subtitles; keeping video-only raw stream.")
            _finalize_video_only(video.raw_path, output)

        if os.path.exists(output) and os.path.getsize(output) > 0:
            for p in (video.raw_path, audio.raw_path if audio else None, subs_path):
                if p and os.path.exists(p) and os.path.abspath(p) != os.path.abspath(output):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            error(f"Merge produced no output — {tmp_dir} kept for --resume-merge")

    expected = video.expected_duration
    verify_duration(output, expected)
    verify_streams(output)

    elapsed = time.time() - t0
    size = os.path.getsize(output) if os.path.exists(output) else 0
    log_run({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "url": m3u8_url,
             "output": output, "success": size > 0,
             "duration_sec": round(elapsed, 1), "size_bytes": size})
    if as_json:
        print(json.dumps({
            "output": output, "size_bytes": size, "elapsed_sec": round(elapsed, 1),
            "expected_duration_sec": round(expected, 1) if expected else None,
            "audio_muxed": bool(audio), "subtitles_muxed": bool(subs_path),
            "failed_segments": failed_total,
        }, indent=2))
    elif size:
        success(f"{output} — {fmt_size(size)} in {fmt_eta(elapsed)}")
    return {"output": output, "size_bytes": size}


def run_batch(path, args):
    """--batch FILE (or '-' for stdin): one URL per line, '#' comments and
    blank lines ignored. Sequential; each URL gets its own history entry and
    its own lock; one failure never stops the rest. -o is ignored (each URL
    is auto-named). A Ctrl-C inside an item finalizes that item's partial
    output, then stops the batch (exit 130)."""
    if path == "-":
        lines = sys.stdin.read().splitlines()
    else:
        with open(path) as f:
            lines = f.read().splitlines()
    urls = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    if not urls:
        sys.exit("Batch list is empty.")
    ok, failed = 0, []
    for i, url in enumerate(urls, 1):
        status(f"[{i}/{len(urls)}] {url}", C.BLUE)
        try:
            download_hls(
                url, output=derive_output_name(url),
                workers=args.workers, preferred_height=args.quality,
                referer=args.referer, origin=args.origin, user_agent=args.user_agent,
                page_url=args.page_url, cookie=args.cookie,
                cookies_from_browser=args.cookies_from_browser,
                rate=args.rate, limit_rate=args.limit_rate, proxy=args.proxy,
                live_poll=not args.no_live_poll, skip_ads=args.skip_ads,
                audio_index=args.audio, subs_index=args.subs,
                quiet=args.quiet, as_json=args.json,
                aria2c=args.aria2c, no_aria2c=args.no_aria2c,
            )
            ok += 1
        except SystemExit as e:
            if e.code:
                error(str(e))
            failed.append(url)
        except Exception as e:
            error(f"{url} — {e}")
            failed.append(url)
        if _INTERRUPTED:
            warn(f"Ctrl-C — stopping the batch here ({ok} ok, {len(failed)} failed)")
            return 130
    (success if not failed else warn)(f"Batch done: {ok} ok, {len(failed)} failed")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(
        prog="hls_downloader.py",
        description="Download, decrypt, and remux HLS (m3u8) streams — VOD or live.")
    ap.add_argument("url", nargs="?", help="m3u8 URL (omit for --doctor / --resume-merge / --batch)")
    ap.add_argument("-o", "--output", help="output filename (ignored with --batch)")
    ap.add_argument("-q", "--quality", type=int, metavar="H", help="preferred max height, e.g. 720")
    ap.add_argument("-w", "--workers", type=int, default=8, help="parallel segment downloads (default 8)")
    ap.add_argument("--rate", type=float, metavar="RPS",
                    help="max HTTP requests/sec across all workers")
    ap.add_argument("--limit-rate", type=_parse_size, metavar="N[K|M|G]",
                    help="max bandwidth in bytes/sec (e.g. 2M) — token bucket, honored by aria2c too")
    ap.add_argument("--proxy", metavar="URL",
                    help="HTTP(S) proxy; HTTP_PROXY/HTTPS_PROXY env vars are always honored")
    ap.add_argument("--page-url", help="embedding page URL; derives Referer/Origin (remembered per host)")
    ap.add_argument("--referer")
    ap.add_argument("--origin")
    ap.add_argument("--user-agent")
    ap.add_argument("--cookie", help="raw Cookie: header copied verbatim from DevTools")
    ap.add_argument("--cookies-from-browser", choices=sorted(BROWSER_COOKIE_LOADERS))
    ap.add_argument("--audio", type=int, metavar="N", help="alternate audio track index (see --probe)")
    ap.add_argument("--subs", type=int, metavar="N", help="subtitle track index — muxed in as mov_text")
    ap.add_argument("--skip-ads", action="store_true")
    ap.add_argument("--no-live-poll", action="store_true")
    ap.add_argument("--aria2c", action="store_true")
    ap.add_argument("--no-aria2c", action="store_true")
    ap.add_argument("--resume-merge", action="store_true",
                    help="rebuild -o's output from <output>_parts/, no network")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--estimate", action="store_true",
                    help="(with --probe) estimated size from variant bandwidth × duration")
    ap.add_argument("--json", action="store_true",
                    help="JSON output for --probe and the final result (on stdout)")
    ap.add_argument("--batch", metavar="FILE",
                    help="download one URL per line from FILE ('-' = stdin)")
    ap.add_argument("--doctor", action="store_true",
                    help="self-check: ffmpeg, aria2c, pycryptodome, cookie lib, config paths")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        if args.doctor:
            sys.exit(run_doctor())
        if args.resume_merge:
            if not args.output:
                sys.exit("--resume-merge requires -o")
            resume_merge(args.output)
            return
        if args.batch:
            code = run_batch(args.batch, args)
            if _INTERRUPTED:
                _hard_exit(130)   # skip the shutdown join; see _hard_exit
            sys.exit(code)
        if not args.url:
            ap.error("an m3u8 URL is required (or --doctor / --batch / --resume-merge)")
        if args.estimate and not args.probe:
            warn("--estimate only applies with --probe")

        download_hls(
            args.url,
            output=args.output or derive_output_name(args.url),
            workers=args.workers, preferred_height=args.quality,
            referer=args.referer, origin=args.origin, user_agent=args.user_agent,
            page_url=args.page_url, cookie=args.cookie,
            cookies_from_browser=args.cookies_from_browser,
            rate=args.rate, limit_rate=args.limit_rate, proxy=args.proxy,
            live_poll=not args.no_live_poll, skip_ads=args.skip_ads,
            audio_index=args.audio, subs_index=args.subs,
            quiet=args.quiet, probe=args.probe, estimate=args.estimate,
            as_json=args.json, aria2c=args.aria2c, no_aria2c=args.no_aria2c,
        )
    except KeyboardInterrupt:
        # Ctrl-C anywhere the per-download handler isn't active: the quality
        # prompt, the mux step, between batch items. Exit hard rather than
        # letting interpreter shutdown block joining worker threads (and
        # printing shutdown noise if the user hits Ctrl-C again).
        print(file=sys.stderr)   # ^C usually leaves the cursor mid-line
        error("Interrupted")
        _hard_exit(130)
    except (RuntimeError, requests.RequestException) as e:
        error(str(e))
        sys.exit(1)

    if _INTERRUPTED:
        # Ctrl-C was caught during segment download and the partial file was
        # already finalized and muxed. Workers may still be draining in the
        # background; interpreter shutdown would block joining them (up to a
        # socket timeout) — and that join is precisely where the third
        # Ctrl-C printed 'Exception ignored on threading shutdown'. Every
        # artifact that matters is flushed and resumable, so just leave.
        _hard_exit(130)


if __name__ == "__main__":
    main()
