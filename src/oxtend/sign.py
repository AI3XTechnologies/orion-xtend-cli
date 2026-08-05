"""`oxtend sign` / signature-aware push (ORION-688 task 5).

cosign-signs `bundle.json`. One signature covers the whole bundle because
`bundle.json` carries the content digest: tampering with any file changes the digest,
which no longer matches the signed document. ORION-665 verifies this on first install
of a `(scope, version)`.

`push` refuses an unsigned artifact unless `--allow-unsigned` is passed, which CI
never passes. That asymmetry is the point: a developer can iterate locally without a
keypair, and a released artifact cannot reach a registry unsigned.

Called by: oxtend/cli.py (`sign`, `push`, `all`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

BUNDLE_JSON = "bundle.json"
SIGNATURE_SUFFIX = ".sig"
CERTIFICATE_SUFFIX = ".pem"


class SigningError(RuntimeError):
    pass


def _cosign() -> str:
    path = shutil.which("cosign")
    if path is None:
        raise SigningError(
            "cosign is not on PATH. Install it (https://docs.sigstore.dev/cosign/installation/) "
            "— oxtend will not fake a signature."
        )
    return path


def sign_bundle(bundle_dir: Path, *, key: str | None = None) -> Path:
    """Sign `bundle.json`, writing `bundle.json.sig` (and `.pem` when keyless).

    `key` signs with a local key file. Without it, keyless OIDC signing is used, which
    is what CI does: the identity is the workflow, asserted by the OIDC token, and
    there is no long-lived private key to leak.
    """
    bundle_dir = Path(bundle_dir)
    target = bundle_dir / BUNDLE_JSON
    if not target.is_file():
        raise SigningError(f"{target} not found — run `oxtend build` first")

    sig_path = bundle_dir / f"{BUNDLE_JSON}{SIGNATURE_SUFFIX}"
    cmd = [_cosign(), "sign-blob", "--yes", "--output-signature", str(sig_path)]
    if key:
        cmd += ["--key", key]
    else:
        cmd += ["--output-certificate", str(bundle_dir / f"{BUNDLE_JSON}{CERTIFICATE_SUFFIX}")]
    cmd.append(str(target))

    env = dict(os.environ)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    if result.returncode != 0:
        raise SigningError(
            f"cosign sign-blob failed: {(result.stderr or result.stdout).strip()[-1500:]}"
        )
    if not sig_path.is_file():
        raise SigningError(f"cosign reported success but {sig_path} was not written")
    return sig_path


def is_signed(bundle_dir: Path) -> bool:
    return (Path(bundle_dir) / f"{BUNDLE_JSON}{SIGNATURE_SUFFIX}").is_file()


def verify_bundle(bundle_dir: Path, *, key: str | None = None, identity: str | None = None,
                  issuer: str | None = None) -> bool:
    """Verify the bundle's signature the same way the kernel will.

    Used by `oxtend sign --verify` and by CI as a post-sign sanity check: a signature
    that the kernel would reject is worse than no signature, because it passes the
    release gate and fails at install.
    """
    bundle_dir = Path(bundle_dir)
    sig = bundle_dir / f"{BUNDLE_JSON}{SIGNATURE_SUFFIX}"
    if not sig.is_file():
        return False
    cmd = [_cosign(), "verify-blob", "--signature", str(sig)]
    if key:
        cmd += ["--key", key]
    elif identity and issuer:
        cmd += ["--certificate-identity", identity, "--certificate-oidc-issuer", issuer]
    else:
        raise SigningError(
            "verification needs either --key or both --identity and --issuer; without a pinned "
            "identity cosign would accept any signer"
        )
    cmd.append(str(bundle_dir / BUNDLE_JSON))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300).returncode == 0
