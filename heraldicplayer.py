#!/usr/bin/env python3
"""
Heraldic Player  -  YouTube player with a pygame GUI.
Local mode. Song/video/podcast filters. Downloads with ffmpeg.
Style: medieval heraldry on black and white with gold accents.
No accounts: uses the system user.
Random welcome message at startup.
Search using YouTube's ranking (several strategies).

Works as a script (.py, installs dependencies on its own) and as a
PyInstaller .exe (everything bundled; only auto-installs VLC via winget).
"""
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as url_quote, urlparse, urlunparse

# ---------------- anti fork-bomb ----------------
# In a PyInstaller .exe, sys.executable is the .exe itself, not Python:
# "sys.executable -m pip ..." would launch another copy of the app in a loop.
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    import multiprocessing
    multiprocessing.freeze_support()

try:
    _DEPTH = int(os.environ.get("HP_DEPTH", "0"))
except ValueError:
    _DEPTH = 0
if _DEPTH >= 2:
    # emergency brake: never more than one chained relaunch
    sys.exit(0)


class _NullIO:
    def write(self, *a, **k): pass
    def flush(self, *a, **k): pass
    def read(self, *a, **k): return ""
    def readline(self, *a, **k): return ""
    def isatty(self): return False


if sys.stdout is None:
    sys.stdout = _NullIO()
if sys.stderr is None:
    sys.stderr = _NullIO()
if sys.stdin is None:
    sys.stdin = _NullIO()


def _run(cmd):
    try:
        return subprocess.call(cmd) == 0
    except Exception:
        return False


def _pip_install(pkg):
    if FROZEN:
        return False
    print(f"  [auto] pip install {pkg}")
    base = [sys.executable, "-m", "pip", "install", "-q", pkg]
    return (_run(base) or _run(base + ["--user"])
            or _run(base + ["--break-system-packages"]))


def _install_vlc():
    if os.name == "nt":
        if shutil.which("winget"):
            return _run(["winget", "install", "-e", "--id", "VideoLAN.VLC",
                         "--accept-source-agreements", "--accept-package-agreements"])
    elif sys.platform == "darwin":
        if shutil.which("brew"):
            return _run(["brew", "install", "--cask", "vlc"])
    else:
        sudo = [] if (hasattr(os, "geteuid") and os.geteuid() == 0) else ["sudo"]
        for mgr, args in (("apt-get", ["install", "-y"]),
                          ("dnf", ["install", "-y"]),
                          ("pacman", ["-S", "--noconfirm"])):
            if shutil.which(mgr):
                return _run(sudo + [mgr] + args + ["vlc"])
    return False


def _pause_exit(code=1):
    try:
        if isinstance(sys.stdin, _NullIO):
            sys.exit(code)
        input("\n  press enter to exit...")
    except Exception:
        pass
    sys.exit(code)


def _restart():
    env = dict(os.environ, HP_RESTARTED="1", HP_DEPTH=str(_DEPTH + 1))
    if FROZEN:
        cmd = [sys.executable] + sys.argv[1:]
    else:
        cmd = [sys.executable] + sys.argv
    sys.exit(subprocess.call(cmd, env=env))


def _ensure_deps():
    import importlib
    import importlib.util
    if not FROZEN:
        changed = False
        for mod, pkg in (("yt_dlp", "yt-dlp"), ("vlc", "python-vlc"),
                         ("PIL", "pillow"), ("pygame", "pygame"),
                         ("numpy", "numpy"),
                         ("imageio_ffmpeg", "imageio-ffmpeg")):
            if importlib.util.find_spec(mod) is None:
                if not _pip_install(pkg):
                    print(f"  [warn] {pkg} failed")
                else:
                    changed = True
        if changed and not os.environ.get("HP_RESTARTED"):
            _restart()
        importlib.invalidate_caches()
    try:
        import yt_dlp  # noqa
    except Exception as e:
        print(f"yt-dlp: {e}")
        _pause_exit()
    try:
        import vlc as _v
        _v.Instance("--no-video", "--quiet").release()
    except Exception:
        if not os.environ.get("HP_VLC_TRIED"):
            os.environ["HP_VLC_TRIED"] = "1"
            if _install_vlc() and not os.environ.get("HP_RESTARTED"):
                _restart()
        print("VLC did not load. Install it from https://www.videolan.org/vlc/")
        _pause_exit()


_ensure_deps()
import yt_dlp  # noqa
import vlc     # noqa
from PIL import Image  # noqa
import pygame  # noqa


# ---------------- clipboard ----------------
_CLIP = {"root": None, "tried_tk": False}


def _clipboard_get():
    if not _CLIP["tried_tk"]:
        _CLIP["tried_tk"] = True
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            _CLIP["root"] = root
        except Exception:
            _CLIP["root"] = None
    if _CLIP["root"] is not None:
        try:
            return _CLIP["root"].clipboard_get() or ""
        except Exception:
            pass
    try:
        pygame.scrap.init()
        data = pygame.scrap.get(pygame.SCRAP_TEXT)
        if data:
            return data.decode("utf-8", "ignore").replace("\x00", "")
    except Exception:
        pass
    return ""


def _clipboard_set(text):
    if _CLIP["root"] is not None:
        try:
            _CLIP["root"].clipboard_clear()
            _CLIP["root"].clipboard_append(text)
            _CLIP["root"].update()
            return True
        except Exception:
            pass
    try:
        pygame.scrap.init()
        pygame.scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8"))
        return True
    except Exception:
        return False


def _clean_clip(s):
    return "".join(ch for ch in s if ch.isprintable() and ch not in "\r\n")


def _resolve_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _resolve_ffmpeg()

DATA = Path.home() / ".heraldic_player"
STORE = DATA / "profiles.json"
DL_DIR = Path.home() / "Music" / "HERALDIC_PLAYER"
PITCH_DIR = DATA / "pitched"
TMP_DIR = DATA / "tmp"
_SAVE_LOCK = threading.Lock()

# Flags to avoid opening a console window on Windows (ffmpeg and friends)
if os.name == "nt":
    _NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW",
                                           0x08000000)}
else:
    _NO_WINDOW = {}

BG        = (8, 8, 8)
BG_PANEL  = (18, 18, 18)
BG_SOFT   = (24, 24, 24)
BG_HOVER  = (32, 32, 32)
BG_SELECT = (48, 48, 48)
BORDER    = (40, 40, 40)
BORDER_HI = (100, 100, 100)
TEXT      = (235, 235, 235)
TEXT_MUT  = (140, 140, 140)
TEXT_DIM  = (82, 82, 82)
ACCENT    = (255, 255, 255)
ACCENT_SOFT = (190, 190, 190)
DANGER    = (200, 90, 90)
GREEN     = (120, 200, 120)
WARN      = (230, 180, 90)
ORO           = (198, 162, 76)
ORO_BRILLANTE = (240, 212, 130)
ORO_OSCURO    = (110, 88, 42)
ORO_MATE      = (150, 122, 58)


# ---------------- system user ----------------
def _system_username():
    import getpass
    name = ""
    try:
        name = getpass.getuser() or ""
    except Exception:
        name = ""
    if not name:
        name = (os.environ.get("USER")
                or os.environ.get("USERNAME")
                or os.environ.get("LOGNAME")
                or "guest")
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")
    name = name.strip()[:20] or "guest"
    return name


STARTUP_MESSAGES = [
    "Greetings, {user}. May the chords guide your day.",
    "The troubadour tunes his lute, {user}.",
    "Welcome back, {user}. The court awaits you.",
    "The trumpets announce your arrival, {user}.",
    "May music fill your tavern, {user}.",
    "The herald proclaims {user}, lord of chords.",
    "Prepare the banners, {user} has returned.",
    "A new day, a new song, {user}.",
    "The laurels await you, {user}.",
    "Tune your guitars, {user}.",
    "May the heraldic sun light your session, {user}.",
    "The castle gates open, {user}.",
    "The music hall receives you, {user}.",
    "Ride toward the rhythm, {user}.",
    "Your shield and lute, ready, {user}.",
]


def _pick_startup_message(user):
    msg = random.choice(STARTUP_MESSAGES)
    try:
        return msg.format(user=user)
    except Exception:
        return msg


def fmt(t):
    if t is None:
        return "--:--"
    t = int(max(0, t))
    return f"{t // 60:02d}:{t % 60:02d}"


def hms(t):
    t = int(max(0, t))
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}"


def new_profile():
    return {"favorites": [], "history": [], "sessions": 0, "tracks": 0,
            "seconds": 0, "last_login": None, "volume": 70, "downloads": 0,
            "saved_playlists": [], "recent_collections": [],
            "speed": 1.0, "pitch": 0}


def load_store():
    try:
        s = json.loads(STORE.read_text(encoding="utf-8"))
        s.setdefault("users", {})
        return s
    except Exception:
        return {"users": {}}


def save_store(store):
    with _SAVE_LOCK:
        DATA.mkdir(exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, STORE)


# ---------------- YouTube ----------------
_TYPE_LABEL = {"song": "song", "video": "video", "podcast": "podcast",
               "playlist": "playlist", "artist": "artist"}


def _classify(entry):
    ch = (entry.get("channel") or entry.get("uploader") or "").strip().lower()
    tl = (entry.get("title") or "").lower()
    dur = entry.get("duration") or 0
    if ch.endswith("- topic") or ch.endswith(" - topic") or "vevo" in ch:
        return "song"
    if "podcast" in tl or "episodio" in tl or "episode" in tl:
        return "podcast"
    if dur and dur >= 40 * 60:
        return "podcast"
    return "video"


def _entry_to_track(e):
    t = {"id": e.get("id"),
         "title": e.get("title") or "?",
         "artist": e.get("channel") or e.get("uploader") or "?",
         "duration": e.get("duration")}
    t["ctype"] = _classify(t)
    return t


# ---- Search engine -------------------------------------------------
# Strategies (in order), all with the same ranking as the YouTube website:
#   1) yt-dlp over the real results URL (with "sp" properly encoded
#      and limited to n results; it used to page without limit and hang)
#   2) our own InnerTube request (urllib), independent of yt-dlp
#   3) scraping of the results page (ytInitialData)
#   4) ytsearchN: from yt-dlp (only for songs/videos)
# If everything fails, a visible error is raised and saved to search_error.log

# "sp" parameter ALREADY DECODED (in the URL it goes as %3D%3D, only once)
SP_VIDEOS = "EgIQAQ=="
SP_PLAYLISTS = "EgIQAw=="
SP_CHANNELS = "EgIQAg=="

_YT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# In-memory search cache (to avoid repeating identical queries)
_SEARCH_CACHE = {}
_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE_MAX = 40

_YTDLP_UPDATE = {"started": False, "notify": None}


def _log_search_error(text):
    try:
        DATA.mkdir(exist_ok=True)
        with open(DATA / "search_error.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except Exception:
        pass


def _short_err(e):
    s = str(e).replace("\n", " ").replace("ERROR: ", "").strip()
    return (type(e).__name__ + ": " + s)[:200]


def _schedule_ytdlp_update():
    """yt-dlp tends to break when YouTube changes: we update it once.
    In the .exe it cannot be done (no pip): we warn to rebuild."""
    if _YTDLP_UPDATE["started"]:
        return
    _YTDLP_UPDATE["started"] = True
    cb = _YTDLP_UPDATE.get("notify")
    if FROZEN:
        if cb:
            cb("yt-dlp outdated: rebuild the exe with a newer yt-dlp", 12)
        return
    if cb:
        cb("yt-dlp seems outdated, updating...", 8)

    def work():
        base = [sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"]
        ok = (_run(base) or _run(base + ["--user"])
              or _run(base + ["--break-system-packages"]))
        cb2 = _YTDLP_UPDATE.get("notify")
        if cb2:
            cb2("yt-dlp updated: restart the app" if ok else
                "could not update yt-dlp (pip install -U yt-dlp)", 12)
    threading.Thread(target=work, daemon=True).start()


def _results_url(q, sp):
    return ("https://www.youtube.com/results?search_query="
            + url_quote(q, safe="") + "&sp=" + url_quote(sp, safe=""))


def _ytdlp_flat(url, n):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "playlistend": n,
            "socket_timeout": 15, "retries": 2, "extractor_retries": 2}
    with yt_dlp.YoutubeDL(opts) as y:
        r = y.extract_info(url, download=False)
    if not r:
        return []
    return [e for e in (r.get("entries") or []) if e]


def _convert_ytdlp(entries, want, n):
    out = []
    for e in entries:
        u = e.get("url") or ""
        if want == "video":
            if not e.get("id"):
                continue
            ie = (e.get("ie_key") or "").lower()
            if "playlist" in ie or "channel" in ie or "tab" in ie:
                continue
            t = _entry_to_track(e)
            if not t.get("title") or t["title"] == "?":
                continue
            out.append(t)
        elif want == "playlist":
            if "list=" not in u:
                continue
            out.append({
                "ctype": "playlist",
                "id": e.get("id") or u,
                "title": e.get("title") or "?",
                "artist": e.get("channel") or e.get("uploader") or "",
                "duration": None,
                "count": e.get("playlist_count"),
                "url": u,
            })
        else:
            if not (("channel/" in u) or
                    u.rstrip("/").split("/")[-1].startswith("@")):
                continue
            out.append({
                "ctype": "artist",
                "id": e.get("id") or u,
                "title": e.get("title") or "?",
                "artist": e.get("channel") or e.get("uploader") or "",
                "duration": None,
                "url": u,
            })
        if len(out) >= n:
            break
    return out


# ---- our own search (without yt-dlp) ----
def _yt_text(x):
    if not x:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        if x.get("simpleText"):
            return x["simpleText"]
        if x.get("content"):
            return x["content"]
        return "".join(r.get("text", "") for r in x.get("runs", []))
    return ""


def _parse_dur(s):
    try:
        secs = 0
        for p in (s or "").split(":"):
            secs = secs * 60 + int(p)
        return secs if secs > 0 else None
    except Exception:
        return None


def _walk_renderers(node, found):
    if isinstance(node, dict):
        for k in ("videoRenderer", "playlistRenderer",
                  "channelRenderer", "lockupViewModel"):
            v = node.get(k)
            if isinstance(v, dict):
                found.append((k, v))
        for v in node.values():
            _walk_renderers(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_renderers(v, found)


def _renderer_to_item(kind, r, want):
    if kind == "videoRenderer" and want == "video":
        vid = r.get("videoId")
        title = _yt_text(r.get("title"))
        if not vid or not title:
            return None
        chan = (_yt_text(r.get("ownerText"))
                or _yt_text(r.get("longBylineText"))
                or _yt_text(r.get("shortBylineText")))
        return _entry_to_track({
            "id": vid, "title": title, "channel": chan,
            "duration": _parse_dur(_yt_text(r.get("lengthText")))})
    if kind == "playlistRenderer" and want == "playlist":
        pid = r.get("playlistId")
        title = _yt_text(r.get("title"))
        if not pid or not title:
            return None
        cnt = str(r.get("videoCount") or "").replace(".", "").replace(",", "")
        return {"ctype": "playlist", "id": pid, "title": title,
                "artist": _yt_text(r.get("shortBylineText")),
                "duration": None,
                "count": int(cnt) if cnt.isdigit() else None,
                "url": "https://www.youtube.com/playlist?list=" + pid}
    if kind == "lockupViewModel" and want == "playlist":
        if r.get("contentType") != "LOCKUP_CONTENT_TYPE_PLAYLIST":
            return None
        pid = r.get("contentId")
        meta = (r.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        title = _yt_text(meta.get("title"))
        if not pid or not title:
            return None
        return {"ctype": "playlist", "id": pid, "title": title,
                "artist": "", "duration": None, "count": None,
                "url": "https://www.youtube.com/playlist?list=" + pid}
    if kind == "channelRenderer" and want == "channel":
        cid = r.get("channelId")
        title = _yt_text(r.get("title"))
        if not cid or not title:
            return None
        base = (((r.get("navigationEndpoint") or {})
                 .get("browseEndpoint") or {}).get("canonicalBaseUrl") or "")
        url = ("https://www.youtube.com" + base if base.startswith("/")
               else "https://www.youtube.com/channel/" + cid)
        return {"ctype": "artist", "id": cid, "title": title,
                "artist": "", "duration": None, "url": url}
    return None


def _items_from_json(data, want, n):
    found = []
    _walk_renderers(data, found)
    out, seen = [], set()
    for kind, r in found:
        it = _renderer_to_item(kind, r, want)
        if not it or it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
        if len(out) >= n:
            break
    return out


def _innertube_search(q, sp, want, n):
    body = {"context": {"client": {"clientName": "WEB",
                                   "clientVersion": "2.20250901.00.00",
                                   "hl": "en"}},
            "query": q, "params": sp}
    req = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
        data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": _YT_UA,
                 "Content-Type": "application/json",
                 "Origin": "https://www.youtube.com",
                 "Accept-Language": "en-US,en;q=0.9,es;q=0.5",
                 "Cookie": "CONSENT=YES+1; SOCS=CAI"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return _items_from_json(data, want, n)


def _scrape_search(q, sp, want, n):
    req = urllib.request.Request(
        _results_url(q, sp),
        headers={"User-Agent": _YT_UA,
                 "Accept-Language": "en-US,en;q=0.9,es;q=0.5",
                 "Cookie": "CONSENT=YES+1; SOCS=CAI"})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", "replace")
    marker = "var ytInitialData = "
    i = html.find(marker)
    if i < 0:
        raise RuntimeError("the page has no results (consent?)")
    data, _ = json.JSONDecoder().raw_decode(html[i + len(marker):])
    return _items_from_json(data, want, n)


def _native_search(q, sp, want, n):
    errs = []
    for fn in (_innertube_search, _scrape_search):
        try:
            res = fn(q, sp, want, n)
            if res:
                return res
        except Exception as e:
            errs.append(fn.__name__.strip("_") + ": " + _short_err(e))
    if errs:
        raise RuntimeError(" | ".join(errs))
    return []


def _search_generic(q, sp, want, n):
    errors = []
    out = []
    ytdlp_failed = False
    try:
        out = _convert_ytdlp(_ytdlp_flat(_results_url(q, sp), n), want, n)
    except Exception as e:
        ytdlp_failed = True
        errors.append("yt-dlp: " + _short_err(e))
    if not out:
        try:
            out = _native_search(q, sp, want, n)
            if out and ytdlp_failed:
                _schedule_ytdlp_update()
        except Exception as e:
            errors.append(_short_err(e))
    if not out and want == "video":
        try:
            out = _convert_ytdlp(_ytdlp_flat(f"ytsearch{n}:{q}", n),
                                 "video", n)
        except Exception as e:
            errors.append("ytsearch: " + _short_err(e))
    if not out and errors:
        msg = " | ".join(errors)
        _log_search_error(f"q={q!r} type={want}: {msg}")
        raise RuntimeError(msg)
    return out


def search_ytdlp(q, n=40):
    """Search songs/videos using YouTube's web ranking.
    In-memory cache only when there are results."""
    q = (q or "").strip()
    if not q:
        return []
    key = (q.lower(), n)
    with _SEARCH_CACHE_LOCK:
        cached = _SEARCH_CACHE.get(key)
    if cached:
        return list(cached)
    out = _search_generic(q, SP_VIDEOS, "video", n)
    if out:
        with _SEARCH_CACHE_LOCK:
            _SEARCH_CACHE[key] = list(out)
            if len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
                for k in list(_SEARCH_CACHE.keys())[:_SEARCH_CACHE_MAX // 2]:
                    _SEARCH_CACHE.pop(k, None)
    return out


def search_playlists(q, n=30):
    return _search_generic(q, SP_PLAYLISTS, "playlist", n)


def search_artists(q, n=30):
    return _search_generic(q, SP_CHANNELS, "channel", n)


def _channel_videos_url(url):
    p = urlparse(url)
    path = p.path.rstrip("/")
    if not path.endswith("/videos"):
        path += "/videos"
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


def load_url(url, limit=200):
    opts = {"quiet": True, "extract_flat": True, "skip_download": True,
            "playlistend": limit, "socket_timeout": 15, "retries": 2}
    with yt_dlp.YoutubeDL(opts) as y:
        r = y.extract_info(url, download=False)
    entries = r.get("entries") or [r]
    return [_entry_to_track(e) for e in entries if e and e.get("id")]


def resolve(video_id):
    opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best",
            "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info("https://www.youtube.com/watch?v=" + video_id,
                              download=False)
    return info["url"], info


def fetch_thumbnail_pil(video_id):
    for url in (f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                f"https://i.ytimg.com/vi/{video_id}/default.jpg"):
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                img = Image.open(io.BytesIO(r.read())).convert("RGB")
                return img.convert("L").convert("RGB")
        except Exception:
            continue
    return None


def download_track(track, outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    codec = "mp3" if FFMPEG else "m4a"
    opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best",
            "outtmpl": str(outdir / "%(title)s.%(ext)s"), "noplaylist": True}
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                   "preferredcodec": codec,
                                   "preferredquality": "192"}]
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([f"https://www.youtube.com/watch?v={track['id']}"])
    return codec


# ---------------- pitch / speed ----------------
class PitchProcessor:
    CACHE_LIMIT = 25
    CACHE_MAX_MB = 250
    SR = 48000
    BITRATE = "128k"

    def __init__(self):
        self._rb_ok = None
        self._codec_ext, self._codec_args = self._pick_codec()
        PITCH_DIR.mkdir(parents=True, exist_ok=True)
        self.purge_all()

    def _pick_codec(self):
        try:
            out = subprocess.run(
                [FFMPEG, "-hide_banner", "-codecs"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=8, **_NO_WINDOW).stdout.decode("utf-8", "ignore")
        except Exception:
            out = ""
        if "libopus" in out:
            return "opus", ["-c:a", "libopus", "-b:a", self.BITRATE]
        if "libvorbis" in out:
            return "ogg", ["-c:a", "libvorbis", "-q:a", "4"]
        return "m4a", ["-c:a", "aac", "-b:a", "160k"]

    def _has_rubberband(self):
        if self._rb_ok is not None:
            return self._rb_ok
        try:
            out = subprocess.run([FFMPEG, "-hide_banner", "-filters"],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 timeout=8,
                                 **_NO_WINDOW).stdout.decode("utf-8", "ignore")
            self._rb_ok = "rubberband" in out
        except Exception:
            self._rb_ok = False
        return self._rb_ok

    def _src_path(self, vid):
        return PITCH_DIR / f"{vid}.src.{self._codec_ext}"

    def _out_path(self, vid, semitones):
        return PITCH_DIR / f"{vid}_p{semitones:+d}.{self._codec_ext}"

    def _clean_cache(self):
        try:
            files = sorted(PITCH_DIR.glob(f"*.{self._codec_ext}"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[self.CACHE_LIMIT:]:
                try:
                    f.unlink()
                except Exception:
                    pass
            files = sorted(PITCH_DIR.glob(f"*.{self._codec_ext}"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            total = 0
            for p in files:
                try:
                    total += p.stat().st_size
                except Exception:
                    pass
            limit = self.CACHE_MAX_MB * 1024 * 1024
            for f in files:
                if total <= limit:
                    break
                try:
                    sz = f.stat().st_size
                    f.unlink()
                    total -= sz
                except Exception:
                    pass
        except Exception:
            pass

    def _cache_stats(self):
        n = 0
        mb = 0.0
        try:
            for p in PITCH_DIR.glob(f"*.{self._codec_ext}"):
                try:
                    n += 1
                    mb += p.stat().st_size / (1024 * 1024)
                except Exception:
                    pass
        except Exception:
            pass
        return n, mb

    def ensure(self, vid, url, semitones):
        if not FFMPEG or semitones == 0:
            return None
        out = self._out_path(vid, semitones)
        if out.exists():
            return out
        src = self._src_path(vid)
        if not src.exists():
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "quiet",
                   "-y", "-nostdin",
                   "-user_agent", "Mozilla/5.0",
                   "-i", url, "-vn",
                   "-ac", "2", "-ar", str(self.SR),
                   *self._codec_args,
                   str(src)]
            try:
                r = subprocess.run(cmd, timeout=180, **_NO_WINDOW)
                if r.returncode != 0 or not src.exists():
                    return None
            except Exception:
                return None
        factor = 2.0 ** (semitones / 12.0)
        if self._has_rubberband():
            af = f"rubberband=pitch={factor:.6f}"
        else:
            af = (f"asetrate={self.SR}*{factor:.6f},"
                  f"aresample={self.SR},atempo={1.0 / factor:.6f}")
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "quiet",
               "-y", "-nostdin",
               "-i", str(src), "-af", af,
               "-ac", "2", "-ar", str(self.SR),
               *self._codec_args,
               str(out)]
        try:
            r = subprocess.run(cmd, timeout=120, **_NO_WINDOW)
            if r.returncode != 0 or not out.exists():
                return None
        except Exception:
            return None
        self._clean_cache()
        return out

    def purge(self, vid):
        for p in PITCH_DIR.glob(f"{vid}*"):
            try:
                p.unlink()
            except Exception:
                pass

    def purge_all(self):
        for p in PITCH_DIR.glob("*"):
            try:
                p.unlink()
            except Exception:
                pass


# ---------------- equalizer ----------------
class EQAnalyzer:
    N_BANDS = 40
    SAMPLE_RATE = 8000
    CHUNK = 1024

    def __init__(self):
        self.bands = [0.0] * self.N_BANDS
        self._lock = threading.Lock()
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self.enabled = False

    def get(self):
        with self._lock:
            return list(self.bands)

    def start(self, url, start_seconds=0.0):
        self.stop()
        if not FFMPEG or not url:
            return
        try:
            import numpy  # noqa
        except Exception:
            self.enabled = False
            return
        self.enabled = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(url, float(start_seconds or 0.0)),
            daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
        with self._lock:
            self.bands = [0.0] * self.N_BANDS

    def _run(self, url, start_seconds):
        try:
            import numpy as np
        except Exception:
            return
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "quiet", "-nostdin"]
        if start_seconds > 1.0:
            cmd += ["-ss", f"{start_seconds:.2f}"]
        cmd += ["-user_agent", "Mozilla/5.0",
                "-i", url, "-vn", "-f", "s16le", "-ac", "1",
                "-ar", str(self.SAMPLE_RATE), "-"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, **_NO_WINDOW)
        except Exception:
            return
        n = self.CHUNK
        bps = 2
        window = np.hanning(n).astype(np.float32)
        freqs = np.fft.rfftfreq(n, d=1.0 / self.SAMPLE_RATE)
        edges = np.logspace(np.log10(50), np.log10(3500), self.N_BANDS + 1)
        masks = [(freqs >= edges[i]) & (freqs < edges[i + 1])
                 for i in range(self.N_BANDS)]
        smoothed = np.zeros(self.N_BANDS, dtype=np.float32)
        peak = 1e-6
        try:
            while not self._stop.is_set():
                data = self._proc.stdout.read(n * bps)
                if not data or len(data) < n * bps:
                    break
                samples = (np.frombuffer(data, dtype=np.int16)
                           .astype(np.float32) / 32768.0)
                spec = np.abs(np.fft.rfft(samples * window))
                vals = np.zeros(self.N_BANDS, dtype=np.float32)
                for i, m in enumerate(masks):
                    if m.any():
                        vals[i] = float(spec[m].mean())
                mx = float(vals.max())
                peak = mx if mx > peak else peak * 0.995 + mx * 0.005
                if peak <= 1e-9:
                    peak = 1e-6
                norm = np.clip(vals / peak, 0.0, 1.0)
                norm = np.power(norm, 0.6) * 20.0
                smoothed = smoothed * 0.55 + norm * 0.45
                with self._lock:
                    self.bands = [float(x) for x in smoothed]
        except Exception:
            pass
        finally:
            try:
                if self._proc:
                    self._proc.wait(timeout=1)
            except Exception:
                pass


# ---------------- audio ----------------
class Audio:
    def __init__(self, say, on_play):
        self.inst = vlc.Instance("--no-video", "--quiet")
        self.mp = self.inst.media_player_new()
        self.queue, self.idx = [], -1
        self.loading = self.paused = self.playing = False
        self.shuffle, self.repeat = False, "off"
        self.volume = 70
        self.gen = 0
        self.started_at = 0.0
        self.failed = False
        self.fails = 0
        self.say, self.on_play = say, on_play
        self.eq = EQAnalyzer()
        self.pitch_proc = PitchProcessor()
        self.current_url = None
        self.speed = 1.0
        self.pitch = 0

    @property
    def cur(self):
        return (self.queue[self.idx]
                if 0 <= self.idx < len(self.queue) else None)

    def play_index(self, i):
        if not (0 <= i < len(self.queue)):
            return
        self.idx = i
        self.gen += 1
        self.loading, self.playing, self.paused = True, False, False
        self.mp.stop()
        self.eq.stop()
        threading.Thread(target=self._resolve,
                         args=(self.queue[i], self.gen),
                         daemon=True).start()

    def _resolve(self, t, g):
        try:
            url, info = resolve(t["id"])
        except Exception as e:
            if g == self.gen:
                self.loading = False
                self.failed = True
                self.say("Error: " + str(e).replace("\n", " ")[:70])
            return
        if g != self.gen:
            return
        if not t.get("duration"):
            t["duration"] = info.get("duration")
        play_url = url
        if self.pitch != 0:
            n, mb = self.pitch_proc._cache_stats()
            self.say(f"processing pitch {self.pitch:+d}... "
                     f"(cache: {n} files, {mb:.0f} MB)")
            path = self.pitch_proc.ensure(t["id"], url, self.pitch)
            if g != self.gen:
                return
            if path:
                play_url = Path(path).as_uri()
            else:
                self.say("could not apply pitch, playing original")
        self.current_url = play_url
        self.mp.set_media(self.inst.media_new(play_url))
        self.mp.play()
        self.mp.audio_set_volume(self.volume)
        try:
            self.mp.set_rate(self.speed)
        except Exception:
            pass
        self.fails = 0
        self.loading, self.playing = False, True
        self.started_at = time.time()
        self.eq.start(play_url, 0.0)
        self.on_play(t)

    def tick(self):
        if self.failed:
            self.failed = False
            self.fails += 1
            if self.fails < 3:
                self.next(auto=True, skip=True)
            else:
                self.stop()
            return
        if (self.playing and not self.loading and
                time.time() - self.started_at > 2):
            st = self.mp.get_state()
            if st == vlc.State.Ended:
                self.next(auto=True)
            elif st == vlc.State.Error:
                self.failed = True

    def next(self, auto=False, skip=False):
        if not self.queue:
            return
        if self.repeat == "one" and auto and not skip:
            self.play_index(self.idx)
            return
        if self.shuffle and len(self.queue) > 1:
            i = random.choice([x for x in range(len(self.queue))
                               if x != self.idx])
        else:
            i = self.idx + 1
        if i >= len(self.queue):
            if self.repeat == "all":
                i = 0
            else:
                self.stop()
                return
        self.play_index(i)

    def prev(self):
        if not self.queue:
            return
        if self.pos() > 3:
            self.mp.set_time(0)
            if self.current_url:
                self.eq.start(self.current_url, 0.0)
        else:
            self.play_index(max(0, self.idx - 1))

    def stop(self):
        self.gen += 1
        self.mp.stop()
        self.eq.stop()
        self.loading = self.playing = self.paused = False

    def toggle_pause(self):
        if self.playing:
            self.mp.pause()
            self.paused = not self.paused
            if self.paused:
                self.eq.stop()
            else:
                if self.current_url:
                    self.eq.start(self.current_url, self.pos())

    def set_volume(self, d):
        self.volume = max(0, min(100, self.volume + d))
        self.mp.audio_set_volume(self.volume)

    def pos(self):
        if not self.playing:
            return 0
        return max(0, self.mp.get_time() / 1000)

    def seek(self, delta):
        if not self.playing:
            return
        dur = (self.cur or {}).get("duration")
        t = max(0, self.mp.get_time() + delta * 1000)
        if dur:
            t = min(t, dur * 1000 - 800)
        self.mp.set_time(int(t))
        if self.current_url and not self.paused:
            self.eq.start(self.current_url, t / 1000.0)

    def seek_to(self, seconds):
        if not self.playing:
            return
        dur = (self.cur or {}).get("duration")
        t = max(0, seconds * 1000)
        if dur:
            t = min(t, dur * 1000 - 800)
        self.mp.set_time(int(t))
        if self.current_url and not self.paused:
            self.eq.start(self.current_url, t / 1000.0)

    def set_speed(self, d):
        old = self.speed
        self.speed = max(0.5, min(2.0, round(self.speed + d, 2)))
        if self.speed == old:
            return
        try:
            self.mp.set_rate(self.speed)
        except Exception:
            pass
        self.say(f"speed x{self.speed:.2f}")

    def set_speed_abs(self, v):
        self.speed = max(0.5, min(2.0, round(v, 2)))
        try:
            self.mp.set_rate(self.speed)
        except Exception:
            pass
        self.say(f"speed x{self.speed:.2f}")

    def set_pitch(self, d):
        old = self.pitch
        self.pitch = max(-12, min(12, self.pitch + d))
        if self.pitch == old:
            return
        self.say(f"pitch {self.pitch:+d} semitones")
        if self.playing and self.cur:
            pos = self.pos()
            self.play_index(self.idx)
            threading.Thread(target=self._seek_later, args=(pos,),
                             daemon=True).start()

    def _seek_later(self, pos):
        time.sleep(0.8)
        if self.playing:
            self.seek_to(pos)


# ---------------- state ----------------
class App:
    def __init__(self, user, store):
        self.user, self.store = user, store
        self.profile = store["users"][user]
        self.profile.setdefault("saved_playlists", [])
        self.profile.setdefault("recent_collections", [])
        self.profile.setdefault("speed", 1.0)
        self.profile.setdefault("pitch", 0)
        self.display = user
        self.startup_msg = ""
        self.audio = Audio(self.say, self.on_play)
        _YTDLP_UPDATE["notify"] = self.say
        self.audio.volume = self.profile.get("volume", 70)
        self.audio.speed = self.profile.get("speed", 1.0)
        self.audio.pitch = self.profile.get("pitch", 0)
        self.mode = "main"
        self.buf = ""
        self.msg, self.msg_t, self.msg_dur = "", 0, 4
        self.played = 0
        self.running = True
        self.filter = "all"
        self.search_mode = "tracks"
        self.view_stack = []
        self.view = {"kind": "library", "items": [], "cur": 0,
                     "loading": False, "title": ""}
        self.downloading = False
        self.home_mode = False
        self.home_scroll = 0
        self.suggestions = []
        self._refresh_suggestions()
        self.focus = "list"
        self.queue_cur = -1
        self.show_speedpitch = True

    def say(self, text, dur=4):
        self.msg, self.msg_t, self.msg_dur = text, time.time(), dur

    def save(self):
        self.profile["volume"] = self.audio.volume
        self.profile["speed"] = self.audio.speed
        self.profile["pitch"] = self.audio.pitch
        save_store(self.store)

    def on_play(self, t):
        self.played += 1
        self.queue_cur = self.audio.idx
        self.profile["tracks"] = self.profile.get("tracks", 0) + 1
        h = [x for x in self.profile["history"] if x["id"] != t["id"]]
        h.insert(0, {k: t.get(k) for k in
                     ("id", "title", "artist", "duration", "ctype")})
        self.profile["history"] = h[:200]
        self.save()

    def is_fav(self, t):
        return bool(t) and any(f["id"] == t["id"]
                               for f in self.profile["favorites"])

    def toggle_fav(self, t):
        if not t or "id" not in t or t.get("ctype") in ("playlist", "artist"):
            return
        if self.is_fav(t):
            self.profile["favorites"] = [
                f for f in self.profile["favorites"] if f["id"] != t["id"]]
        else:
            self.profile["favorites"].append(
                {k: t.get(k) for k in
                 ("id", "title", "artist", "duration", "ctype")})
        self.save()

    def _refresh_suggestions(self):
        seen = set()
        pool = []
        for t in self.profile["favorites"]:
            if t["id"] not in seen:
                seen.add(t["id"])
                pool.append(t)
        for t in self.profile["history"][:80]:
            if t["id"] not in seen:
                seen.add(t["id"])
                pool.append(t)
        random.shuffle(pool)
        self.suggestions = pool[:30]

    def toggle_home(self):
        self.home_mode = not self.home_mode
        if self.home_mode:
            self._refresh_suggestions()
            self.home_scroll = 0

    # ---- saved ----
    def saved_list(self):
        return self.profile.get("saved_playlists", [])

    def find_saved(self, sp_id):
        for sp in self.saved_list():
            if sp.get("id") == sp_id:
                return sp
        return None

    def is_saved_playlist(self, sp_id):
        return self.find_saved(sp_id) is not None

    # ---- recent (auto-registered) ----
    def recent_list(self):
        return self.profile.get("recent_collections", [])

    def find_recent(self, rid):
        for r in self.recent_list():
            if r.get("id") == rid:
                return r
        return None

    def _record_recent(self, src):
        if not src:
            return
        ctype = src.get("ctype") or "playlist"
        if ctype not in ("playlist", "artist"):
            return
        rid = src.get("id") or src.get("url")
        if not rid:
            return
        recs = list(self.recent_list())
        recs = [r for r in recs if r.get("id") != rid]
        recs.insert(0, {
            "id": rid,
            "url": src.get("url") or "",
            "title": src.get("title") or "?",
            "artist": src.get("artist") or "",
            "ctype": ctype,
            "thumb": src.get("thumb"),
            "tracks": list(src.get("tracks", [])),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "opened_at": time.time(),
        })
        self.profile["recent_collections"] = recs[:40]
        self.save()

    def _update_recent(self, rid, items):
        if not rid or not items:
            return
        for r in self.recent_list():
            if r.get("id") == rid:
                r["tracks"] = items
                r["thumb"] = items[0]["id"] if items else None
                r["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                self.save()
                return

    def _remove_recent(self, rid):
        self.profile["recent_collections"] = [
            r for r in self.recent_list() if r.get("id") != rid]
        self.save()

    def clear_recent(self):
        n = len(self.recent_list())
        self.profile["recent_collections"] = []
        self.save()
        self.say(f"{n} recent lists deleted")

    def open_recent(self, rec):
        sp = self.find_saved(rec.get("id"))
        if sp:
            self.open_saved(sp)
            return
        self.push_view()
        self.view = {
            "kind": "collection",
            "items": [dict(t) for t in rec.get("tracks", [])],
            "cur": 0,
            "loading": not bool(rec.get("tracks")),
            "title": rec.get("title") or "?",
            "collection": rec.get("ctype") or "playlist",
            "source": {
                "id": rec["id"],
                "url": rec.get("url") or "",
                "title": rec.get("title") or "",
                "artist": rec.get("artist") or "",
                "ctype": rec.get("ctype") or "playlist",
            },
            "from_recent": rec["id"],
        }
        if rec.get("url"):
            threading.Thread(target=self._load_collection,
                             args=(self.view["source"],),
                             daemon=True).start()
            self.say("opened · reloading...")
        else:
            self.say("opened (no url)")

    # ---- save / remove ----
    def toggle_save_collection(self):
        if self.view.get("kind") != "collection":
            self.say("open a playlist or channel first")
            return
        src = self.view.get("source")
        if not src:
            self.say("cannot save")
            return
        sp_id = src.get("id") or src.get("url")
        if not sp_id:
            self.say("no id")
            return
        if self.is_saved_playlist(sp_id):
            self.profile["saved_playlists"] = [
                s for s in self.saved_list() if s.get("id") != sp_id]
            self.save()
            self.say("playlist removed from saved")
            return
        items = self.view.get("items") or []
        sp = {
            "id": sp_id,
            "url": src.get("url") or "",
            "title": src.get("title") or self.view.get("title") or "?",
            "artist": src.get("artist") or "",
            "ctype": src.get("ctype") or "playlist",
            "thumb": items[0]["id"] if items else None,
            "tracks": [dict(t) for t in items],
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self.profile.setdefault("saved_playlists", []).append(sp)
        self.save()
        self.say("saved · syncing with YT...")
        threading.Thread(target=self._sync_one, args=(sp_id,),
                         daemon=True).start()

    def _sync_one(self, sp_id):
        sp = self.find_saved(sp_id)
        if not sp or not sp.get("url"):
            return
        try:
            url = sp["url"]
            if sp.get("ctype") == "artist":
                url = _channel_videos_url(url)
            items = load_url(url)
            if not items:
                return
            sp["tracks"] = items
            sp["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            sp["thumb"] = items[0]["id"]
            self.save()
            if self.view.get("from_saved") == sp_id:
                self.view["items"] = [dict(t) for t in items]
            self._update_recent(sp_id, items)
        except Exception as e:
            self.say("sync err: " + str(e).replace("\n", " ")[:50])

    def sync_all_saved(self):
        ids = [s["id"] for s in self.saved_list()]
        if not ids:
            self.say("no saved playlists")
            return
        self.say(f"syncing {len(ids)} playlists...")

        def work():
            for i in ids:
                self._sync_one(i)
            self.say(f"{len(ids)} playlists synced")
        threading.Thread(target=work, daemon=True).start()

    def open_saved(self, sp):
        self.push_view()
        self.view = {
            "kind": "collection",
            "items": [dict(t) for t in sp.get("tracks", [])],
            "cur": 0, "loading": False,
            "title": sp.get("title") or "?",
            "collection": sp.get("ctype") or "playlist",
            "source": {
                "id": sp["id"],
                "url": sp.get("url") or "",
                "title": sp.get("title") or "",
                "artist": sp.get("artist") or "",
                "ctype": sp.get("ctype") or "playlist",
            },
            "from_saved": sp["id"],
        }
        self._record_recent(sp)
        threading.Thread(target=self._sync_one, args=(sp["id"],),
                         daemon=True).start()
        self.say("opened · syncing...")

    def remove_saved(self, sp_id):
        self.profile["saved_playlists"] = [
            s for s in self.saved_list() if s.get("id") != sp_id]
        self.save()

    def _filtered(self, items):
        if self.filter == "all" or not items:
            return list(items)
        first = items[0]
        if first.get("ctype") in ("playlist", "artist", "nav"):
            return list(items)
        return [x for x in items if x.get("ctype") == self.filter]

    def items(self):
        k = self.view["kind"]
        if k == "library":
            return [
                {"kind": "nav", "id": "favs", "name": "Favorites",
                 "count": len(self.profile["favorites"]), "ctype": "nav"},
                {"kind": "nav", "id": "history", "name": "History",
                 "count": len(self.profile["history"]), "ctype": "nav"},
                {"kind": "nav", "id": "recent",
                 "name": "Recent playlists",
                 "count": len(self.recent_list()), "ctype": "nav"},
                {"kind": "nav", "id": "saved", "name": "Saved playlists",
                 "count": len(self.saved_list()), "ctype": "nav"},
            ]
        if k == "favs":
            return self._filtered(self.profile["favorites"])
        if k == "history":
            return self._filtered(self.profile["history"])
        if k == "saved":
            return [
                {"ctype": "playlist", "id": s["id"],
                 "title": s.get("title") or "?",
                 "artist": s.get("artist") or "",
                 "url": s.get("url") or "",
                 "count": len(s.get("tracks", [])),
                 "_saved": True, "_thumb": s.get("thumb")}
                for s in self.saved_list()
            ]
        if k == "recent":
            return [
                {"ctype": r.get("ctype") or "playlist",
                 "id": r["id"],
                 "title": r.get("title") or "?",
                 "artist": r.get("artist") or "",
                 "url": r.get("url") or "",
                 "count": len(r.get("tracks", [])),
                 "_recent": True, "_thumb": r.get("thumb")}
                for r in self.recent_list()
            ]
        return self._filtered(self.view["items"])

    def goto(self, kind):
        self.view_stack = []
        self.view = {"kind": kind, "items": [], "cur": 0,
                     "loading": False, "title": ""}

    def push_view(self):
        self.view_stack.append({k: v for k, v in self.view.items()})

    def pop_view(self):
        if self.view_stack:
            self.view = self.view_stack.pop()
            return True
        return False

    def back(self):
        if not self.pop_view():
            if self.view["kind"] != "library":
                self.goto("library")

    def clamp(self):
        n = len(self.items())
        self.view["cur"] = max(0, min(self.view["cur"], max(0, n - 1)))

    def do_search(self, text):
        text = text.strip()
        if not text:
            return
        self.home_mode = False
        self.say("searching...")
        self.view_stack = []
        mode = self.search_mode
        self.view = {"kind": "results", "items": [], "cur": 0,
                     "loading": True, "title": text, "search_mode": mode}
        threading.Thread(target=self._search_thread,
                         args=(text, mode), daemon=True).start()

    def _maybe_record_url(self, text, mode):
        if not text or not text.startswith("http"):
            return
        is_playlist = "list=" in text
        is_channel = any(s in text for s in ("/channel/", "/@", "/c/", "/user/"))
        if not (is_playlist or is_channel):
            return
        ctype = "playlist" if is_playlist else "artist"
        title = text
        try:
            parsed = urlparse(text)
            qs = urllib.parse.parse_qs(parsed.query)
            if "list" in qs:
                title = qs["list"][0]
            elif is_channel:
                segs = [s for s in parsed.path.split("/") if s]
                if segs:
                    title = segs[-1]
        except Exception:
            pass
        rec = {
            "id": text,
            "url": text,
            "title": title,
            "artist": "",
            "ctype": ctype,
            "thumb": None,
            "tracks": [],
        }
        self._record_recent(rec)
        self.say("added to recent playlists")

    def _search_thread(self, text, mode):
        try:
            if mode == "playlists":
                res = search_playlists(text)
            elif mode == "artists":
                res = search_artists(text)
            elif mode == "links":
                res = load_url(text)
            else:
                res = (load_url(text) if text.startswith("http")
                       else search_ytdlp(text))
        except Exception as e:
            self.say("Search failed: "
                     + str(e).replace("\n", " ")[:150], 12)
            self.view["loading"] = False
            return
        if mode in ("tracks", "links"):
            self._maybe_record_url(text, mode)
            if res and text.startswith("http"):
                for r in self.recent_list():
                    if r.get("id") == text and r.get("title") in (None, "", text):
                        r["title"] = res[0].get("artist") or r["title"]
                        r["thumb"] = res[0]["id"]
                        r["tracks"] = res
                        self.save()
        if not res:
            self.say("no results")
            self.view["loading"] = False
            return
        self.view["items"] = res
        self.view["loading"] = False
        if mode == "tracks":
            songs = sum(1 for r in res if r["ctype"] == "song")
            vids = sum(1 for r in res if r["ctype"] == "video")
            pods = sum(1 for r in res if r["ctype"] == "podcast")
            self.say(f"{len(res)} results  ·  {songs}s {vids}v {pods}p")
        elif mode == "playlists":
            self.say(f"{len(res)} playlists")
        elif mode == "artists":
            self.say(f"{len(res)} artists")
        else:
            self.say(f"{len(res)} tracks from the link")

    def open_collection(self, item):
        self.push_view()
        kind = item.get("ctype")
        self.view = {"kind": "collection", "items": [], "cur": 0,
                     "loading": True,
                     "title": item.get("title") or "?", "collection": kind,
                     "source": {
                         "id": item.get("id"),
                         "url": item.get("url") or "",
                         "title": item.get("title") or "",
                         "artist": item.get("artist") or "",
                         "ctype": item.get("ctype") or "playlist",
                     }}
        self._record_recent(item)
        threading.Thread(target=self._load_collection, args=(item,),
                         daemon=True).start()
        if kind in ("playlist", "artist"):
            sp_id = item.get("id")
            if sp_id and not self.is_saved_playlist(sp_id):
                self.say("tip: press G or the shield to save it")

    def _load_collection(self, item):
        try:
            url = item.get("url")
            if not url:
                self.say("no url")
                self.view["loading"] = False
                return
            if item.get("ctype") == "artist":
                url = _channel_videos_url(url)
            items = load_url(url)
            if not items:
                self.say("empty")
                self.view["loading"] = False
                return
            self.view["items"] = items
            self.view["loading"] = False
            self.say(f"{len(items)} tracks")
            self._update_recent(item.get("id") or item.get("url"), items)
        except Exception as e:
            self.say("Error: " + str(e).replace("\n", " ")[:60])
            self.view["loading"] = False

    def download_current(self):
        t = self.audio.cur
        if not t:
            self.say("nothing playing")
            return
        self._download([t], t["title"][:40])

    def download_view(self):
        if self.view["kind"] == "library":
            self.say("open a playlist")
            return
        its = self.items()
        if not its:
            self.say("empty")
            return
        tracks = [x for x in its
                  if x.get("ctype") not in ("playlist", "artist", "nav")]
        if not tracks:
            self.say("nothing downloadable")
            return
        self._download(tracks, f"{len(tracks)} tracks")

    def _download(self, tracks, label):
        if self.downloading:
            self.say("a download is already in progress")
            return
        self.downloading = True
        self.say("downloading: " + label)

        def work():
            ok = 0
            try:
                for i, t in enumerate(tracks, 1):
                    self.say(f"[{i}/{len(tracks)}] {t['title'][:50]}")
                    try:
                        download_track(t, DL_DIR)
                        ok += 1
                        self.profile["downloads"] = (
                            self.profile.get("downloads", 0) + 1)
                    except Exception:
                        time.sleep(0.3)
                self.save()
                self.say(f"{ok}/{len(tracks)} to {DL_DIR}")
            finally:
                self.downloading = False
        threading.Thread(target=work, daemon=True).start()

    def activate(self):
        its = self.items()
        if not its:
            return
        self.clamp()
        v, a = self.view, self.audio
        i = v["cur"]
        e = its[i]
        if e.get("_saved"):
            sp = self.find_saved(e["id"])
            if sp:
                self.open_saved(sp)
            return
        if e.get("_recent"):
            rec = self.find_recent(e["id"])
            if rec:
                self.open_recent(rec)
            return
        if e.get("ctype") in ("playlist", "artist"):
            self.open_collection(e)
            return
        if v["kind"] == "library":
            if e.get("kind") == "nav":
                self.goto(e["id"])
            return
        if v["kind"] != "queue":
            tracks = [x for x in its
                      if x.get("ctype") not in ("playlist", "artist", "nav")]
            if not tracks:
                return
            try:
                play_idx = next(k for k, t in enumerate(tracks)
                                if t.get("id") == e.get("id"))
            except StopIteration:
                play_idx = 0
            a.queue[:] = [dict(t) for t in tracks]
            self.queue_cur = play_idx
            a.play_index(play_idx)
        else:
            a.play_index(i)

    def play_from_queue(self, i):
        self.queue_cur = i
        self.focus = "queue"
        self.audio.play_index(i)

    def add_selected(self):
        its = self.items()
        if self.view["kind"] == "library" or not its:
            return
        self.clamp()
        e = its[self.view["cur"]]
        if e.get("ctype") in ("playlist", "artist"):
            return
        a = self.audio
        was_empty = not a.queue
        a.queue.append(dict(e))
        if was_empty:
            a.play_index(0)

    def delete_selected(self):
        k = self.view["kind"]
        its = self.items()
        if not its:
            return
        if k not in ("favs", "history", "saved", "recent"):
            return
        self.clamp()
        rid = its[self.view["cur"]]["id"]
        if k == "favs":
            self.profile["favorites"] = [
                f for f in self.profile["favorites"] if f["id"] != rid]
        elif k == "history":
            self.profile["history"] = [
                f for f in self.profile["history"] if f["id"] != rid]
        elif k == "saved":
            self.remove_saved(rid)
        elif k == "recent":
            self._remove_recent(rid)
        self.save()
        self.clamp()


# ---------------- font helpers ----------------
def _font(size, bold=False):
    for name in ("Segoe UI", "Consolas", "DejaVu Sans", "Arial"):
        try:
            f = pygame.font.SysFont(name, size, bold=bold)
            if f:
                return f
        except Exception:
            continue
    return pygame.font.Font(None, size)


def _fit(font, text, max_w):
    if font.size(text)[0] <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if font.size(text[:mid] + "…")[0] <= max_w:
            lo = mid + 1
        else:
            hi = mid
    return text[: max(0, lo - 1)] + "…"


def _wrap(font, text, max_w, max_lines=2):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.size(test)[0] <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and len(" ".join(lines)) < len(text):
        lines[-1] = _fit(font, lines[-1] + "…", max_w)
    return lines[:max_lines]


# ================= MEDIEVAL HERALDRY =================
def draw_heraldic_sun(surf, cx, cy, r, color, rays=12, ray_len=None,
                      width=1, filled_core=False):
    if ray_len is None:
        ray_len = max(4, r // 2)
    if filled_core:
        pygame.draw.circle(surf, color, (cx, cy), max(1, r - 3))
    else:
        pygame.draw.circle(surf, color, (cx, cy), r, width)
        pygame.draw.circle(surf, color, (cx, cy), max(1, r - 4), width)
    for k in range(rays):
        a = k * 2 * math.pi / rays
        rl = ray_len if k % 2 == 0 else int(ray_len * 0.55)
        x1 = cx + math.cos(a) * (r + 2)
        y1 = cy + math.sin(a) * (r + 2)
        x2 = cx + math.cos(a) * (r + rl)
        y2 = cy + math.sin(a) * (r + rl)
        pa = a + math.pi / 2
        w = max(2, r // 7)
        px = math.cos(pa) * w
        py = math.sin(pa) * w
        pts = [(int(x1 + px), int(y1 + py)),
               (int(x2), int(y2)),
               (int(x1 - px), int(y1 - py))]
        pygame.draw.polygon(surf, color, pts, width)


def draw_crown(surf, cx, cy, w, color, points=3, width=1):
    h = int(w * 0.62)
    half = w // 2
    base_y = cy + h // 2
    top_y = cy - h // 2
    base_h = max(4, h // 4)
    pygame.draw.rect(surf, color,
                     (cx - half, base_y - base_h, w, base_h), width)
    pygame.draw.line(surf, color,
                     (cx - half + 2, base_y - base_h // 2),
                     (cx + half - 2, base_y - base_h // 2), 1)
    seg = w / points
    for i in range(points):
        px = cx - half + (i + 0.5) * seg
        pts = [(int(px - seg * 0.32), base_y - base_h),
               (int(px), top_y),
               (int(px + seg * 0.32), base_y - base_h)]
        pygame.draw.polygon(surf, color, pts, width)
        pygame.draw.circle(surf, color, (int(px), top_y), 2)
    pygame.draw.circle(surf, color, (cx, base_y - base_h // 2), 3, width)


def draw_shield(surf, cx, cy, w, h, color, width=1, filled=False):
    half = w // 2
    top = cy - h // 2
    bottom = cy + h // 2
    pts = [
        (cx - half, top),
        (cx + half, top),
        (cx + half, top + h // 3),
        (cx, bottom),
        (cx - half, top + h // 3),
    ]
    if filled:
        pygame.draw.polygon(surf, color, pts)
    else:
        pygame.draw.polygon(surf, color, pts, width)
    pygame.draw.line(surf, color,
                     (cx - half, top + h // 3),
                     (cx + half, top + h // 3), width)


def draw_fleur_de_lis(surf, cx, cy, size, color, width=1):
    s = size
    pts = [(cx, cy - s),
           (cx + s // 3, cy - s // 3),
           (cx + s // 4, cy + s // 2),
           (cx, cy + s // 2),
           (cx - s // 4, cy + s // 2),
           (cx - s // 3, cy - s // 3)]
    pygame.draw.polygon(surf, color, pts, width)
    r1 = pygame.Rect(cx - s, cy - s // 2, s, s)
    pygame.draw.arc(surf, color, r1, math.pi / 2, 3 * math.pi / 2, width)
    r2 = pygame.Rect(cx, cy - s // 2, s, s)
    pygame.draw.arc(surf, color, r2, -math.pi / 2, math.pi / 2, width)
    pygame.draw.line(surf, color,
                     (cx - s // 3, cy + s // 2),
                     (cx + s // 3, cy + s // 2), width)


def draw_cross_pattee(surf, cx, cy, size, color, width=1):
    s = size
    t = max(2, s // 4)
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        x1 = cx + dx * (s // 2)
        y1 = cy + dy * (s // 2)
        px, py = -dy, dx
        pts = [
            (cx + px * t // 2, cy + py * t // 2),
            (x1 + px * t, y1 + py * t),
            (x1 - px * t, y1 - py * t),
            (cx - px * t // 2, cy - py * t // 2),
        ]
        pygame.draw.polygon(surf, color, pts, width)


def draw_star_mullet(surf, cx, cy, r, points, color, width=1, filled=False):
    pts = []
    for k in range(points * 2):
        a = k * math.pi / points - math.pi / 2
        rr = r if k % 2 == 0 else max(1, r * 2 // 5)
        pts.append((cx + int(math.cos(a) * rr),
                    cy + int(math.sin(a) * rr)))
    if filled:
        pygame.draw.polygon(surf, color, pts)
    else:
        pygame.draw.polygon(surf, color, pts, width)


def draw_castle_tower(surf, cx, cy, w, h, color, width=1, bg=None, flag=True):
    half = w // 2
    top = cy - h // 2
    base_y = cy + h // 2

    body_top = top + 14
    body_rect = pygame.Rect(cx - half, body_top, w, base_y - body_top)
    if bg is not None:
        pygame.draw.rect(surf, bg, body_rect)
    pygame.draw.rect(surf, color, body_rect, width)

    sillar_h = max(5, h // 9)
    y_ = body_top + sillar_h
    row = 0
    while y_ < base_y - 3:
        pygame.draw.line(surf, color,
                         (cx - half + 2, y_), (cx + half - 2, y_), 1)
        if row % 2 == 0:
            pygame.draw.line(surf, color, (cx, y_ - sillar_h), (cx, y_), 1)
        else:
            off = w // 4
            pygame.draw.line(surf, color,
                             (cx - off, y_ - sillar_h), (cx - off, y_), 1)
            pygame.draw.line(surf, color,
                             (cx + off, y_ - sillar_h), (cx + off, y_), 1)
        y_ += sillar_h
        row += 1

    merlon_h = 14
    n = 5
    merlon_w = max(4, w // (2 * n - 1))
    gap = merlon_w
    total = n * merlon_w + (n - 1) * gap
    x0 = cx - total // 2
    pygame.draw.rect(surf, color,
                     (cx - half, top + merlon_h - 4, w, 5), width)
    for i in range(n):
        mx_ = x0 + i * (merlon_w + gap)
        r_ = pygame.Rect(mx_, top, merlon_w, merlon_h)
        if bg is not None:
            pygame.draw.rect(surf, bg, r_)
            pygame.draw.line(surf, color,
                             (mx_ + merlon_w // 2, top + 3),
                             (mx_ + merlon_w // 2, top + merlon_h - 4), 1)
        pygame.draw.rect(surf, color, r_, width)

    rosette_y = top + merlon_h + 8
    pygame.draw.circle(surf, color, (cx, rosette_y), 4, 1)
    pygame.draw.circle(surf, color, (cx, rosette_y), 1, 1)

    win_w = 4
    win_h = max(8, h // 5)
    wy = top + merlon_h + 22
    for wx in (cx - w // 4, cx + w // 4):
        if bg is not None:
            pygame.draw.rect(surf, bg, (wx - win_w // 2, wy, win_w, win_h))
        pygame.draw.rect(surf, color,
                         (wx - win_w // 2, wy, win_w, win_h), width)
        arc2 = pygame.Rect(wx - win_w // 2, wy - win_w, win_w, win_w)
        pygame.draw.arc(surf, color, arc2, 0, math.pi, width)

    door_w = max(10, w // 3)
    door_h = max(14, h // 3)
    door_x = cx - door_w // 2
    door_y = base_y - door_h
    if bg is not None:
        pygame.draw.rect(surf, bg, (door_x, door_y, door_w, door_h))
    pygame.draw.rect(surf, color, (door_x, door_y, door_w, door_h), width)
    arc_r = pygame.Rect(door_x, door_y - door_w // 2, door_w, door_w)
    pygame.draw.arc(surf, color, arc_r, 0, math.pi, width)
    pygame.draw.line(surf, color, (cx, door_y), (cx, door_y + door_h), 1)
    pygame.draw.circle(surf, color,
                       (cx - door_w // 4, door_y + door_h // 2), 2, 1)

    if flag:
        flag_x = cx
        flag_top = top - 20
        pygame.draw.line(surf, color, (flag_x, flag_top),
                         (flag_x, top + 2), 1)
        pygame.draw.circle(surf, color, (flag_x, flag_top), 2, 1)
        flag_pts = [
            (flag_x, flag_top + 3),
            (flag_x + 13, flag_top + 7),
            (flag_x + 9, flag_top + 12),
            (flag_x, flag_top + 10),
        ]
        pygame.draw.polygon(surf, color, flag_pts, 1)


def draw_banner(surf, x, y, w, h, color, width=1, notch=True):
    if notch:
        pts = [(x, y), (x + w, y), (x + w, y + h - 8),
               (x + w // 2, y + h), (x, y + h - 8)]
    else:
        pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pygame.draw.polygon(surf, color, pts, width)
    pygame.draw.line(surf, color, (x - 4, y), (x + w + 4, y), max(1, width))


def draw_chalice(surf, cx, cy, size, color, width=1):
    s = size
    pts = [(cx - s, cy - s), (cx + s, cy - s),
           (cx + s // 2, cy), (cx - s // 2, cy)]
    pygame.draw.polygon(surf, color, pts, width)
    pygame.draw.line(surf, color, (cx, cy), (cx, cy + s // 2), width)
    pygame.draw.line(surf, color,
                     (cx - s // 2, cy + s // 2),
                     (cx + s // 2, cy + s // 2), width)
    pygame.draw.line(surf, color,
                     (cx - s, cy - s), (cx - s + 4, cy - s),
                     max(2, width))


def draw_sword_shape(surf, cx, cy, angle_deg, length, scale, color, width=0):
    ang = math.radians(angle_deg)
    ca, sa = math.cos(ang), math.sin(ang)

    def rot(dx, dy):
        return (cx + dx * ca - dy * sa, cy + dx * sa + dy * ca)

    L = length
    bw = 2.6 * scale
    tip = rot(0, -L * 0.55)
    blade = [
        tip,
        rot(bw * 0.4, -L * 0.44),
        rot(bw * 0.8, L * 0.02),
        rot(-bw * 0.8, L * 0.02),
        rot(-bw * 0.4, -L * 0.44),
    ]
    g_half = L * 0.15
    g_th = 3 * scale
    guard = [
        rot(-g_half, L * 0.05),
        rot(-g_half, L * 0.05 + g_th),
        rot(g_half, L * 0.05 + g_th),
        rot(g_half, L * 0.05),
        rot(g_half * 0.5, L * 0.09),
        rot(-g_half * 0.5, L * 0.09),
    ]
    h_half = 2.2 * scale
    hilt = [
        rot(-h_half, L * 0.09),
        rot(h_half, L * 0.09),
        rot(h_half, L * 0.36),
        rot(-h_half, L * 0.36),
    ]
    pomo = rot(0, L * 0.40)
    pomo_r = max(3, int(5 * scale))
    if width == 0:
        pygame.draw.polygon(surf, color, blade)
        pygame.draw.polygon(surf, color, guard)
        pygame.draw.polygon(surf, color, hilt)
        pygame.draw.circle(surf, color, (int(pomo[0]), int(pomo[1])),
                           pomo_r)
    else:
        pygame.draw.polygon(surf, color, blade, width)
        pygame.draw.polygon(surf, color, guard, width)
        pygame.draw.polygon(surf, color, hilt, width)
        pygame.draw.circle(surf, color, (int(pomo[0]), int(pomo[1])),
                           pomo_r, width)


def draw_corner_crosses(surf, rect, size=8, color=ORO, margin=0):
    for (x, y) in (
        (rect.x + margin, rect.y + margin),
        (rect.right - margin - 1, rect.y + margin),
        (rect.x + margin, rect.bottom - margin - 1),
        (rect.right - margin - 1, rect.bottom - margin - 1),
    ):
        draw_cross_pattee(surf, x, y, size, color, width=1)


# ---------------- intro ----------------
def intro_animation(message=""):
    pygame.display.init()
    pygame.font.init()
    info = pygame.display.Info()
    W = min(880, info.current_w - 100)
    H = min(640, info.current_h - 100)
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("Heraldic Player")
    clock = pygame.time.Clock()

    f_title = _font(60, bold=True)
    f_hint = _font(13)

    T_APPROACH = 1.15
    T_FLASH = 0.50
    T_TEXT = 1.25
    T_HOLD = 1.10

    cx, cy = W // 2, H // 2 - 60
    SWORD_LEN = int(min(W, H) * 0.55)
    SWORD_SCALE = 1.45
    SWORD_ANG = 38

    t0 = time.time()
    done = False
    while not done:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                pygame.quit()
                return
            if ev.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
                done = True
        if done:
            break
        screen.fill(BG)
        now = time.time() - t0
        arc = pygame.Rect(-100, -230, W + 200, 420)
        pygame.draw.arc(screen, ORO_OSCURO, arc, 0, math.pi, 1)
        for i, bx in enumerate((W // 2 - 170, W // 2 + 130)):
            hgt = 60 + (i * 10)
            draw_banner(screen, bx, 40, 40, hgt, ORO_OSCURO, width=1)
            draw_fleur_de_lis(screen, bx + 20, 40 + hgt // 2, 8, ORO_OSCURO)
        sun_r = int(min(W, H) * 0.16)
        draw_heraldic_sun(screen, cx, cy, sun_r, ORO_OSCURO,
                          rays=16, ray_len=sun_r, width=1)
        draw_heraldic_sun(screen, cx, cy, sun_r - 22, ORO_OSCURO,
                          rays=8, ray_len=sun_r // 2, width=1)
        if now < T_APPROACH:
            p = now / T_APPROACH
            e = 1 - (1 - p) ** 3
            off = (1 - e) * (W * 0.55)
            voff = (1 - e) * 200
            a_l = -SWORD_ANG * e
            a_r = SWORD_ANG * e
            draw_sword_shape(screen, cx - off, cy - voff, a_l,
                             SWORD_LEN, SWORD_SCALE, ORO_BRILLANTE)
            draw_sword_shape(screen, cx + off, cy - voff, a_r,
                             SWORD_LEN, SWORD_SCALE, ORO_BRILLANTE)
        else:
            draw_sword_shape(screen, cx, cy, -SWORD_ANG,
                             SWORD_LEN, SWORD_SCALE, ORO_BRILLANTE)
            draw_sword_shape(screen, cx, cy, SWORD_ANG,
                             SWORD_LEN, SWORD_SCALE, ORO_BRILLANTE)
        if T_APPROACH <= now < T_APPROACH + T_FLASH:
            p = (now - T_APPROACH) / T_FLASH
            a = int(230 * (1 - p))
            glow = pygame.Surface((W, H), pygame.SRCALPHA)
            pygame.draw.circle(glow, (*ORO_BRILLANTE, a), (cx, cy), 55)
            pygame.draw.circle(glow, (*ORO, a // 2), (cx, cy), 130)
            pygame.draw.circle(glow, (*ORO_OSCURO, a // 4), (cx, cy), 240)
            screen.blit(glow, (0, 0))
            for k in range(12):
                ang = k * (2 * math.pi / 12) + p * 0.8
                r0 = 30 + int(70 * p)
                r1 = 70 + int(150 * p)
                x0 = cx + int(math.cos(ang) * r0)
                y0 = cy + int(math.sin(ang) * r0)
                x1 = cx + int(math.cos(ang) * r1)
                y1 = cy + int(math.sin(ang) * r1)
                pygame.draw.line(screen, (*ORO_BRILLANTE, a),
                                 (x0, y0), (x1, y1), 2)
        if now > T_APPROACH + 0.10:
            tp = min(1.0, (now - T_APPROACH - 0.10) / T_TEXT)
            te = 1 - (1 - tp) ** 3
            alpha = int(255 * te)
            txt = "HERALDIC PLAYER"
            surf = f_title.render(txt, True, ORO_BRILLANTE)
            surf.set_alpha(alpha)
            shadow = f_title.render(txt, True, ORO_OSCURO)
            shadow.set_alpha(int(alpha * 0.7))
            ty = cy + int(SWORD_LEN * 0.55) + 30
            tx = W // 2 - surf.get_width() // 2
            screen.blit(shadow, (tx + 3, ty + 3))
            screen.blit(surf, (tx, ty))
            if tp > 0.25:
                ca = int(255 * min(1.0, (tp - 0.25) / 0.5))
                crown_surf = pygame.Surface((W, H), pygame.SRCALPHA)
                draw_crown(crown_surf, W // 2, ty - 24, 90,
                           (*ORO_BRILLANTE, ca), points=3)
                screen.blit(crown_surf, (0, 0))
            if tp > 0.45:
                la = int(255 * min(1.0, (tp - 0.45) / 0.4))
                ls = pygame.Surface((W, H), pygame.SRCALPHA)
                draw_fleur_de_lis(ls, tx - 40, ty + 30, 16, (*ORO, la))
                draw_fleur_de_lis(ls, tx + surf.get_width() + 40,
                                  ty + 30, 16, (*ORO, la))
                screen.blit(ls, (0, 0))
            if tp > 0.5:
                lp = (tp - 0.5) / 0.5
                lw = int(220 * lp)
                ly = ty + surf.get_height() + 12
                if lw > 4:
                    pygame.draw.line(screen, ORO,
                                     (W // 2 - lw, ly), (W // 2 - 26, ly), 1)
                    pygame.draw.line(screen, ORO,
                                     (W // 2 + 26, ly), (W // 2 + lw, ly), 1)
                draw_star_mullet(screen, W // 2, ly, 9, 4,
                                 ORO_BRILLANTE, width=1)
        draw_corner_crosses(screen, pygame.Rect(18, 18, W - 36, H - 36),
                            size=10, color=ORO_OSCURO)
        if now > T_APPROACH + T_TEXT + 0.2:
            p = (math.sin(now * 3.5) + 1) / 2
            a = int(90 + 130 * p)
            hint = f_hint.render("press any key", True, ORO_MATE)
            hint.set_alpha(a)
            screen.blit(hint, (W // 2 - hint.get_width() // 2, H - 42))
        if now > T_APPROACH + T_FLASH + T_TEXT + T_HOLD:
            done = True
        pygame.display.flip()
        clock.tick(60)


# ---------------- main UI ----------------
class PygameUI:
    def __init__(self, app):
        pygame.display.init()
        pygame.font.init()
        info = pygame.display.Info()
        self.min_w = 1000
        self.min_h = 640
        self.max_w = max(self.min_w, info.current_w - 60)
        self.max_h = max(self.min_h, info.current_h - 80)
        self.W = min(1300, self.max_w)
        self.H = min(820, self.max_h)
        self.app = app
        self.screen = pygame.display.set_mode((self.W, self.H),
                                              pygame.RESIZABLE)
        pygame.display.set_caption("Heraldic Player")
        self.clock = pygame.time.Clock()
        try:
            pygame.key.start_text_input()
        except Exception:
            pass
        self._swallow_slash = False

        self.f_h1    = _font(16, bold=True)
        self.f_greet = _font(24, bold=True)
        self.f_body  = _font(14)
        self.f_small = _font(12)
        self.f_tiny  = _font(11)
        self.f_btn   = _font(13, bold=True)
        self.f_brand = _font(15, bold=True)

        self.cover_raw = {}
        self.cover_pending = {}
        self.cover_scaled = {}
        self.cover_loading = set()

        self.hover_queue = -1
        self.hover_list = -1
        self.queue_rects = []
        self.list_rects = []
        self.control_rects = []
        self.mode_rects = {}
        self.back_rect = pygame.Rect(0, 0, 0, 0)
        self.home_rect = pygame.Rect(0, 0, 0, 0)
        self.save_rect = pygame.Rect(0, 0, 0, 0)
        self.search_rect = pygame.Rect(0, 0, 0, 0)
        self.progress_rect = pygame.Rect(0, 0, 0, 0)
        self.volume_rect = pygame.Rect(0, 0, 0, 0)
        self.filter_rects = {}
        self.home_card_rects = []
        self.eq_fallback = [0] * 40
        self.sel_all = False
        self.speed_rects = {"btns": []}
        self.pitch_rects = {"btns": []}

        self.home_stars = []
        self.home_stars_rect = None
        self._stars_last_t = time.time()

    def _diamond(self, cx, cy, r, color=ORO, width=1):
        pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
        pygame.draw.polygon(self.screen, color, pts, width)

    def _ornament_vertical(self, x, y, h):
        if h < 60:
            return
        pygame.draw.line(self.screen, ORO_OSCURO,
                         (x, y + 8), (x, y + h - 8), 1)
        self._diamond(x, y + 12, 3, ORO_OSCURO)
        self._diamond(x, y + h - 12, 3, ORO_OSCURO)

    def _panel(self, rect, radius=8, gold=True):
        pygame.draw.rect(self.screen, BG_PANEL, rect, border_radius=radius)
        pygame.draw.rect(self.screen, ORO_OSCURO if gold else BORDER,
                         rect, 1, border_radius=radius)

    def _panel_title(self, text, x, y, with_swords=False):
        surf = self.f_h1.render(text, True, ORO_BRILLANTE)
        self.screen.blit(surf, (x, y))
        cx = x - 10
        cy = y + surf.get_height() // 2
        if with_swords:
            draw_sword_shape(self.screen, cx, cy, -30, 18, 0.30,
                             ORO, width=1)
            draw_sword_shape(self.screen, cx, cy, 30, 18, 0.30,
                             ORO, width=1)
        else:
            self._diamond(cx, cy, 3)
        return surf.get_width()

    def _request_cover(self, vid):
        if not vid or vid in self.cover_raw or vid in self.cover_loading:
            return
        self.cover_loading.add(vid)

        def work():
            img = fetch_thumbnail_pil(vid)
            if img is not None:
                self.cover_pending[vid] = img
        threading.Thread(target=work, daemon=True).start()

    def _pump_covers(self):
        while self.cover_pending:
            vid, img = self.cover_pending.popitem()
            try:
                self.cover_raw[vid] = pygame.image.fromstring(
                    img.tobytes(), img.size, "RGB")
            except Exception:
                pass
            self.cover_loading.discard(vid)

    def _get_cover(self, vid, w, h):
        if vid not in self.cover_raw:
            return None
        key = (vid, w, h)
        if key in self.cover_scaled:
            return self.cover_scaled[key]
        try:
            raw = self.cover_raw[vid]
            iw, ih = raw.get_size()
            s = min(iw, ih)
            crop = pygame.Rect((iw - s) // 2, (ih - s) // 2, s, s)
            sub = raw.subsurface(crop)
            scaled = pygame.transform.smoothscale(sub, (w, h))
            self.cover_scaled[key] = scaled
            return scaled
        except Exception:
            return None

    def _update_and_draw_stars(self, rect):
        now = time.time()
        dt = min(0.08, now - self._stars_last_t)
        self._stars_last_t = now

        if (not self.home_stars or
                self.home_stars_rect != (rect.x, rect.y, rect.w, rect.h)):
            self.home_stars_rect = (rect.x, rect.y, rect.w, rect.h)
            self.home_stars = []
            for _ in range(70):
                x = random.uniform(rect.x + 4, rect.right - 4)
                y = random.uniform(rect.y + 4, rect.bottom - 4)
                self.home_stars.append({
                    "x": x, "y": y,
                    "tx": random.uniform(rect.x + 4, rect.right - 4),
                    "ty": random.uniform(rect.y + 4, rect.bottom - 4),
                    "phase": random.uniform(0, 2 * math.pi),
                    "tw": random.uniform(0.8, 2.4),
                    "mv": random.uniform(0.15, 0.55),
                    "size": random.uniform(0.7, 1.8),
                    "base": random.choice([
                        (200, 180, 120),
                        (230, 215, 170),
                        (160, 145, 105),
                        (215, 195, 140),
                    ]),
                })

        for s in self.home_stars:
            dx = s["tx"] - s["x"]
            dy = s["ty"] - s["y"]
            s["x"] += dx * s["mv"] * dt
            s["y"] += dy * s["mv"] * dt
            if abs(dx) < 3 and abs(dy) < 3:
                s["tx"] = random.uniform(rect.x + 4, rect.right - 4)
                s["ty"] = random.uniform(rect.y + 4, rect.bottom - 4)
            s["phase"] += dt * s["tw"]

        for s in self.home_stars:
            t = (math.sin(s["phase"]) + 1) * 0.5
            bright = 0.15 + 0.70 * t
            col = (int(s["base"][0] * bright),
                   int(s["base"][1] * bright),
                   int(s["base"][2] * bright))
            r = max(1, int(s["size"] * (0.55 + 0.5 * t) * 1.6))
            px, py = int(s["x"]), int(s["y"])
            pygame.draw.circle(self.screen, col, (px, py), r)
            if t > 0.82 and r >= 1:
                halo = (col[0] // 3, col[1] // 3, col[2] // 3)
                pygame.draw.circle(self.screen, halo, (px, py), r + 2, 1)

    def _do(self, action):
        app = self.app
        a = app.audio
        if action == "prev":
            a.prev()
        elif action == "play":
            a.toggle_pause()
        elif action == "next":
            a.next()
        elif action == "shuffle":
            a.shuffle = not a.shuffle
        elif action == "repeat":
            a.repeat = {"off": "all", "all": "one", "one": "off"}[a.repeat]
        elif action == "download":
            app.download_current()
        elif action == "fav":
            if a.cur:
                app.toggle_fav(a.cur)
        elif action == "save_list":
            app.toggle_save_collection()
        elif action == "sync_all":
            app.sync_all_saved()
        elif action == "clear_recent":
            app.clear_recent()
        elif action == "speed_down":
            a.set_speed(-0.05)
        elif action == "speed_up":
            a.set_speed(0.05)
        elif action == "speed_reset":
            a.set_speed_abs(1.0)
        elif action == "pitch_down":
            a.set_pitch(-1)
        elif action == "pitch_up":
            a.set_pitch(1)
        elif action == "pitch_reset":
            if a.pitch == 0:
                a.say("pitch already at 0 semitones")
            else:
                a.pitch = 0
                a.say("pitch 0 semitones")
                if a.playing and a.cur:
                    pos = a.pos()
                    a.play_index(a.idx)
                    threading.Thread(target=a._seek_later, args=(pos,),
                                     daemon=True).start()

    def handle_event(self, ev):
        app = self.app
        if ev.type == pygame.QUIT:
            app.running = False
            return
        if ev.type == pygame.VIDEORESIZE:
            w = max(self.min_w, min(ev.w, self.max_w))
            h = max(self.min_h, min(ev.h, self.max_h))
            self.W, self.H = w, h
            self.screen = pygame.display.set_mode((w, h), pygame.RESIZABLE)
            return
        if ev.type == pygame.MOUSEWHEEL:
            if app.mode == "main":
                if app.home_mode:
                    app.home_scroll = max(0, app.home_scroll - ev.y * 70)
                else:
                    app.view["cur"] = max(0, app.view["cur"] - ev.y * 2)
                    app.clamp()
            return
        if ev.type == pygame.MOUSEMOTION:
            self.hover_queue = -1
            self.hover_list = -1
            for rect, i in self.queue_rects:
                if rect.collidepoint(ev.pos):
                    self.hover_queue = i
                    break
            for rect, i in self.list_rects:
                if rect.collidepoint(ev.pos):
                    self.hover_list = i
                    break
            return
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if self.home_rect.w > 0 and self.home_rect.collidepoint(ev.pos):
                app.toggle_home()
                return
            for rect, action, _ in self.control_rects:
                if rect.collidepoint(ev.pos):
                    self._do(action)
                    return
            for rect, action in self.speed_rects.get("btns", []):
                if rect.collidepoint(ev.pos):
                    self._do(action)
                    return
            for rect, action in self.pitch_rects.get("btns", []):
                if rect.collidepoint(ev.pos):
                    self._do(action)
                    return
            if self.progress_rect.w > 0 and self.progress_rect.collidepoint(ev.pos):
                dur = (app.audio.cur or {}).get("duration")
                if dur:
                    rel = ((ev.pos[0] - self.progress_rect.x) /
                           max(1, self.progress_rect.w))
                    app.audio.seek_to(rel * dur)
                return
            if self.volume_rect.w > 0 and self.volume_rect.collidepoint(ev.pos):
                rel = ((ev.pos[0] - self.volume_rect.x) /
                       max(1, self.volume_rect.w))
                app.audio.volume = int(max(0, min(1, rel)) * 100)
                app.audio.mp.audio_set_volume(app.audio.volume)
                return
            if app.home_mode:
                if self.search_rect.w > 0 and self.search_rect.collidepoint(ev.pos):
                    app.mode = "input"
                    self.sel_all = False
                    return
                for card, shelf_items, idx, action in self.home_card_rects:
                    if card.collidepoint(ev.pos):
                        if action == "open":
                            sp = app.find_saved(shelf_items[idx].get("id"))
                            if sp:
                                app.open_saved(sp)
                        elif action == "open_recent":
                            app.open_recent(shelf_items[idx])
                        else:
                            app.audio.queue[:] = [dict(x) for x in shelf_items]
                            app.audio.play_index(idx)
                        return
                if app.mode == "input":
                    app.mode = "main"
                return
            for t, rect in self.mode_rects.items():
                if rect.collidepoint(ev.pos):
                    app.search_mode = t
                    return
            if self.back_rect.w > 0 and self.back_rect.collidepoint(ev.pos):
                app.back()
                return
            if self.save_rect.w > 0 and self.save_rect.collidepoint(ev.pos):
                self._do("save_list")
                return
            if self.search_rect.w > 0 and self.search_rect.collidepoint(ev.pos):
                app.mode = "input"
                self.sel_all = False
                return
            for t, rect in self.filter_rects.items():
                if rect.collidepoint(ev.pos):
                    app.filter = t
                    app.view["cur"] = 0
                    return
            for rect, i in self.queue_rects:
                if rect.collidepoint(ev.pos):
                    app.play_from_queue(i)
                    return
            for rect, i in self.list_rects:
                if rect.collidepoint(ev.pos):
                    app.focus = "list"
                    app.view["cur"] = i
                    app.activate()
                    return
            if app.mode == "input":
                app.mode = "main"
            return
        if ev.type == pygame.TEXTINPUT:
            if app.mode == "input":
                if self._swallow_slash:
                    self._swallow_slash = False
                    if ev.text == "/":
                        return
                if self.sel_all:
                    app.buf = ""
                    self.sel_all = False
                if len(app.buf) < 200:
                    app.buf += ev.text
            return
        if ev.type == pygame.KEYDOWN:
            k = ev.key
            if app.mode == "input":
                ctrl = bool(ev.mod & (pygame.KMOD_CTRL | pygame.KMOD_META))
                if k in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    text = app.buf.strip()
                    app.mode = "main"
                    app.buf = ""
                    self.sel_all = False
                    if text:
                        app.do_search(text)
                elif k == pygame.K_ESCAPE:
                    app.mode = "main"
                    app.buf = ""
                    self.sel_all = False
                elif k == pygame.K_BACKSPACE:
                    if self.sel_all:
                        app.buf = ""
                        self.sel_all = False
                    elif ctrl:
                        app.buf = app.buf.rstrip()
                        while app.buf and not app.buf[-1].isspace():
                            app.buf = app.buf[:-1]
                        app.buf = app.buf.rstrip()
                    else:
                        app.buf = app.buf[:-1]
                elif k == pygame.K_v and ctrl:
                    clip = _clean_clip(_clipboard_get())
                    if clip:
                        if self.sel_all:
                            app.buf = ""
                            self.sel_all = False
                        app.buf = (app.buf + clip)[:200]
                elif k == pygame.K_c and ctrl:
                    _clipboard_set(app.buf)
                elif k == pygame.K_x and ctrl:
                    _clipboard_set(app.buf)
                    app.buf = ""
                    self.sel_all = False
                elif k == pygame.K_a and ctrl:
                    self.sel_all = True
                elif k == pygame.K_u and ctrl:
                    app.buf = ""
                    self.sel_all = False
                elif k == pygame.K_TAB:
                    modes = ["tracks", "playlists", "artists", "links"]
                    i = (modes.index(app.search_mode)
                         if app.search_mode in modes else 0)
                    app.search_mode = modes[(i + 1) % len(modes)]
                return
            a = app.audio
            if k == pygame.K_SPACE:
                a.toggle_pause()
            elif k == pygame.K_n:
                a.next()
            elif k == pygame.K_p:
                a.prev()
            elif k in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                a.set_volume(5)
            elif k in (pygame.K_MINUS, pygame.K_KP_MINUS):
                a.set_volume(-5)
            elif k == pygame.K_RIGHT:
                a.seek(10)
            elif k == pygame.K_LEFT:
                a.seek(-10)
            elif k == pygame.K_UP:
                if app.home_mode:
                    app.home_scroll = max(0, app.home_scroll - 70)
                elif app.focus == "queue" and app.audio.queue:
                    app.queue_cur = max(0, app.queue_cur - 1)
                else:
                    app.view["cur"] -= 1
                    app.clamp()
            elif k == pygame.K_DOWN:
                if app.home_mode:
                    app.home_scroll += 70
                elif app.focus == "queue" and app.audio.queue:
                    app.queue_cur = min(len(app.audio.queue) - 1,
                                        app.queue_cur + 1)
                else:
                    app.view["cur"] += 1
                    app.clamp()
            elif k == pygame.K_PAGEUP:
                if app.home_mode:
                    app.home_scroll = max(0, app.home_scroll - 400)
                elif app.focus == "queue" and app.audio.queue:
                    app.queue_cur = max(0, app.queue_cur - 10)
                else:
                    app.view["cur"] -= 10
                    app.clamp()
            elif k == pygame.K_PAGEDOWN:
                if app.home_mode:
                    app.home_scroll += 400
                elif app.focus == "queue" and app.audio.queue:
                    app.queue_cur = min(len(app.audio.queue) - 1,
                                        app.queue_cur + 10)
                else:
                    app.view["cur"] += 10
                    app.clamp()
            elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
                if app.home_mode:
                    pass
                elif app.focus == "queue" and app.audio.queue:
                    app.play_from_queue(app.queue_cur)
                else:
                    app.activate()
            elif k == pygame.K_ESCAPE:
                if app.home_mode:
                    app.home_mode = False
                else:
                    app.back()
            elif k == pygame.K_TAB:
                app.focus = "queue" if app.focus == "list" else "list"
            elif k == pygame.K_a:
                app.add_selected()
            elif k == pygame.K_d:
                app.delete_selected()
            elif k == pygame.K_f:
                self._do("fav")
            elif k == pygame.K_g:
                app.toggle_save_collection()
            elif k == pygame.K_y:
                app.sync_all_saved()
            elif k == pygame.K_LEFTBRACKET:
                a.set_speed(-0.05)
            elif k == pygame.K_RIGHTBRACKET:
                a.set_speed(0.05)
            elif k == pygame.K_BACKSLASH:
                a.set_speed_abs(1.0)
            elif k == pygame.K_COMMA:
                a.set_pitch(-1)
            elif k == pygame.K_PERIOD:
                a.set_pitch(1)
            elif k == pygame.K_SLASH:
                if ev.mod & pygame.KMOD_SHIFT:
                    self._do("pitch_reset")
                else:
                    app.mode, app.buf = "input", ""
                    self.sel_all = False
                    self._swallow_slash = True
            elif k == pygame.K_s:
                self._do("shuffle")
            elif k == pygame.K_r:
                self._do("repeat")
            elif k == pygame.K_l:
                app.goto("library")
            elif k == pygame.K_v:
                app.goto("favs")
            elif k == pygame.K_h:
                if ev.mod & pygame.KMOD_CTRL:
                    app.toggle_home()
                else:
                    app.goto("history")
            elif k == pygame.K_i:
                app.toggle_home()
            elif k == pygame.K_w:
                app.download_view()
            elif k == pygame.K_o:
                app.download_current()
            elif k == pygame.K_1:
                app.filter = "all"
            elif k == pygame.K_2:
                app.filter = "song"
            elif k == pygame.K_3:
                app.filter = "video"
            elif k == pygame.K_4:
                app.filter = "podcast"
            elif k == pygame.K_q:
                app.running = False

    def _text(self, s, font, color, pos, max_w=None):
        if max_w is not None:
            s = _fit(font, s, max_w)
        surf = font.render(s, True, color)
        self.screen.blit(surf, pos)
        return surf.get_width()

    def _btn(self, x, y, w, h, label, active, hover, gold=False):
        r = pygame.Rect(x, y, w, h)
        if active:
            bg = BG_SELECT
            bd = ORO_BRILLANTE if gold else ACCENT
            col = ORO_BRILLANTE if gold else ACCENT
        elif hover:
            bg = BG_HOVER
            bd = ORO if gold else BORDER_HI
            col = ORO_BRILLANTE if gold else TEXT
        else:
            bg = BG_SOFT
            bd = ORO_OSCURO if gold else BORDER
            col = ORO_MATE if gold else TEXT_MUT
        pygame.draw.rect(self.screen, bg, r, border_radius=6)
        pygame.draw.rect(self.screen, bd, r, 1, border_radius=6)
        surf = self.f_btn.render(label, True, col)
        self.screen.blit(surf, (r.centerx - surf.get_width() // 2,
                                r.centery - surf.get_height() // 2))
        return r

    def _tab(self, x, y, label, active, hover):
        w = self.f_small.size(label)[0] + 16
        r = pygame.Rect(x, y, w, 24)
        if active:
            bg, bd, col = BG_SELECT, ORO, ORO_BRILLANTE
        elif hover:
            bg, bd, col = BG_HOVER, ORO_OSCURO, TEXT
        else:
            bg, bd, col = BG_SOFT, BORDER, TEXT_MUT
        pygame.draw.rect(self.screen, bg, r, border_radius=5)
        pygame.draw.rect(self.screen, bd, r, 1, border_radius=5)
        self._text(label, self.f_small, col, (r.x + 8, r.y + 5))
        return r

    def _draw_brand(self, x, y):
        draw_crown(self.screen, x + 12, y + 4, 26, ORO_BRILLANTE, points=3)
        draw_sword_shape(self.screen, x + 12, y + 22, -35, 18, 0.30,
                         ORO, width=1)
        draw_sword_shape(self.screen, x + 12, y + 22, 35, 18, 0.30,
                         ORO, width=1)
        self._text("HERALDIC PLAYER", self.f_brand, ORO_BRILLANTE,
                   (x + 30, y + 12))

    def render(self):
        app = self.app
        W, H = self.W, self.H
        self.screen.fill(BG)

        margin = 14
        gap = 22
        bottom_h = 90

        HEADER_H = 54

        sep_y = HEADER_H - 6
        pygame.draw.line(self.screen, ORO_OSCURO,
                         (margin, sep_y), (W - margin, sep_y), 1)

        self._draw_brand(margin + 14, 6)

        usr = app.display
        uw = self.f_small.size(usr)[0]
        user_box = pygame.Rect(W - margin - uw - 20, 15, uw + 16, 24)
        pygame.draw.rect(self.screen, BG_SOFT, user_box, border_radius=4)
        pygame.draw.rect(self.screen, ORO_OSCURO, user_box, 1,
                         border_radius=4)
        self._text(usr, self.f_small, ORO_MATE,
                   (user_box.x + 8, user_box.y + 5))

        body_top = HEADER_H + 4
        body_h = H - body_top - bottom_h - gap
        if body_h < 200:
            body_h = 200

        if app.home_mode:
            self.queue_rects = []
            self.mode_rects = {}
            self.filter_rects = {}
            self.list_rects = []
            self.back_rect = pygame.Rect(0, 0, 0, 0)
            self.save_rect = pygame.Rect(0, 0, 0, 0)
            body_rect = pygame.Rect(margin, body_top, W - 2 * margin, body_h)
            self._render_home_full(body_rect)
        else:
            self.home_card_rects = []
            left_w = 280
            center_w = min(400, max(300, W // 3))
            right_w = W - 2 * margin - 2 * gap - left_w - center_w
            if right_w < 320:
                center_w = max(260, W - 2 * margin - 2 * gap - left_w - 320)
                right_w = W - 2 * margin - 2 * gap - left_w - center_w
            left_rect = pygame.Rect(margin, body_top, left_w, body_h)
            center_rect = pygame.Rect(left_rect.right + gap, body_top,
                                      center_w, body_h)
            right_rect = pygame.Rect(center_rect.right + gap, body_top,
                                     right_w, body_h)
            self._render_queue(left_rect)
            self._render_center(center_rect)
            self._render_right(right_rect)
            self._ornament_vertical(left_rect.right + gap // 2,
                                    body_top + 8, body_h - 16)
            self._ornament_vertical(center_rect.right + gap // 2,
                                    body_top + 8, body_h - 16)

        self._render_bottom(W, H, bottom_h)

    def _render_queue(self, rect):
        a = self.app.audio
        pad = 14
        self._panel(rect)
        inner = rect.inflate(-pad * 2, -pad * 2)
        self._panel_title("queue", inner.x + 14, inner.y + 2, with_swords=True)
        y = inner.y + 34
        row_h = 44
        avail_rows = max(1, (inner.bottom - y) // row_h)
        self.queue_rects = []
        if not a.queue:
            cxs = inner.centerx
            cys = inner.y + 110
            draw_heraldic_sun(self.screen, cxs, cys - 55, 20, ORO_OSCURO,
                              rays=12, ray_len=14)
            draw_castle_tower(self.screen, cxs, cys + 18, 70, 96, ORO,
                              width=1, bg=BG_PANEL)
            self._text("empty queue", self.f_body, TEXT_DIM,
                       (cxs - 34, cys + 80))
            return
        cur = max(0, a.idx)
        start = max(0, cur - avail_rows // 2)
        start = min(start, max(0, len(a.queue) - avail_rows))
        end = min(len(a.queue), start + avail_rows)

        focused = (self.app.focus == "queue")
        if self.app.queue_cur < 0 or self.app.queue_cur >= len(a.queue):
            self.app.queue_cur = max(0, a.idx)
        for i in range(start, end):
            t = a.queue[i]
            is_cur = (i == a.idx)
            is_hover = (i == self.hover_queue)
            is_focus = focused and (i == self.app.queue_cur)
            row = pygame.Rect(inner.x, y, inner.w, row_h - 4)
            self.queue_rects.append((row, i))
            if is_focus:
                pygame.draw.rect(self.screen, (60, 60, 60), row,
                                 border_radius=5)
                pygame.draw.rect(self.screen, ORO, row, 1, border_radius=5)
            elif is_cur:
                pygame.draw.rect(self.screen, BG_SELECT, row, border_radius=5)
                pygame.draw.rect(self.screen, ORO_OSCURO, row, 1,
                                 border_radius=5)
            elif is_hover:
                pygame.draw.rect(self.screen, BG_HOVER, row, border_radius=5)
            col = ORO_BRILLANTE if (is_cur or is_focus) else TEXT
            draw_star_mullet(self.screen, row.x + 12, row.centery, 6, 4,
                             ORO_BRILLANTE if (is_cur or is_focus) else ORO_MATE)
            self._text(t["title"], self.f_body, col,
                       (row.x + 24, row.y + 5), row.w - 30)
            self._text(t["artist"], self.f_small, TEXT_MUT,
                       (row.x + 24, row.y + 23), row.w - 30)
            y += row_h

    def _render_center(self, rect):
        a = self.app.audio
        t = a.cur
        pad = 18
        self._panel(rect)
        inner = rect.inflate(-pad * 2, -pad * 2)

        eq_h = 28
        eq_gap = 8
        title_lines = 2
        title_h = title_lines * 20
        artist_h = 20
        reserved = title_h + artist_h + eq_h + eq_gap + 30

        max_cover = max(80, inner.h - reserved)
        cover_size = max(80, min(inner.w, max_cover))

        cx = inner.x + (inner.w - cover_size) // 2
        cy = inner.y + 8
        cover_rect = pygame.Rect(cx, cy, cover_size, cover_size)
        pygame.draw.rect(self.screen, (4, 4, 4), cover_rect, border_radius=8)

        if t:
            self._request_cover(t["id"])
            surf = self._get_cover(t["id"], cover_size, cover_size)
            if surf:
                self.screen.blit(surf, cover_rect.topleft)
                pygame.draw.rect(self.screen, ORO_OSCURO, cover_rect, 1,
                                 border_radius=8)
            else:
                draw_heraldic_sun(self.screen, cover_rect.centerx,
                                  cover_rect.centery - 12, 26, ORO,
                                  rays=12, ray_len=18)
                self._text("loading", self.f_body, TEXT_DIM,
                           (cover_rect.centerx - 32,
                            cover_rect.centery + 24))
        else:
            cxs = cover_rect.centerx
            cys = cover_rect.centery
            draw_heraldic_sun(self.screen, cxs, cys - 50, 26, ORO_OSCURO,
                              rays=16, ray_len=22)
            draw_sword_shape(self.screen, cxs - 22, cys + 10, -38,
                             60, 0.9, ORO, width=1)
            draw_sword_shape(self.screen, cxs + 22, cys + 10, 38,
                             60, 0.9, ORO, width=1)
            draw_crown(self.screen, cxs, cys + 54, 60, ORO_BRILLANTE,
                       points=3)
            self._text("no track", self.f_body, ORO_MATE,
                       (cxs - 32, cys + 78))

        y = cover_rect.bottom + 14
        title = t["title"] if t else "—"
        lines = _wrap(self.f_h1, title, inner.w - 8, title_lines)
        for ln in lines:
            self._text(ln, self.f_h1, ORO_BRILLANTE if t else TEXT,
                       (inner.x + 4, y))
            y += 20
        art = t["artist"] if t else ""
        self._text(art, self.f_body, TEXT_MUT, (inner.x + 4, y), inner.w - 8)

        eq_rect = pygame.Rect(inner.x + 4,
                              inner.bottom - eq_h - 4,
                              inner.w - 8,
                              eq_h)
        active = a.playing and not a.paused
        bands = a.eq.get() if a.eq.enabled else []
        if bands:
            n = len(bands)
            bw = max(2, eq_rect.w // n - 1)
            for i in range(n):
                v = bands[i] if active else 0.0
                v = max(0.0, min(20.0, v))
                h = int(eq_rect.h * v / 20.0)
                shade = 60 + int(180 * v / 20.0)
                r_ = min(255, shade + 40)
                g_ = min(255, int(shade * 0.85) + 30)
                b_ = max(20, shade - 30)
                pygame.draw.rect(self.screen, (r_, g_, b_),
                                 pygame.Rect(eq_rect.x + i * (bw + 1),
                                             eq_rect.bottom - h, bw, h))
        else:
            n = 40
            bw = max(2, eq_rect.w // n - 1)
            for i in range(n):
                if active:
                    self.eq_fallback[i] = max(
                        0, min(20, self.eq_fallback[i] +
                               random.choice((-2, -1, 0, 1, 2))))
                else:
                    self.eq_fallback[i] = max(0, self.eq_fallback[i] - 1)
                v = self.eq_fallback[i]
                h = int(eq_rect.h * v / 20)
                shade = 60 + int(180 * v / 20)
                r_ = min(255, shade + 40)
                g_ = min(255, int(shade * 0.85) + 30)
                b_ = max(20, shade - 30)
                pygame.draw.rect(self.screen, (r_, g_, b_),
                                 pygame.Rect(eq_rect.x + i * (bw + 1),
                                             eq_rect.bottom - h, bw, h))

    def _render_right(self, rect):
        app = self.app
        pad = 14
        self._panel(rect)
        inner = rect.inflate(-pad * 2, -pad * 2)
        mx, my = pygame.mouse.get_pos()

        hb_w = 78
        home_btn = pygame.Rect(inner.right - hb_w, inner.y, hb_w, 30)
        self.home_rect = home_btn
        hb_hover = home_btn.collidepoint(mx, my)
        pygame.draw.rect(self.screen, BG_HOVER if hb_hover else BG_SOFT,
                         home_btn, border_radius=6)
        pygame.draw.rect(self.screen, ORO if hb_hover else ORO_OSCURO,
                         home_btn, 1, border_radius=6)
        lbl = self.f_small.render("home", True,
                                  ORO_BRILLANTE if hb_hover else ORO_MATE)
        self.screen.blit(lbl, (home_btn.centerx - lbl.get_width() // 2,
                               home_btn.centery - lbl.get_height() // 2))

        self.back_rect = pygame.Rect(0, 0, 0, 0)
        self.save_rect = pygame.Rect(0, 0, 0, 0)
        x = inner.x
        if app.view_stack:
            bw = 70
            self.back_rect = pygame.Rect(x, inner.y, bw, 30)
            hov = self.back_rect.collidepoint(mx, my)
            pygame.draw.rect(self.screen, BG_HOVER if hov else BG_SOFT,
                             self.back_rect, border_radius=6)
            pygame.draw.rect(self.screen, ORO if hov else ORO_OSCURO,
                             self.back_rect, 1, border_radius=6)
            self._text("< back", self.f_small,
                       ORO_BRILLANTE if hov else ORO_MATE,
                       (self.back_rect.x + 10, self.back_rect.y + 7))
            x = self.back_rect.right + 8
        if app.view.get("kind") == "collection":
            src = app.view.get("source") or {}
            src_id = src.get("id")
            already = bool(src_id and app.is_saved_playlist(src_id))
            label = "saved" if already else "save playlist"
            bw2 = 132
            self.save_rect = pygame.Rect(x, inner.y, bw2, 30)
            hov2 = self.save_rect.collidepoint(mx, my)
            if already:
                bg = (28, 46, 28)
                bd = GREEN
                col = GREEN
            else:
                bg = (58, 46, 18) if hov2 else (34, 28, 12)
                bd = ORO_BRILLANTE if hov2 else ORO_OSCURO
                col = ORO_BRILLANTE if hov2 else ORO
            pygame.draw.rect(self.screen, bg, self.save_rect, border_radius=6)
            pygame.draw.rect(self.screen, bd, self.save_rect, 1,
                             border_radius=6)
            lbl = self.f_small.render(label, True, col)
            self.screen.blit(lbl, (self.save_rect.centerx -
                                   lbl.get_width() // 2,
                                   self.save_rect.centery -
                                   lbl.get_height() // 2))
            x = self.save_rect.right + 8

        search_rect = pygame.Rect(x, inner.y,
                                  max(80, home_btn.x - 8 - x), 34)
        self.search_rect = search_rect
        focused = app.mode == "input"
        pygame.draw.rect(self.screen, BG_SOFT, search_rect, border_radius=17)
        pygame.draw.rect(self.screen, ORO if focused else ORO_OSCURO,
                         search_rect, 1, border_radius=17)
        txt_x = search_rect.x + 14
        txt_w = search_rect.w - 26
        if focused:
            if self.sel_all and app.buf:
                s = self.f_body.render(app.buf[-200:], True, TEXT)
                sel = pygame.Surface((s.get_width(), s.get_height()),
                                     pygame.SRCALPHA)
                sel.fill((80, 65, 30, 180))
                self.screen.blit(sel, (txt_x, search_rect.y + 8))
                self.screen.blit(s, (txt_x, search_rect.y + 8))
            else:
                caret = "|" if int(time.time() * 2) % 2 == 0 else " "
                self._text(app.buf[-200:] + caret, self.f_body, TEXT,
                           (txt_x, search_rect.y + 8), txt_w)
        else:
            if app.search_mode == "playlists":
                hint = "search for a playlist"
            elif app.search_mode == "artists":
                hint = "search for an artist"
            elif app.search_mode == "links":
                hint = "paste a link (video/playlist/channel)"
            else:
                hint = "search"
            self._text(hint, self.f_body, TEXT_DIM,
                       (txt_x, search_rect.y + 8), txt_w)

        y = search_rect.bottom + 10
        modes = [("tracks", "songs"), ("playlists", "playlists"),
                 ("artists", "artists"), ("links", "links")]
        self.mode_rects = {}
        mx_tab = inner.x
        for key, label in modes:
            w = self.f_small.size(label)[0] + 16
            hov = pygame.Rect(mx_tab, y, w, 24).collidepoint(mx, my)
            r = self._tab(mx_tab, y, label, app.search_mode == key, hov)
            self.mode_rects[key] = r
            mx_tab = r.right + 5
        y += 30

        pygame.draw.line(self.screen, ORO_OSCURO,
                         (inner.x, y + 4), (inner.right, y + 4), 1)
        y += 14

        self.filter_rects = {}
        if app.search_mode == "tracks":
            filters = [("all", "all"), ("song", "songs"),
                       ("video", "videos"), ("podcast", "podcasts")]
            fx = inner.x
            for key, label in filters:
                w = self.f_small.size(label)[0] + 14
                r = pygame.Rect(fx, y, w, 22)
                self.filter_rects[key] = r
                sel = (app.filter == key)
                hov = r.collidepoint(mx, my)
                if sel:
                    bg, bd, col = BG_SELECT, ORO, ORO_BRILLANTE
                elif hov:
                    bg, bd, col = BG_HOVER, ORO_OSCURO, TEXT
                else:
                    bg, bd, col = BG_SOFT, BORDER, TEXT_MUT
                pygame.draw.rect(self.screen, bg, r, border_radius=5)
                pygame.draw.rect(self.screen, bd, r, 1, border_radius=5)
                self._text(label, self.f_small, col, (r.x + 7, r.y + 4))
                fx = r.right + 4
            y += 28

        its = app.items()
        app.clamp()
        v = app.view
        ctx = ""
        if v["kind"] == "collection":
            ctx = v.get("title") or ""
        elif v["kind"] == "results":
            ctx = "results"
        elif v["kind"] == "saved":
            ctx = "saved playlists"
        elif v["kind"] == "recent":
            ctx = "recent playlists"
        if ctx:
            self._text(ctx, self.f_small, ORO_MATE, (inner.x, y), inner.w - 60)
        cnt = f"{len(its)}"
        if v.get("loading"):
            cnt = "..."
        self._text(cnt, self.f_small, TEXT_DIM,
                   (inner.right - self.f_small.size(cnt)[0], y))
        y += 22

        row_h = 30
        avail = inner.bottom - y
        visible = max(1, avail // row_h)
        cur = v["cur"]
        start = max(0, cur - visible // 2)
        start = min(start, max(0, len(its) - visible))
        end = min(len(its), start + visible)

        self.list_rects = []
        if not its and not v.get("loading"):
            self._text("empty", self.f_body, TEXT_DIM, (inner.x + 4, y + 4))
        focused_list = (app.focus == "list")
        for idx in range(start, end):
            e = its[idx]
            row = pygame.Rect(inner.x, y, inner.w, row_h - 2)
            self.list_rects.append((row, idx))
            sel = (idx == cur)
            hov = (idx == self.hover_list)
            if sel:
                bg = (60, 60, 60) if focused_list else BG_SELECT
                pygame.draw.rect(self.screen, bg, row, border_radius=5)
                if focused_list:
                    pygame.draw.rect(self.screen, ORO, row, 1,
                                     border_radius=5)
                else:
                    pygame.draw.rect(self.screen, ORO_OSCURO, row, 1,
                                     border_radius=5)
            elif hov:
                pygame.draw.rect(self.screen, BG_HOVER, row, border_radius=5)

            ctype = e.get("ctype", "")
            if v["kind"] == "library":
                self._text(e["name"], self.f_body,
                           ORO_BRILLANTE if sel else TEXT,
                           (row.x + 14, row.y + 5), row.w - 70)
                cs = str(e.get("count", ""))
                cw = self.f_small.size(cs)[0]
                self._text(cs, self.f_small, ORO_MATE,
                           (row.right - cw - 10, row.y + 7))
            elif ctype == "playlist":
                self._text(e["title"], self.f_body,
                           ORO_BRILLANTE if sel else TEXT,
                           (row.x + 14, row.y + 5), row.w - 14 - 100)
                meta = ""
                if e.get("count"):
                    meta = f"{e['count']} tracks"
                elif e.get("artist"):
                    meta = e["artist"]
                self._text(meta, self.f_small, TEXT_MUT,
                           (row.x + row.w - 100, row.y + 7), 90)
            elif ctype == "artist":
                self._text(e["title"], self.f_body,
                           ORO_BRILLANTE if sel else TEXT,
                           (row.x + 14, row.y + 5), row.w - 14 - 90)
                self._text("channel", self.f_small, TEXT_MUT,
                           (row.x + row.w - 90, row.y + 7), 80)
            else:
                playing = app.audio.cur and e["id"] == app.audio.cur["id"]
                col = ORO_BRILLANTE if playing else (ORO_BRILLANTE if sel else TEXT)
                self._text(e["title"], self.f_body, col,
                           (row.x + 14, row.y + 5), row.w - 14 - 220)
                self._text(e["artist"], self.f_small, TEXT_MUT,
                           (row.x + row.w - 220, row.y + 7), 150)
                tp = _TYPE_LABEL.get(ctype, ctype)
                self._text(tp, self.f_small, TEXT_DIM,
                           (row.x + row.w - 130, row.y + 7), 70)
                self._text(fmt(e.get("duration")), self.f_small, TEXT_MUT,
                           (row.x + row.w - 48, row.y + 7))
            y += row_h

    def _render_home_full(self, rect):
        app = self.app
        a = app.audio
        pad = 26
        mx, my = pygame.mouse.get_pos()
        self._panel(rect, radius=10)

        self._update_and_draw_stars(rect)

        top = pygame.Rect(rect.x + pad, rect.y + 16, rect.w - pad * 2, 38)
        ex_w = 108
        explore = pygame.Rect(top.x, top.y, ex_w, 36)
        self.home_rect = explore
        hov = explore.collidepoint(mx, my)
        pygame.draw.rect(self.screen, BG_HOVER if hov else BG_SOFT,
                         explore, border_radius=18)
        pygame.draw.rect(self.screen, ORO if hov else ORO_OSCURO,
                         explore, 1, border_radius=18)
        lbl = self.f_btn.render("explore", True,
                                ORO_BRILLANTE if hov else ORO)
        self.screen.blit(lbl, (explore.centerx - lbl.get_width() // 2,
                               explore.centery - lbl.get_height() // 2))

        s_left = explore.right + 12
        search = pygame.Rect(s_left, top.y, max(80, top.right - s_left), 36)
        self.search_rect = search
        focused = app.mode == "input"
        pygame.draw.rect(self.screen, BG_SOFT, search, border_radius=18)
        pygame.draw.rect(self.screen, ORO if focused else ORO_OSCURO,
                         search, 1, border_radius=18)
        if focused:
            if self.sel_all and app.buf:
                s = self.f_body.render(app.buf[-200:], True, TEXT)
                sel = pygame.Surface((s.get_width(), s.get_height()),
                                     pygame.SRCALPHA)
                sel.fill((80, 65, 30, 180))
                self.screen.blit(sel, (search.x + 18, search.y + 8))
                self.screen.blit(s, (search.x + 18, search.y + 8))
            else:
                caret = "|" if int(time.time() * 2) % 2 == 0 else " "
                self._text(app.buf[-200:] + caret, self.f_body, TEXT,
                           (search.x + 18, search.y + 8), search.w - 36)
        else:
            if app.search_mode == "playlists":
                hint = "search for a playlist"
            elif app.search_mode == "artists":
                hint = "search for an artist"
            elif app.search_mode == "links":
                hint = "paste a link (video/playlist/channel)"
            else:
                hint = "search"
            self._text(hint, self.f_body, TEXT_DIM,
                       (search.x + 18, search.y + 8), search.w - 36)

        content = pygame.Rect(rect.x, top.bottom + 8,
                              rect.w, rect.bottom - top.bottom - 20)
        if content.h < 40:
            return
        prev_clip = self.screen.get_clip()
        self.screen.set_clip(content)

        inner_x = content.x + pad
        inner_w = content.w - pad * 2

        card_w = 152
        cover = card_w
        gap = 16
        card_h = cover + 52
        n_cards = max(2, (inner_w + gap) // (card_w + gap))

        hist = app.profile["history"][:30]
        favs = app.profile["favorites"][:30]
        sugs = app.suggestions[:30]
        saved = app.saved_list()[:30]
        recs = app.recent_list()[:30]

        shelves = []
        if saved:
            shelves.append(("Saved playlists", saved, "open"))
        if recs:
            shelves.append(("Recent playlists", recs, "open_recent"))
        if hist:
            shelves.append(("Recently played", hist, "play"))
        if favs:
            shelves.append(("Your likes", favs, "play"))
        if sugs:
            shelves.append(("Suggestions for you", sugs, "play"))

        greet_h = 82
        shelf_title_h = 36
        shelf_h = shelf_title_h + card_h + 34
        total_h = greet_h + len(shelves) * shelf_h

        max_scroll = max(0, total_h - content.h + 16)
        app.home_scroll = max(0, min(app.home_scroll, max_scroll))

        hh = datetime.now().hour
        greet = ("Good morning" if hh < 12
                 else "Good afternoon" if hh < 20
                 else "Good evening")
        y = content.y + 8 - app.home_scroll
        if y + 30 > content.y - 4 and y < content.bottom:
            self._text(f"{greet}, {app.display}", self.f_greet,
                       ORO_BRILLANTE, (inner_x, y), inner_w - 4)
            msg = getattr(app, "startup_msg", "")
            if msg:
                self._text(msg, self.f_body, ORO_MATE,
                           (inner_x, y + 34), inner_w - 4)
        y += greet_h

        self.home_card_rects = []
        if not shelves:
            cxs = content.centerx
            cys = content.centery
            draw_heraldic_sun(self.screen, cxs, cys - 30, 32, ORO_OSCURO,
                              rays=16, ray_len=26)
            draw_sword_shape(self.screen, cxs - 30, cys + 30, -38,
                             70, 1.0, ORO, width=1)
            draw_sword_shape(self.screen, cxs + 30, cys + 30, 38,
                             70, 1.0, ORO, width=1)
            self._text("nothing here yet", self.f_body, TEXT_DIM,
                       (cxs - 100, cys + 70))
            self._text("search or play something to fill your home",
                       self.f_small, TEXT_DIM, (cxs - 140, cys + 94))
            self.screen.set_clip(prev_clip)
            return

        for title, items, action in shelves:
            if y + shelf_h < content.y - 60 or y > content.bottom + 60:
                y += shelf_h
                continue
            if y + shelf_title_h > content.y - 4 and y < content.bottom:
                self._panel_title(title, inner_x, y + 8)
                tw = self.f_h1.size(title)[0] + 16
                pygame.draw.line(self.screen, ORO_OSCURO,
                                 (inner_x + tw + 10, y + 16),
                                 (inner_x + inner_w, y + 16), 1)
            y += shelf_title_h

            for i in range(min(len(items), n_cards)):
                t = items[i]
                if action == "open":
                    vid_cover = t.get("thumb") or (
                        t.get("tracks", [{}])[0].get("id")
                        if t.get("tracks") else None)
                    title_t = t.get("title") or "?"
                    artist_t = f"{len(t.get('tracks', []))} tracks"
                elif action == "open_recent":
                    vid_cover = t.get("thumb") or (
                        t.get("tracks", [{}])[0].get("id")
                        if t.get("tracks") else None)
                    title_t = t.get("title") or "?"
                    artist_t = f"{len(t.get('tracks', []))} tracks"
                else:
                    vid_cover = t["id"]
                    title_t = t.get("title") or "?"
                    artist_t = t.get("artist") or "?"

                cx = inner_x + i * (card_w + gap)
                card = pygame.Rect(cx, y, card_w, card_h)
                if card.right < content.x - 20 or card.x > content.right + 20:
                    continue
                self.home_card_rects.append((card, items, i, action))

                cov = pygame.Rect(card.x, card.y, cover, cover)
                if vid_cover:
                    self._request_cover(vid_cover)
                    surf = self._get_cover(vid_cover, cover, cover)
                else:
                    surf = None
                if surf:
                    self.screen.blit(surf, cov.topleft)
                else:
                    pygame.draw.rect(self.screen, (26, 26, 26), cov,
                                     border_radius=6)

                is_playing = (action == "play" and a.cur and
                              t["id"] == a.cur["id"])
                hov_card = (card.collidepoint(mx, my) and
                            content.collidepoint(mx, my))
                if is_playing:
                    pygame.draw.rect(self.screen, ORO_BRILLANTE, cov, 2,
                                     border_radius=6)
                elif hov_card:
                    pygame.draw.rect(self.screen, ORO, cov, 1,
                                     border_radius=6)
                else:
                    pygame.draw.rect(self.screen, ORO_OSCURO, cov, 1,
                                     border_radius=6)

                ty = cov.bottom + 6
                for ln in _wrap(self.f_body, title_t, card_w, 2):
                    col = ORO_BRILLANTE if is_playing else TEXT
                    self._text(ln, self.f_body, col, (card.x, ty), card_w)
                    ty += 18
                self._text(artist_t, self.f_small, TEXT_MUT,
                           (card.x, ty), card_w)

            y += card_h + 34

        if total_h > content.h:
            tr = pygame.Rect(content.right - 10, content.y + 6, 4,
                             content.h - 12)
            pygame.draw.rect(self.screen, BG_SOFT, tr, border_radius=2)
            fv = content.h / total_h
            bh = max(28, int(tr.h * fv))
            denom = max(1, total_h - content.h)
            by = tr.y + int((tr.h - bh) * (app.home_scroll / denom))
            pygame.draw.rect(self.screen, ORO,
                             pygame.Rect(tr.x, by, 4, bh), border_radius=2)

        self.screen.set_clip(prev_clip)

    def _render_bottom(self, W, H, bottom_h):
        app = self.app
        a = app.audio
        t = a.cur
        pad = 14
        y0 = H - bottom_h + 8

        times_w = 110
        bar = pygame.Rect(pad, y0, W - 2 * pad - times_w, 10)
        self.progress_rect = bar
        pygame.draw.rect(self.screen, BG_SOFT, bar, border_radius=5)
        pygame.draw.rect(self.screen, ORO_OSCURO, bar, 1, border_radius=5)
        pos = a.pos()
        dur = (t or {}).get("duration")
        if dur and dur > 0:
            frac = min(1.0, pos / dur)
            fill_w = int(bar.w * frac)
            if fill_w > 0:
                fill = pygame.Rect(bar.x, bar.y, fill_w, bar.h)
                pygame.draw.rect(self.screen, ORO, fill, border_radius=5)
                px = bar.x + fill_w
                pygame.draw.circle(self.screen, ORO_BRILLANTE,
                                   (px, bar.centery), 5)
                pygame.draw.circle(self.screen, ORO_OSCURO,
                                   (px, bar.centery), 5, 1)
        self._text(f"{fmt(pos)}/{fmt(dur)}", self.f_small, ORO_MATE,
                   (bar.right + 10, y0 - 4))

        y2 = y0 + 24
        mx, my = pygame.mouse.get_pos()
        self.control_rects = []
        self.speed_rects = {"btns": []}
        self.pitch_rects = {"btns": []}

        def add(label, action, active=False, w=40, h=28, gold=False):
            nonlocal x
            r = pygame.Rect(x, y2, w, h)
            hover = r.collidepoint(mx, my)
            drawn = self._btn(x, y2, w, h, label, active, hover, gold=gold)
            self.control_rects.append((drawn, action, active))
            x += w + 4

        def small_btn(x_, label, action, store, w=28, h=28):
            r = pygame.Rect(x_, y2, w, h)
            hover = r.collidepoint(mx, my)
            bg = BG_HOVER if hover else BG_SOFT
            bd = ORO if hover else ORO_OSCURO
            col = TEXT if hover else ORO_MATE
            pygame.draw.rect(self.screen, bg, r, border_radius=5)
            pygame.draw.rect(self.screen, bd, r, 1, border_radius=5)
            surf = self.f_small.render(label, True, col)
            self.screen.blit(surf, (r.centerx - surf.get_width() // 2,
                                    r.centery - surf.get_height() // 2))
            store.append((r, action))
            return r

        x = pad
        add("|<", "prev", w=36)
        add("||" if (a.playing and not a.paused) else ">", "play",
            active=a.playing and not a.paused, w=42, gold=True)
        add(">|", "next", w=36)
        x += 8
        add("SHF", "shuffle", active=a.shuffle, w=38, gold=True)
        add("REP", "repeat", active=a.repeat != "off", w=38, gold=True)
        add("DL", "download", w=32)
        add("FAV", "fav", active=app.is_fav(a.cur), w=40, gold=True)
        add("SYNC", "sync_all", w=46)
        x += 10

        self._text("vol", self.f_small, ORO_MATE, (x, y2 + 8))
        x += 26
        vol = pygame.Rect(x, y2 + 10, 78, 8)
        self.volume_rect = vol
        pygame.draw.rect(self.screen, BG_SOFT, vol, border_radius=4)
        pygame.draw.rect(self.screen, ORO_OSCURO, vol, 1, border_radius=4)
        vf = pygame.Rect(vol.x, vol.y, int(vol.w * a.volume / 100), vol.h)
        if vf.w > 0:
            pygame.draw.rect(self.screen, ORO, vf, border_radius=4)
        x = vol.right + 6
        self._text(f"{a.volume}", self.f_small, ORO_MATE, (x, y2 + 8))
        x += 28
        x += 8

        self._text("spd", self.f_small, ORO_MATE, (x, y2 + 8))
        x += 24
        r = small_btn(x, "-", "speed_down", self.speed_rects["btns"])
        x = r.right + 3
        rv = pygame.Rect(x, y2, 50, 28)
        sp_active = abs(a.speed - 1.0) > 0.001
        pygame.draw.rect(self.screen, BG_SELECT if sp_active else BG_SOFT,
                         rv, border_radius=5)
        pygame.draw.rect(self.screen,
                         ORO_BRILLANTE if sp_active else ORO_OSCURO,
                         rv, 1, border_radius=5)
        lbl = self.f_small.render(f"x{a.speed:.2f}", True,
                                  ORO_BRILLANTE if sp_active else ORO)
        self.screen.blit(lbl, (rv.centerx - lbl.get_width() // 2,
                               rv.centery - lbl.get_height() // 2))
        self.speed_rects["btns"].append((rv, "speed_reset"))
        x = rv.right + 3
        r = small_btn(x, "+", "speed_up", self.speed_rects["btns"])
        x = r.right + 12

        self._text("pitch", self.f_small, ORO_MATE, (x, y2 + 8))
        x += 36
        r = small_btn(x, "-", "pitch_down", self.pitch_rects["btns"])
        x = r.right + 3
        rv = pygame.Rect(x, y2, 56, 28)
        pi_active = a.pitch != 0
        pygame.draw.rect(self.screen, BG_SELECT if pi_active else BG_SOFT,
                         rv, border_radius=5)
        pygame.draw.rect(self.screen,
                         ORO_BRILLANTE if pi_active else ORO_OSCURO,
                         rv, 1, border_radius=5)
        lbl = self.f_small.render(f"{a.pitch:+d} st", True,
                                  ORO_BRILLANTE if pi_active else ORO)
        self.screen.blit(lbl, (rv.centerx - lbl.get_width() // 2,
                               rv.centery - lbl.get_height() // 2))
        self.pitch_rects["btns"].append((rv, "pitch_reset"))
        x = rv.right + 3
        r = small_btn(x, "+", "pitch_up", self.pitch_rects["btns"])
        x = r.right + 10

        if app.msg and time.time() - app.msg_t < app.msg_dur:
            txt = app.msg
            tw = self.f_small.size(txt)[0]
            if x + tw + 10 < W - pad:
                self._text(txt, self.f_small, ORO_BRILLANTE,
                           (x, y2 + 8), W - pad - x - 4)
            else:
                self._text(txt, self.f_small, ORO_BRILLANTE,
                           (pad, y2 + 32), W - 2 * pad)
        elif a.pitch != 0 and a.playing:
            self._text("applying pitch...", self.f_small, ORO_MATE,
                       (x, y2 + 8), W - pad - x - 4)

    def _show_cleanup(self):
        W, H = self.W, self.H
        overlay = pygame.Surface((W, H), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 215))
        self.screen.blit(overlay, (0, 0))

        box = pygame.Rect(W // 2 - 280, H // 2 - 80, 560, 160)
        pygame.draw.rect(self.screen, BG_PANEL, box, border_radius=12)
        pygame.draw.rect(self.screen, ORO, box, 2, border_radius=12)
        msg = "deleting temporary files..."
        surf = self.f_h1.render(msg, True, ORO_BRILLANTE)
        self.screen.blit(surf, (box.centerx - surf.get_width() // 2,
                                box.y + 68))
        sub = self.f_small.render("this may take a moment",
                                  True, ORO_MATE)
        self.screen.blit(sub, (box.centerx - sub.get_width() // 2,
                               box.y + 98))
        pygame.display.flip()

        try:
            if PITCH_DIR.exists():
                shutil.rmtree(PITCH_DIR, ignore_errors=True)
        except Exception:
            pass
        try:
            if TMP_DIR.exists():
                shutil.rmtree(TMP_DIR, ignore_errors=True)
        except Exception:
            pass

        t0 = time.time()
        while time.time() - t0 < 1.2:
            for _ in pygame.event.get():
                pass
            self.clock.tick(60)

    def run(self):
        app = self.app
        while app.running:
            for ev in pygame.event.get():
                self.handle_event(ev)
            app.audio.tick()
            self._pump_covers()
            self.render()
            pygame.display.flip()
            self.clock.tick(60)
        try:
            self._show_cleanup()
        except Exception:
            pass
        app.save()


def main():
    DATA.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    pygame.init()

    user = _system_username()
    startup_msg = _pick_startup_message(user)

    try:
        intro_animation()
    except Exception:
        pass

    store = load_store()
    if user not in store["users"]:
        store["users"][user] = new_profile()
    u = store["users"][user]
    for k, v in new_profile().items():
        u.setdefault(k, v)
    u["sessions"] = u.get("sessions", 0) + 1
    u["last_login"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_store(store)

    app = App(user, store)
    app.startup_msg = startup_msg
    ui = PygameUI(app)
    if app.saved_list():
        threading.Thread(target=app.sync_all_saved, daemon=True).start()
    try:
        ui.run()
    except KeyboardInterrupt:
        try:
            shutil.rmtree(PITCH_DIR, ignore_errors=True)
            shutil.rmtree(TMP_DIR, ignore_errors=True)
        except Exception:
            pass
    finally:
        app.save()
        pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            DATA.mkdir(exist_ok=True)
            (DATA / "error.log").write_text(tb, encoding="utf-8")
        except Exception:
            pass
        print(tb)
        _pause_exit()