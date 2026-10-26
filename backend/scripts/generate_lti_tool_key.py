"""Generate a DEVELOPMENT RS256 key pair for the AI iEXCEL LTI tool.

    python -m scripts.generate_lti_tool_key [--out-dir .lti] [--kid aisp-dev-1]

Writes <out-dir>/tool-dev.pem (private, chmod 600, gitignored) and
<out-dir>/tool-dev.jwk.json (public JWK, registered at the platform by
ai-iexcel-platform/backend/scripts/dev_course1_setup.py). Refuses to overwrite.
Production keys are provisioned separately and never generated with this.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", default=".lti")
    parser.add_argument("--kid", default="aisp-dev-1")
    args = parser.parse_args()
    out = Path(args.out_dir)
    pem_path, jwk_path = out / "tool-dev.pem", out / "tool-dev.jwk.json"
    if pem_path.exists() or jwk_path.exists():
        print(f"refusing to overwrite existing key files in {out}/", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(pem_path, 0o600)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": args.kid, "alg": "RS256", "use": "sig"})
    jwk_path.write_text(json.dumps(jwk, indent=2) + "\n")
    print(f"private key: {pem_path.resolve()}")
    print(f"public JWK:  {jwk_path.resolve()}")
    print(f"LTI_TOOL_KEY_FILE={pem_path.resolve()}")
    print(f"LTI_TOOL_KEY_ID={args.kid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
