#!/usr/bin/env python3
"""
repo-scraper - repository explorer.

repo-scraper gives you a quick, friendly overview of a public GitHub profile.
It shows the projects someone has shared, the tools and languages they use,
and how their work has changed over time. Use it for your own projects,
team research, or a respectful look at a developer's public portfolio.

GUI: GitHub-dark, retro monospace look. Sortable repo table with checkboxes,
rendered README, collapsible file tree, repo stats, JSON / Markdown / HTML
export and a self-contained, searchable HTML report.
"""

import html as htmllib
import json
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

APP_NAME = "repo-scraper"
APP_VERSION = "2.0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) repo-scraper/2.0"
CFG_PATH = Path.home() / ".repo_scraper.json"

# ---------------------------------------------------------------- palette
C = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "panel2": "#1c2128",
    "raised": "#21262d",
    "border": "#30363d",
    "border_hi": "#484f58",
    "fg": "#e6edf3",
    "text": "#c9d1d9",
    "muted": "#7d8590",
    "dim": "#484f58",
    "accent": "#2f81f7",
    "green": "#3fb950",
    "green_d": "#238636",
    "yellow": "#d29922",
    "red": "#f85149",
    "sel": "#1b2a41",
    "zebra": "#0f141b",
    "code": "#010409",
}

LANG_COLORS = {
    "Python": "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "HTML": "#e34c26", "CSS": "#563d7c", "Java": "#b07219", "C": "#555555",
    "C++": "#f34b7d", "C#": "#178600", "Go": "#00ADD8", "Rust": "#dea584",
    "Shell": "#89e051", "Ruby": "#701516", "PHP": "#4F5D95", "Kotlin": "#A97BFF",
    "Swift": "#F05138", "Jupyter Notebook": "#DA5B0B", "Lua": "#000080",
    "Dart": "#00B4AB", "Vue": "#41b883", "SCSS": "#c6538c", "Batchfile": "#C1F12E",
    "PowerShell": "#012456", "Dockerfile": "#384d54", "Makefile": "#427819",
}


def lang_color(lang):
    return LANG_COLORS.get(lang, "#8b949e")


# ---------------------------------------------------------------- helpers
def human_size(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "-"


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fmt_date(s):
    d = parse_iso(s)
    return d.strftime("%Y-%m-%d") if d else "-"


def load_cfg():
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cfg(data):
    try:
        CFG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- network
class Net:
    token = ""
    rate_remaining = None
    rate_limit = None


def _headers(api):
    h = {"User-Agent": UA}
    if api:
        h["Accept"] = "application/vnd.github+json"
        if Net.token:
            h["Authorization"] = f"Bearer {Net.token}"
    return h


def fetch(url, as_json=False, api=False):
    """GET a URL. Token is only ever sent to api.github.com."""
    try:
        req = Request(url, headers=_headers(api))
        with urlopen(req, timeout=30) as resp:
            rem = resp.headers.get("X-RateLimit-Remaining")
            if rem is not None:
                try:
                    Net.rate_remaining = int(rem)
                    Net.rate_limit = int(resp.headers.get("X-RateLimit-Limit", 0))
                except ValueError:
                    pass
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if as_json else raw
    except HTTPError as e:
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            Net.rate_remaining = 0
        return {"__error__": f"HTTP {e.code}"} if as_json else None
    except (URLError, TimeoutError, ValueError, OSError):
        return {"__error__": "network error"} if as_json else None


def _blank_meta(name):
    return {"name": name, "description": None, "language": None, "stars": 0,
            "forks": 0, "updated": None, "fork": False, "archived": False,
            "topics": [], "license": None, "default_branch": None}


def get_repo_list(username, log, skip_forks=False):
    """Prefer the API (gives metadata). Fall back to scraping the HTML tab."""
    metas, page = [], 1
    while True:
        url = (f"https://api.github.com/users/{username}/repos"
               f"?per_page=100&page={page}&sort=updated")
        data = fetch(url, as_json=True, api=True)
        if isinstance(data, dict):
            if page == 1:
                log(f"  API unavailable ({data.get('__error__')}), using HTML fallback")
            break
        if not data:
            break
        for r in data:
            lic = (r.get("license") or {}).get("spdx_id")
            metas.append({
                "name": r["name"], "description": r.get("description"),
                "language": r.get("language"), "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0), "updated": r.get("pushed_at") or r.get("updated_at"),
                "fork": bool(r.get("fork")), "archived": bool(r.get("archived")),
                "topics": r.get("topics") or [], "license": lic if lic != "NOASSERTION" else None,
                "default_branch": r.get("default_branch"),
            })
        log(f"  Page {page}: {len(data)} repos")
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.2)

    if not metas:
        names, page = [], 1
        reserved = {"followers", "following", "stars", "tab"}
        while True:
            page_html = fetch(f"https://github.com/{username}?tab=repositories&page={page}")
            if not page_html:
                break
            found = re.findall(rf'href="/{re.escape(username)}/([^"/?]+)"', page_html)
            new = [n for n in dict.fromkeys(found) if n not in names and n not in reserved]
            if not new:
                break
            names.extend(new)
            log(f"  Page {page}: {len(new)} repos")
            page += 1
            time.sleep(0.4)
        metas = [_blank_meta(n) for n in names]

    if skip_forks:
        before = len(metas)
        metas = [m for m in metas if not m["fork"]]
        if before != len(metas):
            log(f"  Skipped {before - len(metas)} forks")
    return metas


def get_readme(username, repo, branch_hint):
    branches = [branch_hint] if branch_hint else ["main", "master"]
    for branch in branches:
        for fname in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
            text = fetch(f"https://raw.githubusercontent.com/{username}/{repo}/{branch}/{fname}")
            if text:
                return text, branch
    return None, None


def get_tree(username, repo, branch):
    if Net.rate_remaining == 0:
        return [], False, "API rate limit reached (add a token to raise it)"
    url = f"https://api.github.com/repos/{username}/{repo}/git/trees/{branch}?recursive=1"
    data = fetch(url, as_json=True, api=True)
    if not isinstance(data, dict) or "__error__" in data:
        err = data.get("__error__") if isinstance(data, dict) else "failed"
        return [], False, err
    tree = [{"path": e["path"], "type": e["type"], "size": e.get("size"),
             "sha": e.get("sha")}
            for e in data.get("tree", [])]
    return tree, bool(data.get("truncated")), None


def scrape_repo(username, meta):
    name = meta["name"]
    hint = meta.get("default_branch")
    readme, branch = get_readme(username, name, hint)
    candidates = [branch or hint] if (branch or hint) else ["main", "master"]
    tree, truncated, err = [], False, None
    for b in candidates:
        tree, truncated, err = get_tree(username, name, b)
        if tree:
            branch = b
            break
    branch = branch or hint or candidates[0]
    return {
        "repo": name,
        "url": f"https://github.com/{username}/{name}",
        "description": meta.get("description"),
        "language": meta.get("language"),
        "stars": meta.get("stars", 0),
        "forks": meta.get("forks", 0),
        "updated": meta.get("updated"),
        "fork": meta.get("fork", False),
        "archived": meta.get("archived", False),
        "topics": meta.get("topics", []),
        "license": meta.get("license"),
        "default_branch": branch,
        "readme": readme,
        "tree": tree,
        "truncated": truncated,
        "tree_error": None if tree else err,
    }


# ---------------------------------------------------------------- analysis
def repo_stats(r):
    blobs = [e for e in r["tree"] if e["type"] != "tree"]
    exts = Counter()
    for e in blobs:
        name = e["path"].rsplit("/", 1)[-1]
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name.strip(".") else "(none)"
        exts[ext] += 1
    largest = sorted((e for e in blobs if e.get("size")), key=lambda e: e["size"], reverse=True)[:8]
    return {
        "files": len(blobs),
        "dirs": sum(1 for e in r["tree"] if e["type"] == "tree"),
        "size": sum(e.get("size") or 0 for e in blobs),
        "exts": exts,
        "largest": largest,
    }


def build_tree(tree):
    """Nested dict: name -> {type, size, children}."""
    root = {}
    for e in tree:
        parts = e["path"].split("/")
        cur = root
        for p in parts[:-1]:
            cur = cur.setdefault(p, {"type": "tree", "size": None, "children": {}})["children"]
        leaf = cur.setdefault(parts[-1], {"type": e["type"], "size": e.get("size"), "children": {}})
        leaf["type"], leaf["size"] = e["type"], e.get("size")
    return root


def sorted_nodes(children):
    return sorted(children.items(), key=lambda kv: (kv[1]["type"] != "tree", kv[0].lower()))


def count_files(node):
    total = 0
    for ch in node["children"].values():
        total += count_files(ch) if ch["type"] == "tree" else 1
    return total


# ---------------------------------------------------------------- markdown
_TAGS = re.compile(
    r"</?(?:p|div|img|br|a|b|i|em|strong|picture|source|details|summary|h[1-6]|sub|sup|"
    r"center|span|table|thead|tbody|tr|td|th|ul|ol|li|kbd|hr|svg|video|samp|small|big|u|s|"
    r"strike|font)\b[^>]*>", re.I)
_BADGE = re.compile(r"\[!\[([^\]]*)\]\([^)]*\)\]\(([^)\s]*)[^)]*\)")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LI = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _strip_tags(line):
    line = _TAGS.sub("", line)
    return _BADGE.sub(lambda m: f"[{m.group(1) or 'badge'}]({m.group(2)})", line)


def _inline(s):
    esc = htmllib.escape
    out = []
    for part in re.split(r"(`[^`]+`)", s):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{esc(part[1:-1])}</code>")
            continue
        p = esc(part)
        p = re.sub(r"!\[([^\]]*)\]\((https?://[^)\s]+)[^)]*\)",
                   r'<a href="\2" target="_blank" rel="noopener">[img: \1]</a>', p)
        p = re.sub(r"\[([^\]]*)\]\((https?://[^)\s]+)[^)]*\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', p)
        p = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", p)
        p = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<em>\1</em>", p)
        p = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", p)
        out.append(p)
    return "".join(out)


def _task(txt):
    return re.sub(r"^\[( |x|X)\]\s+", lambda m: "\u2610 " if m.group(1) == " " else "\u2611 ", txt)


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _render_list(items):
    out, stack = [], []
    for indent, ordered, txt in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0]:
            out.append("</li>")
        elif not stack or indent > stack[-1][0]:
            out.append(f"<{tag}>")
            stack.append((indent, tag))
        out.append(f"<li>{_inline(txt)}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def md_to_html(text):
    """Small, safe markdown -> HTML (everything is escaped first)."""
    esc = htmllib.escape
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, para = [], []
    i, n = 0, len(lines)

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    while i < n:
        raw = lines[i]
        m = re.match(r"^\s*(```|~~~)", raw)
        if m:
            flush()
            fence, code = m.group(1), []
            i += 1
            while i < n and not lines[i].lstrip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre class='code'><code>" + esc("\n".join(code)) + "</code></pre>")
            continue
        line = _strip_tags(raw)
        if not line.strip():
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            lv = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{lv}>{_inline(m.group(2))}</h{lv}>")
            i += 1
            continue
        if _HR.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head = _cells(line)
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(_strip_tags(lines[i])))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                           for row in rows)
            out.append(f"<div class='tw'><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>")
            continue
        if line.lstrip().startswith(">"):
            flush()
            buf = []
            while i < n and _strip_tags(lines[i]).lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", _strip_tags(lines[i])))
                i += 1
            out.append("<blockquote>" + md_to_html("\n".join(buf)) + "</blockquote>")
            continue
        if _LI.match(line):
            flush()
            items = []
            while i < n:
                mm = _LI.match(_strip_tags(lines[i]))
                if not mm:
                    break
                items.append((len(mm.group(1).replace("\t", "    ")),
                              mm.group(2)[0].isdigit(), _task(mm.group(3))))
                i += 1
            out.append(_render_list(items))
            continue
        para.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


def readme_blurb(readme, limit=180):
    for raw in (readme or "").splitlines():
        line = _strip_tags(raw).strip()
        if not line or line.startswith(("#", "```", "~~~", "|", ">", "![", "---", "===", "<")):
            continue
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[*_`]", "", line).strip()
        if len(line) > 25:
            return line[:limit] + ("\u2026" if len(line) > limit else "")
    return ""


# ---------------------------------------------------------------- text exports
def repo_dump_text(r):
    st = repo_stats(r)
    lines = ["=" * 64, f"REPOSITORY: {r['repo']}", f"URL:        {r['url']}",
             f"BRANCH:     {r['default_branch'] or 'unknown'}",
             f"LANGUAGE:   {r.get('language') or '-'}",
             f"FILES:      {st['files']} ({human_size(st['size'])})", "=" * 64, "",
             "--- README ---", r["readme"] or "(no README)", "", "--- FILE TREE ---"]
    if r["tree"]:
        for e in r["tree"]:
            mark = "[D]" if e["type"] == "tree" else "[F]"
            size = f"  ({e['size']} B)" if e.get("size") else ""
            lines.append(f"{mark} {e['path']}{size}")
    else:
        lines.append("(no files listed)")
    lines.append("")
    return "\n".join(lines)


def repo_highlights(r):
    """Create a compact repository summary with README and file tree."""
    readme = (r.get("readme") or "").strip()
    if len(readme) > 1600:
        readme = readme[:1600].rstrip() + "\n[README excerpt trimmed]"
    lines = [
        f"REPOSITORY: {r['repo']}",
        f"URL:        {r['url']}",
        f"BRANCH:     {r.get('default_branch') or 'unknown'}",
        "",
        "--- README ---",
        readme or "(no README)",
        "",
        "--- FILE TREE ---",
    ]
    lines.extend(
        e["path"] + ("/" if e["type"] == "tree" else "")
        for e in r.get("tree", [])
    )
    if not r.get("tree"):
        lines.append("(no files listed)")
    return "\n".join(lines)


def repos_to_markdown(items, username):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"# repo-scraper: {username}", "", f"_{len(items)} repositories - generated {now}_", "",
           "| Repository | Language | Stars | Files | Updated |", "|---|---|---|---|---|"]
    for r in items:
        st = repo_stats(r)
        out.append(f"| [{r['repo']}]({r['url']}) | {r.get('language') or '-'} | "
                   f"{r.get('stars') or 0} | {st['files']} | {fmt_date(r.get('updated'))} |")
    out.append("")
    for r in items:
        st = repo_stats(r)
        desc = (r.get("description") or readme_blurb(r["readme"])).replace("\n", " ")
        out += [f"## [{r['repo']}]({r['url']})", ""]
        if desc:
            out += [f"> {desc}", ""]
        out.append(f"- Branch: `{r['default_branch'] or 'unknown'}`  \n"
                   f"- Language: {r.get('language') or '-'}  \n"
                   f"- Files: {st['files']} ({human_size(st['size'])})")
        out += ["", "<details><summary>README</summary>", "", r["readme"] or "_No README._", "",
                "</details>", "", "<details><summary>File tree</summary>", "", "```text"]
        out += [e["path"] + ("/" if e["type"] == "tree" else "") for e in r["tree"]] or ["(none)"]
        out += ["```", "", "</details>", "", "---", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- HTML report
def _tree_html(children, depth=0):
    esc = htmllib.escape
    out = ["<ul class='tree'>"]
    for name, n in sorted_nodes(children):
        if n["type"] == "tree":
            out.append(f"<li><details><summary>{esc(name)}/<small>{count_files(n)}</small></summary>"
                       f"{_tree_html(n['children'], depth + 1)}</details></li>")
        else:
            size = f"<small>{human_size(n['size'])}</small>" if n["size"] else ""
            out.append(f"<li class='f'>{esc(name)}{size}</li>")
    out.append("</ul>")
    return "".join(out)


REPORT_TEMPLATE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<script>try{var t=localStorage.getItem('atlas-theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>
<style>
:root{--bg:#0d1117;--sub:#161b22;--sub2:#1c2128;--bd:#30363d;--bd2:#21262d;--fg:#e6edf3;--mut:#7d8590;--acc:#2f81f7;--grn:#3fb950;--code:#010409;
--mono:ui-monospace,"Cascadia Mono","SF Mono",Consolas,"Liberation Mono",Menlo,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color-scheme:dark}
[data-theme=light]{--bg:#fff;--sub:#f6f8fa;--sub2:#eaeef2;--bd:#d0d7de;--bd2:#e6eaef;--fg:#1f2328;--mut:#656d76;--acc:#0969da;--grn:#1a7f37;--code:#f6f8fa;color-scheme:light}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background-image:linear-gradient(var(--bd2) 1px,transparent 1px),linear-gradient(90deg,var(--bd2) 1px,transparent 1px);
background-size:40px 40px;opacity:.6;
-webkit-mask-image:radial-gradient(ellipse 80% 55% at 50% 0,#000,transparent);mask-image:radial-gradient(ellipse 80% 55% at 50% 0,#000,transparent)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--bd);border-radius:6px;border:2px solid var(--bg)}::-webkit-scrollbar-track{background:transparent}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px}
.top{display:flex;justify-content:space-between;align-items:center;padding:18px 0;border-bottom:1px solid var(--bd);font:12px var(--mono);color:var(--mut);letter-spacing:.08em;text-transform:uppercase}
.top b{color:var(--fg);font-weight:600}
.logo::before{content:"";display:inline-block;width:8px;height:14px;background:var(--acc);margin-right:10px;vertical-align:-2px}
.hero{padding:56px 0 8px}
.eyebrow{font:12px var(--mono);color:var(--acc);letter-spacing:.14em;text-transform:uppercase}
h1{font:600 clamp(30px,5vw,52px)/1.1 var(--mono);letter-spacing:-.03em;margin:12px 0 10px;word-break:break-word}
h1 .at{color:var(--mut)}
.sub{color:var(--mut);margin:0;font:13px var(--mono)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--bd);border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin:30px 0 22px}
.stat{background:var(--sub);padding:16px 18px}
.stat span{display:block;font:11px var(--mono);color:var(--mut);letter-spacing:.12em;text-transform:uppercase}
.stat strong{display:block;font:600 24px var(--mono);margin-top:4px;letter-spacing:-.02em}
.langbar{display:flex;height:8px;border-radius:4px;overflow:hidden;gap:2px;background:var(--bd2)}
.langbar i{display:block;min-width:3px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:12px 0 0;font:12px var(--mono);color:var(--mut)}
.legend b{color:var(--fg);font-weight:500}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.bar{position:sticky;top:0;z-index:10;background:var(--bg);background:color-mix(in srgb,var(--bg) 88%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);border-bottom:1px solid var(--bd);margin-top:34px}
.bar .wrap{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding-top:12px;padding-bottom:12px}
.search{flex:1 1 260px;position:relative}
.search input{width:100%;background:var(--sub);border:1px solid var(--bd);color:var(--fg);border-radius:6px;padding:8px 40px 8px 12px;font:13px var(--mono);outline:none}
.search input:focus{border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 25%,transparent)}
.search kbd{position:absolute;right:10px;top:50%;transform:translateY(-50%);font:11px var(--mono);color:var(--mut);border:1px solid var(--bd);border-radius:4px;padding:0 5px;pointer-events:none}
select,.btn,.chip{background:var(--sub);border:1px solid var(--bd);color:var(--fg);border-radius:6px;padding:7px 12px;font:12px var(--mono);cursor:pointer;text-decoration:none;display:inline-block;line-height:1.3}
select:hover,.btn:hover,.chip:hover{border-color:var(--mut)}
.btn.on,.chip.on{border-color:var(--acc);color:var(--acc)}
.copy-btn{background:#8f1d2c;border-color:#c63d50;color:#fff}.copy-btn:hover{background:#b52b3e;border-color:#ef6677}
.btn.pri{background:#238636;border-color:#2ea043;color:#fff}.btn.pri:hover{background:#2ea043}
.chips{flex-basis:100%;display:flex;flex-wrap:wrap;gap:6px}
.chip{padding:3px 10px;border-radius:20px;font-size:11px}
.chip b{color:var(--mut);font-weight:500;margin-left:4px}
main{padding:22px 0 80px}
.count{font:12px var(--mono);color:var(--mut);margin:0 0 14px;letter-spacing:.06em;text-transform:uppercase}
.repo{border:1px solid var(--bd);border-radius:8px;background:var(--sub);margin:0 0 12px;transition:border-color .15s}
.repo:hover{border-color:var(--mut)}
.repo[hidden]{display:none}
.repo>summary{list-style:none;cursor:pointer;padding:18px 20px;display:flex;gap:14px;align-items:flex-start}
.repo>summary::-webkit-details-marker{display:none}
.chev{font:14px var(--mono);color:var(--acc);width:14px;margin-top:2px;flex:none}
.chev::before{content:"+"}
.repo[open]>summary .chev::before{content:"\2212"}
.head{min-width:0;flex:1}
.name{font:600 16px var(--mono);color:var(--acc);word-break:break-all}
.tag{font:11px var(--mono);border:1px solid var(--bd);border-radius:20px;padding:0 8px;color:var(--mut);margin-left:8px;white-space:nowrap}
.desc{margin:6px 0 0;color:var(--mut);font-size:13.5px}
.meta{display:flex;flex-wrap:wrap;gap:4px 18px;margin-top:10px;font:12px var(--mono);color:var(--mut)}
.body{border-top:1px solid var(--bd);padding:18px 20px}
.actions{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.topics{display:flex;gap:6px;flex-wrap:wrap;margin-left:6px}
.cols{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:20px}
@media(max-width:860px){.cols{grid-template-columns:1fr}.top .hide-s{display:none}}
.ptitle{font:11px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--mut);margin:0 0 8px}
.ptitle::before{content:"[ ";color:var(--acc)}.ptitle::after{content:" ]";color:var(--acc)}
.ptitle em{font-style:normal;color:var(--fg);margin-left:6px}
.md{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:16px 18px;max-height:520px;overflow:auto;font-size:13.5px;overflow-wrap:anywhere}
.md>:first-child{margin-top:0}.md>:last-child{margin-bottom:0}
.md p{margin:0 0 12px}
.md h3{font:600 20px var(--sans);margin:18px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--bd)}
.md h4{font:600 17px var(--sans);margin:16px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--bd2)}
.md h5{font:600 15px var(--sans);margin:14px 0 8px}.md h6{font:600 13px var(--sans);margin:12px 0 6px;color:var(--mut)}
.md a{color:var(--acc);text-decoration:none}.md a:hover{text-decoration:underline}
.md code{background:var(--sub2);padding:.15em .4em;border-radius:4px;font:12px var(--mono)}
.md pre.code{background:var(--code);border:1px solid var(--bd);border-radius:6px;padding:12px 14px;overflow:auto;margin:0 0 12px}
.md pre code{background:none;padding:0;font:12px/1.55 var(--mono)}
.md blockquote{margin:0 0 12px;padding:0 14px;border-left:3px solid var(--bd);color:var(--mut)}
.md ul,.md ol{margin:0 0 12px;padding-left:22px}
.md hr{border:0;border-top:1px solid var(--bd);margin:16px 0}
.md .tw{overflow:auto;margin-bottom:12px}.md table{border-collapse:collapse;font-size:13px}.md th,.md td{border:1px solid var(--bd);padding:5px 11px}.md th{background:var(--sub2)}
.nada{color:var(--mut);font:12px var(--mono)}
.treebox{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:10px 14px;max-height:520px;overflow:auto}
.tree{list-style:none;margin:0;padding-left:18px;font:12.5px/1.85 var(--mono)}
.treebox>.tree{padding-left:0}
.tree summary{cursor:pointer;list-style:none}.tree summary::-webkit-details-marker{display:none}
.tree summary::before{content:"+";display:inline-block;width:14px;color:var(--acc)}
.tree details[open]>summary::before{content:"\2212"}
.tree li.f{color:var(--mut)}.tree li.f::before{content:"\00B7";display:inline-block;width:14px}
.tree small{float:right;color:var(--mut);margin-left:14px;font-size:11px}
.empty{display:none;text-align:center;padding:70px 0;font:13px var(--mono);color:var(--mut)}
footer{border-top:1px solid var(--bd);padding:24px 0 40px;color:var(--mut);font:12px var(--mono)}
footer .wrap{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media print{body::before,.bar{display:none}.repo{break-inside:avoid}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><span class="logo"><b>repo-scraper</b></span><span class="hide-s">repository export / v__VERSION__</span></div>
  <section class="hero">
    <div class="eyebrow">// repository index</div>
    <h1><span class="at">@</span>__USER__</h1>
    <p class="sub">__SUBLINE__</p>
    <div class="stats">__STATS__</div>
    __LANGBLOCK__
  </section>
</div>
<div class="bar"><div class="wrap">
  <label class="search"><input id="q" type="search" placeholder="filter repositories, languages, topics..." autocomplete="off"><kbd>/</kbd></label>
  <select id="sort" aria-label="Sort">
    <option value="order">Sort: scraped order</option><option value="name">Sort: name</option>
    <option value="updated">Sort: recently updated</option><option value="stars">Sort: stars</option>
    <option value="files">Sort: file count</option><option value="size">Sort: size</option>
  </select>
  <button class="btn" id="nofork" type="button">hide forks</button>
  <button class="btn" id="noarch" type="button">hide archived</button>
  <button class="btn" id="expand" type="button">expand all</button>
  <button class="btn" id="collapse" type="button">collapse all</button>
  <button class="btn" id="theme" type="button">theme</button>
  <button class="btn copy-btn" id="copyall" type="button">copy all</button>
  <button class="btn copy-btn" id="copyhighlights" type="button">copy highlights</button>
  __CHIPS__
</div></div>
<main class="wrap">
  <p class="count" id="count"></p>
  <div id="list">__CARDS__</div>
  <div class="empty" id="empty">// no repositories match</div>
</main>
<footer><div class="wrap"><span>generated by repo-scraper __VERSION__</span><span>__GENERATED__</span></div></footer>
<script>
(function(){
var $=function(s){return document.querySelector(s)},$$=function(s){return Array.prototype.slice.call(document.querySelectorAll(s))};
var cards=$$('.repo'),list=$('#list'),q=$('#q'),sortSel=$('#sort'),count=$('#count'),empty=$('#empty');
var lang='',noFork=false,noArch=false;
function apply(){
  var term=q.value.trim().toLowerCase(),n=0;
  cards.forEach(function(c){
    var d=c.dataset;
    var ok=(!term||d.text.indexOf(term)>-1)&&(!lang||d.lang===lang)&&!(noFork&&d.fork==='1')&&!(noArch&&d.archived==='1');
    c.hidden=!ok;if(ok)n++;
  });
  count.textContent=n+' / '+cards.length+' repositories';
  empty.style.display=n?'none':'block';
}
function sortCards(){
  var k=sortSel.value;
  var s=cards.slice().sort(function(a,b){
    if(k==='name')return a.dataset.name.localeCompare(b.dataset.name);
    if(k==='order')return (+a.dataset.idx)-(+b.dataset.idx);
    return ((+b.dataset[k])-(+a.dataset[k]))||a.dataset.name.localeCompare(b.dataset.name);
  });
  s.forEach(function(c){list.appendChild(c)});
}
q.addEventListener('input',apply);
sortSel.addEventListener('change',sortCards);
$$('.chip').forEach(function(ch){ch.addEventListener('click',function(){
  var v=ch.dataset.lang;lang=(lang===v)?'':v;
  $$('.chip').forEach(function(x){x.classList.toggle('on',x.dataset.lang===lang&&lang!=='')});apply();
})});
function flag(id,set){var b=$(id);b.addEventListener('click',function(){var on=set();b.classList.toggle('on',on);apply()})}
flag('#nofork',function(){noFork=!noFork;return noFork});
flag('#noarch',function(){noArch=!noArch;return noArch});
$('#expand').addEventListener('click',function(){cards.forEach(function(c){if(!c.hidden)c.open=true})});
$('#collapse').addEventListener('click',function(){cards.forEach(function(c){c.open=false})});
$('#theme').addEventListener('click',function(){
  var r=document.documentElement,t=r.getAttribute('data-theme')==='dark'?'light':'dark';
  r.setAttribute('data-theme',t);try{localStorage.setItem('atlas-theme',t)}catch(e){}
});
function copyText(v,b){
  var old=b.textContent;
  function done(){b.textContent='copied';setTimeout(function(){b.textContent=old},1200)}
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(v).then(done,done)}
  else{var t=document.createElement('textarea');t.value=v;document.body.appendChild(t);t.select();try{document.execCommand('copy')}catch(x){}document.body.removeChild(t);done()}
}
var allCopy=$('#copyall');
allCopy.addEventListener('click',function(){copyText(allCopy.dataset.copy||'',allCopy)});
var highlightsCopy=$('#copyhighlights');
highlightsCopy.addEventListener('click',function(){copyText(highlightsCopy.dataset.copy||'',highlightsCopy)});
$$('[data-copy]').forEach(function(b){b.addEventListener('click',function(e){
  e.preventDefault();copyText(b.dataset.copy,b)
})});
document.addEventListener('keydown',function(e){
  var tag=(document.activeElement||{}).tagName;
  if(e.key==='/'&&tag!=='INPUT'&&tag!=='SELECT'){e.preventDefault();q.focus()}
  else if(e.key==='Escape'&&document.activeElement===q){q.value='';apply();q.blur()}
});
apply();
})();
</script>
</body>
</html>
"""


def render_report_html(results, username):
    esc = htmllib.escape
    stats = [repo_stats(r) for r in results]
    total_files = sum(s["files"] for s in stats)
    total_size = sum(s["size"] for s in stats)
    total_stars = sum(r.get("stars") or 0 for r in results)
    langs = Counter(r["language"] for r in results if r.get("language"))

    tiles = [("Repositories", str(len(results))), ("Stars", str(total_stars)),
             ("Files indexed", f"{total_files:,}"), ("Total size", human_size(total_size)),
             ("Languages", str(len(langs)))]
    stats_html = "".join(f"<div class='stat'><span>{esc(k)}</span><strong>{esc(v)}</strong></div>"
                         for k, v in tiles)

    lang_block, chips = "", ""
    if langs:
        bar = "".join(f"<i style='flex:{n};background:{lang_color(l)}' title='{esc(l)} - {n}'></i>"
                      for l, n in langs.most_common())
        legend = "".join(f"<span><i class='dot' style='background:{lang_color(l)}'></i>{esc(l)} <b>{n}</b></span>"
                         for l, n in langs.most_common())
        lang_block = f"<div class='langbar'>{bar}</div><div class='legend'>{legend}</div>"
        chips = "<div class='chips'>" + "".join(
            f"<button class='chip' type='button' data-lang='{esc(l, True)}'>"
            f"<i class='dot' style='background:{lang_color(l)}'></i>{esc(l)}<b>{n}</b></button>"
            for l, n in langs.most_common()) + "</div>"

    cards = []
    all_dump = "\n".join(repo_dump_text(r) for r in results)
    highlights_dump = "\n\n".join(repo_highlights(r) for r in results)
    for idx, (r, st) in enumerate(zip(results, stats)):
        desc = r.get("description") or readme_blurb(r["readme"])
        upd = parse_iso(r.get("updated"))
        topics = r.get("topics") or []
        text = " ".join([r["repo"], desc or "", r.get("language") or "", " ".join(topics)]).lower()
        badges = ""
        if r.get("fork"):
            badges += "<span class='tag'>fork</span>"
        if r.get("archived"):
            badges += "<span class='tag'>archived</span>"
        meta = []
        if r.get("language"):
            meta.append(f"<span><i class='dot' style='background:{lang_color(r['language'])}'></i>{esc(r['language'])}</span>")
        if r.get("stars"):
            meta.append(f"<span>\u2605 {r['stars']}</span>")
        if r.get("forks"):
            meta.append(f"<span>forks {r['forks']}</span>")
        meta.append(f"<span>{st['files']} files</span>")
        meta.append(f"<span>{human_size(st['size'])}</span>")
        if upd:
            meta.append(f"<span>updated {fmt_date(r['updated'])}</span>")
        if r.get("license"):
            meta.append(f"<span>{esc(r['license'])}</span>")
        meta.append(f"<span>@{esc(r['default_branch'] or 'unknown')}</span>")

        readme_html = md_to_html(r["readme"]) if r["readme"] else "<p class='nada'>// no README found</p>"
        if r["tree"]:
            capped = r["tree"][:6000]
            tree_html = _tree_html(build_tree(capped))
            if len(r["tree"]) > len(capped) or r.get("truncated"):
                tree_html += "<p class='nada'>// listing truncated</p>"
        else:
            tree_html = f"<p class='nada'>// {esc(r.get('tree_error') or 'no files listed')}</p>"
        topic_html = "".join(f"<span class='tag'>{esc(t)}</span>" for t in topics[:8])
        repo_copy = esc(repo_dump_text(r), True)
        repo_highlight_copy = esc(repo_highlights(r), True)

        cards.append(
            f"<details class='repo' id='r-{idx}' data-idx='{idx}' data-name='{esc(r['repo'].lower(), True)}' "
            f"data-text='{esc(text, True)}' data-lang='{esc(r.get('language') or '', True)}' "
            f"data-stars='{r.get('stars') or 0}' data-files='{st['files']}' data-size='{st['size']}' "
            f"data-updated='{int(upd.timestamp()) if upd else 0}' data-fork='{int(bool(r.get('fork')))}' "
            f"data-archived='{int(bool(r.get('archived')))}'>"
            f"<summary><span class='chev'></span><div class='head'>"
            f"<div><span class='name'>{esc(r['repo'])}</span>{badges}</div>"
            f"{f'<p class=desc>{esc(desc)}</p>' if desc else ''}"
            f"<div class='meta'>{''.join(meta)}</div></div></summary>"
            f"<div class='body'><div class='actions'>"
            f"<a class='btn pri' href='{esc(r['url'], True)}' target='_blank' rel='noopener'>open on github &#8599;</a>"
            f"<button class='btn copy-btn' type='button' data-copy='{repo_copy}'>copy repository</button>"
            f"<button class='btn copy-btn' type='button' data-copy='{repo_highlight_copy}'>copy highlights</button>"
            f"<span class='topics'>{topic_html}</span></div>"
            f"<div class='cols'><section><div class='ptitle'>README</div><div class='md'>{readme_html}</div></section>"
            f"<section><div class='ptitle'>FILES<em>{st['files']}</em></div><div class='treebox'>{tree_html}</div></section>"
            f"</div></div></details>")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    mapping = {
        "TITLE": f"repo-scraper / {esc(username)}", "USER": esc(username), "VERSION": APP_VERSION,
        "SUBLINE": f"{len(results)} repositories &middot; generated {now}",
        "STATS": stats_html, "LANGBLOCK": lang_block, "CHIPS": chips,
        "CARDS": "".join(cards), "GENERATED": now,
    }
    report = re.sub(r"__([A-Z]+)__", lambda m: mapping.get(m.group(1), m.group(0)), REPORT_TEMPLATE)
    return report.replace(
        '<button class="btn copy-btn" id="copyall" type="button">copy all</button>',
        f'<button class="btn copy-btn" id="copyall" type="button" data-copy="{esc(all_dump, True)}">copy all</button>',
    ).replace(
        '<button class="btn copy-btn" id="copyhighlights" type="button">copy highlights</button>',
        f'<button class="btn copy-btn" id="copyhighlights" type="button" data-copy="{esc(highlights_dump, True)}">copy highlights</button>',
    )


# ---------------------------------------------------------------- widgets
class Btn(tk.Label):
    KINDS = {
        "default": dict(bg=C["raised"], fg=C["fg"], hover=C["border"], bd=C["border"]),
        "primary": dict(bg=C["green_d"], fg="#ffffff", hover="#2ea043", bd="#2ea043"),
        "danger": dict(bg=C["raised"], fg=C["red"], hover="#3a1d20", bd=C["border"]),
        "ghost": dict(bg=C["panel"], fg=C["muted"], hover=C["raised"], bd=C["border"]),
    }

    def __init__(self, master, text, command=None, kind="default", font=None, padx=12, pady=5):
        self._k = self.KINDS[kind]
        self._command = command
        self._enabled = True
        super().__init__(master, text=text, bg=self._k["bg"], fg=self._k["fg"], font=font,
                         padx=padx, pady=pady, cursor="hand2", bd=0, highlightthickness=1,
                         highlightbackground=self._k["bd"])
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_enter(self, _):
        if self._enabled:
            self.config(bg=self._k["hover"])

    def _on_leave(self, _):
        self.config(bg=self._k["bg"])

    def _on_release(self, e):
        if (self._enabled and self._command and 0 <= e.x <= self.winfo_width()
                and 0 <= e.y <= self.winfo_height()):
            self._command()

    def enable(self, on=True):
        self._enabled = on
        self.config(fg=self._k["fg"] if on else C["dim"], bg=self._k["bg"],
                    cursor="hand2" if on else "arrow")


class Toggle(tk.Label):
    """Dark-theme friendly checkbox-style switch."""

    def __init__(self, master, text, value=False, command=None, font=None, bg=None):
        self._label_text = text
        self.value = value
        self._command = command
        super().__init__(master, bg=bg or C["bg"], font=font, cursor="hand2")
        self._render()
        self.bind("<Button-1>", self.toggle)

    def _render(self):
        self.config(text=("☑ " if self.value else "☐ ") + self._label_text,
                    fg=C["fg"] if self.value else C["muted"])

    def toggle(self, _=None):
        self.value = not self.value
        self._render()
        if self._command:
            self._command(self.value)


def make_input(parent, var, width, font, prefix="", show=None, bg=None):
    bg = bg or C["bg"]
    box = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=C["border"])
    if prefix:
        tk.Label(box, text=prefix, bg=bg, fg=C["muted"], font=font).pack(side="left", padx=(8, 0))
    e = tk.Entry(box, textvariable=var, width=width, bg=bg, fg=C["fg"], insertbackground=C["fg"],
                 relief="flat", bd=0, font=font, highlightthickness=0,
                 selectbackground=C["accent"], selectforeground="#ffffff")
    if show:
        e.config(show=show)
    e.pack(side="left", padx=8, pady=6)
    e.bind("<FocusIn>", lambda _: box.config(highlightbackground=C["accent"]))
    e.bind("<FocusOut>", lambda _: box.config(highlightbackground=C["border"]))
    return box, e


CHK_ON, CHK_OFF = "☑", "☐"
TOKEN_RE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|!?\[[^\]]*\]\([^)\s]+[^)]*\))")


# ---------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} - Repository Explorer")
        self.geometry("1320x820")
        self.minsize(1040, 660)
        self.configure(bg=C["bg"])

        self.cfg = load_cfg()
        self.results = []
        self.stats = []
        self.checked = set()
        self.visible = []
        self.current = None
        self.scraped_user = ""
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.sort_col = None
        self.sort_rev = False
        self._link_n = 0
        self._fcount = 0
        self._api_text = ""

        self._pick_fonts()
        self._apply_style()
        self._build_ui()
        self._show_welcome()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_queue)
        self.entry.focus_set()

    # ---------- setup
    def _pick_fonts(self):
        fams = set(tkfont.families())
        mono = next((f for f in ("Cascadia Mono", "JetBrains Mono", "Consolas", "SF Mono", "Menlo",
                                 "DejaVu Sans Mono", "Liberation Mono", "Courier New") if f in fams),
                    "Courier")
        self.mono = mono
        self.f = (mono, 10)
        self.f_small = (mono, 9)
        self.f_tiny = (mono, 8)
        self.f_bold = (mono, 10, "bold")
        self.f_title = (mono, 16, "bold")
        self.f_logo = (mono, 11, "bold")

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=C["panel"], foreground=C["fg"], bordercolor=C["border"],
                    lightcolor=C["panel"], darkcolor=C["panel"], troughcolor=C["bg"], font=self.f)
        s.configure("Atlas.Horizontal.TProgressbar", troughcolor=C["bg"], background=C["accent"],
                    bordercolor=C["bg"], lightcolor=C["accent"], darkcolor=C["accent"], thickness=3)
        s.configure("TNotebook", background=C["panel"], borderwidth=0, tabmargins=[0, 0, 0, 0])
        s.configure("TNotebook.Tab", background=C["panel"], foreground=C["muted"],
                    padding=[18, 8], borderwidth=0, font=self.f_bold)
        s.map("TNotebook.Tab", background=[("selected", C["bg"]), ("active", C["panel2"])],
              foreground=[("selected", C["fg"]), ("active", C["fg"])])
        s.layout("TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])
        s.configure("Treeview", background=C["bg"], fieldbackground=C["bg"], foreground=C["text"],
                    borderwidth=0, rowheight=26, font=self.f_small)
        s.configure("Treeview.Heading", background=C["panel2"], foreground=C["muted"],
                    relief="flat", borderwidth=0, font=self.f_tiny, padding=[6, 6])
        s.map("Treeview", background=[("selected", C["sel"])], foreground=[("selected", C["fg"])])
        s.map("Treeview.Heading", background=[("active", C["raised"])], foreground=[("active", C["fg"])])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.layout("Atlas.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.layout("Atlas.Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {
            "sticky": "ew", "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"Atlas.{orient}.TScrollbar", background=C["raised"], troughcolor=C["bg"],
                        bordercolor=C["bg"], lightcolor=C["raised"], darkcolor=C["raised"],
                        gripcount=0, width=10, arrowsize=10)
            s.map(f"Atlas.{orient}.TScrollbar", background=[("active", C["border_hi"])])

    def _menu(self):
        return tk.Menu(self, tearoff=0, bg=C["panel"], fg=C["fg"], activebackground=C["accent"],
                       activeforeground="#ffffff", bd=1, relief="flat", font=self.f_small,
                       activeborderwidth=0)

    # ---------- layout
    def _build_ui(self):
        # header
        hdr = tk.Frame(self, bg=C["panel"])
        hdr.pack(fill="x")
        tk.Label(hdr, text="\u258c", bg=C["panel"], fg=C["accent"], font=self.f_logo).pack(side="left", padx=(16, 0), pady=10)
        tk.Label(hdr, text="REPO-SCRAPER", bg=C["panel"], fg=C["fg"], font=self.f_logo).pack(side="left")
        tk.Label(hdr, text=f"  repository explorer  v{APP_VERSION}", bg=C["panel"], fg=C["muted"],
                 font=self.f_small).pack(side="left")
        self.api_lbl = tk.Label(hdr, text="API  idle", bg=C["panel"], fg=C["muted"], font=self.f_small)
        self.api_lbl.pack(side="right", padx=16)
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # command bar
        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=16, pady=(14, 12))
        self.username_var = tk.StringVar(value=self.cfg.get("username", ""))
        box, self.entry = make_input(bar, self.username_var, 24, self.f, prefix="@")
        box.pack(side="left")
        self.entry.bind("<Return>", lambda e: self.start_scrape())

        self.token_var = tk.StringVar()
        tbox, _ = make_input(bar, self.token_var, 16, self.f_small, prefix="token", show="\u2022")
        tbox.pack(side="left", padx=(8, 0))

        self.skip_forks = Toggle(bar, "skip forks", self.cfg.get("skip_forks", False), font=self.f_small)
        self.skip_forks.pack(side="left", padx=14)
        self.auto_report = Toggle(
            bar, "open HTML after scan", self.cfg.get("auto_report", True), font=self.f_small
        )
        self.auto_report.pack(side="left", padx=(0, 14))

        self.btn_scrape = Btn(bar, "SCRAPE  \u23ce", self.start_scrape, "primary", self.f_bold)
        self.btn_scrape.pack(side="left")
        self.btn_cancel = Btn(bar, "CANCEL", self.cancel_scrape, "danger", self.f_bold)
        self.btn_cancel.pack(side="left", padx=(6, 0))
        self.btn_cancel.enable(False)

        self.btn_copy = Btn(bar, "COPY \u25be", lambda: self._popup(self.btn_copy, self.copy_menu), "default", self.f_bold)
        self.btn_copy.pack(side="right")
        self.btn_export = Btn(bar, "EXPORT \u25be", lambda: self._popup(self.btn_export, self.export_menu), "default", self.f_bold)
        self.btn_export.pack(side="right", padx=6)
        self.btn_report = Btn(bar, "REPORT \u2197", self.open_report, "default", self.f_bold)
        self.btn_report.pack(side="right")
        for b in (self.btn_copy, self.btn_export, self.btn_report):
            b.enable(False)

        self.copy_menu = self._menu()
        self.copy_menu.add_command(label="Full dump (text)", command=self.copy_dump)
        self.copy_menu.add_command(label="Repository URLs", command=self.copy_urls)
        self.copy_menu.add_command(label="Markdown", command=self.copy_markdown)
        self.copy_menu.add_command(label="README of open repo", command=self.copy_readme)
        self.export_menu = self._menu()
        self.export_menu.add_command(label="JSON...", command=self.export_json)
        self.export_menu.add_command(label="Markdown...", command=self.export_md)
        self.export_menu.add_command(label="HTML report...", command=self.export_html)

        self.progress = ttk.Progressbar(self, mode="determinate", style="Atlas.Horizontal.TProgressbar")
        self.progress.pack(fill="x")

        # status bar + log (packed bottom first)
        self.status_bar = tk.Frame(self, bg=C["panel"])
        self.status_bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="READY")
        self.status_lbl = tk.Label(self.status_bar, textvariable=self.status_var, bg=C["panel"],
                                   fg=C["muted"], anchor="w", font=self.f_small, padx=16, pady=5)
        self.status_lbl.pack(side="left")
        self.hint_lbl = tk.Label(self.status_bar, bg=C["panel"], fg=C["dim"], font=self.f_tiny, padx=16,
                                 text="^F filter   ^A all   SPACE toggle   ENTER open   ^L log")
        self.hint_lbl.pack(side="right")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", side="bottom")

        self.log_wrap = tk.Frame(self, bg=C["bg"])
        self.log_wrap.pack(fill="x", side="bottom", padx=16, pady=(0, 10))
        self.log_visible = True
        self.log_box = tk.Text(self.log_wrap, height=5, wrap="word", bg=C["code"], fg=C["muted"],
                               relief="flat", bd=0, font=self.f_tiny, state="disabled", padx=10, pady=8,
                               highlightthickness=1, highlightbackground=C["border"])
        self.log_box.pack(fill="x")
        self.log_box.tag_config("ok", foreground=C["green"])
        self.log_box.tag_config("err", foreground=C["red"])
        self.log_box.tag_config("ts", foreground=C["dim"])

        # main split
        paned = tk.PanedWindow(self, orient="horizontal", bg=C["bg"], sashwidth=8, sashrelief="flat", bd=0)
        paned.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        left = tk.Frame(paned, bg=C["panel"], highlightthickness=1, highlightbackground=C["border"])
        right = tk.Frame(paned, bg=C["panel"], highlightthickness=1, highlightbackground=C["border"])
        paned.add(left, minsize=340, width=430)
        paned.add(right, minsize=520)
        self._build_left(left)
        self._build_right(right)

    def _build_left(self, left):
        top = tk.Frame(left, bg=C["panel"])
        top.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(top, text="REPOSITORIES", bg=C["panel"], fg=C["fg"], font=self.f_bold).pack(side="left")
        self.stats_var = tk.StringVar(value="0 repos  /  0 selected")
        tk.Label(top, textvariable=self.stats_var, bg=C["panel"], fg=C["muted"], font=self.f_tiny).pack(side="right")

        frow = tk.Frame(left, bg=C["panel"])
        frow.pack(fill="x", padx=12, pady=(0, 8))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refresh_list())
        fbox, self.filter_entry = make_input(frow, self.filter_var, 10, self.f_small, prefix="filter", bg=C["bg"])
        fbox.pack(side="left", fill="x", expand=True)
        for text, mode in (("ALL", "all"), ("NONE", "none"), ("INV", "invert")):
            Btn(frow, text, lambda m=mode: self._select_visible(m), "ghost", self.f_tiny, padx=8, pady=5).pack(
                side="left", padx=(5, 0))

        wrap = tk.Frame(left, bg=C["bg"])
        wrap.pack(fill="both", expand=True)
        cols = ("chk", "name", "lang", "files", "size")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        self.tree.column("chk", width=38, anchor="center", stretch=False)
        self.tree.column("name", width=170, anchor="w", stretch=True)
        self.tree.column("lang", width=84, anchor="w", stretch=False)
        self.tree.column("files", width=54, anchor="e", stretch=False)
        self.tree.column("size", width=72, anchor="e", stretch=False)
        self.tree.tag_configure("odd", background=C["zebra"])
        self.tree.tag_configure("even", background=C["bg"])
        self.tree.tag_configure("checked", foreground=C["accent"])
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview, style="Atlas.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self._update_headings()
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_tree_double)

    def _build_right(self, right):
        det = tk.Frame(right, bg=C["panel"])
        det.pack(fill="x", padx=18, pady=(16, 10))
        top = tk.Frame(det, bg=C["panel"])
        top.pack(fill="x")
        self.d_title = tk.Label(top, text="", bg=C["panel"], fg=C["fg"], font=self.f_title, anchor="w")
        self.d_title.pack(side="left")
        self.btn_readme_copy = Btn(top, "COPY README", self.copy_readme, "ghost", self.f_tiny, padx=8, pady=4)
        self.btn_open = Btn(top, "OPEN \u2197", self.open_current, "default", self.f_tiny, padx=10, pady=4)
        self.btn_open.pack(side="right")
        self.btn_readme_copy.pack(side="right", padx=6)
        self.btn_open.enable(False)
        self.btn_readme_copy.enable(False)
        self.d_url = tk.Label(det, text="", bg=C["panel"], fg=C["accent"], font=self.f_small, anchor="w", cursor="hand2")
        self.d_url.pack(fill="x", pady=(2, 0))
        self.d_url.bind("<Button-1>", lambda e: self.open_current())
        self.d_desc = tk.Label(det, text="", bg=C["panel"], fg=C["muted"], font=self.f_small,
                               anchor="w", justify="left", wraplength=600)
        self.d_desc.pack(fill="x", pady=(8, 0))
        self.d_chips = tk.Frame(det, bg=C["panel"])
        self.d_chips.pack(fill="x", pady=(10, 0))
        det.bind("<Configure>", lambda e: self.d_desc.config(wraplength=max(240, e.width - 10)))
        tk.Frame(right, bg=C["border"], height=1).pack(fill="x")

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)

        # README tab
        self.readme_box = self._make_text(self.nb, "README", wrap="word")
        self._tag_readme(self.readme_box)
        # FILES tab
        ff = tk.Frame(self.nb, bg=C["bg"])
        self.nb.add(ff, text="FILES")
        self.files_tab = ff
        tb = tk.Frame(ff, bg=C["bg"])
        tb.pack(fill="x", padx=12, pady=10)
        self.files_filter = tk.StringVar()
        self.files_filter.trace_add("write", lambda *_: self._fill_files())
        fb, _ = make_input(tb, self.files_filter, 22, self.f_small, prefix="find")
        fb.pack(side="left")
        Btn(tb, "EXPAND", self._expand_files, "ghost", self.f_tiny, 8, 5).pack(side="left", padx=(8, 0))
        Btn(tb, "COLLAPSE", self._collapse_files, "ghost", self.f_tiny, 8, 5).pack(side="left", padx=(5, 0))
        self.files_note = tk.Label(tb, text="", bg=C["bg"], fg=C["muted"], font=self.f_tiny)
        self.files_note.pack(side="right")
        fw = tk.Frame(ff, bg=C["bg"])
        fw.pack(fill="both", expand=True)
        self.ftree = ttk.Treeview(fw, columns=("size",), show="tree", selectmode="browse")
        self.ftree.column("#0", stretch=True, width=300)
        self.ftree.column("size", width=90, anchor="e", stretch=False)
        self.ftree.tag_configure("dir", foreground=C["fg"])
        self.ftree.tag_configure("file", foreground=C["text"])
        self.ftree.tag_configure("note", foreground=C["muted"])
        fvs = ttk.Scrollbar(fw, orient="vertical", command=self.ftree.yview, style="Atlas.Vertical.TScrollbar")
        self.ftree.configure(yscrollcommand=fvs.set)
        self.ftree.pack(side="left", fill="both", expand=True)
        fvs.pack(side="right", fill="y")
        # INFO tab
        self.info_box = self._make_text(self.nb, "INFO", wrap="none")
        b = self.info_box
        b.tag_config("h", foreground=C["accent"], font=self.f_bold, spacing1=8)
        b.tag_config("k", foreground=C["muted"])
        b.tag_config("v", foreground=C["fg"])
        b.tag_config("bar", foreground=C["accent"])
        b.tag_config("dim", foreground=C["dim"])

    def _make_text(self, nb, title, wrap="word"):
        frame = tk.Frame(nb, bg=C["bg"])
        txt = tk.Text(frame, wrap=wrap, bg=C["bg"], fg=C["text"], insertbackground=C["fg"], relief="flat",
                      bd=0, font=self.f, padx=18, pady=14, selectbackground=C["accent"],
                      selectforeground="#ffffff", highlightthickness=0, state="disabled", cursor="arrow")
        vs = ttk.Scrollbar(frame, orient="vertical", command=txt.yview, style="Atlas.Vertical.TScrollbar")
        txt.configure(yscrollcommand=vs.set)
        txt.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        nb.add(frame, text=title)
        txt.bind("<Control-a>", lambda e: (txt.tag_add("sel", "1.0", "end"), "break")[1])
        txt.bind("<Button-1>", lambda e: txt.focus_set())
        return txt

    def _tag_readme(self, w):
        m = self.mono
        w.tag_config("h1", font=(m, 16, "bold"), foreground=C["fg"], spacing1=14, spacing3=6)
        w.tag_config("h2", font=(m, 13, "bold"), foreground=C["fg"], spacing1=12, spacing3=4)
        w.tag_config("h3", font=(m, 11, "bold"), foreground=C["muted"], spacing1=8, spacing3=2)
        w.tag_config("bold", font=(m, 10, "bold"), foreground=C["fg"])
        w.tag_config("icode", background=C["raised"], foreground=C["fg"])
        w.tag_config("code", background=C["code"], foreground=C["text"], lmargin1=14, lmargin2=14, font=self.f_small)
        w.tag_config("quote", foreground=C["muted"], lmargin1=16, lmargin2=16)
        w.tag_config("bullet", foreground=C["accent"])
        w.tag_config("muted", foreground=C["muted"])
        w.tag_config("accent", foreground=C["accent"])
        w.tag_config("dim", foreground=C["dim"])

    def _bind_keys(self):
        self.bind("<Control-f>", lambda e: (self.filter_entry.focus_set(), "break")[1])
        self.bind("<Control-l>", lambda e: self._toggle_log())
        self.bind("<Escape>", self._on_escape)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Control-a>", lambda e: (self._select_visible("all"), "break")[1])
        self.tree.bind("<Control-c>", lambda e: (self.copy_dump(), "break")[1])
        self.tree.bind("<Return>", lambda e: self.open_current())

    # ---------- welcome
    def _show_welcome(self):
        w = self.readme_box
        w.config(state="normal")
        w.delete("1.0", "end")
        art = (
            "\n"
            "   \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510\n"
            "   \u2502  R E P O - S C R A P E R                \u2502\n"
            "   \u2502  repository explorer                    \u2502\n"
            "   \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518\n\n")
        w.insert("end", art, "accent")
        lines = [
            "   repo-scraper gives you a simple view of a public GitHub profile.",
            "   See what someone is building, which tools they use, and how",
            "   their projects are growing - useful for portfolio and team research.",
            "",
            "   1. type a GitHub username or org, press ENTER",
            "   2. click a repo to read its README and file tree",
            "   3. tick the boxes, then COPY / EXPORT / REPORT",
            "",
            "   optional: paste a personal access token to lift the",
            "   60 requests/hour API limit (never saved to disk).",
        ]
        w.insert("end", "\n".join(lines) + "\n", "muted")
        w.config(state="disabled")

    # ---------- scraping
    def start_scrape(self):
        username = self.username_var.get().strip().lstrip("@")
        if not username:
            messagebox.showwarning("MISSING", "Enter a GitHub username.")
            return
        if self.running:
            return
        Net.token = self.token_var.get().strip()
        Net.rate_remaining = None
        self.cfg["username"] = username
        self.cfg["skip_forks"] = self.skip_forks.value
        self.cfg["auto_report"] = self.auto_report.value
        save_cfg(self.cfg)

        self.running = True
        self.cancel_event = threading.Event()
        self.scraped_user = username
        self.results, self.stats, self.visible = [], [], []
        self.checked.clear()
        self.current = None
        self.filter_var.set("")
        self.tree.delete(*self.tree.get_children())
        self._update_counts()
        self._clear_detail()
        for b in (self.btn_copy, self.btn_export, self.btn_report):
            b.enable(False)
        self.btn_scrape.enable(False)
        self.btn_cancel.enable(True)
        self.progress.config(value=0, maximum=1)
        self._log_clear()
        self._set_status(f"SCRAPING @{username} ...")
        threading.Thread(target=self._worker, args=(username, self.skip_forks.value, self.cancel_event),
                         daemon=True).start()

    def cancel_scrape(self):
        if self.running:
            self.cancel_event.set()
            self._set_status("CANCELLING ...")

    def _worker(self, username, skip_forks, cancel):
        q = self.queue
        try:
            q.put(("log", f"Fetching repositories for {username} ..."))
            metas = get_repo_list(username, lambda m: q.put(("log", m)), skip_forks)
            if cancel.is_set():
                q.put(("done", ("cancelled", [])))
                return
            if not metas:
                q.put(("done", ("error", f"No repositories found for '{username}'.")))
                return
            n = len(metas)
            q.put(("total", n))
            results = [None] * n

            def job(i, meta):
                if cancel.is_set():
                    return i, None
                try:
                    return i, scrape_repo(username, meta)
                except Exception as exc:  # keep going on a single bad repo
                    q.put(("log", f"  ! {meta['name']}: {type(exc).__name__}: {exc}"))
                    return i, None

            done = 0
            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(job, i, m) for i, m in enumerate(metas)]
                for fut in as_completed(futures):
                    i, r = fut.result()
                    done += 1
                    if r is not None:
                        results[i] = r
                        q.put(("log", f"  ok  {r['repo']}"))
                    q.put(("progress", done))
                    q.put(("status", f"SCRAPING [{done}/{n}]"))
            kept = [r for r in results if r]
            q.put(("done", ("cancelled" if cancel.is_set() else "ok", kept)))
        except Exception as e:
            q.put(("done", ("error", f"{type(e).__name__}: {e}")))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "total":
                    self.progress.config(maximum=payload, value=0)
                elif kind == "progress":
                    self.progress.config(value=payload)
                elif kind == "done":
                    self._finish(*payload)
        except queue.Empty:
            pass
        self._update_api_label()
        self.after(80, self._drain_queue)

    def _update_api_label(self):
        if Net.rate_remaining is None:
            text, color = "API  idle", C["muted"]
        else:
            lim = Net.rate_limit or "?"
            text = f"API  {Net.rate_remaining}/{lim}"
            color = C["red"] if Net.rate_remaining <= 5 else (C["yellow"] if Net.rate_remaining <= 15 else C["green"])
        if text != self._api_text:
            self._api_text = text
            self.api_lbl.config(text=text, fg=color)

    def _finish(self, status, data):
        self.running = False
        self.btn_scrape.enable(True)
        self.btn_cancel.enable(False)
        if status == "error":
            self._set_status(f"ERROR: {data}")
            self._log(f"ERROR: {data}", "err")
            messagebox.showerror("SCRAPE FAILED", data)
            return
        self.results = data
        self.stats = [repo_stats(r) for r in data]
        self._refresh_list()
        has = bool(self.results)
        for b in (self.btn_copy, self.btn_export, self.btn_report):
            b.enable(has)
        if status == "cancelled":
            self._set_status(f"CANCELLED. {len(data)} REPOS KEPT.")
            self._log(f"Cancelled. {len(data)} repos kept.", "err")
        else:
            self._set_status(f"DONE. {len(data)} REPOS.", flash=True)
            self._log(f"Finished: {len(data)} repos.", "ok")
            if self.auto_report.value:
                self.open_report()
        if self.visible:
            first = str(self.visible[0])
            self.tree.selection_set(first)
            self.tree.focus(first)

    # ---------- repo list
    def _match(self, r, q):
        if not q:
            return True
        hay = " ".join([r["repo"], r.get("description") or "", r.get("language") or "",
                        " ".join(r.get("topics") or [])]).lower()
        return q in hay

    def _sort_key(self, i):
        r, st, c = self.results[i], self.stats[i], self.sort_col
        if c == "name":
            return r["repo"].lower()
        if c == "lang":
            return (r.get("language") or "~").lower()
        if c == "files":
            return st["files"]
        if c == "size":
            return st["size"]
        return i

    def _refresh_list(self):
        q = self.filter_var.get().strip().lower()
        idxs = [i for i, r in enumerate(self.results) if self._match(r, q)]
        if self.sort_col:
            idxs.sort(key=self._sort_key, reverse=self.sort_rev)
        prev = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for n, i in enumerate(idxs):
            r, st = self.results[i], self.stats[i]
            tags = ["even" if n % 2 == 0 else "odd"]
            if i in self.checked:
                tags.append("checked")
            self.tree.insert("", "end", iid=str(i), tags=tuple(tags), values=(
                CHK_ON if i in self.checked else CHK_OFF, r["repo"], r.get("language") or "-",
                st["files"], human_size(st["size"])))
        self.visible = idxs
        if prev and self.tree.exists(prev[0]):
            self.tree.selection_set(prev[0])
        self._update_counts()

    def _update_headings(self):
        names = {"name": "REPOSITORY", "lang": "LANG", "files": "FILES", "size": "SIZE"}
        self.tree.heading("chk", text=CHK_OFF, command=self._toggle_visible_all)
        for c, t in names.items():
            arrow = (" \u25bc" if self.sort_rev else " \u25b2") if c == self.sort_col else ""
            self.tree.heading(c, text=t + arrow, anchor="w" if c in ("name", "lang") else "e",
                              command=lambda col=c: self._sort_by(col))

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col = col
            self.sort_rev = col in ("files", "size")
        self._update_headings()
        self._refresh_list()

    def _update_counts(self):
        self.stats_var.set(f"{len(self.visible)}/{len(self.results)} repos  /  {len(self.checked)} selected")

    def _set_checked(self, i, on):
        iid = str(i)
        (self.checked.add if on else self.checked.discard)(i)
        if self.tree.exists(iid):
            vals = list(self.tree.item(iid, "values"))
            vals[0] = CHK_ON if on else CHK_OFF
            tags = [t for t in self.tree.item(iid, "tags") if t != "checked"]
            if on:
                tags.append("checked")
            self.tree.item(iid, values=vals, tags=tuple(tags))

    def _toggle_one(self, i):
        self._set_checked(i, i not in self.checked)
        self._update_counts()

    def _select_visible(self, mode):
        for i in self.visible:
            if mode == "all":
                self._set_checked(i, True)
            elif mode == "none":
                self._set_checked(i, False)
            else:
                self._set_checked(i, i not in self.checked)
        self._update_counts()

    def _toggle_visible_all(self):
        all_on = self.visible and all(i in self.checked for i in self.visible)
        self._select_visible("none" if all_on else "all")

    def _on_tree_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        iid = self.tree.identify_row(e.y)
        if iid and self.tree.identify_column(e.x) == "#1":
            self._toggle_one(int(iid))
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            return "break"

    def _on_tree_double(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and self.tree.identify_column(e.x) != "#1":
            self.open_current()

    def _on_space(self, _):
        sel = self.tree.selection()
        if sel:
            self._toggle_one(int(sel[0]))
        return "break"

    def _on_select(self, _):
        sel = self.tree.selection()
        if sel:
            self._show_repo(int(sel[0]))

    def _on_escape(self, _):
        if self.filter_var.get():
            self.filter_var.set("")
        self.tree.focus_set()

    # ---------- detail view
    def _clear_detail(self):
        self.d_title.config(text="")
        self.d_url.config(text="")
        self.d_desc.config(text="")
        for w in self.d_chips.winfo_children():
            w.destroy()
        self.btn_open.enable(False)
        self.btn_readme_copy.enable(False)
        self.nb.tab(self.files_tab, text="FILES")
        self.ftree.delete(*self.ftree.get_children())
        for w in (self.info_box,):
            w.config(state="normal")
            w.delete("1.0", "end")
            w.config(state="disabled")
        self._show_welcome()

    def _chip(self, text, fg=None):
        tk.Label(self.d_chips, text=text, bg=C["bg"], fg=fg or C["muted"], font=self.f_tiny, padx=8, pady=3,
                 highlightthickness=1, highlightbackground=C["border"]).pack(side="left", padx=(0, 6))

    def _show_repo(self, i):
        r, st = self.results[i], self.stats[i]
        self.current = i
        self.d_title.config(text=r["repo"])
        self.d_url.config(text=r["url"])
        self.d_desc.config(text=r.get("description") or readme_blurb(r["readme"]) or "no description")
        for w in self.d_chips.winfo_children():
            w.destroy()
        if r.get("language"):
            self._chip(r["language"], C["fg"])
        if r.get("stars"):
            self._chip(f"\u2605 {r['stars']}")
        if r.get("forks"):
            self._chip(f"forks {r['forks']}")
        self._chip(f"@{r['default_branch'] or 'unknown'}")
        if r.get("updated"):
            self._chip(f"updated {fmt_date(r['updated'])}")
        if r.get("fork"):
            self._chip("fork", C["yellow"])
        if r.get("archived"):
            self._chip("archived", C["yellow"])
        self.btn_open.enable(True)
        self.btn_readme_copy.enable(bool(r["readme"]))
        self.nb.tab(self.files_tab, text=f"FILES ({st['files']})")
        self._render_readme(r["readme"])
        self._fill_files()
        self._fill_info(r, st)

    # ---------- README rendering
    def _insert_inline(self, w, s, base=None):
        for p in TOKEN_RE.split(s):
            if not p:
                continue
            tags = (base,) if base else ()
            if p.startswith("**") and p.endswith("**") and len(p) > 4:
                w.insert("end", p[2:-2], tags if (base and base.startswith("h")) else tags + ("bold",))
            elif p.startswith("`") and p.endswith("`") and len(p) > 2:
                w.insert("end", p[1:-1], tags + ("icode",))
            else:
                m = re.match(r"^(!?)\[([^\]]*)\]\(([^)\s]+)", p)
                if m:
                    label = m.group(2) or "link"
                    if m.group(1):
                        label = f"[img: {label}]"
                    url = m.group(3)
                    if url.startswith(("http://", "https://")):
                        self._link_n += 1
                        tag = f"lnk{self._link_n}"
                        w.tag_config(tag, foreground=C["accent"], underline=True)
                        w.tag_bind(tag, "<Button-1>", lambda e, u=url: webbrowser.open(u))
                        w.tag_bind(tag, "<Enter>", lambda e: w.config(cursor="hand2"))
                        w.tag_bind(tag, "<Leave>", lambda e: w.config(cursor="arrow"))
                        w.insert("end", label, tags + (tag,))
                    else:
                        w.insert("end", label, tags)
                else:
                    w.insert("end", p, tags)

    def _render_readme(self, text):
        w = self.readme_box
        w.config(state="normal")
        w.delete("1.0", "end")
        for t in w.tag_names():
            if t.startswith("lnk"):
                w.tag_delete(t)
        if not text:
            w.insert("end", "(no README found)", "muted")
            w.config(state="disabled")
            return
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        in_code = False
        for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if re.match(r"^\s*(```|~~~)", raw):
                in_code = not in_code
                continue
            if in_code:
                w.insert("end", raw + "\n", "code")
                continue
            line = _strip_tags(raw)
            if not line.strip():
                w.insert("end", "\n")
                continue
            m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
            if m:
                self._insert_inline(w, m.group(2), f"h{min(len(m.group(1)), 3)}")
                w.insert("end", "\n")
            elif _HR.match(line):
                w.insert("end", "\u2500" * 56 + "\n", "dim")
            elif line.lstrip().startswith(">"):
                self._insert_inline(w, re.sub(r"^\s*>\s?", "", line), "quote")
                w.insert("end", "\n", "quote")
            elif _LI.match(line):
                mm = _LI.match(line)
                depth = len(mm.group(1).replace("\t", "    ")) // 2
                marker = "\u2022" if not mm.group(2)[0].isdigit() else mm.group(2)
                w.insert("end", "  " * depth + " " + marker + " ", "bullet")
                self._insert_inline(w, _task(mm.group(3)))
                w.insert("end", "\n")
            else:
                self._insert_inline(w, line.strip())
                w.insert("end", "\n")
        w.config(state="disabled")
        w.yview_moveto(0)

    # ---------- files
    def _fill_files(self):
        t = self.ftree
        t.delete(*t.get_children())
        if self.current is None:
            return
        r = self.results[self.current]
        q = self.files_filter.get().strip().lower()
        if not r["tree"]:
            t.insert("", "end", text="  " + (r.get("tree_error") or "no files listed"), tags=("note",))
            self.files_note.config(text="")
            return
        if q:
            hits = [e for e in r["tree"] if e["type"] != "tree" and q in e["path"].lower()]
            for e in hits[:2000]:
                t.insert("", "end", text=e["path"], values=(human_size(e.get("size")),), tags=("file",))
            self.files_note.config(text=f"{len(hits)} match(es)" + (" (showing 2000)" if len(hits) > 2000 else ""))
            return
        self._fcount = 0
        self._insert_nodes("", build_tree(r["tree"]))
        note = "listing truncated by GitHub" if r.get("truncated") else (
            "view capped at 6000 entries" if self._fcount >= 6000 else "")
        self.files_note.config(text=note)

    def _insert_nodes(self, parent, children):
        for name, n in sorted_nodes(children):
            if self._fcount >= 6000:
                return
            self._fcount += 1
            if n["type"] == "tree":
                iid = self.ftree.insert(parent, "end", text=name + "/",
                                        values=(f"{count_files(n)} files",), tags=("dir",))
                self._insert_nodes(iid, n["children"])
            else:
                self.ftree.insert(parent, "end", text=name, values=(human_size(n["size"]),), tags=("file",))

    def _walk_files(self, parent, open_):
        for iid in self.ftree.get_children(parent):
            self.ftree.item(iid, open=open_)
            self._walk_files(iid, open_)

    def _expand_files(self):
        self._walk_files("", True)

    def _collapse_files(self):
        self._walk_files("", False)

    # ---------- info
    def _fill_info(self, r, st):
        w = self.info_box
        w.config(state="normal")
        w.delete("1.0", "end")

        def sec(t):
            w.insert("end", f"\n  \u258c {t}\n\n", "h")

        def kv(k, v):
            w.insert("end", f"    {k:<13}", "k")
            w.insert("end", f"{v}\n", "v")

        sec("REPOSITORY")
        kv("name", r["repo"])
        kv("url", r["url"])
        kv("branch", r["default_branch"] or "unknown")
        kv("language", r.get("language") or "-")
        kv("stars / forks", f"{r.get('stars') or 0} / {r.get('forks') or 0}")
        kv("updated", fmt_date(r.get("updated")))
        kv("license", r.get("license") or "-")
        kv("topics", ", ".join(r.get("topics") or []) or "-")
        flags = [x for x, on in (("fork", r.get("fork")), ("archived", r.get("archived"))) if on]
        kv("flags", ", ".join(flags) or "-")
        sec("CONTENTS")
        kv("files", f"{st['files']:,}")
        kv("directories", f"{st['dirs']:,}")
        kv("total size", human_size(st["size"]))
        readme_lines = len(r["readme"].splitlines()) if r["readme"] else 0
        kv("readme", f"{readme_lines} lines" if r["readme"] else "missing")
        if st["exts"]:
            sec("FILE TYPES")
            total = sum(st["exts"].values())
            top = st["exts"].most_common(10)
            for ext, cnt in top:
                filled = max(1, round(24 * cnt / total))
                w.insert("end", f"    {ext:<10}", "k")
                w.insert("end", "\u2588" * filled, "bar")
                w.insert("end", "\u2591" * (24 - filled), "dim")
                w.insert("end", f"  {cnt:>5}  {100 * cnt / total:4.0f}%\n", "v")
        if st["largest"]:
            sec("LARGEST FILES")
            for e in st["largest"]:
                path = e["path"] if len(e["path"]) <= 64 else "\u2026" + e["path"][-63:]
                w.insert("end", f"    {human_size(e['size']):>9}  ", "k")
                w.insert("end", path + "\n", "v")
        w.config(state="disabled")
        w.yview_moveto(0)

    # ---------- actions
    def _popup(self, btn, menu):
        if not btn._enabled:
            return
        try:
            menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
        finally:
            menu.grab_release()

    def _scope(self):
        if self.checked:
            items = [self.results[i] for i in sorted(self.checked)]
            return items, f"{len(items)} selected"
        items = list(self.results)
        return items, f"all {len(items)}"

    def open_current(self):
        if self.current is not None:
            webbrowser.open(self.results[self.current]["url"])

    def _to_clipboard(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log(f"Copied {what} to clipboard.", "ok")
        self._set_status(f"COPIED {what.upper()}", flash=True)

    def copy_dump(self):
        if not self.results:
            return
        items, label = self._scope()
        self._to_clipboard("\n".join(repo_dump_text(r) for r in items), f"dump ({label})")

    def copy_urls(self):
        if not self.results:
            return
        items, label = self._scope()
        self._to_clipboard("\n".join(r["url"] for r in items), f"urls ({label})")

    def copy_markdown(self):
        if not self.results:
            return
        items, label = self._scope()
        self._to_clipboard(repos_to_markdown(items, self.scraped_user), f"markdown ({label})")

    def copy_readme(self):
        if self.current is None:
            return
        text = self.results[self.current]["readme"]
        if text:
            self._to_clipboard(text, "README")

    def _save_dialog(self, ext, kind, suffix):
        return filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=[(kind, f"*{ext}")],
            initialfile=f"{self.scraped_user}_repos{suffix}{ext}")

    def export_json(self):
        if not self.results:
            return
        items, label = self._scope()
        path = self._save_dialog(".json", "JSON", "")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("EXPORT FAILED", str(exc))
            return
        self._log(f"Saved JSON ({label}) -> {path}", "ok")
        self._set_status(f"SAVED JSON ({label.upper()})", flash=True)

    def export_md(self):
        if not self.results:
            return
        items, label = self._scope()
        path = self._save_dialog(".md", "Markdown", "")
        if not path:
            return
        try:
            Path(path).write_text(repos_to_markdown(items, self.scraped_user), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("EXPORT FAILED", str(exc))
            return
        self._log(f"Saved Markdown ({label}) -> {path}", "ok")
        self._set_status(f"SAVED MARKDOWN ({label.upper()})", flash=True)

    def export_html(self):
        if not self.results:
            return
        items, label = self._scope()
        path = self._save_dialog(".html", "HTML", "")
        if not path:
            return
        try:
            Path(path).write_text(render_report_html(items, self.scraped_user), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("EXPORT FAILED", str(exc))
            return
        self._log(f"Saved HTML report ({label}) -> {path}", "ok")
        self._set_status(f"SAVED HTML ({label.upper()})", flash=True)

    def open_report(self):
        if not self.results:
            return
        items, label = self._scope()
        out_dir = Path(tempfile.gettempdir()) / "repo_scraper"
        out_dir.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.scraped_user) or "report"
        path = out_dir / f"{safe}_{datetime.now().strftime('%H%M%S')}.html"
        try:
            path.write_text(render_report_html(items, self.scraped_user), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("REPORT FAILED", str(exc))
            return
        webbrowser.open(path.resolve().as_uri())
        self._log(f"Opened report ({label}) -> {path}", "ok")
        self._set_status(f"OPENED REPORT ({label.upper()})")

    # ---------- status / log
    def _set_status(self, text, flash=False):
        self.status_var.set(text)
        if flash:
            self.status_bar.config(bg=C["green_d"])
            self.status_lbl.config(bg=C["green_d"], fg="#ffffff")
            self.hint_lbl.config(bg=C["green_d"], fg=C["green_d"])
            self.after(1500, self._unflash)

    def _unflash(self):
        self.status_bar.config(bg=C["panel"])
        self.status_lbl.config(bg=C["panel"], fg=C["muted"])
        self.hint_lbl.config(bg=C["panel"], fg=C["dim"])

    def _log(self, text, tag=None):
        w = self.log_box
        w.config(state="normal")
        w.insert("end", time.strftime("%H:%M:%S  "), "ts")
        w.insert("end", text + "\n", tag or ())
        w.see("end")
        w.config(state="disabled")

    def _log_clear(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def _toggle_log(self):
        if self.log_visible:
            self.log_wrap.pack_forget()
        else:
            self.log_wrap.pack(fill="x", side="bottom", padx=16, pady=(0, 10), before=self.status_bar.master.winfo_children()[0]
                               if False else None)
        self.log_visible = not self.log_visible

    def _on_close(self):
        self.cancel_event.set()
        self.cfg["username"] = self.username_var.get().strip().lstrip("@")
        self.cfg["skip_forks"] = self.skip_forks.value
        self.cfg["auto_report"] = self.auto_report.value
        save_cfg(self.cfg)
        self.destroy()


def main():
    try:  # crisp text on Windows high-DPI displays
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()