FROM python:3.12-slim
WORKDIR /snapshot
ARG DATASET_URL=https://kucoin-futures-dataset-runner-production.up.railway.app/output/dataset.zip
ARG DATASET_SHA256=a838545afefdf4d22cf5a8509f112a683140b608dbd21b3f4cb5e150f3668038
RUN python - <<'PY'
import hashlib, os, urllib.request, zipfile, pathlib
url=os.environ.get('DATASET_URL') or 'https://kucoin-futures-dataset-runner-production.up.railway.app/output/dataset.zip'
expected=os.environ.get('DATASET_SHA256') or 'a838545afefdf4d22cf5a8509f112a683140b608dbd21b3f4cb5e150f3668038'
root=pathlib.Path('/data/bgx-missed-market-002'); root.mkdir(parents=True,exist_ok=True)
payload=urllib.request.urlopen(url,timeout=60).read()
actual=hashlib.sha256(payload).hexdigest()
print(f'BGX_SNAPSHOT_FETCH bytes={len(payload)} expected={expected} actual={actual}',flush=True)
if actual != expected: raise SystemExit('dataset SHA-256 mismatch; refusing snapshot')
zip_path=root/'output'/'dataset.zip'; zip_path.parent.mkdir(parents=True,exist_ok=True); zip_path.write_bytes(payload)
with zipfile.ZipFile(zip_path,'r') as z: z.extractall(root)
(root/'output'/'snapshot_bundle.sha256').write_text(actual+'  dataset.zip\n',encoding='utf-8')
PY
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python","-m","http.server","8080","--directory","/data/bgx-missed-market-002"]
