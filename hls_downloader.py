#!/usr/bin/env python3
"""
HLS (m3u8) Downloader
Features: master playlist quality selection, alternate audio track selection,
AES-128 decryption (explicit and implicit IVs, per-segment key rotation),
byte-range segments, fMP4/CMAF init-segment handling, ad-break skipping
(CUE-OUT/CUE-IN), live playlist polling, concurrent downloads with resume,
retries with backoff, optional global rate limiting, remembered per-host header
profiles, cookie support, 401/403 diagnostics with curl-replay output, a run
history log, a probe/dry-run mode, and Tokyo Night-themed progress display.

Usage:
    python hls_downloader.py <m3u8_url> [-o output.mp4] [-q 720] [-w 8]
                              [--rate 1] [--referer URL] [--origin URL]
                              [--user-agent UA] [--page-url URL]
                              [--cookie "name=value; ..."] [--audio N]
                              [--skip-ads] [--no-live-poll] [--probe] [--quiet]
"""

import argparse
import json
import os
import re
import shlex
import sys
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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
    BLUE = "\033{38;2;122;162;247m"
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


# --------------------------------------------------------------------------
# Global rate limiter — honored by every fetch() from any worker thread
# --------------------------------------------------------------------------

_RATE_LIMITER = None


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


def remember_profile(host, referer=None, origin=None, user_agent=None, rate=None):
    """Only called with explicitly user-supplied values — never with
    auto-derived guesses — so the cache only ever holds confirmed-working
    headers. Cookies are deliberately NOT remembered here (credentials).
    `rate` (requests/sec) is remembered the same way, alongside headers, so
    a host's polite pace only has to be discovered once."""
    if not host or not any([referer, origin, user_agent, rate]):
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


def load_browser_cookies(browser, host):
    """Read cookies for `host` straight out of an installed browser's cookie
    store via browser-cookie3, keyed the same way --cookie is: {name: value}.
    Explicit --cookie values (applied after this in build_session) win on
    any name collision."""
    if not HAS_BROWSER_COOKIE3:
        sys.exit("--cookies-from-browser needs the browser-cookie3 package. "
                  "Run: pip install browser-cookie3")
    loader_name = BROWSER_COOKIE_LOADERS.get(browser)
    loader = getattr(browser_cookie3, loader_name, None) if loader_name else None
    if loader is None:
        sys.exit(f"--cookies-from-browser: unsupported browser '{browser}'")
    try:
        jar = loader(domain_name=host) if host else loader()
    except Exception as e:
        sys.exit(f"Couldn't read {browser} cookies (is {browser} closed? "
                  f"some browsers lock their cookie DB while running): {e}")
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
                   user_agent=None, cookie=None, cookies_from_browser=None, retries=3):
    """Scoped session: retries+backoff, auto-derived Referer/Origin (with
    explicit overrides), an optional raw Cookie header, and SSL warnings
    silenced only for this session rather than globally."""
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
    if cookies_from_browser:
        host = urlparse(m3u8_url).hostname if m3u8_url else None
        browser_cookies = load_browser_cookies(cookies_from_browser, host)
        if browser_cookies:
            session.cookies.update(browser_cookies)
            status(f"Loaded {len(browser_cookies)} cookie(s) from {cookies_from_browser} "
                   f"for {host}", C.GRAY)
        else:
            warn(f"No cookies found in {cookies_from_browser} for {host}")
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


_DIAGNOSIS_SHOWN = False


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
    parts.append(shlex.quote(url))
    return " ".join(parts)


def fetch(session, url, byte_range=None, timeout=30):
    global _DIAGNOSIS_SHOWN
    if _RATE_LIMITER:
        _RATE_LIMITER.wait()
    headers = {}
    if byte_range:
        length, offset = byte_range
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    r = session.get(url, headers=headers, timeout=timeout)
    if r.status_code in (401, 403) and not _DIAGNOSIS_SHOWN:
        _DIAGNOSIS_SHOWN = True
        server = r.headers.get("Server", "?")
        body = re.sub(r"\s+", " ", r.text or "")[:600].strip()
        warn(f"HTTP {r.status_code} — Server: {server}")
        if body:
            warn(f"Response body: {body}")
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
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# Playlist parsing
# --------------------------------------------------------------------------

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
    Returns (variants, segments, is_live, target_duration, audio_tracks,
             discontinuity_count, part_count):
      - variants: list of (url, resolution, bandwidth) if a master playlist
      - segments: list of Segment if a media playlist. Segments tagged
        #EXT-X-GAP have is_gap=True (the origin doesn't actually have that
        segment — HLS players skip it rather than stall on it, and so do we)
      - is_live: True if no #EXT-X-ENDLIST was found (media playlists only)
      - target_duration: float, from #EXT-X-TARGETDURATION
      - audio_tracks: list of {name, language, url, default} from
        #EXT-X-MEDIA:TYPE=AUDIO entries (master playlists only)
      - discontinuity_count: number of #EXT-X-DISCONTINUITY tags seen
        (informational — not all discontinuities are ad breaks)
      - part_count: number of #EXT-X-PART (low-latency HLS partial segment)
        tags seen. We don't download these separately — by the time a
        playlist is re-fetched the parts are normally already folded into a
        full #EXTINF segment — this is reported so --probe can flag
        low-latency sources where that assumption is worth double-checking.
    """
    text = fetch(session, m3u8_url).text
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    variants = []
    segments = []
    audio_tracks = []
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

        elif line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
            uri = _attr(line, "URI")
            if uri:
                audio_tracks.append({
                    "name": _attr(line, "NAME"),
                    "language": _attr(line, "LANGUAGE"),
                    "url": urljoin(m3u8_url, uri),
                    "default": (_attr(line, "DEFAULT", quoted=False) or "").upper() == "YES",
                })

        elif line.startswith("#EXT-X-KEY"):
            # Keys can change mid-playlist; each segment carries a snapshot
            # of whatever key was active when it was listed.
            method = re.search(r'METHOD=([A-Za-z0-9-]+)', line)
            uri = re.search(r'URI="([^"]+)"', line)
            iv_match = re.search(r'IV=0[xX]([0-9a-fA-F]+)', line)
            if method and method.group(1) == "NONE":
                current_key = None
            elif method and uri:
                current_key = {
                    "method": method.group(1),
                    "key_url": urljoin(m3u8_url, uri.group(1)),
                    # None here means "use the implicit (sequence-number) IV"
                    "explicit_iv": bytes.fromhex(iv_match.group(1)) if iv_match else None,
                }

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

    return (variants, segments, is_live, target_duration, audio_tracks,
            discontinuity_count, part_count)


def select_variant(session, m3u8_url, preferred_height=None):
    """Follow a chain of master playlists to reach a media playlist.
    Iterative (not recursive) with cycle detection. Returns
    (media_url, audio_tracks) — audio_tracks come from the first (master)
    level, if any."""
    seen = set()
    audio_tracks = []
    first = True
    while True:
        if m3u8_url in seen:
            raise RuntimeError("Circular master playlist reference detected.")
        seen.add(m3u8_url)

        variants, _segments, _is_live, _td, tracks, _disc, _parts = parse_playlist(session, m3u8_url)
        if first:
            audio_tracks = tracks
            first = False
        if not variants:
            return m3u8_url, audio_tracks

        status(f"Master playlist — {len(variants)} quality levels found:")
        for i, (url, res, bw) in enumerate(variants):
            print(f"  {C.GRAY}[{i}]{C.RESET} {res}  ({bw // 1000} kbps)", file=sys.stderr)

        if preferred_height:
            best = None
            for url, res, _ in variants:
                try:
                    h = int(res.split("x")[1])
                    if h <= preferred_height and (best is None or h > best[1]):
                        best = (url, h)
                except (ValueError, IndexError):
                    continue
            chosen = best[0] if best else variants[0][0]
            status(f"Auto-selected: {best[1] if best else variants[0][1]}p", C.GREEN)
        else:
            choice = input(f"{C.BOLD}Select quality [default 0]: {C.RESET}").strip()
            idx = int(choice) if choice.isdigit() and int(choice) < len(variants) else 0
            chosen = variants[idx][0]
            status(f"Selected: {variants[idx][1]}", C.GREEN)

        m3u8_url = chosen


def probe_playlist(session, url):
    """Dry-run: report variants, audio tracks, encryption, live/VOD status,
    and discontinuity markers without downloading anything."""
    variants, segments, is_live, target_duration, audio_tracks, disc_count, part_count = \
        parse_playlist(session, url)

    if variants:
        status(f"Master playlist — {len(variants)} video quality level(s):")
        for i, (u, res, bw) in enumerate(variants):
            print(f"  {C.GRAY}[{i}]{C.RESET} {res}  ({bw // 1000} kbps)", file=sys.stderr)
        if audio_tracks:
            status(f"{len(audio_tracks)} audio track(s):")
            for i, t in enumerate(audio_tracks):
                tag = " (default)" if t.get("default") else ""
                print(f"  {C.GRAY}[{i}]{C.RESET} {t.get('name') or '?'} "
                      f"[{t.get('language') or '?'}]{tag}", file=sys.stderr)
        # Descend one level into the first variant for media-level info.
        media_url = variants[0][0]
        _, segments, is_live, target_duration, _, disc_count, part_count = \
            parse_playlist(session, media_url)

    status(f"{'LIVE' if is_live else 'VOD'} playlist — {len(segments)} segment(s), "
           f"~{target_duration:.1f}s target duration", C.GREEN)
    encrypted = any(s.key_info for s in segments)
    status(f"Encryption: {'AES-128' if encrypted else 'none detected'}")
    ad_segments = sum(1 for s in segments if s.is_ad)
    if ad_segments:
        status(f"{ad_segments} segment(s) inside CUE-OUT/CUE-IN ad breaks — use --skip-ads to drop them", C.YELLOW)
    gap_segments = sum(1 for s in segments if s.is_gap)
    if gap_segments:
        status(f"{gap_segments} segment(s) marked #EXT-X-GAP — origin doesn't have them, "
               f"skipped automatically", C.YELLOW)
    if disc_count:
        status(f"{disc_count} discontinuity marker(s) found (not necessarily ads)", C.GRAY)
    if part_count:
        status(f"{part_count} #EXT-X-PART partial-segment tag(s) — low-latency HLS source; "
               f"parts aren't fetched separately, only their finalized full segments", C.GRAY)


# --------------------------------------------------------------------------
# Decryption
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


def decrypt_segment(data, key_info, seq, session):
    if not key_info or key_info["method"] != "AES-128":
        return data
    if not HAS_CRYPTO:
        raise RuntimeError("AES decryption needed. Run: pip install pycryptodome")
    key_bytes = get_key_bytes(session, key_info["key_url"])
    iv = resolve_iv(key_info, seq)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    plaintext = cipher.decrypt(data)          # decrypt FIRST
    pad_len = plaintext[-1]                   # then read padding from plaintext
    return plaintext[:-pad_len] if 1 <= pad_len <= 16 else plaintext


# --------------------------------------------------------------------------
# Segment download
# --------------------------------------------------------------------------

def download_segment(session, seg, tmp_dir, ext, prefix="seg"):
    """Download (and decrypt) one segment. A '.done' marker is only written
    after a full, successful write, so a killed/partial file is retried
    on resume rather than treated as complete. Returns (path, byte_size)."""
    out_path = os.path.join(tmp_dir, f"{prefix}_{seg.seq:08d}{ext}")
    marker = out_path + ".done"
    if os.path.exists(marker) and os.path.exists(out_path):
        return out_path, os.path.getsize(out_path)

    data = fetch(session, seg.url, byte_range=seg.byte_range).content
    data = decrypt_segment(data, seg.key_info, seg.seq, session)

    with open(out_path, "wb") as f:
        f.write(data)
    open(marker, "w").close()
    return out_path, len(data)


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
                    data = decrypt_segment(f.read(), s.key_info, s.seq, session)
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

def run_download(session, media_url, tmp_dir, workers, live_poll, skip_ads, prefix,
                  aria2c=False, no_aria2c=False):
    """Download every segment of one media playlist (polling if live),
    merge them, and return (raw_merged_path, ext, total_bytes, elapsed_sec).

    `aria2c=True` forces the aria2c backend (see download_batch_aria2c);
    otherwise it's used automatically once a batch is large enough
    (ARIA2C_AUTO_THRESHOLD) and aria2c is on PATH, unless `no_aria2c` was
    given to disable it entirely."""
    seen_seqs = set()
    skipped_seqs = set()
    state = {"ext": ".ts", "init_path": None}
    stats = {"bytes": 0, "start": time.time()}
    aria2c_available = (not no_aria2c) and shutil.which("aria2c") is not None
    if aria2c and not aria2c_available and not no_aria2c:
        warn("--aria2c given but aria2c isn't on PATH — using the built-in downloader instead.")

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
                with Spinner(f"Fetching {prefix} init segment (fMP4)..."):
                    r = fetch(session, map_url, byte_range=map_range)
                state["init_path"] = os.path.join(tmp_dir, f"{prefix}_init{state['ext']}")
                with open(state["init_path"], "wb") as f:
                    f.write(r.content)

        ext = state["ext"]
        total = len(candidates)

        use_aria2c = aria2c_available and (aria2c or total >= ARIA2C_AUTO_THRESHOLD)
        if use_aria2c:
            status(f"{total} {prefix} segment(s) to download — using aria2c backend", C.GRAY)
            with Spinner(f"aria2c downloading {prefix} segments..."):
                result = download_batch_aria2c(session, candidates, tmp_dir, ext, prefix, workers)
            if result is None:
                warn("aria2c backend failed entirely — falling back to the built-in downloader.")
                use_aria2c = False
            else:
                succeeded, failed = result
                for s, size in succeeded:
                    seen_seqs.add(s.seq)
                    stats["bytes"] += size
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

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(download_segment, session, s, tmp_dir, ext, prefix): s
                           for s in candidates}
                for future in as_completed(futures):
                    s = futures[future]
                    try:
                        _, size = future.result()
                        with lock:
                            seen_seqs.add(s.seq)
                            stats["bytes"] += size
                    except Exception as e:
                        failed.append((s, str(e)))
                    if not QUIET:
                        with lock:
                            done += 1
                            pct = done * 100 // total
                            bar = "#" * (pct // 4) + "-" * (25 - pct // 4)
                            elapsed = time.time() - stats["start"]
                            speed_mb = stats["bytes"] / elapsed / (1024 * 1024) if elapsed > 0 else 0.0
                            avg = elapsed / done if done else 0
                            eta = fmt_eta(avg * (total - done)) if avg else "?"
                        print(f"\r  {C.BLUE}[{bar}]{C.RESET} {pct}% ({done}/{total})"
                              f"  {speed_mb:.2f} MB/s  ETA {eta}",
                              end="", file=sys.stderr, flush=True)
            if not QUIET:
                print(file=sys.stderr)

            if failed:
                warn(f"{len(failed)} {prefix} segment(s) failed, retrying sequentially...")
                for s, err in failed:
                    try:
                        _, size = download_segment(session, s, tmp_dir, ext, prefix)
                        seen_seqs.add(s.seq)
                        stats["bytes"] += size
                    except Exception as e:
                        error(f"Segment {s.seq} failed permanently: {e}")

    try:
        _, segments, is_live, target_duration, _tracks, _disc, _parts = \
            parse_playlist(session, media_url)
        if not segments:
            sys.exit(f"No segments found in {prefix} playlist.")
        process_batch(segments)

        if is_live and live_poll:
            status(f"Live {prefix} playlist — polling for new segments (Ctrl-C to stop and finalize)", C.YELLOW)
            while is_live:
                time.sleep(max(target_duration / 2, 1.0))
                try:
                    _, segments, is_live, target_duration, _tracks, _disc, _parts = \
                        parse_playlist(session, media_url)
                except Exception as e:
                    warn(f"Playlist refresh failed, retrying: {e}")
                    continue
                process_batch(segments)
    except KeyboardInterrupt:
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

    elapsed = time.time() - stats["start"]
    return raw_path, ext, stats["bytes"], elapsed


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


def resume_merge(output):
    """--resume-merge entry point: no network activity at all. Rebuilds
    video (and audio, if present) purely from '<output>_parts/', muxes them
    with ffmpeg if both are present, and finalizes to `output` — for when a
    run finished (or nearly finished) downloading but the merge/mux step
    itself was interrupted or crashed."""
    tmp_dir = output + "_parts"
    if not os.path.isdir(tmp_dir):
        sys.exit(f"No {tmp_dir} directory found — nothing to recover.")

    status(f"Rebuilding from {tmp_dir} — no network access, using what's on disk...")
    video_path, _vext = merge_dir_to_file(tmp_dir, "video")
    if video_path is None:
        sys.exit(f"No completed video segments found in {tmp_dir}.")
    audio_path, _aext = merge_dir_to_file(tmp_dir, "audio")

    if audio_path and shutil.which("ffmpeg"):
        status("Muxing recovered video with recovered audio track (ffmpeg)...")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", output],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode != 0 or not os.path.exists(output) or os.path.getsize(output) == 0:
            warn("Audio mux failed — falling back to video-only output.")
            _finalize_video_only(video_path, output)
    else:
        if audio_path:
            warn("ffmpeg not found — can't mux the separate audio track; keeping video-only output.")
        _finalize_video_only(video_path, output)

    for p in (video_path, audio_path):
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
# Orchestration
# --------------------------------------------------------------------------

def download_hls(m3u8_url, output="output.mp4", workers=8, preferred_height=None,
                  referer=None, origin=None, user_agent=None, page_url=None,
                  cookie=None, cookies_from_browser=None, rate=None,
                  live_poll=True, skip_ads=False, audio_index=None,
                  quiet=False, probe=False, aria2c=False, no_aria2c=False):
    global QUIET, _RATE_LIMITER
    QUIET = quiet

    host = urlparse(m3u8_url).hostname
    # Only saves headers/rate that were explicitly given — never auto-derived
    # guesses — so the cache only ever holds values a person confirmed work.
    remember_profile(host, referer, origin, user_agent, rate)

    if referer is None and origin is None and user_agent is None:
        cached = get_cached_profile(host)
        if cached and any(cached.get(k) for k in ("referer", "origin", "user_agent")):
            referer = cached.get("referer")
            origin = cached.get("origin")
            user_agent = cached.get("user_agent")
            status(f"Using remembered header profile for {host}", C.GRAY)

    if rate is None:
        cached = get_cached_profile(host)
        cached_rate = cached.get("rate") if cached else None
        if cached_rate:
            rate = cached_rate
            status(f"Using remembered rate limit for {host}: {rate:g} req/s", C.GRAY)

    if rate:
        _RATE_LIMITER = RateLimiter(rate)
        status(f"Rate limit: {rate:g} request(s)/second across all workers", C.GRAY)

    session = build_session(m3u8_url=m3u8_url, page_url=page_url, referer=referer,
                             origin=origin, user_agent=user_agent, cookie=cookie,
                             cookies_from_browser=cookies_from_browser)
    if not referer and not page_url and not get_cached_profile(host):
        auto_ref = session.headers.get("Referer")
        status(f"Auto Referer/Origin: {auto_ref}" if auto_ref else
               "Local/proxy URL detected — sending no extra Referer/Origin", C.GRAY)

    if probe:
        probe_playlist(session, m3u8_url)
        return

    media_url, audio_tracks = select_variant(session, m3u8_url, preferred_height)

    tmp_dir = output + "_parts"
    os.makedirs(tmp_dir, exist_ok=True)

    video_path, _ext, _vb, _vt = run_download(session, media_url, tmp_dir, workers,
                                                live_poll, skip_ads, "video",
                                                aria2c=aria2c, no_aria2c=no_aria2c)

    audio_path = None
    if audio_index is not None:
        if not audio_tracks:
            warn("No alternate audio tracks found in the master playlist; ignoring --audio")
        elif not (0 <= audio_index < len(audio_tracks)):
            warn(f"--audio {audio_index} is out of range (0-{len(audio_tracks) - 1}); ignoring")
        else:
            track = audio_tracks[audio_index]
            status(f"Downloading audio track: {track.get('name') or track.get('language') or '?'}")
            audio_path, _aext, _ab, _at = run_download(session, track["url"], tmp_dir, workers,
                                                         live_poll, skip_ads, "audio",
                                                         aria2c=aria2c, no_aria2c=no_aria2c)

    if audio_path and shutil.which("ffmpeg"):
        status("Muxing video with selected audio track (ffmpeg)...")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", output],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode != 0 or not os.path.exists(output) or os.path.getsize(output) == 0:
            warn("Audio mux failed — falling back to video-only output.")
            _finalize_video_only(video_path, output)
    else:
        if audio_path:
            warn("ffmpeg not found — can't mux the separate audio track; keeping video-only output.")
        _finalize_video_only(video_path, output)

    for p in (video_path, audio_path):
        if p and os.path.exists(p) and os.path.abspath(p) != os.path.abspath(output):
            os.remove(p)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    size_mb = os.path.getsize(output) / (1024 * 1024)
    success(f"Done: {output} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Download HLS (m3u8) streams")
    parser.add_argument("url", nargs="?", default=None,
                         help="m3u8 playlist URL. Not needed with --resume-merge.")
    parser.add_argument("-o", "--output", default=None,
                         help="Output filename. Default: derived from the URL's 'embed' "
                              "query param if present, else output.mp4. Required with "
                              "--resume-merge (it names the '<output>_parts/' directory "
                              "to recover from).")
    parser.add_argument("-q", "--quality", type=int, default=None, metavar="H",
                         help="Preferred max height, e.g. 720")
    parser.add_argument("-w", "--workers", type=int, default=8, help="Parallel downloads")
    parser.add_argument("--rate", type=float, default=None, metavar="RPS",
                         help="Max HTTP requests per second across ALL workers and request "
                              "types (e.g. --rate 1). Use for sources that publish rate "
                              "limits — polite pacing beats getting blocked. Remembered "
                              "per-host, like --referer/--origin/--user-agent.")
    parser.add_argument("--page-url", default=None,
                         help="URL of the page that embeds the stream; used to auto-derive "
                              "Referer/Origin when those aren't set explicitly")
    parser.add_argument("--referer", default=None,
                         help="Explicit Referer header. Overrides auto-derivation and is "
                              "remembered for this host next time.")
    parser.add_argument("--origin", default=None,
                         help="Explicit Origin header. Overrides auto-derivation and is "
                              "remembered for this host next time.")
    parser.add_argument("--user-agent", default=None,
                         help="Custom User-Agent string. Remembered for this host next time. "
                              "Note: some sources explicitly block browser-impersonating "
                              "tools — an honest UA like 'mytool/1.0 (contact: you@mail)' "
                              "can work better.")
    parser.add_argument("--cookie", default=None,
                         help="Raw Cookie header copied VERBATIM from DevTools (Network → the "
                              ".m3u8 request → Request Headers → cookie: line → Copy Value). "
                              "NOT saved to profiles.")
    parser.add_argument("--cookies-from-browser", default=None, metavar="BROWSER",
                         choices=sorted(BROWSER_COOKIE_LOADERS),
                         help="Load cookies for this URL's host straight from an installed "
                              "browser's cookie store instead of pasting --cookie by hand. "
                              "Needs the browser-cookie3 package (pip install browser-cookie3). "
                              "Choices: " + ", ".join(sorted(BROWSER_COOKIE_LOADERS)) + ". "
                              "An explicit --cookie still wins on any name collision.")
    parser.add_argument("--audio", type=int, default=None, metavar="N",
                         help="Index of an alternate audio track to mux in (see --probe "
                              "to list available tracks)")
    parser.add_argument("--skip-ads", action="store_true",
                         help="Drop segments inside CUE-OUT/CUE-IN ad-break markers")
    parser.add_argument("--no-live-poll", action="store_true",
                         help="For live playlists, grab what's there now and stop, "
                              "instead of polling for new segments")
    parser.add_argument("--aria2c", action="store_true",
                         help="Force the aria2c backend for segment downloads (needs aria2c "
                              "on PATH). Normally used automatically once a batch reaches "
                              f"{ARIA2C_AUTO_THRESHOLD} segments and aria2c is available.")
    parser.add_argument("--no-aria2c", action="store_true",
                         help="Never use aria2c, even for very large segment counts.")
    parser.add_argument("--resume-merge", action="store_true",
                         help="Skip the network entirely and rebuild --output from an "
                              "existing '<output>_parts/' directory left over from an "
                              "interrupted run — for when segments finished downloading "
                              "but the merge/mux step itself didn't complete. Requires -o.")
    parser.add_argument("--probe", action="store_true",
                         help="Report variants, audio tracks, encryption, and live/VOD "
                              "status, then exit without downloading")
    parser.add_argument("--quiet", action="store_true",
                         help="Suppress progress/status output; only warnings, errors, "
                              "and the final result line are printed")
    args = parser.parse_args()

    if args.resume_merge:
        if not args.output:
            parser.error("--resume-merge needs -o/--output (it names the "
                          "'<output>_parts/' directory to recover from)")
        global QUIET
        QUIET = args.quiet
        try:
            resume_merge(args.output)
        except (Exception, SystemExit) as e:
            error(str(e) or repr(e))
            sys.exit(1)
        return

    if not args.url:
        parser.error("url is required (unless using --resume-merge)")

    output = args.output or derive_output_name(args.url)
    start = time.time()
    ok, err_msg = True, None

    try:
        download_hls(args.url, output, args.workers, args.quality,
                     referer=args.referer, origin=args.origin, user_agent=args.user_agent,
                     page_url=args.page_url, cookie=args.cookie,
                     cookies_from_browser=args.cookies_from_browser, rate=args.rate,
                     live_poll=not args.no_live_poll,
                     skip_ads=args.skip_ads, audio_index=args.audio,
                     quiet=args.quiet, probe=args.probe,
                     aria2c=args.aria2c, no_aria2c=args.no_aria2c)
    except (Exception, SystemExit) as e:
        ok = False
        err_msg = str(e) or repr(e)
        error(err_msg)
    finally:
        if not args.probe:
            log_run({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "url": args.url,
                "output": output,
                "success": ok,
                "error": err_msg,
                "duration_sec": round(time.time() - start, 1),
                "size_mb": (round(os.path.getsize(output) / (1024 * 1024), 1)
                            if ok and os.path.exists(output) else None),
            })

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
