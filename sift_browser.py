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
# 09-12: 80 -> 110s。慢速住宅代理 ping0 冷启动, 80s 常采不完(画像空->排序600)。
P0_TIMEOUT = int(os.environ.get("P0_TIMEOUT", "110"))          # 单个画像采集预算(秒)
# 09-12 晚: 110 -> 150 -> 300s。三轮实测(run 34700064422)候选页面打开就花 134s,
# 150s 预算一到位就被杀, 根本没机会点击解验证码。候选本来就 0~1 个/轮,
# 300s 不会拖爆 job(有 JOB_BUDGET 护栏兜底), 却让慢住宅代理真能跑完 Turnstile。
TRY_TIMEOUT = int(os.environ.get("TRY_TIMEOUT", "300"))
# job 级时间护栏: 超过这个已用秒数就不再开新候选, 保证已过盾的结果能写回 Gist
JOB_BUDGET = int(os.environ.get("STAGE2_BUDGET", str(70 * 60)))
P0_DROP_KW = ("机房", "IDC", "数据中心", "广播")               # 唯一硬淘汰信号
# 09-12 灰度实测(run 34689810547, 同一代理对照): pageLoadStrategy=normal 时
# uc_open_with_reconnect() 卡死到被杀, eager 6.2s 打开页面并在 37s 内拿到 token。
# 这就是「候选全灭」的真凶 —— 卡在打开页面, 不是点击 Turnstile。
UC_PLS = os.environ.get("STAGE2_PLS", "eager")

SUB_LOG = "/tmp/sub_proc.log"

def _tail_text(path, keep=8000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - keep))
            return f.read().decode(errors="replace")
    except Exception:
        return ""

def run_isolated(cmd, env, timeout, tag=""):
    """跑子进程, 超时杀整个进程组; 子进程输出落盘, 失败时回显尾部。
    09-12: 原来用 subprocess.run(timeout=N) —— 它只杀直接子进程, 而这里是
    xvfb-run(shell 脚本) -> python -> chromedriver -> chrome。
    超时杀掉 xvfb-run 后, 底下的 chrome 全部变孤儿继续吃内存(每轮最多 40 次调用,
    失败路径几乎都走超时), runner 上攒够就是 OOM/拖慢后续轮次。
    用 start_new_session 让子进程自成进程组, 超时对整组 SIGKILL。
    09-12 二次修复: 子进程 stdout/stderr 原来直接丢 DEVNULL —— uc_ts.py 打的
    「初始 token_len / click 异常 / 代理队列」全被吞掉, Actions 日志里只剩
    「❌/超时」, 排查只能靠猜(池子连续多轮 0 产出就是这个盲区)。现在落盘并回显。"""
    try: os.remove(SUB_LOG)
    except Exception: pass
    with open(SUB_LOG, "wb") as fo:
        p = subprocess.Popen(cmd, env=env, start_new_session=True,
                             stdout=fo, stderr=subprocess.STDOUT)
        try:
            p.wait(timeout=timeout)
            ok = True
        except subprocess.TimeoutExpired:
            ok = False
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try: p.kill()
                except Exception: pass
            try: p.wait(timeout=10)
            except Exception: pass
    pre = "[sub%s] " % ((" " + tag) if tag else "")
    tail = [l.rstrip() for l in _tail_text(SUB_LOG).splitlines() if l.strip()][-10:]
    for line in tail:
        print(pre + line[:220], flush=True)
    if not tail:
        print(pre + "(子进程无任何输出)", flush=True)
    return ok

def print_env_versions():
    """跑之前先把 runner 的 chrome / seleniumbase 版本打进日志 ——
    ubuntu-latest 的 Chrome 是滚动更新的, chromedriver 版本一旦对不上,
    所有候选都会瞬间失败, 而日志里只会看到「❌」看不出原因。"""
    import importlib.metadata as md
    cmds = [["google-chrome", "--version"], ["chromedriver", "--version"]]
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=90)
            print("[env] %s -> %s" % (c[0], (r.stdout or r.stderr).strip()[:120]), flush=True)
        except Exception as e:
            print("[env] %s 查询失败: %s" % (c[0], str(e)[:80]), flush=True)
    for pkg in ("seleniumbase", "selenium"):
        try:
            print("[env] %s==%s" % (pkg, md.version(pkg)), flush=True)
        except Exception as e:
            print("[env] %s 版本未知: %s" % (pkg, str(e)[:60]), flush=True)

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
    env = dict(os.environ, SINGLE_PROXY=px, UC_PLS=UC_PLS)
    if not run_isolated(["xvfb-run", "-a", sys.executable, "uc_ping0.py"],
                        env, P0_TIMEOUT, tag="p0 " + px):
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

def test_one(px, idx=0):
    for f in ("ts_token.txt", "ts_proxy.txt", "uc_debug.png"):
        if os.path.exists(f): os.remove(f)
    env = dict(os.environ, SINGLE_PROXY=px, UC_PLS=UC_PLS)
    if not run_isolated(["xvfb-run", "-a", sys.executable, "uc_ts.py"],
                        env, TRY_TIMEOUT, tag="try " + px):
        print(f"[try] {px} 超时({TRY_TIMEOUT}s), 进程组已清理")
    tok = ""
    if os.path.exists("ts_token.txt"):
        tok = open("ts_token.txt").read().strip()
    if not tok and os.path.exists("uc_debug.png"):
        # 失败截图留档: uc_ts 失败时会截一张, 但下一个候选会覆盖它 ->
        # 按序号改名, workflow 收尾统一上传成 artifact 供人工看「卡在哪一步」。
        try: os.rename("uc_debug.png", f"uc_debug_{idx:02d}.png")
        except Exception: pass
    return tok

if __name__ == "__main__":
    print_env_versions()
    cands = [l.strip() for l in open("sifted.txt") if l.strip()][:BUDGET]
    good = list(dict.fromkeys(l.strip() for l in gist_file("good_pool.txt").splitlines() if l.strip()))
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
    # ===== 09-12 三修: 画像采完必须重新分离「新 IP / 复验 IP」(原队列塌方根因) =====
    # 现象: good_pool 反复卡在 4~5。r507→r511 连续 5 轮 0 产出, 好不容易 r512 收了
    # 72.195.101.99、r513 收了 66.42.224.229, 到 r514 池子又只剩 4 个。
    # 根因(日志实锤): 上面这段画像排序把「新 IP + 复验 IP」混在一起重新赋值给 new_first:
    #     new_first = [px for _, px in keyed] + new_first[P0_PROFILE_MAX:]
    # 而 keyed 同时含复验 IP(它们评分低=风控 82~90% 排最前), 于是复验 IP 被塞进 new_first 队首。
    # 后果一(名额错乱): 后面 `if px not in good_set` 用 good_set 静态判定, 复验 IP「通过」
    #   本该不占名额, 现在因为「新 IP 4 个名额」被队首的复验 IP 提前凑满而误触发提前收工:
    #   r505 回炉 3 个复验 IP 跑到新 IP 位置, 1 个通过就凑满 4 个 → 收工 → 3 个候选次也没测。
    # 后果二(判死错乱): 更致命 —— 同一批复验 IP 同时出现在 new_first 和 reverify 两处,
    #   循环里同一个 IP 被测试两次; 第一次失败走 `tested` 判死逻辑, 第二次(尾部那份)却因为
    #   提前收工躺在 untested 里逃过判死, 状态机自相矛盾。
    # 修法: 记录画像得到的新排序, 但「复验」身份由 reverify 集合唯一决定, 两队列物理隔离。
    keyed.sort(key=lambda x: x[0])
    new_sorted = [px for _, px in keyed] + new_first[P0_PROFILE_MAX:]
    new_first = [px for px in new_sorted if px not in good_set]
    reverify = [px for px in new_sorted if px in good_set] + \
               [px for px in reverify if px not in good_set]
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
        tok = test_one(px, i + 1)
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
        if passed_new >= 4 and i + 1 >= len(new_first):
            # 每轮最多收 4 个「新」优质(护住 ipapi 配额节奏); 复验通过不占名额。
            # 09-12: 原来收满 4 个立刻 break —— 队列尾巴(常是几十个复验 IP)整批不测,
            # 躺进 untested「下轮再测」, 而下轮队列又是新的一批, 复验 IP 永远轮不到。
            # 现在只在「新 IP 段已跑完」时收工; 剩下的复验段继续跑掉。
            print("[stage2] 新优质已满 4 个, 新候选段跑完, 继续跑复验段(不空烧下轮)"); break
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
    # ---- 09-12 四修: reserve 复验通过 = 复活回 good(此前只有「从 good 跌到 reserve」单向,
    # 没有「从 reserve 爬回 good」的通道 —— 结果 reserve 越攒越多(48 个), good 却永远补不回来,
    # 而 reserve 成员每轮只挑 2 个回炉(还要 meta 超 9h), 48 个一轮最多捞 2 个, 排队几个月。
    restore = [p for p in passed if p in reserve_set and p not in good]
    if restore:
        print(f"[stage2] reserve 复验通过, 复活回 good_pool: {', '.join(restore)}")
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
    if new_good or demoted or restore:
        # 09-12 fix: 空 content PATCH「已存在」的文件 = GitHub 直接删除该文件(实测 200),
        # 整个 good_pool 会消失; 而 content 为空 PATCH「不存在」的文件 = 422 整个 patch 全丢。
        # 触发场景: 一轮内所有 good 全超 DEMOTE_HOURS 降级且无新过盾 -> new_good+fresh 为空。
        # 兜底写 "\n" 保留文件(panel._patch_gist 早有同样兜底, 这里原先漏了)。
        for p in restore:            # 09-12: 复活的 IP 也要刷新 meta 计时, 否则立刻又被降级
            meta[p] = now_ts
        # 09-12 卫生: good_pool 全量去重(输入 good 可能已含历史重复, 复验/增量写攒出来的),
        # 重复条目会稀释注册机置顶权重, 也没必要。去重保序。
        gfinal = list(dict.fromkeys(new_good + restore + fresh))
        files["good_pool.txt"] = {"content": ("\n".join(gfinal)) or "\n"}   # 无上限(09-07 用户要求), 面板翻页展示
        reserve = [l.strip() for l in gist_file("reserve_pool.txt").splitlines() if l.strip()]
        # 09-12 卫生: 已经回到 good 的 IP 不再留在 reserve(清掉跨池重复)
        reserve = [p for p in reserve if p not in set(new_good + fresh + restore)]
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
