"""Put enough in a fresh core that the admin UI is not an empty shell.

A migrated database is not a usable one: with no collection, the admin UI lists
nothing, retrieval has nothing to scope to, and a bundle that registers custom
fields has nothing to register them against. Seeding is idempotent so it can run
on every `workspace up` without accumulating.

Deliberately not seeded: documents. Ingestion needs an embedding model and an
LLM key, neither of which the local stack configures, and a seed that half-works
is worse than one that stops at the line and says so.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_COLLECTION_NAME = "Demo Knowledge Base"
DEFAULT_COLLECTION_DESCRIPTION = "Seeded by `oxtend seed` for local development."


class SeedError(RuntimeError):
    """Seeding could not complete."""


@dataclass(frozen=True)
class SeedResult:
    slug: str
    name: str
    created: bool


def expected_slug(name: str) -> str:
    """Reproduce the server's slug derivation so we can check before creating.

    `POST /admin/collections` derives the slug from the name and does not accept
    one, so this must match admin_ext.create_admin_collection exactly or the
    idempotency check looks for the wrong row.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if len(slug) < 3:
        slug = f"collection-{slug}"
    return slug[:64]


def _request(method: str, url: str, payload: dict | None = None) -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode() or "null"
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body or "null")
        except json.JSONDecodeError:
            return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        raise SeedError(
            f"cannot reach {url}: {getattr(exc, 'reason', exc)}. Is the core up?"
        ) from exc


def seed_collection(
    core_url: str,
    name: str = DEFAULT_COLLECTION_NAME,
    description: str = DEFAULT_COLLECTION_DESCRIPTION,
) -> SeedResult:
    """Create the demo collection unless a collection with its slug exists."""
    base = f"{core_url.rstrip('/')}/api/v1/admin/collections"
    slug = expected_slug(name)

    status, body = _request("GET", base)
    if status == 401 or status == 403:
        raise SeedError(
            f"the collections API returned {status}. The local stack sets "
            f"DEV_AUTH_BYPASS=true; without it this needs a real client-admin token."
        )
    if status != 200:
        raise SeedError(f"GET {base} returned {status}: {body}")

    # The list endpoint paginates and has shipped both a bare list and an
    # {items: [...]} envelope; accept either rather than pinning to one shape.
    items = body.get("items", []) if isinstance(body, dict) else body
    existing = {
        item.get("slug")
        for item in (items or [])
        if isinstance(item, dict) and item.get("slug")
    }
    if slug in existing:
        return SeedResult(slug=slug, name=name, created=False)

    status, body = _request(
        "POST", base, {"name": name, "description": description, "tags": ["demo"]}
    )
    if status != 201:
        raise SeedError(f"POST {base} returned {status}: {body}")
    created_slug = body.get("slug", slug) if isinstance(body, dict) else slug
    return SeedResult(slug=created_slug, name=name, created=True)
