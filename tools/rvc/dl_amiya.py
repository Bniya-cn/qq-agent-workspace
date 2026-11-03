import json,urllib.request,urllib.parse,os,sys
base="https://huggingface.co/datasets/Ethernos/ArkEcho-Dataset-Amiya-Chinese-Heavy"
d="阿米娅普通话"
tree=json.load(urllib.request.urlopen(base.replace("/datasets/","/api/datasets/")+"/tree/main/"+urllib.parse.quote(d)))
out="data_raw/amiya/arkecho"; os.makedirs(out,exist_ok=True)
n=0
for t in tree:
    if t["type"]!="file": continue
    fn=os.path.basename(t["path"]); dst=os.path.join(out,fn)
    if os.path.exists(dst) and os.path.getsize(dst)==t["size"]: n+=1; continue
    url=base+"/resolve/main/"+urllib.parse.quote(t["path"])
    for k in range(3):
        try:
            urllib.request.urlretrieve(url,dst); break
        except Exception as e: print("retry",fn,e,flush=True)
    n+=1; print(n,fn,flush=True)
print("DONE",n)
