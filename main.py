#!/usr/bin/env python3
"""GitHub Star Rankings — static HTML generator.

Fetches top-starred repositories from the GitHub Search API and renders
a self-contained rankings page with Jinja2.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import certifi
import requests
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

# Use certifi CA bundle; fall back to verify=False when network intercepts SSL
_verify = certifi.where()
_good = False
try:
    import socket as _socket, ssl as _ssl
    _ctx = _ssl.create_default_context(cafile=_verify)
    _s = _socket.create_connection(("api.github.com", 443), timeout=5)
    _ctx.wrap_socket(_s, server_hostname="api.github.com").close()
    _good = True
except Exception:
    pass
if not _good and not os.environ.get("SSL_NO_VERIFY"):
    import urllib3
    urllib3.disable_warnings()
    _verify = False
    print("[warn] SSL verify disabled (network interception detected)")

_session = requests.Session()
_session.verify = _verify

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
CACHE_FILE = BASE_DIR / ".translation_cache.json"
SNAPSHOT_FILE = BASE_DIR / ".star_snapshot.json"

GITHUB_API = "https://api.github.com"
SEARCH_ENDPOINT = f"{GITHUB_API}/search/repositories"

LANGUAGES = [
    "", "Python", "JavaScript", "TypeScript", "Go", "Rust", "Java",
    "C++", "C", "C#", "Ruby", "Swift", "Kotlin", "PHP", "R",
    "Shell", "Vue", "HTML", "CSS", "Dart", "Lua", "Zig", "Elixir",
]

TIME_RANGES = {
    "all": "全部时间",
    "monthly": "本月热门",
    "weekly": "本周热门",
    "daily": "今日热门",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub star rankings page")
    parser.add_argument("--language", default="", help="Default language filter")
    parser.add_argument("--time-range", default="all", choices=["all", "monthly", "weekly", "daily"],
                        help="Default time-range tab")
    parser.add_argument("--min-stars", type=int, default=1000, help="Minimum star threshold")
    parser.add_argument("--per-page", type=int, default=30, help="Results per request (max 100)")
    parser.add_argument("--pages", type=int, default=3, help="Number of pages to fetch")
    parser.add_argument("--output", default=str(OUTPUT_DIR / "index.html"), help="Output HTML path")
    parser.add_argument("--translate", action="store_true", help="Translate descriptions to Chinese")
    return parser.parse_args()


def build_query(language: str, min_stars: int) -> str:
    parts = [f"stars:>{min_stars}"]
    if language:
        parts.append(f"language:{language}")
    return " ".join(parts)


def fetch_repos(token: str, query: str, per_page: int, pages: int) -> list[dict]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "github-star-rankings/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    all_items = []
    for page in range(1, pages + 1):
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": per_page, "page": page}
        resp = _session.get(SEARCH_ENDPOINT, headers=headers, params=params, timeout=30)

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            print("GitHub API rate limit exceeded. Set GITHUB_TOKEN in .env for higher limits.")
            break
        if resp.status_code == 422:
            print(f"GitHub API validation error: {resp.json().get('message', 'unknown')}")
            break
        resp.raise_for_status()

        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)

    return all_items


def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        try:
            return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_snapshot(repos: list[dict]) -> None:
    snap = {}
    for r in repos:
        snap[r["name"]] = {"stars": r["stars"], "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    SNAPSHOT_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")


def process_repos(items: list[dict]) -> list[dict]:
    prev = load_snapshot()
    now = datetime.now(timezone.utc)
    repos = []
    for item in items:
        name = item["full_name"]
        stars = item["stargazers_count"]
        created_str = item.get("created_at", "")
        # Stars per day since creation
        spd = 0
        if created_str:
            try:
                created_dt = datetime.strptime(created_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days = max((now - created_dt).days, 1)
                spd = round(stars / days, 1)
            except (ValueError, TypeError):
                pass
        # 7-day delta from snapshot
        delta_7d = 0
        if name in prev:
            delta_7d = stars - prev[name]["stars"]
        repos.append({
            "rank": 0,
            "name": name,
            "url": item["html_url"],
            "stars": stars,
            "language": item.get("language") or "Unknown",
            "description": (item.get("description") or "").replace("<", "&lt;").replace(">", "&gt;"),
            "description_cn": "",
            "topics": item.get("topics", [])[:5],
            "forks": item["forks_count"],
            "open_issues": item["open_issues_count"],
            "created_at": created_str[:10] if created_str else "",
            "pushed_at": item["pushed_at"][:10] if item.get("pushed_at") else "",
            "avatar": item["owner"]["avatar_url"] if item.get("owner") else "",
            "stars_per_day": spd,
            "star_delta_7d": delta_7d,
        })
    repos.sort(key=lambda r: r["stars"], reverse=True)
    for i, r in enumerate(repos):
        r["rank"] = i + 1
    save_snapshot(repos)
    return repos


def load_translation_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_translation_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def translate_text(text: str, cache: dict) -> str:
    """Translate English text to Chinese via MyMemory (free, no key needed)."""
    if not text or not text.strip():
        return text
    if text in cache:
        return cache[text]

    to_translate = text[:300]
    backends = [
        # MyMemory — free, works in China
        lambda: _translate_mymemory(to_translate),
        # Google — fast if accessible
        lambda: _translate_google(to_translate),
    ]
    for backend in backends:
        try:
            result = backend()
            if result and result != to_translate:
                cache[text] = result
                return result
        except Exception:
            continue

    cache[text] = text
    return text


def _translate_mymemory(text: str) -> str:
    url = "https://api.mymemory.translated.net/get"
    resp = _session.get(url, params={"q": text, "langpair": "en|zh-CN"}, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        translated = data.get("responseData", {}).get("translatedText", "")
        if translated:
            return translated
    raise RuntimeError(f"MyMemory returned {resp.status_code}")


def _translate_google(text: str) -> str:
    url = "https://translate.googleapis.com/translate_a/single"
    resp = _session.get(url, params={
        "client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text,
    }, timeout=10)
    if resp.status_code == 200:
        result = resp.json()
        return "".join(part[0] for part in result[0] if part[0])
    raise RuntimeError(f"Google returned {resp.status_code}")


def translate_descriptions(repos: list[dict]) -> None:
    cache = load_translation_cache()
    total = len(repos)
    for i, repo in enumerate(repos):
        desc = repo["description"]
        if not desc:
            continue
        print(f"  Translating [{i + 1}/{total}] {repo['name']}...")
        repo["description_cn"] = translate_text(desc, cache)
        time.sleep(0.15)  # be gentle to the free API
    save_translation_cache(cache)


def render_html(repos: list[dict], filters: dict, languages: list[str]) -> str:
    env = Environment(loader=FileSystemLoader([str(TEMPLATES_DIR), str(BASE_DIR)]), autoescape=True)
    template = env.get_template("index.html")
    css_content = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    marked_js = (BASE_DIR / "static" / "marked.min.js").read_text(encoding="utf-8")
    return template.render(
        repos=repos,
        css=css_content,
        marked_js=marked_js,
        languages=languages,
        current_language=filters["language"],
        current_time_range=filters["time_range"],
        time_ranges=TIME_RANGES,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
    )


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    args = parse_args()

    query = build_query(args.language, args.min_stars)
    print(f"Query: {query}")

    token = os.getenv("GITHUB_TOKEN", "")
    items = fetch_repos(token, query, args.per_page, args.pages)
    print(f"Fetched {len(items)} repositories")

    repos = process_repos(items)

    if args.translate:
        print("Translating descriptions to Chinese...")
        translate_descriptions(repos)

    filters = {"language": args.language, "time_range": args.time_range}
    html = render_html(repos, filters, LANGUAGES)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Written {output_path} ({output_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
