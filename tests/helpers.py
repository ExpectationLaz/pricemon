"""Shared helpers for tests that need a fake website."""

import json

import httpx

from pricemon.config import Settings
from pricemon.fetch import Fetcher


def page(price, availability="InStock", name="Drill"):
    """A product page with JSON-LD, priced in EUR."""
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "EUR",
            "availability": f"https://schema.org/{availability}",
        },
    }
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body></body></html>'


class Site:
    """A fake web: maps full URLs to (status, body). Unknown URLs return 404."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.requested = []

    def handler(self, request):
        self.requested.append(str(request.url))
        status, body = self.pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body)


def make_fetcher(site):
    """A Fetcher that talks to a Site, with no waiting, no retries and no robots checks."""
    settings = Settings(respect_robots_txt=False, min_interval_seconds=0, retries=0)
    client = httpx.Client(transport=httpx.MockTransport(site.handler))
    return Fetcher(settings, client=client, sleep=lambda seconds: None)
