#!/usr/bin/env python3
"""Writes themes.json, the community themes omarchy.org/themes lists, for the
launcher's theme store: each theme's title, repo and screenshot as the page has
them, in its order, with GitHub's numbers and files for sorting and previews.

  GITHUB_TOKEN=... tools/themes.py themes.json [--previous old-themes.json]

The site has no feed, so this reads the page. Stars, creation dates and each
repo's head commit come from GitHub's GraphQL API, 50 repos a call. A repo's
files (its wallpapers, its colors file and their size) come from its git tree,
one REST call each, so only repos whose head moved since --previous are asked
again; the rest keep the previous answer. A repo GitHub doesn't answer about
keeps what --previous had, or goes out without numbers.

The format is read by launchers already installed: only add keys. A change
they couldn't read goes into a new file next to this one.

  {"version": 1, "generated": "2026-11-28T12:00:00Z", "source": "https://omarchy.org/themes/",
   "themes": [{"title": "Batman", "repo": "https://github.com/owner/name",
               "image": "https://omarchy.org/assets/themes/batman.webp",
               "created": "2025-09-01T10:00:00Z", "stars": 12,
               "colors": "colors.toml", "wallpapers": ["backgrounds/1.png"], "bytes": 3000000}]}

Every key but title, repo and image may be missing: unknown. When "bytes" is
there the files were read, and a missing "colors" means the repo has none.
Exits non-zero when the page reads to nothing or GitHub fails altogether; CI
then keeps the published copy.
"""
import html, json, os, re, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

PAGE = "https://omarchy.org/themes/"
SITE = "https://omarchy.org"
# As the launcher read the page before this file: <li><a href="repo"><img src="/assets/themes/x.webp" …/><span …>Title</span></a></li>.
ENTRY = re.compile(r'<li><a href="(https://github\.com/[^"]+)"[^>]*><img src="([^"]+)"[^>]*/?><span[^>]*>([^<]+)</span></a></li>')
REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
# The launcher's IMAGE_EXTENSIONS, and its order of colors files: colors.toml before alacritty.toml.
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
COLORS_FILES = ["colors.toml", "alacritty.toml"]
BATCH = 50
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def get(url, body=None):
    headers = {"User-Agent": "omarchy-widgets-themes", "Accept": "application/vnd.github+json"}
    if TOKEN and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(5 * (attempt + 1))


def page_themes():
    out, seen = [], set()
    for href, src, title in ENTRY.findall(get(PAGE).decode()):
        m = REPO.match(href)
        # Only the site's own screenshots, as the launcher takes them.
        if not m or not src.startswith("/") or src.startswith("//"):
            continue
        key = f"{m[1]}/{m[2]}".lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": html.unescape(title.strip()), "repo": f"https://github.com/{m[1]}/{m[2]}", "image": SITE + src, "_owner": m[1], "_name": m[2]})
    return out


def graphql_stats(themes):
    """Stars, creation date and head commit, by index into themes; GitHub's null for a repo that's gone."""
    out = {}
    for start in range(0, len(themes), BATCH):
        part = themes[start : start + BATCH]
        fields = " ".join(
            f'r{start + i}: repository(owner: {json.dumps(t["_owner"])}, name: {json.dumps(t["_name"])}) '
            "{ createdAt stargazerCount defaultBranchRef { target { oid } } }"
            for i, t in enumerate(part)
        )
        answer = json.loads(get("https://api.github.com/graphql", json.dumps({"query": "{ " + fields + " }"}).encode()))
        data = answer.get("data")
        if data is None:
            raise RuntimeError(f"GraphQL: {answer.get('errors')}")
        for key, repo in data.items():
            if repo:
                out[int(key[1:])] = repo
    return out


def repo_files(owner, name, oid):
    tree = json.loads(get(f"https://api.github.com/repos/{owner}/{name}/git/trees/{oid}?recursive=1"))
    sizes = {e["path"]: e.get("size", 0) for e in tree.get("tree", []) if e.get("type") == "blob"}
    wallpapers = sorted(
        p for p in sizes
        if p.startswith("backgrounds/") and "/" not in p[len("backgrounds/") :] and p.rsplit(".", 1)[-1].lower() in IMAGE_EXTENSIONS
    )
    files = {"wallpapers": wallpapers, "bytes": sum(sizes.values())}
    colors = next((f for f in COLORS_FILES if f in sizes), None)
    if colors:
        files["colors"] = colors
    return files


def main():
    args = sys.argv[1:]
    previous = {}
    if "--previous" in args:
        i = args.index("--previous")
        path = args[i + 1]
        del args[i : i + 2]
        try:
            with open(path) as f:
                previous = {t["repo"].lower(): t for t in json.load(f).get("themes", [])}
        except (OSError, ValueError) as e:
            print(f"no previous catalog: {e}", file=sys.stderr)
    if len(args) != 1 or not TOKEN:
        sys.exit(__doc__)
    themes = page_themes()
    if not themes:
        sys.exit("omarchy.org/themes read to no themes: the page changed")
    stats = graphql_stats(themes)
    asked = 0
    for i, t in enumerate(themes):
        old = previous.get(t["repo"].lower(), {})
        repo = stats.get(i)
        if repo is None:
            # Gone or not answered: what was known stays.
            t.update({k: v for k, v in old.items() if k not in t and k != "_head"})
            continue
        t["created"] = repo["createdAt"]
        t["stars"] = repo["stargazerCount"]
        head = (repo.get("defaultBranchRef") or {}).get("target", {}).get("oid")
        if head and head == old.get("head") and "bytes" in old:
            files = {k: old[k] for k in ("colors", "wallpapers", "bytes") if k in old}
        elif head:
            asked += 1
            try:
                files = repo_files(t["_owner"], t["_name"], head)
            except (urllib.error.URLError, ValueError) as e:
                print(f"no files for {t['repo']}: {e}", file=sys.stderr)
                files, head = {k: old[k] for k in ("colors", "wallpapers", "bytes") if k in old}, old.get("head")
        else:
            # An empty repo.
            files = {}
        t.update(files)
        if head:
            t["head"] = head
    for t in themes:
        del t["_owner"], t["_name"]
    catalog = {
        "version": 1,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": PAGE,
        "themes": themes,
    }
    with open(args[0], "w") as f:
        json.dump(catalog, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(themes)} themes, {len(stats)} answered by GitHub, {asked} trees read", file=sys.stderr)


if __name__ == "__main__":
    main()
