"""對線上（或本機）的 bot 試跑幾則訊息，印出它本來會回什麼。"""
import json, sys, time, requests
from dotenv import dotenv_values
from line_utils import signature
secret = dotenv_values(".env")["LINE_CHANNEL_SECRET"]
BASE = "https://mama-stocker-backend.onrender.com"
ME = "Uf1f9a1f82b13e78f10519726df8e85bf"
def ask(text, show_sources=False, base=BASE):
    raw = json.dumps({"events":[{"type":"message","replyToken":"0"*32,"source":{"type":"user","userId":ME},"message":{"type":"text","id":"1","text":text}}]}).encode()
    s=time.time()
    try:
        r = requests.post(base+"/line/webhook", data=raw, headers={"X-Line-Signature": signature.sign(raw, secret), "X-Dry-Run":"1"}, timeout=60)
        d = r.json()
    except Exception as e:
        print(f"▶ {text}  ERR {type(e).__name__} {time.time()-s:.1f}s"); return None
    if "dry_run" not in d: print(f"▶ {text}  (舊版，沒有試跑) {r.status_code}"); return None
    for e in d["dry_run"]:
        rep = e["replies"][0] if e["replies"] else "(沒有回覆)"
        print(f"▶ {text}  [{e['seconds']}s]  " + (rep if isinstance(rep,str) else json.dumps(rep, ensure_ascii=False)[:90]).replace("\n"," / "))
    if show_sources:
        for k,v in d["sources"].items(): print("    ", k, v)
    return d
if __name__ == "__main__":
    for i,t in enumerate(sys.argv[1:]): ask(t, show_sources=(i==len(sys.argv)-2))
