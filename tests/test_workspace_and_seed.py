"""The outer loop — `oxtend workspace` (workspace.py) and `oxtend seed` (seed.py).

Both are pure-python here: nothing in this file needs core importable or Docker.

`seed_collection` is exercised against a real loopback HTTP server, matching
test_devloop's reasoning — the thing worth testing is the request we send and the
envelope we read, and a mock of our own call would assert that we call ourselves
the way we call ourselves.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from oxtend.seed import SeedError, expected_slug, seed_collection
from oxtend.workspace import WorkspaceError, load_workspace

# ---------------------------------------------------------------------------
# Workspace file parsing
# ---------------------------------------------------------------------------


def _checkout(tmp_path: Path, *bundles: str) -> Path:
    """A minimal but structurally real workspace: a core with a compose file."""
    core = tmp_path / "platform" / "orion-core"
    core.mkdir(parents=True)
    (core / "compose.local.yaml").write_text("services: {}\n")
    for name in bundles:
        bundle = tmp_path / "platform" / name
        bundle.mkdir(parents=True)
        (bundle / "oxtend.yaml").write_text(f"scope: {name}\n")
    return tmp_path


def _write(root: Path, doc: object) -> Path:
    path = root / "workspace.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_paths_resolve_against_the_file_not_the_cwd(tmp_path: Path, monkeypatch) -> None:
    """The whole point of the outer loop is running it from anywhere in the tree."""
    root = _checkout(tmp_path, "x_demo")
    path = _write(
        root,
        {"core": {"path": "platform/orion-core"}, "bundles": ["platform/x_demo"]},
    )
    nested = root / "platform" / "x_demo"
    monkeypatch.chdir(nested)

    ws = load_workspace(path)

    assert ws.core_dir == (root / "platform" / "orion-core").resolve()
    assert ws.bundles == [nested.resolve()]


def test_core_shorthand_is_accepted(tmp_path: Path) -> None:
    """`core: platform/orion-core` as a bare string, not just a mapping."""
    root = _checkout(tmp_path)
    ws = load_workspace(_write(root, {"core": "platform/orion-core"}))
    assert ws.core_dir.name == "orion-core"
    assert ws.compose_file == "compose.local.yaml"
    assert ws.core_url == "http://localhost:8000"


def test_bundle_entries_may_be_mappings(tmp_path: Path) -> None:
    """A bare path today must not preclude per-bundle options tomorrow."""
    root = _checkout(tmp_path, "x_demo")
    ws = load_workspace(
        _write(
            root,
            {
                "core": "platform/orion-core",
                "bundles": [{"path": "platform/x_demo"}],
            },
        )
    )
    assert [b.name for b in ws.bundles] == ["x_demo"]


def test_trailing_slash_on_url_is_stripped(tmp_path: Path) -> None:
    """Otherwise every derived URL doubles the separator."""
    root = _checkout(tmp_path)
    ws = load_workspace(
        _write(root, {"core": {"path": "platform/orion-core", "url": "http://x:8000/"}})
    )
    assert ws.core_url == "http://x:8000"


def test_missing_file_names_the_path_and_the_default(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as exc:
        load_workspace(tmp_path / "workspace.yaml")
    assert "workspace.yaml" in str(exc.value)


def test_missing_core_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="core.path"):
        load_workspace(_write(tmp_path, {"bundles": []}))


def test_a_directory_without_oxtend_yaml_is_not_a_bundle(tmp_path: Path) -> None:
    """The common typo is pointing at the repo instead of the bundle inside it."""
    root = _checkout(tmp_path)
    (root / "platform" / "not-a-bundle").mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="oxtend.yaml"):
        load_workspace(
            _write(
                root,
                {"core": "platform/orion-core", "bundles": ["platform/not-a-bundle"]},
            )
        )


def test_naming_the_wrong_compose_file_says_which_is_which(tmp_path: Path) -> None:
    """compose.yaml is the 30-service deployment stack and will not come up here."""
    root = _checkout(tmp_path)
    with pytest.raises(WorkspaceError, match="compose.local.yaml"):
        load_workspace(
            _write(
                root,
                {"core": {"path": "platform/orion-core", "compose": "compose.yaml"}},
            )
        )


def test_invalid_yaml_is_reported_as_such(tmp_path: Path) -> None:
    path = tmp_path / "workspace.yaml"
    path.write_text("core: [unclosed\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not valid YAML"):
        load_workspace(path)


# ---------------------------------------------------------------------------
# Slug derivation — must match the server's, or the idempotency check misses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Demo Knowledge Base", "demo-knowledge-base"),
        ("  Spaced  Out  ", "spaced-out"),
        ("Ünïcödé & Symbols!", "n-c-d-symbols"),
        ("AB", "collection-ab"),  # shorter than 3 chars gets the prefix
        ("x" * 100, "x" * 64),  # truncated to 64
    ],
)
def test_expected_slug_matches_the_servers_derivation(name: str, slug: str) -> None:
    """admin_ext.create_admin_collection derives the slug and does not accept one.

    If this drifts from the server, `seed` looks for the wrong row and creates a
    duplicate on every run instead of being idempotent.
    """
    assert expected_slug(name) == slug


# ---------------------------------------------------------------------------
# seed_collection against a real server
# ---------------------------------------------------------------------------


class _Collections(BaseHTTPRequestHandler):
    existing: list[dict] = []
    posted: list[dict] = []
    list_status = 200
    envelope = False

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.list_status != 200:
            self._send(self.list_status, {"detail": "nope"})
            return
        payload = {"items": self.existing} if self.envelope else self.existing
        self._send(200, payload)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        type(self).posted.append(body)
        self._send(201, {"slug": expected_slug(body["name"]), "name": body["name"]})

    def log_message(self, *args) -> None:  # noqa: A002
        pass


@pytest.fixture
def server():
    _Collections.existing = []
    _Collections.posted = []
    _Collections.list_status = 200
    _Collections.envelope = False
    httpd = HTTPServer(("127.0.0.1", 0), _Collections)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_creates_when_absent(server: str) -> None:
    result = seed_collection(server, name="Demo Knowledge Base")
    assert result.created is True
    assert result.slug == "demo-knowledge-base"
    assert _Collections.posted[0]["name"] == "Demo Knowledge Base"


def test_is_idempotent_when_the_slug_exists(server: str) -> None:
    """Safe to run on every `workspace up` — that is the only reason it is called there."""
    _Collections.existing = [{"slug": "demo-knowledge-base"}]
    result = seed_collection(server, name="Demo Knowledge Base")
    assert result.created is False
    assert _Collections.posted == []


def test_accepts_both_list_and_envelope_shapes(server: str) -> None:
    """The list endpoint has shipped as a bare list and as {items: [...]}."""
    _Collections.envelope = True
    _Collections.existing = [{"slug": "demo-knowledge-base"}]
    assert seed_collection(server, name="Demo Knowledge Base").created is False


def test_auth_failure_points_at_dev_auth_bypass(server: str) -> None:
    """403 here means the bypass is off, not that the payload is wrong."""
    _Collections.list_status = 403
    with pytest.raises(SeedError, match="DEV_AUTH_BYPASS"):
        seed_collection(server)


def test_unreachable_core_says_so(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="cannot reach"):
        seed_collection("http://127.0.0.1:1")
