<div align="center">

# 📺 HLS Downloader 📺

**Download, decrypt, and remux HLS (`.m3u8`) streams from the terminal — VOD or live.**

One file. Two dependencies. No install.

[![Python](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)](#-installation)
[![Dependencies](https://img.shields.io/badge/deps-requests%2C%20browser-cookie3%2C%20pycryptodome-orange)](#-installation)
[![ffmpeg](https://img.shields.io/badge/ffmpeg-optional-3DA639?logo=ffmpeg&logoColor=white)](#-installation)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey?logo=gnubash&logoColor=white)](#-installation)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-21BB76)](#-contributing)

[Features](#-features) · [Installation](#-installation) · [Quick start](#-quick-start) · [Usage](#-usage) · [Troubleshooting](#-when-downloads-are-blocked) · [FAQ](#-faq)

</div>

---

`hls_downloader.py` is a single-file CLI for archiving HTTP Live Streaming media: it parses master and media playlists, handles quality, audio-track, and subtitle-track selection, decrypts AES-128 segments *and* per-sample SAMPLE-AES (CENC/CBCS) fMP4/CMAF segments, downloads concurrently with resume, and remuxes to a clean MP4. It also plays nice with anti-bot frontends — it diagnoses 401/403 blocks, tells you when cookies can and can't matter, supports verbatim browser cookies, and lets you identify your client honestly and pace your requests for sites that publish bot policies.

## ✨ Features

| Feature | What you get |
|---|---|
| 🎚️ **Quality selection** | Lists every rendition in the master playlist — choose interactively, or auto-pick with `-q 720` |
| 🎵 **Alternate audio tracks** | Discovers `EXT-X-MEDIA:TYPE=AUDIO` tracks (languages, commentary) and muxes your pick in with ffmpeg |
| 💬 **Subtitles** | Discovers `EXT-X-MEDIA:TYPE=SUBTITLES` tracks; `--subs N` downloads the WebVTT track and muxes it in as `mov_text` |
| 🔐 **AES-128 decryption** | Explicit and implicit (sequence-number) IVs; keys may rotate mid-playlist |
| 🔑 **SAMPLE-AES (CENC/CBCS)** | Per-sample `SAMPLE-AES`/`SAMPLE-AES-CTR` decryption for fMP4/CMAF (`cenc`/`cbcs`, clearkey); DRM key formats are detected and refused with a clear message |
| 📦 **fMP4 / CMAF & byte ranges** | Handles `EXT-X-MAP` init segments and `EXT-X-BYTERANGE`, including implicit offsets |
| 🕳️ **Gap-aware** | Skips segments marked `EXT-X-GAP` (not actually present at the origin) instead of erroring on them; flags `EXT-X-PART` low-latency sources in `--probe` |
| ✂️ **Ad-break skipping** | `--skip-ads` drops segments inside `EXT-X-CUE-OUT`/`EXT-X-CUE-IN` markers |
| 📡 **Live capture** | Polls live playlists until `EXT-X-ENDLIST`, or `Ctrl-C` to stop and finalize what's captured |
| ⚡ **Concurrent + resumable** | Parallel segment fetches with retry/backoff, `.done`-marker integrity, live speed/ETA readout |
| ⏯️ **Parallel tracks** | Video and `--audio N` download concurrently — audio no longer waits for video |
| ⛽ **Range resume** | Interrupted unencrypted segments resume from their `.part` bytes via HTTP Range instead of refetching whole |
| 🚀 **aria2c backend** | Auto-engages for very large segment counts (or force with `--aria2c`) when `aria2c` is on `PATH` — falls back to the built-in downloader on failure |
| 🩹 **Interrupted-merge recovery** | `--resume-merge` rebuilds the output straight from a leftover `<output>_parts/` directory — no refetching |
| 🍪 **Verbatim cookies** | `--cookie` takes a raw `Cookie:` header pasted from DevTools — `HttpOnly` cookies included |
| 🌐 **Cookies from your browser** | `--cookies-from-browser firefox\|chrome\|...` pulls cookies straight from an installed browser's cookie store |
| 🧭 **Auth awareness** | Detects signed URLs (JWT, CloudFront, Akamai, `e=&s=`), warns when cookies *can't* matter, checks JWT expiry against your clock, and continues gracefully when cookie extraction fails |
| 🔎 **Cookie extraction diagnostics** | On failure, checks which profile directories actually exist on your machine (XDG, Snap, Flatpak) and hands you the exact symlink fix |
| 🚦 **Polite rate limiting** | `--rate 1` paces *every* request (manifests, keys, segments, polls) across all worker threads — remembered per host |
| 🐢 **Bandwidth throttling** | `--limit-rate 2M` caps bytes/sec with a token bucket (aria2c-style suffixes; honored by the aria2c backend too) |
| 🌍 **Proxy support** | `--proxy URL`, or just set `HTTP_PROXY`/`HTTPS_PROXY` — honored everywhere, including aria2c and the curl replay |
| 🛡️ **401/403 diagnostics** | Names the blocker (`Server` header + block-page body), prints targeted hints, and emits a ready-to-run `curl` replay |
| 🧠 **Remembered header, rate & page profiles** | Referer/Origin/User-Agent/rate/`--page-url` you set explicitly are cached per host — debug each source once |
| 🔒 **Output locking** | `<output>.lock` prevents two concurrent runs (cron + manual) from corrupting the same `_parts/` directory |
| 🩺 **Post-merge integrity check** | ffprobes the final file and warns if its duration deviates from the playlist's summed EXTINF time |
| 🏷️ **Auto-named output** | Derives a filename from the URL's `embed=` query param instead of a generic `output.mp4` |
| 🔍 **Probe mode** | `--probe` reports qualities, audio/subtitle tracks, encryption, live/VOD status, and ad/gap/part markers without downloading |
| 📏 **`--estimate`** | With `--probe`: approximate file size from the variant's declared bandwidth × playlist duration |
| 🧾 **`--json`** | Machine-readable probe reports and final results on stdout — no scraping colored stderr |
| 📋 **Batch mode** | `--batch urls.txt` (or `-` for stdin): sequential downloads, per-item history, one failure never stops the rest |
| 🩺 **`--doctor`** | One command reports whether ffmpeg, aria2c, pycryptodome, and the cookie library are present — plus which browsers' profiles are readable |
| 🧹 **ffmpeg remux** | Clean MP4 container when ffmpeg is present; raw concatenated stream otherwise |
| 📜 **Run history** | Every run appended to a local JSONL log |

## 📦 Installation

**Requirements:** Python 3.8+, [`requests`](https://pypi.org/project/requests/), and [`pycryptodome`](https://pypi.org/project/pycryptodome/) (only for encrypted streams). [`ffmpeg`](https://ffmpeg.org/) on `PATH` is optional but recommended.

```bash
git clone https://github.com/Lunatic16/hls_downloader.git
cd hls_downloader
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

Without ffmpeg you still get a valid concatenated stream file — it just won't be remuxed into a clean MP4, and separate audio/subtitle tracks can't be muxed in.

</details>

<details>
<summary><b>Optional: aria2c backend (large downloads)</b></summary>

```bash
# Debian/Ubuntu
sudo apt install aria2
# macOS
brew install aria2
# Windows
winget install aria2.aria2
```

Once `aria2c` is on `PATH`, it's used automatically for very large segment counts (or force it any time with `--aria2c`; disable with `--no-aria2c`). Segments are still decrypted by this tool afterward — aria2c only handles the raw fetch.

</details>

<details>
<summary><b>Optional: cookies straight from your browser</b></summary>

```bash
pip install browser-cookie3
```

Needed only for `--cookies-from-browser firefox|chrome|chromium|edge|brave|opera|vivaldi|safari`. Without it, `--cookie` (pasting the raw header from DevTools) still works exactly as before. On Linux this reads the key via your desktop keyring (GNOME Keyring/KWallet) — the browser and keyring must be unlocked; on locked/headless sessions use `--cookie` instead.

Extraction reads each browser's standard profile location plus the XDG layout (`~/.config/mozilla/…` on Firefox 121+/Fedora), Snap (`~/snap/<name>/…`), and Flatpak (`~/.var/app/<app-id>/…`) installs. On failure, the error names the profile directories actually present on your machine, with a ready-made symlink fix for sandboxed installs.

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
› 2 subtitle track(s) — add with --subs N:
  [0] English [en] (default)
  [1] Español [es]
```

> [!TIP]
> **Always quote the URL.** Manifest URLs often contain `&`, `?`, or `%` — an unquoted `&` makes your shell background the command and silently truncates the URL.

## 📖 Usage

```bash
python hls_downloader.py <m3u8_url> [options]
```

| Flag | Description |
|---|---|
| `-o`, `--output` | Output filename. Default: derived from the URL's `embed` param if present, else `output.mp4`. Ignored with `--batch`. |
| `-q`, `--quality` | Preferred max height (e.g. `720`). Auto-selects the best rendition at or below it, skipping the prompt. |
| `-w`, `--workers` | Parallel segment downloads. Default `8`. Lower this if the source caps concurrent connections. |
| `--rate` | Max HTTP requests **per second across all workers and request types** (e.g. `--rate 1`). For sources that publish rate limits. Remembered per host. |
| `--limit-rate` | Max **bandwidth** in bytes/sec with `K`/`M`/`G` suffixes (e.g. `--limit-rate 2M`). Token-bucket paced; the aria2c backend honors it too. |
| `--proxy` | Route everything through an HTTP(S) proxy. `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` env vars are always honored without this flag. |
| `--page-url` | URL of the page that embeds the stream; auto-derives Referer/Origin — the strongest signal for Referer-checking CDNs. Remembered per host. |
| `--referer` / `--origin` | Explicit overrides. Remembered for this host next time. |
| `--user-agent` | Custom UA string, remembered for this host. See the [warning below](#-when-downloads-are-blocked) before impersonating a browser. |
| `--cookie` | Raw `Cookie:` header copied **verbatim** from DevTools → the failing request → Request Headers. Includes `HttpOnly` cookies. Never persisted. |
| `--cookies-from-browser` | Load cookies for the URL's host straight from an installed browser (`firefox`, `chrome`, `chromium`, `edge`, `brave`, `opera`, `vivaldi`, `safari`). Needs `pip install browser-cookie3`. An explicit `--cookie` still wins on any name collision. The tool warns when cookies can't matter (signed URLs), continues cookieless if extraction fails, checks the URL's JWT expiry, and diagnoses profile-location problems (XDG/Snap/Flatpak) with symlink fixes. |
| `--audio N` | Index of an alternate audio track to mux in (`--probe` lists them). Downloaded concurrently with video. |
| `--subs N` | Index of a subtitle track (`--probe` lists them); the WebVTT is assembled and muxed in as a `mov_text` track. |
| `--skip-ads` | Drop segments inside `CUE-OUT`/`CUE-IN` ad markers. |
| `--no-live-poll` | Grab the live playlist's current window and stop instead of polling. |
| `--aria2c` | Force the `aria2c` backend for segment downloads (needs `aria2c` on `PATH`). Otherwise used automatically once a batch is large enough. |
| `--no-aria2c` | Never use `aria2c`, even for very large segment counts. |
| `--resume-merge` | Skip the network entirely and rebuild `-o`'s output from a leftover `<output>_parts/` directory — for when segments finished downloading but the merge/mux step itself was interrupted. Requires `-o`; the URL can be omitted. |
| `--probe` | Dry-run: report variants, audio/subtitle tracks, I-frame renditions, encryption, live/VOD status; download nothing. |
| `--estimate` | With `--probe`: print an approximate output size (variant BANDWIDTH × summed EXTINF duration). |
| `--json` | Print the `--probe` report and the final download result as JSON on **stdout**. |
| `--batch FILE` | Download one URL per line from FILE (`-` = stdin). `#` comments and blank lines allowed; sequential; each failure logged, never fatal. |
| `--doctor` | Self-check: python, requests, pycryptodome, ffmpeg/ffprobe, aria2c, browser-cookie3, usable browser profiles, config paths, proxy env vars. |
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

**Specific audio track, subtitles, ads stripped:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --audio 1 --subs 0 --skip-ads
```

**Cap bandwidth on a metered connection:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --limit-rate 2M -q 720
```

**Geo-restricted source through a proxy:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --proxy http://127.0.0.1:8080
```

**Know the size before committing:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --probe --estimate
```

**Drive it from a script:**

```bash
python hls_downloader.py "$URL" --probe --json | jq '.variants[0].bandwidth'
python hls_downloader.py "$URL" -q 720 --json --quiet
```

**Overnight batch (cron-friendly):**

```bash
python hls_downloader.py --batch urls.txt -q 1080 --quiet
printf '%s\n' "https://cdn.example.com/a.m3u8" "https://cdn.example.com/b.m3u8" \
  | python hls_downloader.py --batch - -q 1080
```

**Live snapshot without polling:**

```bash
python hls_downloader.py "https://cdn.example.com/live/master.m3u8" --no-live-poll
```

**Quiet mode for cron:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --quiet -o nightly.mp4
```

**Cookies straight from Firefox, no copy-pasting:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --cookies-from-browser firefox
```

**Huge VOD, force the faster aria2c backend:**

```bash
python hls_downloader.py "https://cdn.example.com/master.m3u8" --aria2c -w 16
```

**Recover from an interrupted run** (segments finished downloading, but the merge/mux step got killed or crashed):

```bash
python hls_downloader.py --resume-merge -o video.mp4
```

**Check the environment before a long unattended run:**

```bash
python hls_downloader.py --doctor
```

</details>

## 🔍 How header auto-detection works

Requests are built with Referer/Origin in this priority order:

1. **Explicit flags** — `--referer` / `--origin`, saved to the per-host profile cache.
2. **Remembered profile** — headers (and `--page-url`) from a previous explicit run are reused automatically.
3. **`--page-url`** — Referer set to the exact page URL, Origin to its host.
4. **Manifest origin (fallback)** — scheme + host of the `.m3u8` itself; skipped for local/private addresses (your own proxy), since a fabricated Referer forwarded upstream can trip *that* source's checks.

> [!WARNING]
> **Don't impersonate browsers around anti-bot layers.** Frontends like DDoS-Guard compare the UA against the TLS/HTTP fingerprint — "claims Firefox, handshakes like python-urllib" reads as impersonation and gets blocked. If a block page mentions bots or "pretending to be a browser," switch to an honest UA with contact info and use `--rate`. Sites that publish bot policies generally *welcome* identified tools.

## 🧯 When downloads are blocked

On the first 401/403, the tool prints everything needed to diagnose it in one pass: the `Server` header, the block-page body, blocker-specific hints, and a **`curl` command replaying the exact request** (including the proxy, if one was used). Run that curl as-is:

- **curl succeeds, Python fails** → your headers/cookies are fine; the block is client *fingerprinting* (Python's TLS handshake). Workarounds: shell fetches out to curl, or use `yt-dlp --impersonate`.
- **curl fails identically** → it's a headers/cookie/token/IP problem. Work the decision tree below.

**Decision tree, in order of likelihood:**

1. **Stale signed URL.** `?e=...&s=...` params are expiry + signature — often short-lived or single-use. Re-copy a **fresh** URL from the page's network tab before each attempt. (`e=` is a Unix expiry timestamp.) The tool reads JWT `exp` claims up front and tells you the token's local-clock expiry before wasting a run.
2. **Session-bound token.** Works in your browser, 403s everywhere else → `--cookie` with the **full verbatim cookie line** (don't rebuild it by hand — you'll miss `HttpOnly` cookies like `__ddg2`), plus the **same** `--user-agent` the cookies were issued to, plus the **same** public IP (`curl -s https://api.ipify.org`).
3. **Bot-policy site.** Block page addresses *you* and mentions rate limits? These sites allow identified tools and block impersonation. Drop the fake UA **and** the browser cookies, then use an honest UA with contact info plus `--rate 1 -w 3`.
4. **IP binding.** Anti-bot cookies (`__ddg9_`/`__ddg10_`, `cf_clearance`) are signed against the browser's IP. A VPN in the browser but not the terminal (or vice versa) will 403 forever.
5. **Still blocked?** If the block page offers a contact address, use it — include your UA string and IP. Operators who write custom block pages usually answer polite, identified archivers.

> [!IMPORTANT]
> **Etiquette keeps the door open.** Honor published rate limits (`--rate`) and concurrency caps (`-w`). The identifiable-UA pattern only keeps working if identified tools behave.

## 💾 Where things are stored

| Path | Contents |
|---|---|
| `~/.config/hls_downloader/profiles.json` | Remembered Referer/Origin/User-Agent/rate/`--page-url` per host (never cookies — credentials) |
| `~/.local/share/hls_downloader/history.jsonl` | One JSON line per run: timestamp, URL, output, success, duration, size |
| `<output>_parts/` | In-progress segments next to the output file; removed automatically after merging. Left behind if a run is interrupted before the final merge/mux — `--resume-merge -o <output>` rebuilds straight from it, no refetching. |
| `<output>.lock` | Advisory lock held while a run is writing into `<output>_parts/` — a second run on the same output refuses to start instead of corrupting it; a lock left by a dead process is removed automatically. |

## ❓ FAQ

<details>
<summary><b>Do I need <code>--cookie</code> for a site's video?</b></summary>

Usually no. If the URL contains `token=`/`signature=`/`e=&s=` (the tool tells you, and decodes JWT expiry), or a `--probe` works without cookies, the credential is the link and the Referer — not your site session. A browser never sends e.g. Patreon's cookies to the stream's CDN, so cookie extraction failing or coming up empty is normally harmless; the tool says so explicitly when that's the case, and only connects a cookie failure to your problem if an actual 401/403 shows up.
</details>

<details>
<summary><b><code>--cookies-from-browser</code> says it can't find my Firefox profile — why?</b></summary>

Modern Firefox installs keep profiles in more than one place: the classic `~/.mozilla/firefox/`, the XDG layout `~/.config/mozilla/firefox/` (Firefox 121+, and Fedora's default), Snap's `~/snap/firefox/common/.mozilla/firefox/`, and Flatpak's `~/.var/app/org.mozilla.firefox/.mozilla/firefox/`. Some `browser-cookie3` builds only search the classic path. The tool checks which of these actually exist on your machine and prints the exact location plus a symlink fix, e.g. `ln -s ~/.config/mozilla ~/.mozilla` — or just `pip install -U browser-cookie3`. `--doctor` lists which browsers' profiles are readable before you even try.
</details>

<details>
<summary><b>Does it handle DRM (Widevine, FairPlay, PlayReady)?</b></summary>

No — by design. Standard HLS AES-128 (clearkey) and clearkey `SAMPLE-AES`/`SAMPLE-AES-CTR` per-sample encryption on fMP4/CMAF (`cenc`/`cbcs` schemes) are decrypted in-process. If the playlist's `EXT-X-KEY` declares a `KEYFORMAT` like `com.apple.streamingkeydelivery` (FairPlay) or `com.microsoft.playready`, the tool detects it, reports it in `--probe` (`drm_protected: true` in `--json`), and refuses rather than producing garbage.
</details>

<details>
<summary><b>Why is my <code>--rate 1</code> download slow?</b></summary>

It's pacing to one request per second across everything — manifests, keys, segments, live polls. For a 987-segment VOD that's a ~17-minute floor. That's the point: it keeps you inside limits like "no more than 1 request per second." If your bottleneck is bandwidth rather than request count, use `--limit-rate` instead — it caps bytes/sec without throttling request frequency.
</details>

<details>
<summary><b>My cookie flag was ignored — why?</b></summary>

The value must contain `name=value` pairs. A bare token (just the value) parses to nothing. Copy the entire `cookie:` request-header line from DevTools — right-click → Copy Value — rather than rebuilding it by hand.
</details>

<details>
<summary><b>Partial download failed midway — start over?</b></summary>

No. Re-run the same command with the same `-o` filename: completed segments are detected by their `.done` markers and skipped, and merging picks up missing ones. Interrupted *in-flight* segments also resume from the bytes already on disk via HTTP Range. Verify the token URL is still fresh first.
</details>

<details>
<summary><b>Segments all finished but the run died during merge/mux — do I have to redownload?</b></summary>

No. As long as `<output>_parts/` is still there, run:

```bash
python hls_downloader.py --resume-merge -o video.mp4
```

This rebuilds the output entirely from what's on disk — no network access at all — then muxes/remuxes as normal.
</details>

<details>
<summary><b>Two runs picked the same output file — what happens?</b></summary>

The second run refuses to start and names the pid holding the lock, instead of both runs interleaving writes into the same `_parts/` directory. If the lock's owner is dead, it's removed automatically and the run proceeds.
</details>

<details>
<summary><b>The player shows more quality levels than the tool does.</b></summary>

Players invent renditions via ABR from whatever the master lists — the tool shows what's actually declared. If resolutions look wrong, check the master playlist for `RESOLUTION` attributes; renditions without them appear as `unknown`. I-frame renditions (`EXT-X-I-FRAME-STREAM-INF`) are preview/thumbnail streams and are listed but not selectable.
</details>

## 🧭 Roadmap

- [x] `--cookies-from-browser firefox|chrome` via `browser-cookie3`
- [x] Optional `aria2c` backend for very large segment counts
- [x] HLS `EXT-X-GAP` / partial-segment (`EXT-X-PART`) awareness
- [x] Per-host remembered rate limits alongside header profiles
- [x] Interrupted-merge recovery (rebuild output from `_parts/` without refetching)
- [x] SAMPLE-AES / SAMPLE-AES-CTR per-sample decryption for fMP4/CMAF (cenc/cbcs)
- [x] Subtitle (WebVTT) track support: `--subs N`, muxed as `mov_text`
- [x] `EXT-X-I-FRAME-STREAM-INF` rendition awareness
- [x] Parallel video + audio downloads
- [x] Byte-rate throttling (`--limit-rate`) alongside request-rate (`--rate`)
- [x] Segment-level HTTP Range resume for interrupted `.part` files
- [x] `--doctor` self-check, `--batch` mode, `--estimate`, `--json`
- [x] Per-output lock files and post-merge duration verification
- [x] Proxy support (`--proxy` + env vars)
- [x] Signed-URL / cookie-necessity detection with JWT expiry checking
- [x] Cookie-extraction diagnostics: profile-location checks (XDG/Snap/Flatpak) with symlink fixes

## 🤝 Contributing

Issues and PRs welcome. Good first targets: any `EXT-X` tag the parser doesn't know about yet (open an issue with a sanitized playlist snippet), and reproductions of tricky anti-bot configurations for the diagnostics hints. On the CENC side, edge cases like `saiz`/`saio` in-band IVs, `sbgp`/`sgpd` per-group crypt overrides, and the `cens` scheme are known gaps — sanitized fMP4 samples are welcome. Please keep examples in issues sanitized — no tokens, cookies, or credentials.

## 📜 License

<!-- Pick a license (MIT/Apache-2.0 are common for CLI tools), add a LICENSE file,
     then swap this section and the badge above:
     [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) -->

Distributed under the MIT License.

## 🚨 Disclaimer

> [!CAUTION]
> This tool only fetches and reassembles segments from a playlist URL you provide. You're responsible for using it in accordance with the terms of service and copyright of whatever you point it at — including published bot policies, rate limits, and access rules. Identify your client honestly, pace your requests, and honor contact addresses when a site offers them.
