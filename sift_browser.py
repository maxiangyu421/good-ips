#!/usr/bin/env python3
"""阶段2: 对 sifted.txt 逐个用 uc_ts.py(SINGLE_PROXY) 真·试 Turnstile。
出 token → 写 Gist good_pool.txt(置顶池, 注册流程自动优先用); 失败 → 累积 dead_pool.txt。
每个代理一个 xvfb-run 子进程, 干净隔离; 单个预算 110s。"""
import os, sys, subprocess, json, time

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
    st, d = jreq(f"https://api.github.com/gists/{GIST_ID}", "PATCH", {"files": files},
                 {"Authorization": "token " + GIST_TOKEN})
    print(f"[gist] patch status={st} files={list(files)}")   # 09-08: 不再静默, 4xx 直接暴露在日志
    if st >= 300:
        print(f"[gist] patch 失败详情: {json.dumps(d)[:300]}")

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
    good = [l.strip() for l in gist_file("good_pool.txt").splitlines() if l.strip()]
    good_set = set(good)
    # 09-12 fix: 新 IP 排前、已 good 的复验殿后; 「4 个名额」只数新 IP ——
    # 修 09-10 诊断: 复验通过占满名额触发提前收工, 新 IP 根本轮不到试盾(good_pool 流干)。
    new_first = [p for p in cands if p not in good_set]
    reverify = [p for p in cands if p in good_set]
    cands = new_first + reverify
    if reverify:
        print(f"[stage2] 复验 {len(reverify)} 个殿后, 新 IP {len(new_first)} 个优先", flush=True)
    print(f"[stage2] {len(cands)} 个候选", flush=True)
    passed, passed_new = [], 0
    for i, px in enumerate(cands):
        print(f"[try] {i+1}/{len(cands)} {px} …", flush=True)
        tok = test_one(px)
        if tok:
            print(f"[try] ✅ {px} token_len={len(tok)}", flush=True)
            passed.append(px)
            if px not in good_set:
                passed_new += 1
        else:
            print(f"[try] ❌ {px}", flush=True)
        if passed_new >= 4:   # 每轮最多收 4 个「新」优质; 复验通过不占名额不触发收工
            print("[stage2] 新优质已满 4 个, 提前收工"); break
    dead = [l.strip() for l in gist_file("dead_pool.txt").splitlines() if l.strip()]
    # 09-10 明星机制(hall_of_fame 永赦)已按用户指示删除。
    # 替代语义: 已 good 的 IP 复验失败 → 降级 reserve(瞬断可复活), 不进 dead;
    # 新候选失败照旧进 dead。单次失败不再永久判死已验证 IP, 但也不再终身免检。
    new_good = [p for p in passed if p not in good]
    fail_good = [p for p in cands if p not in passed and p in good]
    # ---- 优质池保鲜(09-07): 记录每个 IP 最近一次过盾时间, 超 12h 未复验就降级去 reserve 池 ----
    # 免费代理寿命小时级, 死 IP 占名额会稀释抽样还烧 35s 超时; 降级不硬删(瞬断 IP 会复活)。
    DEMOTE_HOURS = int(os.environ.get("DEMOTE_HOURS", "12"))
    now_ts = int(time.time())
    meta_raw = gist_file("good_pool_meta.json")
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except Exception:
        meta = {}
    for p in new_good:
        meta[p] = now_ts
    fresh, demoted = [], []
    for p in good:
        ts = meta.get(p)
        if ts and now_ts - ts > DEMOTE_HOURS * 3600:
            demoted.append(p)
        else:
            if not ts:
                meta[p] = now_ts   # 旧条目没时间戳, 从现在起算宽限期
            fresh.append(p)
    if demoted:
        print(f"[stage2] 超{DEMOTE_HOURS}h未复验, 降级 {len(demoted)} 个: " + ", ".join(demoted))
    # 复验失败的好 IP 一并降级(合并去重), 从 good/meta 里摘掉
    demoted = list(dict.fromkeys(demoted + fail_good))
    fresh = [p for p in fresh if p not in set(demoted)]
    for p in fail_good:
        meta.pop(p, None)
    # 复验失败降级不进 dead(新候选失败才进 dead)
    new_dead = [p for p in cands
                if p not in passed and p not in dead and p not in set(demoted)]
    if fail_good:
        print("[stage2] 复验失败, 降级 reserve(不拉黑): " + ", ".join(fail_good))
    files = {}
    if new_good or demoted:
        files["good_pool.txt"] = {"content": "\n".join(new_good + fresh)}   # 无上限(09-07 用户要求), 面板翻页展示
        reserve = [l.strip() for l in gist_file("reserve_pool.txt").splitlines() if l.strip()]
        # 09-12 卫生: 已经回到 good 的 IP 不再留在 reserve(清掉跨池重复)
        reserve = [p for p in reserve if p not in set(new_good + fresh)]
        new_reserve = [p for p in demoted if p not in reserve]
        reserve_all = (new_reserve + reserve)[:500]
        # 09-08 fix: Gist PATCH 里新建空文件(content="")会 422 且整个 patch 被静默丢弃,
        # 此前导致所有"通过 N 个"的新 IP 从未入库。空 reserve 就不写这个文件。
        if reserve_all:
            files["reserve_pool.txt"] = {"content": "\n".join(reserve_all)}
        files["good_pool_meta.json"] = {"content": json.dumps(meta)}
    if new_dead:
        files["dead_pool.txt"] = {"content": "\n".join((new_dead + dead)[:2000])}
    # 种子队列跑过就清掉本轮已测的, 避免下轮重复烧预算
    seedq = [l.strip() for l in gist_file("seed_queue.txt").splitlines() if l.strip()]
    if seedq:
        left = [p for p in seedq if p not in cands]
        if len(left) != len(seedq):
            files["seed_queue.txt"] = {"content": ("\n".join(left) + "\n") if left else "\n"}
            print(f"[stage2] 种子队列消耗 {len(seedq)-len(left)} 个, 剩 {len(left)}")
    if files:
        gist_patch(files)
    # job summary
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write(f"## 试盾结果\n- 候选 {len(cands)} / 通过 {len(passed)} / 新增优质 {len(new_good)}\n")
        f.write("\n".join(f"- ✅ {p}" for p in new_good) + "\n")
    print(f"[stage2] 通过 {len(passed)}, good_pool {len(good)+len(new_good)}, dead_pool {len(dead)+len(new_dead)}")
