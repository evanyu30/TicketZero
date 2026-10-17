"""You.com research client: web search and page contents, cached in SQLite.

Search:   POST https://ydc-index.io/v1/search    {"query", "count"} -> results.web[]
Contents: POST https://ydc-index.io/v1/contents  {"urls": [...], "formats": ["markdown"]} -> [{url, title, markdown}]
(field names verified against https://you.com/docs/openapi/contents.json)
"""
import hashlib
from typing import Any

import requests

from . import config
from .store import store
from .util import redact, stable_json

UNAVAILABLE = {"unavailable": "You.com research is unavailable: YDC_API_KEY is not set."}


def _cache_key(url: str, body: dict) -> str:
    return hashlib.sha256((url + "\n" + stable_json(body)).encode()).hexdigest()


class YouCom:
    def _post(self, url: str, body: dict) -> Any:
        """POST with a SQLite cache keyed by request body hash. Errors are returned, not cached."""
        key = _cache_key(url, body)
        cached = store().cache_get(key)
        if cached is not None:
            return cached
        r = requests.post(url, json=body, headers={"X-API-Key": config.YDC_API_KEY}, timeout=45)
        if r.status_code >= 400:
            return {"_error": f"HTTP {r.status_code}", "_body": redact(r.text[:1500])}
        data = r.json()
        store().cache_set(key, data)
        return data

    def search(self, run, query: str) -> dict:
        """At most 5 results as {url, title, snippet}. Crusoe queries are biased toward official docs first."""
        if not config.YDC_API_KEY:
            return dict(UNAVAILABLE)
        query = (query or "").strip()[:400]
        attempts = []
        if "crusoe" in query.lower() and "site:" not in query.lower():
            attempts.append(f"{query} site:docs.crusoecloud.com OR site:support.crusoecloud.com")
        attempts.append(query)
        used, results = query, []
        for q in attempts:
            used = q
            data = self._post(config.YDC_SEARCH_URL, {"query": q, "count": 5})
            if isinstance(data, dict) and data.get("_error"):
                return {"error": data["_error"], "response_body": data.get("_body", "")}
            web = ((data or {}).get("results") or {}).get("web") or [] if isinstance(data, dict) else []
            results = []
            for item in web[:5]:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                snippets = item.get("snippets") or []
                snippet = " ".join(s for s in snippets if isinstance(s, str)) or item.get("description") or ""
                results.append({"url": item["url"], "title": item.get("title") or "", "snippet": snippet[:500]})
            if results:
                break
        with run.lock:
            for r in results:
                run.seen_urls[r["url"]] = {"title": r["title"], "snippet": r["snippet"]}
        return {"query": used, "results": results}

    def contents(self, run, url: str) -> dict:
        """Page markdown (first 6000 chars). Only URLs that came back from web_search in this run."""
        if not config.YDC_API_KEY:
            return dict(UNAVAILABLE)
        if url not in run.seen_urls:
            return {"error": "fetch_page only accepts URLs returned by web_search earlier in this run."}
        data = self._post(config.YDC_CONTENTS_URL, {"urls": [url], "formats": ["markdown"]})
        if isinstance(data, dict) and data.get("_error"):
            return {"error": data["_error"], "response_body": data.get("_body", "")}
        items = data if isinstance(data, list) else (data.get("results") or data.get("contents") or [])
        item = next((i for i in items if isinstance(i, dict)), {})
        md = item.get("markdown") or item.get("html") or ""
        title = item.get("title") or run.seen_urls[url].get("title", "")
        return {"url": url, "title": title, "markdown": md[:6000]}


YOU = YouCom()
