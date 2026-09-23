import base64
import hashlib
import os
import pathlib
import zipfile
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = pathlib.Path(os.environ.get("DATA_ROOT", "/data/bgx-missed-market-002"))
OUT = ROOT / "output"
OUT.mkdir(parents=True, exist_ok=True)


def serve():
    os.chdir(ROOT)
    port = int(os.environ.get("PORT", "8080"))
    print(f"BGX_HTTP_SERVING port={port} root={ROOT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()


count = int(os.environ.get("BGX_PERSISTED_BUNDLE_CHUNKS", "0") or "0")
if count > 0:
    expected_sha = os.environ.get("BGX_PERSISTED_BUNDLE_SHA256", "")
    chunks = []
    for index in range(count):
        key = f"BGX_PERSISTED_BUNDLE_{index:04d}"
        value = os.environ.get(key)
        if value is None:
            raise RuntimeError(f"Missing Railway persisted bundle chunk {key}")
        chunks.append(value)
    payload = base64.b64decode("".join(chunks), validate=True)
    actual_sha = hashlib.sha256(payload).hexdigest()
    if not expected_sha or actual_sha != expected_sha:
        raise RuntimeError(
            f"Railway persisted bundle SHA mismatch expected={expected_sha} actual={actual_sha}"
        )
    zip_path = OUT / "dataset.zip"
    zip_path.write_bytes(payload)
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(ROOT)
    manifest_sha_path = OUT / "manifest.sha256"
    manifest_sha = manifest_sha_path.read_text(encoding="utf-8").split()[0]
    print(
        f"BGX_PERSISTENCE_RESTORE=YES chunks={count} bundle_sha256={actual_sha} "
        f"manifest_sha256={manifest_sha}",
        flush=True,
    )
    serve()

# No persisted bundle yet: execute the acquisition implementation, but increase
# the log transport chunk size so the controller can freeze the exact ZIP into
# a manageable number of Railway service variables. Acquisition itself remains
# unchanged and still executes entirely inside this Railway container.
source_path = pathlib.Path("/research/research_dataset_v2.py")
source = source_path.read_text(encoding="utf-8")
old = "chunks=[b64[i:i+2500] for i in range(0,len(b64),2500)]"
new = "chunk_size=int(os.getenv('BGX_LOG_CHUNK_SIZE','20000')); chunks=[b64[i:i+chunk_size] for i in range(0,len(b64),chunk_size)]"
if old not in source:
    raise RuntimeError("Expected v2 chunk transport expression not found")
source = source.replace(old, new, 1)
exec(compile(source, str(source_path), "exec"), globals(), globals())
