<div align="center">

# HLS Downloader

**Download, decrypt, and remux HLS (`.m3u8`) streams from the terminal — VOD or live.**

One file. Two dependencies. No install.

[![Python](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)](#-installation)
[![Dependencies](https://img.shields.io/badge/deps-requests%2C%20pycryptodome-orange)](#-installation)
[![ffmpeg](https://img.shields.io/badge/ffmpeg-optional-3DA639?logo=ffmpeg&logoColor=white)](#-installation)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey?logo=gnubash&logoColor=white)](#-installation)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-21BB76)](#-contributing)

[Features](#-features) · [Installation](#-installation) · [Quick start](#-quick-start) · [Usage](#-usage) · [Troubleshooting](#-when-downloads-are-blocked) · [FAQ](#-faq)

</div>

---

`hls_downloader.py` is a single-file CLI for archiving HTTP Live Streaming media: it parses master and media playlists, handles quality and audio-track selection, decrypts AES-128 segments, downloads concurrently with resume, and remuxes to a clean MP4. It also plays nice with anti-bot frontends — it diagnoses 401/403 blocks, supports verbatim browser cookies, and lets you identify your client honestly and pace your requests for sites that publish bot policies.

## ✨ Features

| Feature | What you get |
|---|---|
| 🎚️ **Quality selection** | Lists every rendition in the master playlist — choose interactively, or auto-pick with `-q 720` |
| 🎵 **Alternate audio tracks** | Discovers `EXT-X-MEDIA:TYPE=AUDIO` tracks (languages, commentary) and muxes your pick in with ffmpeg |
| 🔐 **AES-128 decryption** | Explicit and implicit (sequence-number) IVs; keys may rotate mid-playlist |
| 📦 **fMP4 / CMAF & byte ranges** | Handles `EXT-X-MAP` init segments and `EXT-X-BYTERANGE`, including implicit offsets |
| ✂️ **Ad-break skipping** | `--skip-ads` drops segments inside `EXT-X-CUE-OUT`/`EXT-X-CUE-IN` markers |
| 📡 **Live capture** | Polls live playlists until `EXT-X-ENDLIST`, or `Ctrl-C` to stop and finalize what's captured |
| ⚡ **Concurrent + resumable** | Parallel segment fetches with retry/backoff, `.done`-marker integrity, live speed/ETA readout |
| 🍪 **Verbatim cookies** | `--cookie` takes a raw `Cookie:` header pasted from DevTools — `HttpOnly` cookies included |
| 🚦 **Polite rate limiting** | `--rate 1` paces *every* request (manifests, keys, segments, polls) across all worker threads |
| 🛡️ **401/403 diagnostics** | Names the blocker (`Server` header + block-page body), prints targeted hints, and emits a ready-to-run `curl` replay |
| 🧠 **Remembered header profiles** | Referer/Origin/User-Agent you set explicitly are cached per host — debug each source once |
| 🏷️ **Auto-named output** | Derives a filename from the URL's `embed=` query param instead of a generic `output.mp4` |
| 🔍 **Probe mode** | `--probe` reports qualities, audio tracks, encryption, live/VOD status, and ad markers without downloading |
| 🧹 **ffmpeg remux** | Clean MP4 container when ffmpeg is present; raw concatenated stream otherwise |
| 📜 **Run history** | Every run appended to a local JSONL log |

## 📦 Installation

**Requirements:** Python 3.8+, [`requests`](https://pypi.org/project/requests/), and [`pycryptodome`](https://pypi.org/project/pycryptodome/) (only for AES-128 streams). [`ffmpeg`](https://ffmpeg.org/) on `PATH` is optional but recommended.

```bash
git clone https://github.com/Lunatic16/hls_downloader.git
cd hls-downloader
pip install -r requirements.txt        # requests, pycryptodome
```

No package, no entry point — the tool *is* the one file:

```bash
python hls_downloader.py --help
```

<details>
<summary><b>Installing ffmpeg (optional)</b></summary>

```bash
# Debian/Ubuntu
sudo apt install ffmpeg
# macOS
brew install ffmpeg
# Windows
winget install Gyan.FFmpeg
```

Without ffmpeg you still get a valid concatenated stream file — it just won't be remuxed into a clean MP4, and separate audio tracks can't be muxed in.

</details>

## 🚀 Quick start

```bash
# 1. See what a source offers — no download yet
python hls_downloader.py "https://cdn.example.com/master.m3u8" --probe

# 2. Download at up to 1080p
python hls_downloader.py "https://cdn.example.com/master.m3u8" -q 1080 -o video.mp4
```

Typical probe output:

```text
› Master playlist — 4 video quality level(s):
  [0] 1920x1080  (3687 kbps)
  [1] 1280x720   (1888 kbps)
  [2] 854x480    (940 kbps)
  [3] 480x270    (502 kbps)
› VOD playlist — 987 segment(s), ~6.0s target duration
› Encryption: none detected
```

> [!TIP]
> **Always quote the URL.** Manifest URLs often contain `&`, `?`, or `%` — an unquoted `&` makes your shell background the command and silently truncates the URL.

## 📖 Usage

```bash
python hls_downloader.py <m3u8_url> [options]
```

| Flag | Description |
|---|---|
| `-o`, `--output` | Output filename. Default: derived from the URL's `embed` param if present, else `output.mp4`. |
| `-q`, `--quality` | Preferred max height (e.g. `720`). Auto-selects the best rendition at or below it, skipping the prompt. |
| `-w`, `--workers` | Parallel segment downloads. Default `8`. Lower this if the source caps concurrent connections. |
| `--rate` | Max HTTP requests **per second across all workers and request types** (e.g. `--rate 1`). For sources that publish rate limits. |
| `--page-url` | URL of the page that embeds the stream; auto-derives Referer/Origin — the strongest signal for Referer-checking CDNs. |
| `--referer` / `--origin` | Explicit overrides. Remembered for this host next time. |
| `--user-agent` | Custom UA string, remembered for this host. See the [warning below](#-when-downloads-are-blocked) before impersonating a browser. |
| `--cookie` | Raw `Cookie:` header copied **verbatim** from DevTools → the failing request → Request Headers. Includes `HttpOnly` cookies. Never persisted. |
| `--audio N` | Index of an alternate audio track to mux in (`--probe` lists them). |
| `--skip-ads` | Drop segments inside `CUE-OUT`/`CUE-IN` ad markers. |
| `--no-live-poll` | Grab the live playlist's current window and stop instead of polling. |
| `--probe` | Dry-run: report variants, audio, encryption, live/VOD status; download nothing. |
| `--quiet` | Suppress status/spinner output; warnings, errors, and the final line still print. |

### Examples

**Source that checks Referer** — pass the page you found the stream on:

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" \
  --page-url "https://example-site.com/watch/some-event"
```

**Login-gated source** — pass your browser's session cookies *and* the UA they were issued to:

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" \
  --page-url "https://example-site.com/watch/some-event" \
  --cookie "session=abc123; other=xyz" \
  --user-agent "Mozilla/5.0 ..."
```

**Source with a published bot policy** — identify honestly, stay under their caps:

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" \
  --user-agent "my-archiver/1.0 (personal backup; contact: you@example.com)" \
  --rate 1 -w 3 -q 1080
```

**Live stream** — record until it ends or `Ctrl-C` to finalize early:

```bash
python hls_downloader.py "https://cdn.example.com/live/master.m3u8" -o live.mp4
```

<details>
<summary><b>More examples</b></summary>

**Specific audio track, ads stripped:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --audio 1 --skip-ads
```

**Live snapshot without polling:**

```bash
python hls_downloader.py "https://cdn.example.com/live/master.m3u8" --no-live-poll
```

**Quiet mode for cron:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --quiet -o nightly.mp4
```

</details>

## 🔍 How header auto-detection works

Requests are built with Referer/Origin in this priority order:

1. **Explicit flags** — `--referer` / `--origin`, saved to the per-host profile cache.
2. **Remembered profile** — headers from a previous explicit run are reused automatically.
3. **`--page-url`** — Referer set to the exact page URL, Origin to its host.
4. **Manifest origin (fallback)** — scheme + host of the `.m3u8` itself; skipped for local/private addresses (your own proxy), since a fabricated Referer forwarded upstream can trip *that* source's checks.

> [!WARNING]
> **Don't impersonate browsers around anti-bot layers.** Frontends like DDoS-Guard compare the UA against the TLS/HTTP fingerprint — "claims Firefox, handshakes like python-urllib" reads as impersonation and gets blocked. If a block page mentions bots or "pretending to be a browser," switch to an honest UA with contact info and use `--rate`. Sites that publish bot policies generally *welcome* identified tools.

## 🧯 When downloads are blocked

On the first 401/403, the tool prints everything needed to diagnose it in one pass: the `Server` header, the block-page body, blocker-specific hints, and a **`curl` command replaying the exact request**. Run that curl as-is:

- **curl succeeds, Python fails** → your headers/cookies are fine; the block is client *fingerprinting* (Python's TLS handshake). Workarounds: shell fetches out to curl, or use `yt-dlp --impersonate`.
- **curl fails identically** → it's a headers/cookie/token/IP problem. Work the decision tree below.

**Decision tree, in order of likelihood:**

1. **Stale signed URL.** `?e=...&s=...` params are expiry + signature — often short-lived or single-use. Re-copy a **fresh** URL from the page's network tab before each attempt. (`e=` is a Unix expiry timestamp.)
2. **Session-bound token.** Works in your browser, 403s everywhere else → `--cookie` with the **full verbatim cookie line** (don't rebuild it by hand — you'll miss `HttpOnly` cookies like `__ddg2`), plus the **same** `--user-agent` the cookies were issued to, plus the **same** public IP (`curl -s https://api.ipify.org`).
3. **Bot-policy site.** Block page addresses *you* and mentions rate limits? These sites allow identified tools and block impersonation. Drop the fake UA **and** the browser cookies, then use an honest UA with contact info plus `--rate 1 -w 3`.
4. **IP binding.** Anti-bot cookies (`__ddg9_`/`__ddg10_`, `cf_clearance`) are signed against the browser's IP. A VPN in the browser but not the terminal (or vice versa) will 403 forever.
5. **Still blocked?** If the block page offers a contact address, use it — include your UA string and IP. Operators who write custom block pages usually answer polite, identified archivers.

> [!IMPORTANT]
> **Etiquette keeps the door open.** Honor published rate limits (`--rate`) and concurrency caps (`-w`). The identifiable-UA pattern only keeps working if identified tools behave.

## 💾 Where things are stored

| Path | Contents |
|---|---|
| `~/.config/hls_downloader/profiles.json` | Remembered Referer/Origin/User-Agent per host (never cookies — credentials) |
| `~/.local/share/hls_downloader/history.jsonl` | One JSON line per run: timestamp, URL, output, success, duration, size |
| `<output>_parts/` | In-progress segments next to the output file; removed automatically after merging |

## ❓ FAQ

<details>
<summary><b>Does it handle DRM (Widevine, FairPlay, PlayReady)?</b></summary>

No — by design. Only standard HLS AES-128 (clearkey) streams are decrypted. If the master playlist references DRM initiation data, this tool can't and won't help.
</details>

<details>
<summary><b>Why is my <code>--rate 1</code> download slow?</b></summary>

It's pacing to one request per second across everything — manifests, keys, segments, live polls. For a 987-segment VOD that's a ~17-minute floor. That's the point: it keeps you inside limits like "no more than 1 request per second."
</details>

<details>
<summary><b>My cookie flag was ignored — why?</b></summary>

The value must contain `name=value` pairs. A bare token (just the value) parses to nothing. Copy the entire `cookie:` request-header line from DevTools — right-click → Copy Value — rather than rebuilding it by hand.
</details>

<details>
<summary><b>Partial download failed midway — start over?</b></summary>

No. Re-run the same command with the same `-o` filename: completed segments are detected by their `.done` markers and skipped, and merging picks up missing ones. Verify the token URL is still fresh first.
</details>

<details>
<summary><b>The player shows more quality levels than the tool does.</b></summary>

Players invent renditions via ABR from whatever the master lists — the tool shows what's actually declared. If resolutions look wrong, check the master playlist for `RESOLUTION` attributes; renditions without them appear as `unknown` bandwidth estimates.
</details>

## 🧭 Roadmap

- [ ] `--cookies-from-browser firefox|chrome` via `browser-cookie3`
- [ ] Optional `aria2c` backend for very large segment counts
- [ ] HLS `EXT-X-GAP` / partial-segment (`EXT-X-PART`) awareness
- [ ] Per-host remembered rate limits alongside header profiles
- [ ] Interrupted-merge recovery (rebuild output from `_parts/` without refetching)

## 🤝 Contributing

Issues and PRs welcome. Good first targets: any `EXT-X` tag the parser doesn't know about yet (open an issue with a sanitized playlist snippet), and reproductions of tricky anti-bot configurations for the diagnostics hints. Please keep examples in issues sanitized — no tokens, cookies, or credentials.

## 📜 License

<!-- Pick a license (MIT/Apache-2.0 are common for CLI tools), add a LICENSE file,
     then swap this section and the badge above:
     [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) -->

Distributed under the MIT License.

## 🚨 Disclaimer

> [!CAUTION]
> This tool only fetches and reassembles segments from a playlist URL you provide. You're responsible for using it in accordance with the terms of service and copyright of whatever you point it at — including published bot policies, rate limits, and access rules. Identify your client honestly, pace your requests, and honor contact addresses when a site offers them.
