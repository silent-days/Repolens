#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RepoLens v4.0 — core layer.

Shared GitHub API client, configuration and helpers used by both the
GUI edition and the terminal edition. Standard library only.

New in v4.0 — "daylight" pack:
  · LIGHT REPORTS — the built-in PDF engine now writes a crisp light
    A4 document (azure header band, light chips, readable tables) to
    match the Daylight Light app
  · full RepoLens rebrand — reports, exports and metadata all carry
    the new name and version
  · save_pdf() is bullet-proof — every profile/repo field is read with
    safe defaults, so cached or odd data can never crash the PDF
    engine; it also accepts an explicit output path (out=...) for the
    GUI's Save-As download flow
  · everything from v3 stays: polished A4 PDF reports from a tiny
    built-in engine, smart cache, retry with backoff, live rate-limit,
    Repo Hunt, TXT / JSON / CSV / Markdown exports

From v2.1 — "daylight pack":
  · PDF reports — one click produces a polished A4 PDF (branded
    header, stats chips, language bar, repo table, activity) written
    by a tiny built-in PDF engine, no external libraries needed

From v2.0 — "power pack":
  · smart response cache (TTL) — repeat lookups are instant and
    never burn rate limit
  · automatic retry with backoff on network errors / server hiccups
  · live rate-limit tracking (X-RateLimit headers) surfaced in the UI
  · global repository search engine (Repo Hunt)
  · language breakdown analytics
"""

import copy
import csv
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "4.0"
APP_NAME = "RepoLens"
TAGLINE = "GitHub Account & Repository Inspector"
PROFILE_TAGLINE = "See every repository clearly."
REPO_URL = "https://github.com/repolens-app/RepoLens"
AUTHOR = "repolens-app"
LICENSE_NAME = "Proprietary — free for personal use"

# single source of truth for the About dialog (GUI + CLI)
ABOUT = {
    "name": APP_NAME,
    "version": VERSION,
    "edition": "Daylight",
    "tagline": TAGLINE,
    "motto": PROFILE_TAGLINE,
    "description": (
        "RepoLens is a fast, animated desktop tool for exploring publicly "
        "available GitHub account and repository information. Search users "
        "by username, full name or email; inspect profiles, repositories, "
        "gists and public activity; hunt repositories across all of GitHub "
        "with qualifier queries; clone public projects; and export reports "
        "as PDF, TXT, JSON, CSV or Markdown — all from one clean light "
        "interface that boots in about a second."
    ),
    "features": [
        "User search — username, full name or email",
        "Profile inspector — avatar, stats, orgs, language mix",
        "Repo Hunt — global repository search with qualifiers",
        "Sortable tables, search history, live rate-limit budget",
        "Clone public repos with adjustable shallow depth",
        "One-click PDF / TXT / JSON / CSV / Markdown reports",
        "Fast boot splash, API cache and retry with backoff",
        "Terminal edition for SSH / headless machines",
    ],
    "shortcuts": [
        "Enter — run the search", "↑ / ↓ — search history",
        "F5 / ↻ — reload the profile", "F1 — this About dialog",
        "Ctrl+P / Ctrl+S — save the PDF report", "Esc — close dialogs",
    ],
    "author": AUTHOR,
    "license": LICENSE_NAME,
}

CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "repolens"
CONF_FILE = CONF_DIR / "config.json"
REPORT_DIR = Path.home() / "repolens-reports"
API = "https://api.github.com"

CONF = {"token": "", "clone_dir": str(Path.home() / "github")}

# extra keys are optional; old configs keep working
CONF_DEFAULTS = {
    "repo_sort": "Best match",
    "history": [],
    "splash_on": True,      # show the fast boot splash
    "splash_ms": 1200,      # splash duration in milliseconds (400–3000)
    "clone_depth": 1,       # git shallow depth; 0 = full history
    "history_max": 10,      # remembered search terms (1–25)
}


class ApiError(Exception):
    """Raised for any GitHub API / network problem with a friendly message."""


# ---------------------------------------------------------------- config ---

def load_conf():
    """Load configuration from disk (compatible with v1.0 configs)."""
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if CONF_FILE.exists():
        try:
            CONF.update(json.loads(CONF_FILE.read_text()))
        except Exception:
            pass
    CONF.setdefault("token", "")
    CONF.setdefault("clone_dir", str(Path.home() / "github"))
    for k, v in CONF_DEFAULTS.items():
        CONF.setdefault(k, copy.deepcopy(v))


def save_conf():
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        CONF_FILE.write_text(json.dumps(CONF, indent=2))
        os.chmod(CONF_FILE, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ rate ---

RATE = {"limit": None, "remaining": None, "reset": None, "last_path": ""}


def _note_rate(headers, path=""):
    """Track X-RateLimit headers so the UI can show a live budget."""
    try:
        if "X-RateLimit-Limit" in headers:
            RATE["limit"] = int(headers["X-RateLimit-Limit"])
            RATE["remaining"] = int(headers["X-RateLimit-Remaining"])
            RATE["reset"] = int(headers.get("X-RateLimit-Reset", 0) or 0)
            RATE["last_path"] = path
    except (TypeError, ValueError):
        pass


def rate_text():
    """'42/60' style remaining/limit string, or 'n/a' when unknown."""
    if RATE["limit"] is None:
        return "n/a"
    return f"{RATE['remaining']}/{RATE['limit']}"


def rate_limit_status():
    """Explicit /rate_limit probe (does not itself count against budget)."""
    data = gh_api("/rate_limit")
    core = data.get("resources", {}).get("core", {})
    _note_rate({
        "X-RateLimit-Limit": core.get("limit"),
        "X-RateLimit-Remaining": core.get("remaining"),
        "X-RateLimit-Reset": core.get("reset"),
    }, "/rate_limit")
    return RATE


# ------------------------------------------------------------------ api ----

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": f"RepoLens/{VERSION}",
}

_CACHE = {}
_CACHE_TTL = 300          # seconds for normal endpoints
_CACHE_TTL_SEARCH = 90    # search results go stale faster


def clear_cache():
    """Drop every cached API response."""
    _CACHE.clear()


def cache_size():
    return len(_CACHE)


def _headers():
    h = dict(_HEADERS)
    if CONF["token"]:
        h["Authorization"] = "Bearer " + CONF["token"]
    return h


def _search_path(path):
    return path.startswith("/search/")


def gh_api(path, params=None, cache_ttl=None):
    """GET one JSON document from the GitHub API.

    · caches responses (TTL: 300 s normal / 90 s search endpoints)
    · retries transient network + 5xx failures with a short backoff
    · tracks the rate-limit budget from response headers
    """
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    key = (path, tuple(sorted((params or {}).items())))
    now = time.monotonic()
    ttl = _CACHE_TTL_SEARCH if _search_path(path) else _CACHE_TTL
    if cache_ttl is not None:
        ttl = cache_ttl
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return copy.deepcopy(hit[1])

    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                _note_rate(r.headers, path)
                data = json.load(r)
            _CACHE[key] = (now, data)
            return copy.deepcopy(data)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get("message", str(e))
            except Exception:
                msg = str(e)
            low = msg.lower()
            if e.code == 404:
                raise ApiError("No such account found (404).") from None
            if e.code == 403 and ("rate limit" in low or "secondary rate" in low):
                if _search_path(path):
                    raise ApiError(
                        "Search rate limit reached. Wait a minute or add a token in Settings."
                    ) from None
                raise ApiError("API rate limit reached — add a token in Settings.") from None
            if e.code == 401:
                raise ApiError("Invalid token — set it again in Settings.") from None
            if e.code == 422:
                raise ApiError("GitHub could not process that search query (422).") from None
            if e.code >= 500 and attempt < 2:      # server hiccup → backoff
                last = ApiError(f"{e.code}: {msg}")
                time.sleep(0.6 * (attempt + 1))
                continue
            raise ApiError(f"{e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            last = ApiError(f"Network problem: {reason}")
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
                continue
    raise last or ApiError("Request failed.")


def gh_api_all(path, params=None, cap=600):
    """Fetch every page (up to cap) of a paginated endpoint."""
    out, page = [], 1
    while len(out) < cap:
        p = dict(params or {})
        p.update({"per_page": 100, "page": page})
        chunk = gh_api(path, p)
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return out[:cap]


SEARCH_MODES = {"Username": "in:login", "Full Name": "in:name", "Email": "in:email"}


def search_users(text, mode, per_page=20):
    """Search GitHub users by login / full name / email."""
    qual = SEARCH_MODES.get(mode, "in:login")
    q = f"{text} {qual}" if qual != "in:login" else text
    data = gh_api("/search/users", {"q": q, "per_page": per_page})
    return data.get("items", [])


REPO_SORTS = {"Best match": None, "Stars": "stars", "Forks": "forks", "Recently updated": "updated"}


def search_repositories(text, sort=None, per_page=30):
    """Global repository search (Repo Hunt).

    `text` may use GitHub qualifiers, e.g.
        "tetris language:python stars:>500"  ·  "neovim topic:plugin"
    Returns (items, total_count).
    """
    q = {"q": text.strip(), "per_page": per_page}
    if sort and REPO_SORTS.get(sort):
        q["sort"] = REPO_SORTS[sort]
    data = gh_api("/search/repositories", q)
    return data.get("items", []), data.get("total_count", 0)


# ---------------------------------------------------------------- output ---

def fmt(n):
    """1234 -> '1,234'."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def time_ago(iso):
    """'2026-01-02T10:00:00Z' -> '10 months ago' (coarse)."""
    try:
        d = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return (iso or "")[:10]
    s = max(0, int(time.time() - d.timestamp()))
    for div, label in ((31536000, "year"), (2592000, "month"), (604800, "week"),
                       (86400, "day"), (3600, "hour"), (60, "minute")):
        if s >= div:
            n = s // div
            return f"{n} {label}{'s' if n != 1 else ''} ago"
    return "just now"


def open_browser(url):
    """Open a URL in the default browser. Returns True on success."""
    try:
        if subprocess.Popen(["xdg-open", url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).poll() is None:
            return True
    except (FileNotFoundError, OSError):
        pass
    return False


def clone_repo(full, dest_dir, on_line=None, on_done=None, depth=1):
    """Clone `owner/name` into dest_dir. Streams git progress.

    depth: 1 (default) = shallow clone (fast), 0 = full history.
    on_line(text) is called for each progress line; on_done(ok, message)
    once finished. Runs synchronously — call from a worker thread.
    """
    dest = Path(dest_dir).expanduser() / full.split("/")[-1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if on_done:
            on_done(False, f"Already exists: {dest}")
        return
    cmd = ["git", "clone", "--progress"]
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        depth = 1
    if depth > 0:
        cmd += ["--depth", str(depth)]
    cmd += [f"https://github.com/{full}.git", str(dest)]
    try:
        p = subprocess.Popen(cmd,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                             )
    except FileNotFoundError:
        if on_done:
            on_done(False, "git is not installed: sudo apt install git")
        return
    for line in p.stderr or []:
        line = line.strip()
        if line and on_line:
            on_line(line.splitlines()[-1][:90])
    p.wait()
    if p.returncode == 0:
        if on_done:
            on_done(True, f"Cloned → {dest}")
    else:
        if on_done:
            on_done(False, "Clone failed — check the repo URL / connection.")


# ------------------------------------------------------------------ misc --

def fetch_bytes(url, timeout=25):
    """Download raw bytes (used for profile avatars)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"RepoLens/{VERSION}",
        "Accept": "image/png,image/*;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def language_breakdown(repos, top=6):
    """[(language, repo_count, share 0..1)] for the biggest languages."""
    counts = {}
    for r in repos or []:
        if not isinstance(r, dict):
            continue
        l = r.get("language")
        if l:
            counts[l] = counts.get(l, 0) + 1
    pairs = sorted(counts.items(), key=lambda kv: -kv[1])[:top]
    total = sum(c for _, c in pairs) or 1
    return [(l, c, c / total) for l, c in pairs]


# ---------------------------------------------------------------- reports --

def _report_base(login):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR / f"{login}-{stamp}"


def open_folder(path):
    """Reveal a saved file in the desktop file manager — never blocks.

    Windows → Explorer selects the file, macOS → Finder reveals it,
    Linux/BSD → xdg-open opens the containing folder. Any failure is
    silent (the report is saved either way).
    """
    path = Path(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path.parent))            # noqa: no cover on POSIX
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(path.parent)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _report_paths(login):
    b = _report_base(login)
    return b.with_suffix(".txt"), b.with_suffix(".json")


def save_report(u, repos, gists, events):
    """Write the classic .txt report. Returns the file path."""
    f, _ = _report_paths(u["login"])
    L = [f"RepoLens report — @{u['login']}",
         f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
         "=" * 60,
         f"Name     : {u.get('name') or '-'}",
         f"Type     : {u['type']}",
         f"Company  : {u.get('company') or '-'}",
         f"Location : {u.get('location') or '-'}",
         f"Bio      : {u.get('bio') or '-'}",
         f"Blog     : {u.get('blog') or '-'}",
         f"Twitter  : {u.get('twitter_username') or '-'}",
         f"Email    : {u.get('email') or '-'}",
         f"Repos    : {u.get('public_repos')}  Gists: {u.get('public_gists')}",
         f"Followers: {u.get('followers')}  Following: {u.get('following')}",
         f"Created  : {u['created_at'][:10]}  Updated: {u['updated_at'][:10]}",
         f"Profile  : {u['html_url']}", "=" * 60, "", "REPOSITORIES:"]
    for r in repos:
        L.append(f"- {r['full_name']}  ★{r['stargazers_count']} ⑂{r['forks_count']} "
                 f"{r.get('language') or 'n/a'}  {r['updated_at'][:10]}")
        if r.get("description"):
            L.append(f"    {r['description']}")
        if r.get("topics"):
            L.append(f"    topics: {', '.join(r['topics'])}")
    L += ["", "GISTS:"]
    for g in gists:
        L.append(f"- {g['html_url']}  {g.get('description') or ''}")
    L += ["", "RECENT ACTIVITY:"]
    for e in events:
        L.append(f"- {e['created_at'][:19]}  {e['type']}  {e['repo']['name']}")
    f.write_text("\n".join(L))
    return f


def save_json(u, repos, gists, events):
    """Write the .json export. Returns the file path."""
    _, f = _report_paths(u["login"])
    payload = {
        "tool": f"RepoLens {VERSION}",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "profile": u,
        "stats": stats_summary(u, repos),
        "languages": language_breakdown(repos),
        "repositories": repos,
        "gists": gists,
        "recent_activity": events,
    }
    f.write_text(json.dumps(payload, indent=2))
    return f


def save_csv(u, repos, gists=None, events=None):
    """Write the repositories table as .csv. Returns the file path."""
    f = _report_base(u["login"]).with_suffix(".csv")
    cols = ["name", "full_name", "language", "stars", "forks", "watchers",
            "open_issues", "archived", "is_fork", "pushed_at", "url", "description"]
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["# RepoLens v" + VERSION + " — @" + u["login"],
                    datetime.now().isoformat(timespec="seconds")])
        w.writerow(cols)
        for r in repos:
            w.writerow([r.get("name", ""), r.get("full_name", ""),
                        r.get("language") or "", r.get("stargazers_count", 0),
                        r.get("forks_count", 0), r.get("watchers_count", 0),
                        r.get("open_issues_count", 0),
                        "yes" if r.get("archived") else "no",
                        "yes" if r.get("fork") else "no",
                        (r.get("pushed_at") or "")[:10],
                        r.get("html_url", ""),
                        (r.get("description") or "").replace("\n", " ")[:300]])
    return f


def save_markdown(u, repos, gists, events):
    """Write a .md report. Returns the file path."""
    f = _report_base(u["login"]).with_suffix(".md")
    s = stats_summary(u, repos)
    L = [f"# RepoLens report — @{u['login']}",
         f"*Generated {datetime.now():%Y-%m-%d %H:%M:%S} with RepoLens v{VERSION}*",
         "",
         f"**{u.get('name') or u['login']}** · {u['type']} · "
         f"[{u['html_url']}]({u['html_url']})",
         "",
         f"> {u.get('bio') or '_No bio._'}",
         "",
         f"| Followers | Following | Repos | Gists | ⭐ Total | ⑂ Total | Top language |",
         f"|---:|---:|---:|---:|---:|---:|---|",
         f"| {u.get('followers', 0)} | {u.get('following', 0)} | "
         f"{u.get('public_repos', 0)} | {u.get('public_gists', 0)} | "
         f"{s['stars']} | {s['forks']} | {s['top_language']} |",
         "",
         "## Repositories",
         "",
         "| # | Repository | Lang | ⭐ | ⑂ | Updated |",
         "|---:|---|---|---:|---:|---|"]
    for i, r in enumerate(repos, 1):
        L.append(f"| {i} | [{r['name']}]({r['html_url']}) | "
                 f"{r.get('language') or '—'} | {r['stargazers_count']} | "
                 f"{r['forks_count']} | {r['updated_at'][:10]} |")
        if r.get("description"):
            L.append(f"| | *{r['description'][:120]}* | | | | |")
    if gists:
        L += ["", "## Gists", ""]
        for g in gists:
            desc = (g.get("description") or "(no description)").strip()
            L.append(f"- [{desc}]({g['html_url']})")
    if events:
        L += ["", "## Recent activity", "",
              "| When | Event | Repository |", "|---|---|---|"]
        for e in events:
            L.append(f"| {e['created_at'][:16].replace('T', ' ')} | "
                     f"{e.get('type', '')} | {e['repo']['name']} |")
    f.write_text("\n".join(L))
    return f


# ------------------------------------------------------------------- pdf ---
# A tiny built-in PDF writer (PDF 1.4, Helvetica core fonts, WinAnsi).
# Zero external dependencies — the report is drawn with plain content
# stream operators, so the PDF button works on any fresh Linux box.

_PDF_W, _PDF_H = 595, 842          # A4 portrait, points
_PDF_M = 48                        # page margin

# Daylight Light brand palette (matches the app's light look: white
# surfaces, azure signal, light-safe accents)
_INK    = (0.122, 0.161, 0.216)    # #1F2933 headings / emphasis text
_BAND   = (0.941, 0.953, 0.973)    # #F0F3F8 light header band
_CARD   = (0.976, 0.982, 0.988)    # #F9FAFC chips / table header wash
_BORDER = (0.824, 0.855, 0.890)    # #D2DAE3 hairlines
_GREEN  = (0.102, 0.498, 0.216)    # #1A7F37 emerald
_BLUE   = (0.035, 0.412, 0.855)    # #0969DA azure
_VIOLET = (0.510, 0.314, 0.875)    # #8250DF violet
_AMBER  = (0.604, 0.404, 0.000)    # #9A6700 amber (light-safe)
_RED    = (0.812, 0.133, 0.180)    # #CF222E red
_WHITE  = (1.000, 1.000, 1.000)    # pure white (marks on colour fills)
_GREY   = (0.431, 0.478, 0.541)    # #6E7681 secondary text
_ZEBRA  = (0.965, 0.973, 0.980)    # #F6F8FA row wash
_TEXT   = (0.122, 0.161, 0.216)    # #1F2933 body text

# rough Helvetica advance widths (em fractions) for wrap / truncate math
_HELV_NARROW = set("iljtfrI.,:;'`|!()[]{}-")
_HELV_WIDE = set("mwMW@")


def _tw(s, size):
    """Approximate rendered width of `s` in Helvetica at `size` pt."""
    w = 0.0
    for ch in s:
        if ch in _HELV_NARROW:
            w += 0.31
        elif ch in _HELV_WIDE:
            w += 0.86
        elif ch == " ":
            w += 0.28
        elif ch.isupper() or ch.isdigit():
            w += 0.64
        else:
            w += 0.52
    return w * size


def _latin(s):
    """WinAnsi-safe text: cp1252 encodable, emoji folded to plain marks."""
    if not s:
        return ""
    table = str.maketrans({
        "★": "*", "☆": "*", "⭐": "*", "⑂": "F", "❄": "*",
        "→": "->", "←": "<-", "⇒": "=>", "✓": "v", "✔": "v", "✗": "x",
        "…": "...", " ": " ", "​": "", "️": "",
    })
    s = s.translate(table)
    return s.encode("cp1252", errors="replace").decode("cp1252")


def _esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(s, size, max_w):
    words, lines, cur = _latin(s).split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if cur and _tw(t, size) > max_w:
            lines.append(cur)
            cur = wd
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines or [""]


def _clip(s, size, max_w):
    s = _latin(s)
    if _tw(s, size) <= max_w:
        return s
    while s and _tw(s + "...", size) > max_w:
        s = s[:-1]
    return s + "..."


class _Pdf:
    """Minimal one-file PDF builder — pages of raw content-stream ops."""

    def __init__(self):
        self.pages = []          # list[list[str]] of ops per page
        self.cur = None
        self._page()

    # -- object plumbing ----------------------------------------------------
    def _page(self):
        self.cur = []
        self.pages.append(self.cur)

    def op(self, s):
        self.cur.append(s)

    def _rgb(self, c):
        return f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f}"

    # -- primitives ----------------------------------------------------------
    def fill(self, x, y, w, h, c):
        self.op(f"{self._rgb(c)} rg {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f")

    def stroke_rect(self, x, y, w, h, c, lw=0.7):
        self.op(f"{self._rgb(c)} RG {lw} w {x:.1f} {y:.1f} {w:.1f} {h:.1f} re S")

    def line(self, x1, y1, x2, y2, c, lw=0.7):
        self.op(f"{self._rgb(c)} RG {lw} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def rrect(self, x, y, w, h, r, c, filled=True, stroked=False, sc=None):
        """Rounded rectangle via bezier corners."""
        k = 0.5523 * r
        p = [f"{self._rgb(c)} rg"] if filled else []
        if stroked:
            p.append(f"{self._rgb(sc or c)} RG 0.7 w")
        p.append(f"{x + r:.1f} {y:.1f} m")
        p.append(f"{x + w - r:.1f} {y:.1f} l")
        p.append(f"{x + w - r + k:.1f} {y:.1f} {x + w:.1f} {y + r - k:.1f} "
                 f"{x + w:.1f} {y + r:.1f} c")
        p.append(f"{x + w:.1f} {y + h - r:.1f} l")
        p.append(f"{x + w:.1f} {y + h - r + k:.1f} {x + w - r + k:.1f} {y + h:.1f} "
                 f"{x + w - r:.1f} {y + h:.1f} c")
        p.append(f"{x + r:.1f} {y + h:.1f} l")
        p.append(f"{x + r - k:.1f} {y + h:.1f} {x:.1f} {y + h - r + k:.1f} "
                 f"{x:.1f} {y + h - r:.1f} c")
        p.append(f"{x:.1f} {y + r:.1f} l")
        p.append(f"{x:.1f} {y + r - k:.1f} {x + r - k:.1f} {y:.1f} "
                 f"{x + r:.1f} {y:.1f} c")
        if filled and stroked:
            p.append("B")
        elif filled:
            p.append("f")
        else:
            p.append("S")
        self.op(" ".join(p))

    def text(self, x, y, s, size=9, bold=False, italic=False, c=_TEXT):
        font = "/F2" if bold else ("/F3" if italic else "/F1")
        self.op(f"{self._rgb(c)} rg BT {font} {size:.1f} Tf "
                f"1 0 0 1 {x:.1f} {y:.1f} Tm ({_esc(_latin(s))}) Tj ET")

    def text_r(self, xr, y, s, size=9, bold=False, c=_TEXT):
        self.text(xr - _tw(_latin(s), size), y, s, size, bold, False, c)

    def text_c(self, xc, y, s, size=9, bold=False, c=_TEXT):
        self.text(xc - _tw(_latin(s), size) / 2, y, s, size, bold, False, c)

    def snowflake(self, cx, cy, r, c=_WHITE, lw=0.9):
        """Six-arm vector mark — the decorative brand glyph on the PDF
        header, no font needed."""
        self.op(f"{self._rgb(c)} RG {lw} w")
        for i in range(6):
            a = math.radians(i * 60)
            dx, dy = math.cos(a) * r, math.sin(a) * r
            self.op(f"{cx:.1f} {cy:.1f} m {cx + dx:.1f} {cy + dy:.1f} l S")
            for t in (0.55, 0.78):          # small V branches on each spoke
                bx, by = cx + dx * t, cy + dy * t
                for sgn in (-1, 1):
                    aa = a + sgn * math.radians(40)
                    self.op(f"{bx:.1f} {by:.1f} m "
                            f"{bx + math.cos(aa) * r * 0.22:.1f} "
                            f"{by + math.sin(aa) * r * 0.22:.1f} l S")

    # -- save ----------------------------------------------------------------
    def save(self, path, footer_left, footer_right):
        n = len(self.pages)
        for i, page in enumerate(self.pages):
            fy = 34
            page.append(f"{self._rgb(_BORDER)} RG 0.6 w "
                        f"{_PDF_M} {fy + 12} m {_PDF_W - _PDF_M} {fy + 12} l S")
            page.append(f"{self._rgb(_GREY)} rg BT /F1 7 Tf "
                        f"1 0 0 1 {_PDF_M} {fy} Tm ({_esc(footer_left)}) Tj ET")
            pr = f"page {i + 1} of {n}"
            page.append(f"{self._rgb(_GREY)} rg BT /F1 7 Tf 1 0 0 1 "
                        f"{_PDF_W - _PDF_M - _tw(pr, 7):.1f} {fy} Tm "
                        f"({_esc(pr)}) Tj ET")

        objs = {}
        objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"
        kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
        objs[2] = (f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
        objs[3] = ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   "/Encoding /WinAnsiEncoding >>")
        f2, f3 = 3 + 2 * n + 1, 3 + 2 * n + 2
        objs[f2] = ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                    "/Encoding /WinAnsiEncoding >>")
        objs[f3] = ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique "
                    "/Encoding /WinAnsiEncoding >>")
        for i, page in enumerate(self.pages):
            stream = "\n".join(page)
            pno, cno = 4 + 2 * i, 5 + 2 * i
            objs[pno] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PDF_W} "
                         f"{_PDF_H}] /Resources << /Font << /F1 3 0 R "
                         f"/F2 {f2} 0 R /F3 {f3} 0 R >> >> /Contents {cno} 0 R >>")
            objs[cno] = (f"<< /Length {len(stream.encode('cp1252', 'replace'))} >>\n"
                         f"stream\n{stream}\nendstream")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = {}
        for num in sorted(objs):
            offsets[num] = len(out)
            out += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode("cp1252", "replace")
        xref = len(out)
        total = len(objs) + 1
        out += f"xref\n0 {total}\n0000000000 65535 f \n".encode()
        for num in sorted(objs):
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref}\n"
                f"%%EOF").encode()
        path.write_bytes(bytes(out))


def save_pdf(u, repos, gists, events, out=None):
    """Write the polished A4 .pdf report. Returns the file path.

    Daylight-branded: light header band with the vector lens mark and
    the profile tagline "See every repository clearly.", stats chips,
    language-mix bar, repository table, gists and recent activity.

    v3: pass out=<path> to write exactly there (the GUI Save-As flow);
    every field is read defensively so odd or cached data can never
    raise — anything missing simply renders as "—".
    """
    u = u or {}
    repos = repos or []
    gists = gists or []
    events = events or []
    if out is not None:
        f = Path(out)
        f.parent.mkdir(parents=True, exist_ok=True)
    else:
        f = _report_base(u.get("login") or "user").with_suffix(".pdf")
    login = u.get("login") or "user"
    d = _Pdf()
    s = stats_summary(u, repos)
    langs = language_breakdown(repos, top=6)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    M, RGT = _PDF_M, _PDF_W - _PDF_M
    usable = RGT - M

    # ---- light daylight header band ----------------------------------------
    d.fill(0, _PDF_H - 128, _PDF_W, 128, _BAND)
    d.fill(0, _PDF_H - 132, _PDF_W, 4, _BLUE)
    d.snowflake(M + 18, _PDF_H - 64, 15, _BLUE)
    d.text(M + 46, _PDF_H - 52, "R E P O L E N S", 9, True, False, _BLUE)
    d.text(M + 46, _PDF_H - 76, f"Report — @{login}", 21, True, False, _INK)
    d.text(M + 47, _PDF_H - 94, f"* {PROFILE_TAGLINE}", 9, False, True, (0.42, 0.48, 0.55))
    d.text_r(RGT, _PDF_H - 52, f"Generated {now}", 8, False, (0.42, 0.48, 0.55))
    d.text_r(RGT, _PDF_H - 66, f"Daylight edition v{VERSION}", 8, False, (0.42, 0.48, 0.55))
    d.text_r(RGT, _PDF_H - 80, u.get("html_url") or "—", 8, False, _BLUE)

    # ---- profile facts block ------------------------------------------------
    y = _PDF_H - 158
    d.rrect(M, y - 58, usable, 64, 6, (0.972, 0.976, 0.984), stroked=True, sc=_BORDER)
    facts_l = [
        ("Name", u.get("name") or "-"), ("Type", u.get("type") or "User"),
        ("Company", u.get("company") or "-"), ("Location", u.get("location") or "-"),
    ]
    facts_r = [
        ("Blog", _clip(u.get("blog") or "-", 8, 190)),
        ("Twitter", u.get("twitter_username") or "-"),
        ("Email", _clip(u.get("email") or "-", 8, 190)),
        ("Joined", f"{(u.get('created_at') or '')[:10]}   ·   "
                   f"updated {(u.get('updated_at') or '')[:10]}"),
    ]
    for i, (k, v) in enumerate(facts_l):
        yy = y - 12 - i * 13
        d.text(M + 12, yy, k.upper(), 6.5, True, False, _GREY)
        d.text(M + 76, yy, str(v)[:44], 8.5, False, False, _TEXT)
    for i, (k, v) in enumerate(facts_r):
        yy = y - 12 - i * 13
        d.text(M + 258, yy, k.upper(), 6.5, True, False, _GREY)
        d.text(M + 318, yy, str(v), 8.5, False, False, _TEXT)
    bio = _wrap(u.get("bio") or "", 8, usable - 30)
    if bio and bio[0]:
        d.line(M + 10, y - 54, RGT - 10, y - 54, _BORDER, 0.5)
        d.text(M + 12, y - 70, _clip("Bio: " + " ".join(bio), 8, usable - 28),
               8, False, True, _GREY)
        y -= 16
    y -= 74

    # ---- stats chips ---------------------------------------------------------
    chips = [
        ("REPOS", f"{s['loaded']}/{s['repos']}", _GREEN),
        ("STARS", str(s["stars"]), _AMBER),
        ("FORKS", str(s["forks"]), _BLUE),
        ("FOLLOWERS", str(s["followers"]), _VIOLET),
        ("GISTS", str(s["gists"]), _GREEN),
    ]
    cw = (usable - 4 * 8) / 5
    for i, (lab, val, col) in enumerate(chips):
        cx = M + i * (cw + 8)
        d.rrect(cx, y - 34, cw, 34, 5, _CARD, stroked=True, sc=_BORDER)
        d.text_c(cx + cw / 2, y - 15, val, 12, True, _INK)
        d.text_c(cx + cw / 2, y - 27, lab, 6, True, col)
    y -= 50

    # ---- language mix bar -----------------------------------------------------
    if langs:
        d.text(M, y, "LANGUAGE MIX", 7.5, True, False, _GREY)
        bar_y, bh = y - 16, 7
        seg_pal = [_GREEN, _BLUE, _VIOLET, _AMBER, _RED, _GREY]
        x = M
        for i, (name, cnt, share) in enumerate(langs):
            seg = max(2.0, usable * share)
            d.fill(x, bar_y, seg - 1.5, bh, seg_pal[i % len(seg_pal)])
            x += seg
        lx = M
        for i, (name, cnt, share) in enumerate(langs):
            d.fill(lx, y - 32, 5, 5, seg_pal[i % len(seg_pal)])
            t = f"{name} {int(share * 100)}%"
            d.text(lx + 8, y - 31, t, 7, False, False, _GREY)
            lx += 12 + _tw(t, 7) + 10
        y -= 46

    # ---- sections -------------------------------------------------------------
    def section(title, sub=""):
        nonlocal y
        if y < 120:
            d._page()
            d.text(M, _PDF_H - 40, f"RepoLens — @{u['login']}", 8, True, False, _GREY)
            y = _PDF_H - 62
        d.fill(M, y - 11, 3, 12, _GREEN)
        d.text(M + 10, y - 10, title, 12, True, False, _INK)
        if sub:
            d.text_r(RGT, y - 9, sub, 7.5, False, _GREY)
        y -= 20

    # repositories table
    section("REPOSITORIES", f"{len(repos)} loaded")
    if repos:
        cols = {"n": (M, 26), "repo": (M + 26, 196), "lang": (M + 222, 78),
                "stars": (M + 300, 44), "forks": (M + 348, 44),
                "upd": (M + 396, usable - 396)}
        d.rrect(M, y - 15, usable, 16, 0, _CARD, stroked=True, sc=_BORDER)
        heads = [("n", "#"), ("repo", "REPOSITORY"), ("lang", "LANGUAGE"),
                 ("stars", "STARS"), ("forks", "FORKS"), ("upd", "UPDATED")]
        for key, lab in heads:
            x, wd = cols[key]
            if key in ("stars", "forks", "upd"):
                d.text_r(x + wd - 4, y - 11, lab, 6.5, True, _INK)
            else:
                d.text(x + 4, y - 11, lab, 6.5, True, False, _INK)
        y -= 16
        for i, r in enumerate(repos):
            r = r if isinstance(r, dict) else {}
            desc = (r.get("description") or "").strip()
            topics = r.get("topics") or []
            desc_lines = _wrap(desc, 7, usable - 60)[:2] if desc else []
            rh = 15 + len(desc_lines) * 9 + (9 if topics else 0)
            if y - rh < 56:
                d._page()
                d.text(M, _PDF_H - 40, f"RepoLens — @{login}", 8, True, False, _GREY)
                y = _PDF_H - 62
            if i % 2 == 0:
                d.fill(M, y - rh + 3, usable, rh - 1, _ZEBRA)
            d.text(M + 4, y - 11, str(i + 1), 8, False, False, _GREY)
            name = _clip(str(r.get("name") or "repo"), 8.5, cols["repo"][1] - 8)
            d.text(M + 30, y - 11, name, 8.5, True, False, _BLUE)
            lang = _clip(r.get("language") or "—", 7.5, cols["lang"][1] - 8)
            d.text(M + 226, y - 11, lang, 7.5, False, False, _TEXT)
            d.text_r(M + 340, y - 11, str(r.get("stargazers_count", 0)), 8, True, _AMBER)
            d.text_r(M + 388, y - 11, str(r.get("forks_count", 0)), 8, False, _GREY)
            d.text_r(RGT - 4, y - 11, (r.get("updated_at") or "")[:10], 7.5, False, _GREY)
            yy = y - 20
            for dl in desc_lines:
                d.text(M + 30, yy, dl, 7, False, True, _GREY)
                yy -= 9
            if topics:
                d.text(M + 30, yy, " ".join(f"#{t}" for t in topics[:6]), 7, False, False, _VIOLET)
            y -= rh
            d.line(M, y + 2, RGT, y + 2, (0.88, 0.90, 0.93), 0.4)
    else:
        d.text(M + 4, y - 12, "No repositories found.", 8.5, False, True, _GREY)
        y -= 20

    # gists
    if gists:
        y -= 12
        section("GISTS", f"{len(gists)}")
        for g in gists:
            if y < 70:
                d._page()
                y = _PDF_H - 62
            d.text(M + 4, y - 10, "•", 8, True, False, _GREEN)
            url = _clip(g.get("html_url") or "—", 8, 250)
            d.text(M + 16, y - 10, url, 8, False, False, _BLUE)
            gd = _clip((g.get("description") or "").strip(), 7.5, usable - 280)
            if gd:
                d.text(M + 16 + _tw(url, 8) + 10, y - 10, gd, 7.5, False, True, _GREY)
            y -= 14

    # activity
    if events:
        y -= 12
        section("RECENT ACTIVITY", f"{len(events)} events")
        for e in events:
            if y < 70:
                d._page()
                y = _PDF_H - 62
            when = (e.get("created_at") or "")[:16].replace("T", " ")
            d.text(M + 4, y - 10, when, 7.5, False, False, _GREY)
            d.text(M + 84, y - 10, _clip(e.get("type") or "", 7.5, 120), 7.5, True, False, _TEXT)
            d.text(M + 210, y - 10, _clip((e.get("repo") or {}).get("name") or "—",
                                          7.5, usable - 216), 7.5, False, False, _BLUE)
            y -= 13

    footer_left = f"RepoLens v{VERSION} · Daylight — generated {now} — {REPORT_DIR.name}/"
    d.save(f, footer_left, "")
    return f


def stats_summary(u, repos):
    """Aggregate numbers for the stats bar."""
    u = u or {}
    langs = {}
    stars = forks = watchers = issues = archived = 0
    for r in repos or []:
        if not isinstance(r, dict):
            continue
        stars += r.get("stargazers_count", 0) or 0
        forks += r.get("forks_count", 0) or 0
        watchers += r.get("watchers_count", 0) or 0
        issues += r.get("open_issues_count", 0) or 0
        archived += 1 if r.get("archived") else 0
        l = r.get("language")
        if l:
            langs[l] = langs.get(l, 0) + 1
    top_lang = max(langs, key=langs.get) if langs else "—"
    return {
        "repos": u.get("public_repos", len(repos) if repos else 0),
        "loaded": len(repos) if repos else 0,
        "stars": stars,
        "forks": forks,
        "watchers": watchers,
        "issues": issues,
        "archived": archived,
        "top_language": top_lang,
        "followers": u.get("followers", 0),
        "following": u.get("following", 0),
        "gists": u.get("public_gists", 0),
    }
