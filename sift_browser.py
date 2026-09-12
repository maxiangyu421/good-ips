#!/usr/bin/env python3
"""阶段2: 对 sifted.txt 逐个用 uc_ts.py(SINGLE_PROXY) 真·试 Turnstile。
出 token → 写 Gist good_pool.txt(置顶池, 注册流程自动优先用); 失败 → 累积 dead_pool.txt。
每个代理一个 xvfb-run 子进程, 干净隔离; 单个预算 110s。
09-12 新增: 试盾前先 uc_ping0.py 采 ping0 出口画像(stage1 无头隧道已死, 迁来这里):
  - 标「IDC机房/广播」的新 IP → 跳过试盾直接判死(省 110s 浏览器预算)
  - 其余按「风控值升序 + 代理红标降权」排序送盾(干净排前)
  - 复验 IP 不做 ping0 判死(不加新 kill 路径, 防重演 09-12 断崖); 画像仅参考"""
import os, sys, subprocess, json, time, signal

from cfg_open import load as _cfg
_CFG = _cfg()
GIST_TOKEN = os.environ["GIST_TOKEN"]; GIST_ID = _CFG["GIST_ID"]
BUDGET = int(os.environ.get("SIFT_COUNT", "10"))
P0_PROFILE_MAX = int(os.environ.get("P0_PROFILE_MAX", "15"))   # 画像采集上限(护 job 预算)
# 09-12: 50 -> 80s。实测新候选是慢速住宅代理(冷启动 Chrome 过隧道 40~60s), 50s 时
# 7/9 全部超时 -> 画像全空 -> 排序全变 600, 风控排序形同失效。
P0_TIMEOUT = int(os.environ.get("P0_TIMEOUT", "80"))           # 单个画像采集预算(秒)
# 09-12: 试盾 110 -> 150s。同理, 慢代理 110s 内常拿不到 token(4/9 超时)。
TRY_TIMEOUT = int(os.environ.get("TRY_TIMEOUT", "150"))
# job 级时间护栏: 超过这个已用秒数就不再开新候选, 保证已过盾的结果能写回 Gist
JOB_BUDGET = int(os.environ.get("STAGE2_BUDGET", str(70 * 60)))
P0_DROP_KW = ("机房", "IDC", "数据中心", "广播")               # 唯一硬淘汰信号

def run_isolated(cmd, env, timeout):
    """跑子进程, 超时杀整个进程组。
    09-12: 原来用 subprocess.run(timeout=N) —— 它只杀直接子进程, 而这里是
    xvfb-run(shell 脚本) -> python -> chromedriver -> chrome。
    超时杀掉 xvfb-run 后, 底下的 chrome 全部变孤儿继续吃内存(每轮最多 40 次调用,
    失败路径几乎都走超时), runner 上攒够就是 OOM/拖慢后续轮次。
    用 start_new_session 让子进程自成进程组, 超时对整组 SIGKILL。"""
    p = subprocess.Popen(cmd, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            try: p.kill()
            except Exception: pass
        try: p.wait(timeout=10)
        except Exception: pass
        return False

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

def ping0_profile(px):
    """uc_ping0.py 采集 ping0 出口画像(浏览器过挑战)。失败返回 {} 绝不抛。"""
    if os.path.exists("ping0_profile.json"): os.remove("ping0_profile.json")
    env = dict(os.environ, SINGLE_PROXY=px)
    if not run_isolated(["xvfb-run", "-a", sys.executable, "uc_ping0.py"],
                        env, P0_TIMEOUT):
        print(f"[p0] {px} 采集超时({P0_TIMEOUT}s), 进程组已清理", flush=True)
    try:
        return json.load(open("ping0_profile.json"))
    except Exception:
        return {}

def p0_is_idc(prof):
    """画像里带机房/广播类标签 → 硬淘汰(与过盾强负相关)。"""
    if not prof: return False
    text = " ".join(prof.get("labels") or []) + " " + str(prof.get("iptype") or "")
    return any(k.lower() in text.lower() for k in P0_DROP_KW)

def p0_sortkey(prof):
    """排序键: 风控值升序, 代理红标 +50 降权, 无画像 600 排尾。
    风控不淘汰(池内过盾 IP 风控普遍 80%+, 相关性弱, 用户定调「免费的别要求太高」)。"""
    if not prof or prof.get("error"): return 600
    risk = prof.get("risk")
    labels = " ".join(prof.get("labels") or [])
    sk = risk if isinstance(risk, int) else 600
    if "代理" in labels: sk += 50
    return sk

def test_one(px):
    for f in ("ts_token.txt", "ts_proxy.txt"):
        if os.path.exists(f): os.remove(f)
    env = dict(os.environ, SINGLE_PROXY=px)
    if not run_isolated(["xvfb-run", "-a", sys.executable, "uc_ts.py"],
                        env, TRY_TIMEOUT):
        print(f"[try] {px} 超时({TRY_TIMEOUT}s), 进程组已清理")
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
    # ---- ping0 画像采集(09-12): 只对「新 IP」判死+排序, 复验 IP 不加新 kill 路径 ----
    dropped_p0 = []
    keyed = []
    for px in new_first[:P0_PROFILE_MAX]:
        prof = ping0_profile(px)
        if p0_is_idc(prof):
            lab = " ".join(prof.get("labels") or []) or str(prof.get("iptype") or "?")
            dropped_p0.append(px)
            print(f"[p0] ❌ {px} {lab} -> 跳过试盾直接判死", flush=True)
            continue
        sk = p0_sortkey(prof)
        red = any("代理" in lb for lb in (prof.get("labels") or []))
        tag = f"risk={prof.get('risk')}%" if isinstance(prof.get("risk"), int) else "无画像"
        extra = " 代理红标" if red else ""
        native = prof.get("native") or ""
        if native: extra += f" {native}"
        print(f"[p0] {'⚠️' if red else '▫️'} {px} {tag}{extra} 排序{sk}", flush=True)
        keyed.append((sk, px))
    # 超出 P0_PROFILE_MAX 的不采集, 按原顺序排尾
    keyed.sort(key=lambda x: x[0])
    new_first = [px for _, px in keyed] + new_first[P0_PROFILE_MAX:]
    cands = new_first + reverify
    if dropped_p0:
        print(f"[p0] ping0 机房/广播判死 {len(dropped_p0)} 个: " + ", ".join(dropped_p0), flush=True)
    if reverify:
        print(f"[stage2] 复验 {len(reverify)} 个殿后, 新 IP {len(new_first)} 个优先(已按风控排序)", flush=True)
    print(f"[stage2] {len(cands)} 个候选", flush=True)
    passed, passed_new, tested_n = [], 0, 0
    t_stage2 = time.time()
    for i, px in enumerate(cands):
        used = time.time() - t_stage2
        if used > JOB_BUDGET:
            print(f"[stage2] 已用 {used/60:.1f}min 触达时间护栏({JOB_BUDGET/60:.0f}min), "
                  f"停手写回(剩 {len(cands)-i} 个下轮再测)", flush=True)
            break
        tested_n = i + 1
        print(f"[try] {i+1}/{len(cands)} {px} …", flush=True)
        tok = test_one(px)
        if tok:
            print(f"[try] ✅ {px} token_len={len(tok)}", flush=True)
            passed.append(px)
            if px not in good_set:
                passed_new += 1
                # 09-12: 增量落盘。原来只在整轮结束时一次性 PATCH, 中途超时/job 取消
                # (70min 限额贴边时常见) = 本轮所有过盾成果蒸发, 且 meta 时间戳没刷新,
                # 这批 IP 还会被当成「未复验」降级。过一个就写一次, 最坏只丢最后一个。
                try:
                    gnow = [l.strip() for l in gist_file("good_pool.txt").splitlines()
                            if l.strip()]
                    if px not in gnow:
                        if not gnow:
                            # 池子为空: 顺带清掉 dead 里可能存在的同 IP
                            gnow = [px]
                        else:
                            gnow = [px] + gnow
                        mraw = gist_file("good_pool_meta.json")
                        try: mnow = json.loads(mraw) if mraw else {}
                        except Exception: mnow = {}
                        mnow[px] = int(time.time())
                        inc = {"good_pool.txt": {"content": "\n".join(gnow)},
                               "good_pool_meta.json": {"content": json.dumps(mnow)}}
                        if not (gist_file("good_proxy.txt").strip()):
                            inc["good_proxy.txt"] = {"content": px + "\n"}
                        gist_patch(inc)
                        print(f"[gist] 增量落盘 {px} (good_pool {len(gnow)})", flush=True)
                except Exception as e:
                    print(f"[gist] 增量落盘失败(不影响主流程): {str(e)[:90]}", flush=True)
        else:
            print(f"[try] ❌ {px}", flush=True)
        if passed_new >= 4:   # 每轮最多收 4 个「新」优质; 复验通过不占名额不触发收工
            print("[stage2] 新优质已满 4 个, 提前收工"); break
    # 09-12 fix: 提前收工后队尾根本没测过, 旧代码把未测的也写进 dead(新)/降级(复验),
    # 等于收工即团灭队尾。现在只判「实测过」的; 未测的原地保留下轮再测。
    tested = cands[:tested_n]
    untested = cands[tested_n:]
    if untested:
        print(f"[stage2] 提前收工, {len(untested)} 个未测不判死(下轮再测): " + ", ".join(untested[:5]))
    dead = [l.strip() for l in gist_file("dead_pool.txt").splitlines() if l.strip()]
    reserve_set = set(l.strip() for l in gist_file("reserve_pool.txt").splitlines() if l.strip())
    # 09-10 明星机制(hall_of_fame 永赦)已按用户指示删除。
    # 替代语义: 已 good 的 IP 复验失败 → 降级 reserve(瞬断可复活), 不进 dead;
    # 新候选失败照旧进 dead。单次失败不再永久判死已验证 IP, 但也不再终身免检。
    new_good = [p for p in passed if p not in good]
    fail_good = [p for p in tested if p not in passed and p in good]
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
    # 复验失败降级不进 dead(新候选失败才进 dead); ping0 机房/广播判死也进 dead(经实测画像)
    new_dead = [p for p in tested
                if p not in passed and p not in dead and p not in set(demoted)
                and p not in reserve_set]   # reserve 成员复验失败仍留 reserve(不判死)
    new_dead += [p for p in dropped_p0 if p not in dead and p not in new_dead]
    if fail_good:
        print("[stage2] 复验失败, 降级 reserve(不拉黑): " + ", ".join(fail_good))
    files = {}
    if new_good or demoted:
        # 09-12 fix: 空 content PATCH「已存在」的文件 = GitHub 直接删除该文件(实测 200),
        # 整个 good_pool 会消失; 而 content 为空 PATCH「不存在」的文件 = 422 整个 patch 全丢。
        # 触发场景: 一轮内所有 good 全超 DEMOTE_HOURS 降级且无新过盾 -> new_good+fresh 为空。
        # 兜底写 "\n" 保留文件(panel._patch_gist 早有同样兜底, 这里原先漏了)。
        files["good_pool.txt"] = {"content": ("\n".join(new_good + fresh)) or "\n"}   # 无上限(09-07 用户要求), 面板翻页展示
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
        left = [p for p in seedq if p not in tested]
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
