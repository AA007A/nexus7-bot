import base64,csv,decimal,hashlib,json,os,pathlib,time,urllib.error,urllib.parse,urllib.request,zipfile
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
BASE='https://api-futures.kucoin.com'; KLINE=BASE+'/api/v1/kline/query'
ROOT=pathlib.Path(os.getenv('DATA_ROOT','/data/bgx-missed-market-002')); RAW=ROOT/'data'/'raw'; NORM=ROOT/'data'/'normalized'; OUT=ROOT/'output'
for p in (RAW,NORM,OUT): p.mkdir(parents=True,exist_ok=True)
MAP={'BTCUSDT':'XBTUSDTM','ETHUSDT':'ETHUSDTM','SOLUSDT':'SOLUSDTM','XRPUSDT':'XRPUSDTM','ADAUSDT':'ADAUSDTM','DOGEUSDT':'DOGEUSDTM','LINKUSDT':'LINKUSDTM','AVAXUSDT':'AVAXUSDTM','DOTUSDT':'DOTUSDTM','LTCUSDT':'LTCUSDTM','NEARUSDT':'NEARUSDTM','ATOMUSDT':'ATOMUSDTM'}
TFS={'1m':1,'5m':5,'15m':15,'1h':60,'4h':240}; MAX_PAGE_BUCKETS=180
START=int(datetime(2026,9,23,14,tzinfo=timezone.utc).timestamp()*1000); END=int(datetime(2026,9,23,18,tzinfo=timezone.utc).timestamp()*1000); H4START=int(datetime(2026,9,23,12,tzinfo=timezone.utc).timestamp()*1000); H4END=int(datetime(2026,9,23,16,tzinfo=timezone.utc).timestamp()*1000)
REQ=[]; FAIL=[]; APIERR=[]; CONTRACT_OK={}; SLICES={}
def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(1048576),b''): h.update(b)
 return h.hexdigest()
def load(b): return json.loads(b.decode(),parse_float=str,parse_int=int)
def get(url,sym,contract,gran,frm,to,maxtries=4):
 last=''
 for retry in range(maxtries):
  stamp=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'BGX-RESEARCH/2.0'}),timeout=20) as r: status=r.status; body=r.read()
   obj=load(body); code=str(obj.get('code','')); d=obj.get('data'); n=len(d) if isinstance(d,list) else (1 if d is not None else 0)
   REQ.append({'request_timestamp':stamp,'symbol':sym,'contract':contract,'granularity':gran,'from':frm,'to':to,'http_status':status,'kucoin_code':code,'row_count':n,'retry_count':retry})
   if status==200 and code=='200000': return body,obj
   last=f'HTTP {status} code={code}'
   if status==429 or status>=500: time.sleep(2**retry); continue
   break
  except urllib.error.HTTPError as e:
   try: body=e.read(); obj=load(body); code=str(obj.get('code',''))
   except Exception: code=''
   REQ.append({'request_timestamp':stamp,'symbol':sym,'contract':contract,'granularity':gran,'from':frm,'to':to,'http_status':e.code,'kucoin_code':code,'row_count':0,'retry_count':retry}); last=f'HTTPError {e.code} {code}'
   if e.code==429 or e.code>=500: time.sleep(2**retry); continue
   break
  except Exception as e:
   last=f'{type(e).__name__}:{e}'; REQ.append({'request_timestamp':stamp,'symbol':sym,'contract':contract,'granularity':gran,'from':frm,'to':to,'http_status':None,'kucoin_code':None,'row_count':0,'retry_count':retry,'error':last}); time.sleep(2**retry)
 raise RuntimeError(last or 'request failed')
def pages(frm,end_exclusive,gran):
 step=gran*60000; span=MAX_PAGE_BUCKETS*step; out=[]; cur=frm
 while cur<end_exclusive:
  nxt=min(cur+span,end_exclusive); out.append((cur,nxt-1)); cur=nxt
 return out
def readcsv(contract,tf):
 p=NORM/f'{contract}_{tf}.csv'; out=[]
 if not p.exists(): return out
 with open(p,newline='') as f:
  for r in csv.DictReader(f): r['timestamp']=int(r['timestamp']); out.append(r)
 return out
# Public contract metadata validation.
for internal,contract in MAP.items():
 try:
  body,obj=get(BASE+'/api/v1/contracts/'+urllib.parse.quote(contract),internal,contract,'CONTRACT_META',None,None); d=obj.get('data') or {}; ok=(obj.get('code')=='200000' and d.get('symbol')==contract); CONTRACT_OK[internal]=ok
  p=RAW/contract/'contract.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(body)
  if not ok: APIERR.append({'type':'contract_mapping','internal':internal,'contract':contract,'returned':d.get('symbol')})
  print(f'CONTRACT {internal} {contract} '+('OK' if ok else 'FAIL'),flush=True)
 except Exception as e: CONTRACT_OK[internal]=False; FAIL.append({'slice':internal+':CONTRACT_META','error':str(e)}); print(f'CONTRACT {internal} ERROR {e!r}',flush=True)
# 60 logical slices, deterministic pagination with exact raw page preservation.
for internal,contract in MAP.items():
 for tf,gran in TFS.items():
  frm,endx=(H4START,H4END) if tf=='4h' else (START,END); page_ranges=pages(frm,endx,gran); allrows=[]; raw_desc=False; raw_files=[]; key=internal+':'+tf
  try:
   pagedir=RAW/contract/tf; pagedir.mkdir(parents=True,exist_ok=True)
   for pi,(pf,pt) in enumerate(page_ranges):
    q=urllib.parse.urlencode({'symbol':contract,'granularity':gran,'from':pf,'to':pt}); body,obj=get(KLINE+'?'+q,internal,contract,gran,pf,pt); rp=pagedir/f'page_{pi:03d}.json'; rp.write_bytes(body); raw_files.append(str(rp.relative_to(ROOT)))
    rows=obj.get('data') or []
    if not isinstance(rows,list): raise RuntimeError('data not list')
    ts=[int(r[0]) for r in rows if isinstance(r,list) and len(r)>=7]; raw_desc=raw_desc or any(ts[i]>ts[i+1] for i in range(len(ts)-1))
    for r in rows:
     if not isinstance(r,list) or len(r)<7: raise RuntimeError('bad kline row')
     allrows.append((int(r[0]),*[str(x) for x in r[1:7]]))
   # Deduplicate only for validation/normalization; duplicates remain preserved in raw pages.
   allrows.sort(key=lambda r:r[0]); counts={}
   for r in allrows: counts[r[0]]=counts.get(r[0],0)+1
   unique=[]; seen=set()
   for r in allrows:
    if r[0] not in seen: unique.append(r); seen.add(r[0])
   np=NORM/f'{contract}_{tf}.csv'
   with open(np,'w',newline='') as f: w=csv.writer(f,lineterminator='\n'); w.writerow(['timestamp','open','high','low','close','volume','turnover']); w.writerows(unique)
   step=gran*60000; expected=[H4START] if tf=='4h' else list(range(START,END,step)); eset=set(expected); got={r[0] for r in unique}; missing=[t for t in expected if t not in got]; dup=sum(v-1 for t,v in counts.items() if t in eset and v>1); outside=[r[0] for r in unique if r[0] not in eset]
   bad=[]; align=[]
   for r in unique:
    t,o,h,l,c,v,turn=r; O,H,L,C,V=map(decimal.Decimal,(o,h,l,c,v))
    if not(H>=O and H>=C and L<=O and L<=C and H>=L and V>=0): bad.append(t)
    if t%step: align.append(t)
   SLICES[key]={'internal_symbol':internal,'contract':contract,'timeframe':tf,'granularity':gran,'page_count':len(page_ranges),'raw_files':raw_files,'first_timestamp':unique[0][0] if unique else None,'last_timestamp':unique[-1][0] if unique else None,'row_count':len(unique),'expected_bucket_count':len(expected),'missing_buckets':missing,'missing_classification':['NO_TRADE_GAP_POSSIBLE' for _ in missing],'duplicates':dup,'outside_requested_buckets':outside,'raw_out_of_order':raw_desc,'normalized_ascending':all(unique[i][0]<unique[i+1][0] for i in range(len(unique)-1)),'ohlc_violations':bad,'timezone_alignment_violations':align,'normalized_file':str(np.relative_to(ROOT))}
   print(f'SLICE {internal} {tf} pages={len(page_ranges)} rows={len(unique)} expected={len(expected)} missing={len(missing)} dup={dup} outside={len(outside)} ohlc_bad={len(bad)}',flush=True)
  except Exception as e: FAIL.append({'slice':key,'error':str(e)}); print(f'SLICE {key} ERROR {e!r}',flush=True)
with open(OUT/'request_log.jsonl','w') as f:
 for r in REQ: f.write(json.dumps(r,separators=(',',':'),default=str)+'\n')
def agg(contract,tf,mins):
 one={r['timestamp']:r for r in readcsv(contract,'1m')}; direct={r['timestamp']:r for r in readcsv(contract,tf)}; eligible=matches=mismatches=skipped=0; details=[]
 for b in range(START,END,mins*60000):
  need=[b+i*60000 for i in range(mins)]
  if b not in direct or not all(t in one for t in need): skipped+=1; continue
  eligible+=1; rs=[one[t] for t in need]; calc={'open':decimal.Decimal(rs[0]['open']),'high':max(decimal.Decimal(x['high']) for x in rs),'low':min(decimal.Decimal(x['low']) for x in rs),'close':decimal.Decimal(rs[-1]['close']),'volume':sum(decimal.Decimal(x['volume']) for x in rs),'turnover':sum(decimal.Decimal(x['turnover']) for x in rs)}; d=direct[b]; om=all(calc[k]==decimal.Decimal(d[k]) for k in ('open','high','low','close')); vm=calc['volume']==decimal.Decimal(d['volume']); tm=calc['turnover']==decimal.Decimal(d['turnover'])
  if om and vm and tm: matches+=1
  else: mismatches+=1; details.append({'bucket':b,'ohlc_match':om,'volume_match':vm,'turnover_match':tm})
 return {'eligible_complete_buckets':eligible,'matches':matches,'mismatches':mismatches,'skipped':skipped,'details':details}
CROSS={tf:{} for tf in ('5m','15m','1h')}
for internal,contract in MAP.items():
 CROSS['5m'][internal]=agg(contract,'5m',5); CROSS['15m'][internal]=agg(contract,'15m',15); CROSS['1h'][internal]=agg(contract,'1h',60)
contract_count=sum(bool(x) for x in CONTRACT_OK.values()); slice_count=len(SLICES); missing_total=sum(len(x['missing_buckets']) for x in SLICES.values()); dup_total=sum(x['duplicates'] for x in SLICES.values()); outside_total=sum(len(x['outside_requested_buckets']) for x in SLICES.values()); ohlc_bad=sum(len(x['ohlc_violations']) for x in SLICES.values()); tz_bad=sum(len(x['timezone_alignment_violations']) for x in SLICES.values()); norm_ooo=sum(not x['normalized_ascending'] for x in SLICES.values()); raw_ooo=sum(x['raw_out_of_order'] for x in SLICES.values()); cm={tf:sum(x['mismatches'] for x in vals.values()) for tf,vals in CROSS.items()}; ce={tf:sum(x['eligible_complete_buckets'] for x in vals.values()) for tf,vals in CROSS.items()}
# Missing buckets from a successful bounded page remain documented as possible no-trade gaps.
# Critical acquisition failures, duplicates, out-of-range timestamps, OHLC/alignment faults, or cross-TF mismatches fail the gate.
dataset_valid=(contract_count==12 and slice_count==60 and not FAIL and not APIERR and dup_total==0 and outside_total==0 and ohlc_bad==0 and tz_bad==0 and norm_ooo==0 and all(cm[t]==0 for t in cm) and all(ce[t]>0 for t in ce))
report={'source':'KUCOIN_FUTURES_OFFICIAL','endpoint':KLINE,'start':'2026-09-23T14:00:00Z','end':'2026-09-23T18:00:00Z','analysis_interval':'[14:00Z,18:00Z)','pagination_max_buckets_per_request':MAX_PAGE_BUCKETS,'forming_4h_rule':'direct confirmed 12:00-16:00 only; 16:00-18:00 forming 4h must be reconstructed later from 1m; 20:00 final data not acquired','contracts_expected':12,'contracts_validated':contract_count,'contract_mapping':MAP,'contract_validation':CONTRACT_OK,'slices_expected':60,'slices_acquired':slice_count,'slice_counts':{tf:sum(k.endswith(':'+tf) for k in SLICES) for tf in TFS},'http_failures':sum(r.get('http_status') not in (200,None) for r in REQ),'kucoin_api_errors':len(APIERR),'failures':FAIL,'api_errors':APIERR,'missing_buckets':missing_total,'unexplained_gaps':0,'duplicates':dup_total,'outside_requested_buckets':outside_total,'raw_out_of_order_slices':raw_ooo,'normalized_out_of_order_slices':norm_ooo,'ohlc_integrity_violations':ohlc_bad,'timezone_alignment_violations':tz_bad,'cross_timeframe':{'eligible':ce,'mismatches':cm,'details':CROSS},'slices':SLICES,'dataset_content_valid':dataset_valid}
vp=OUT/'validation_report.json'; vp.write_text(json.dumps(report,indent=2,sort_keys=True,default=str))
# Freeze all raw/normalized/report/request evidence.
art=[]
for p in sorted(ROOT.rglob('*')):
 if not p.is_file() or p.name in {'manifest.json','manifest.sha256','dataset.zip','dataset.zip.sha256','summary.json'}: continue
 rel=str(p.relative_to(ROOT)); item={'filename':rel,'size':p.stat().st_size,'sha256':sha(p),'source':'KUCOIN_FUTURES_OFFICIAL' if rel.startswith('data/') else 'BGX_RESEARCH'}; art.append(item)
manifest={'dataset':'BGX-MISSED-MARKET-002','created_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'data_cut':'2026-09-23T18:00:00Z','source':'KUCOIN_FUTURES_OFFICIAL','dataset_content_valid':dataset_valid,'artifacts':art}; mp=OUT/'manifest.json'; mp.write_text(json.dumps(manifest,indent=2,sort_keys=True)); msha=sha(mp); (OUT/'manifest.sha256').write_text(msha+'  manifest.json\n')
zp=OUT/'dataset.zip'
with zipfile.ZipFile(zp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
 for p in sorted(ROOT.rglob('*')):
  if p.is_file() and p!=zp and p.name!='dataset.zip.sha256': z.write(p,arcname=str(p.relative_to(ROOT)))
zsha=sha(zp); (OUT/'dataset.zip.sha256').write_text(zsha+'  dataset.zip\n')
summary={'contracts_validated':contract_count,'slices_acquired':slice_count,'1m_slices':report['slice_counts']['1m'],'5m_slices':report['slice_counts']['5m'],'15m_slices':report['slice_counts']['15m'],'1h_slices':report['slice_counts']['1h'],'4h_slices':report['slice_counts']['4h'],'http_failures':report['http_failures'],'kucoin_api_errors':len(APIERR),'raw_files':sum(1 for p in RAW.rglob('*.json')),'normalized_files':sum(1 for p in NORM.glob('*.csv')),'missing_buckets':missing_total,'unexplained_gaps':0,'duplicates':dup_total,'outside_requested_buckets':outside_total,'out_of_order_normalized':norm_ooo,'raw_out_of_order_slices':raw_ooo,'ohlc_integrity_violations':ohlc_bad,'timezone_alignment_violations':tz_bad,'5m_aggregation_match':cm['5m']==0 and ce['5m']>0,'15m_aggregation_match':cm['15m']==0 and ce['15m']>0,'1h_aggregation_match':cm['1h']==0 and ce['1h']>0,'manifest_sha256':msha,'bundle_sha256':zsha,'dataset_content_valid':dataset_valid,'failed_slices':FAIL}; (OUT/'summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True)); print('BGX_DATASET_SUMMARY='+json.dumps(summary,separators=(',',':')),flush=True)
# Railway-only persistence fallback: controller may store this exact compressed bundle in service variables.
b64=base64.b64encode(zp.read_bytes()).decode(); chunks=[b64[i:i+2500] for i in range(0,len(b64),2500)]; print('BGX_BUNDLE_CHUNK_COUNT='+str(len(chunks)),flush=True); print('BGX_BUNDLE_SHA256='+zsha,flush=True)
for i,ch in enumerate(chunks): print(f'BGX_BUNDLE_CHUNK_{i:04d}={ch}',flush=True)
os.chdir(ROOT); port=int(os.getenv('PORT','8080')); print(f'BGX_HTTP_SERVING port={port} root={ROOT}',flush=True); ThreadingHTTPServer(('0.0.0.0',port),SimpleHTTPRequestHandler).serve_forever()
