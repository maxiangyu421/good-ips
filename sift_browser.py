#!/usr/bin/env python3
"""阶段2: 对 sifted.txt 逐个用 uc_ts.py(SINGLE_PROXY) 真·试 Turnstile。
出 token → 写 Gist good_pool.txt(置顶池, 注册流程自动优先用); 失败 → 累积 dead_pool.txt。
每个代理一个 xvfb-run 子进程, 干净隔离; 单个预算 110s。"""
import os, sys, subprocess

from cfg_open import load as _cfg
_CFG = _cfg()
GIST_TOKEN = os.environ["GIST_TOKEN"]; GIST_ID = _CFG["GIST_ID"]
BUDGET = int(os.environ.get("SIFT_COUNT", "10"))

def jreq(url, method="GET", data=None, hdrs=None, timeout=20):
    import json, urllib.request as U
    h = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if hdrs: h.update(hdrs)
    body = json.dumps(data).encode() if data is not None else None
    r = U.Request(url, data=body, headers=h, method=method)
    try:
        with U.urlopen(r, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode())
    except Exception as e:
        try: return getattr(e, "code", -1) or -1, json.loads(e.read().decode())
        except Exception: return -1, {"error": str(e)[:120]}

def gist_file(name):
    st, d = jreq(f"https://api.github.com/gists/{GIST_ID}",
                 hdrs={"Authorization": "token " + GIST_TOKEN})
    return ((d.get("files") or {}).get(name, {}).get("content") or "")

def gist_patch(files):
    jreq(f"https://api.github.com/gists/{GIST_ID}", "PATCH", {"files": files},
         {"Authorization": "token " + GIST_TOKEN})

def test_one(px):
    for f in ("ts_token.txt", "ts_proxy.txt"):
        if os.path.exists(f): os.remove(f)
    env = dict(os.environ, SINGLE_PROXY=px)
    try:
        subprocess.run(["xvfb-run", "-a", sys.executable, "uc_ts.py"],
                       env=env, timeout=110, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print(f"[try] {px} 超时(110s)")
    tok = ""
    if os.path.exists("ts_token.txt"):
        tok = open("ts_token.txt").read().strip()
    return tok

if __name__ == "__main__":
    cands = [l.strip() for l in open("sifted.txt") if l.strip()][:BUDGET]
    print(f"[stage2] {len(cands)} 个候选", flush=True)
    passed = []
    for i, px in enumerate(cands):
        print(f"[try] {i+1}/{len(cands)} {px} …", flush=True)
        tok = test_one(px)
        if tok:
            print(f"[try] ✅ {px} token_len={len(tok)}", flush=True)
            passed.append(px)
        else:
            print(f"[try] ❌ {px}", flush=True)
        if len(passed) >= 4:   # 每轮最多收 4 个新优质, 够注册池滚动用了
            print("[stage2] 已满 4 个, 提前收工"); break
    good = [l.strip() for l in gist_file("good_pool.txt").splitlines() if l.strip()]
    dead = [l.strip() for l in gist_file("dead_pool.txt").splitlines() if l.strip()]
    new_good = [p for p in passed if p not in good]
    new_dead = [p for p in cands if p not in passed and p not in dead]
    if new_good:
        gist_patch({"good_pool.txt": {"content": "\n".join((new_good + good)[:10])}})
    if new_dead:
        gist_patch({"dead_pool.txt": {"content": "\n".join((new_dead + dead)[:600])}})
    # job summary
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write(f"## 试盾结果\n- 候选 {len(cands)} / 通过 {len(passed)} / 新增优质 {len(new_good)}\n")
        f.write("\n".join(f"- ✅ {p}" for p in new_good) + "\n")
    print(f"[stage2] 通过 {len(passed)}, good_pool {len(good)+len(new_good)}, dead_pool {len(dead)+len(new_dead)}")
