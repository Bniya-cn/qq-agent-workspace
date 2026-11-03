import json,urllib.request,urllib.parse,os
base="https://huggingface.co/datasets/huanx/daniya-voice-gptsovits"
tree=json.load(urllib.request.urlopen(base.replace("/datasets/","/api/datasets/")+"/tree/main?recursive=true"))
out="data_raw/dania/huanx"; os.makedirs(out,exist_ok=True)
n=0
for t in tree:
    if t["type"]!="file" or not (t["path"].startswith("audio/") or t["path"] in ("metadata.csv","daniya.list")): continue
    dst=os.path.join(out,os.path.basename(t["path"]))
    if os.path.exists(dst) and os.path.getsize(dst)==t["size"]: n+=1; continue
    for k in range(3):
        try: urllib.request.urlretrieve(base+"/resolve/main/"+urllib.parse.quote(t["path"]),dst); break
        except Exception as e: print("retry",dst,e,flush=True)
    n+=1; print(n,dst,flush=True)
print("DONE",n)
