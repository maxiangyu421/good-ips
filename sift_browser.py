#!/usr/bin/env python3
"""阶段2: 对 sifted.txt 逐个用 uc_ts.py(SINGLE_PROXY) 真·试 Turnstile。
出 token → 写 Gist good_pool.txt(置顶池, 注册流程自动优先用); 失败 → 累积 dead_pool.txt。
每个代理一个 xvfb-run 子进程, 干净隔离; 单个预算 110s。
09-26 改造: 试盾前的出口画像源 ping0 → ipapi.is(api.ipapi.is, POST 批量 ≤100/次)。
  ping0 自 09-25 13:48Z 起对 GitHub runner 出口全量弹阿里盾+Turnstile(100% 失败),
  已不可用; ipapi.is 真 API 无盾、免费 1000/天、key 层输出 is_datacenter 等全量字段。
  - is_datacenter=True(机房/IDC) → 跳过试盾直接判死(恢复 dead_pool 唯一合法来源)
  - 其余按「折分升序 + 代理红标降权」排序送盾(干净排前)
  - 复验 IP 不做判死(不加新 kill 路径, 防重演 09-12 断崖); 画像仅参考"""
import os, sys, subprocess, json, time, signal
import urllib.request as U

from cfg_open import load as _cfg
_CFG = _cfg()
GIST_TOKEN = os.environ["GIST_TOKEN"]; GIST_ID = _CFG["GIST_ID"]
BUDGET = int(os.environ.get("SIFT_COUNT", "10"))
P0_PROFILE_MAX = int(os.environ.get("P0_PROFILE_MAX", "15"))   # 画像采集上限(护 job 预算)
# 09-26: P0_TIMEOUT(ping0 单 IP 浏览器预算)已随 ping0 一并废弃 —— ipapi.is 是纯 HTTP
# 批量查询(≤100/次, 实测 0.13s), 无浏览器、无单 IP 超时预算问题。
# 09-12 晚: 110 -> 150 -> 300s。三轮实测(run 34700064422)候选页面打开就花 134s,
# 150s 预算一到位就被杀, 根本没机会点击解验证码。候选本来就 0~1 个/轮,
# 300s 不会拖爆 job(有 JOB_BUDGET 护栏兜底), 却让慢住宅代理真能跑完 Turnstile。
TRY_TIMEOUT = int(os.environ.get("TRY_TIMEOUT", "300"))
# job 级时间护栏: 超过这个已用秒数就不再开新候选, 保证已过盾的结果能写回 Gist
JOB_BUDGET = int(os.environ.get("STAGE2_BUDGET", str(70 * 60)))
P0_DROP_KW = ("机房", "IDC", "数据中心", "广播")               # 唯一硬淘汰信号
# xvfb 默认屏幕只有 640x480x24 —— Chrome 窗口被压在 640x480 里, Turnstile 控件
# 靠视口下沿时可能被裁掉, 截图诊断也看不清。显式给大屏(09-12 交互式挑战迭代)。
XVFB_ARGS = os.environ.get("XVFB_ARGS", "-screen 0 1280x1024x24")
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

# ===== 出口画像(09-26: ping0 浏览器 → ipapi.is 纯 HTTP API) =====
# 配置里键名是小写 `ipapi_key`(与 ip_sift.py 同源), 两种大小写都认 + env 兜底。
IPIS_KEY = (_CFG.get("IPAPI_KEY") or _CFG.get("ipapi_key")
            or os.environ.get("IPAPI_KEY") or os.environ.get("ipapi_key") or "").strip()
IPIS_CACHE = {}       # host -> ipapi.is verdict(批量预取, 循环内零延迟)

def _ipis_org(v):
    """兼容 asn 是 dict(旧) 或 str(新) 两代返回, 永远给字符串。"""
    asn = v.get("asn")
    if isinstance(asn, dict): org = asn.get("org") or asn.get("descr") or ""
    elif isinstance(asn, str): org = asn
    else: org = ""
    if not org:
        comp = v.get("company")
        if isinstance(comp, dict): org = comp.get("name") or ""
        elif isinstance(comp, str): org = comp
    return str(org)

def _ipis_num(s):
    """abuser_score 形如 "0.0003 (Very Low)" → 0.0003。取不到返回 None。"""
    try: return float(str(s).split()[0])
    except Exception: return None

def _ipis_batch(hosts):
    """POST https://api.ipapi.is 批量(≤100/次), 返回 {host: verdict}。异常抛给调用方。"""
    out = {}
    for i in range(0, len(hosts), 100):
        chunk = hosts[i:i + 100]
        body = json.dumps({"ips": chunk, "key": IPIS_KEY}).encode()
        req = U.Request("https://api.ipapi.is", data=body,
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "Mozilla/5.0"})
        with U.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode())
        if not isinstance(d, dict):
            raise RuntimeError(f"batch 返回非 dict: {type(d)}")
        got = 0
        for k, v in d.items():
            if isinstance(v, dict) and "is_datacenter" in v:
                out[v.get("ip") or k] = v; got += 1
        if got == 0:
            raise RuntimeError("batch 无有效结果: " + json.dumps(d)[:100])
        time.sleep(0.3)
    return out

def _ipis_single(h):
    """逐条 GET(批量失败 / 个别缺项的兜底)。返回 verdict 或 None。"""
    u = f"https://api.ipapi.is/?q={h}"
    if IPIS_KEY: u += f"&key={IPIS_KEY}"
    for attempt in range(2):
        try:
            with U.urlopen(U.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == 0: time.sleep(3)
            else: print(f"[ipis] {h} 单查失败: {str(e)[:50]}")
    return None

def ipis_prefetch(pxs):
    """批量预取候选画像(一次 HTTP 顶掉 N 次), 结果进 IPIS_CACHE。失败静默降级。"""
    global IPIS_CACHE
    IPIS_CACHE = {}
    if not IPIS_KEY:
        print("[ipis] ⚠️ 未配置 ipapi key, 画像采集跳过(全部按无画像排序600)", flush=True)
        return
    hosts = list(dict.fromkeys(p.split(":")[0] for p in pxs if p))
    if not hosts: return
    try:
        IPIS_CACHE = _ipis_batch(hosts)
        print(f"[ipis] 批量画像查回 {len(IPIS_CACHE)}/{len(hosts)} 个", flush=True)
    except Exception as e:
        print(f"[ipis] ⚠️ 批量画像不可用({str(e)[:70]}), 转逐条兜底", flush=True)
        IPIS_CACHE = {}

def _ipis_egress(v):
    es = v.get("egress_service")
    if not es: return ""
    if isinstance(es, str): return es
    if isinstance(es, dict):
        for k in ("service", "name", "provider", "type", "descr"):
            if es.get(k): return str(es[k])
        flags = [k for k, val in es.items() if val is True]
        return "/".join(flags)
    return ""

def _ipis_fold(v):
    """ipapi.is verdict → 统一画像 dict(与旧 ping0 画像消费方兼容)。
    labels 供 p0_is_idc 判机房; risk = 折算分(0~98, 值域与旧风控%近似, 面板阈值可直接复用)。
    折算规则(公司/ASN 级 abuser_score 分档, 无逐 IP 风控% 的等价物):
      <0.001→5  <0.005→10  <0.02→20  <0.05→35  <0.1→50  else→70; 取不到→40
      is_vpn +30 / is_abuser +20 / is_tor +25 / is_mobile +15 / is_crawler +10
      is_datacenter → 99(最高, 反正会被 p0_is_idc 直接判死)"""
    labels = []
    dc = str((v.get("datacenter") or {}).get("datacenter") or "") if isinstance(v.get("datacenter"), dict) else ""
    if v.get("is_datacenter"):
        labels.append("IDC机房 IP" + (f"({dc})" if dc else ""))
    if v.get("is_proxy"): labels.append("代理 IP")
    if v.get("is_vpn"): labels.append("VPN")
    if v.get("is_tor"): labels.append("Tor 出口")
    if v.get("is_abuser"): labels.append("滥用记录")
    if v.get("is_mobile"): labels.append("移动网络")
    if v.get("is_crawler"): labels.append("爬虫")
    eg = _ipis_egress(v)
    if eg: labels.append("出口:" + eg)
    if not labels: labels.append("家庭宽带" if not v.get("is_datacenter") else "机房")

    if v.get("is_datacenter"):
        risk = 99
    else:
        ab = None
        for src in (v.get("company"), v.get("asn")):
            if isinstance(src, dict):
                ab = _ipis_num(src.get("abuser_score"))
                if ab is not None: break
        if ab is None: risk = 40
        elif ab < 0.001: risk = 5
        elif ab < 0.005: risk = 10
        elif ab < 0.02: risk = 20
        elif ab < 0.05: risk = 35
        elif ab < 0.1: risk = 50
        else: risk = 70
        if v.get("is_vpn"): risk += 30
        if v.get("is_abuser"): risk += 20
        if v.get("is_tor"): risk += 25
        if v.get("is_mobile"): risk += 15
        if v.get("is_crawler"): risk += 10
        risk = min(risk, 98)
    return {"risk": risk, "labels": labels, "iptype": dc,
            "datacenter": dc, "org": _ipis_org(v),
            "native": "" if (v.get("is_datacenter") or v.get("is_proxy")
                             or v.get("is_vpn") or v.get("is_tor")) else "原生 IP",
            "ip": v.get("ip") or ""}

def ipis_profile(px):
    """取单个候选的 ipapi.is 画像(先查批量缓存, 缺项单查兜底)。失败返回 {} 或 {'error'}。绝不抛。"""
    h = px.split(":")[0]
    v = IPIS_CACHE.get(h)
    if v is None:
        v = _ipis_single(h)
    if not isinstance(v, dict) or "is_datacenter" not in v:
        return {"error": "no_ipis", "ip": h}
    try:
        return _ipis_fold(v)
    except Exception as e:
        return {"error": str(e)[:60], "ip": h}

def p0_is_idc(prof):
    """画像里带机房/广播类标签 → 硬淘汰(与过盾强负相关)。"""
    if not prof: return False
    text = " ".join(prof.get("labels") or []) + " " + str(prof.get("iptype") or "")
    return any(k.lower() in text.lower() for k in P0_DROP_KW)

def p0_sortkey(prof):
    """排序键: 折算分升序, 代理红标 +50 降权, 无画像 600 排尾。
    折分不淘汰(池内过盾 IP 折分普遍偏高, 相关性弱, 用户定调「免费的别要求太高」)。"""
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
    if not run_isolated(["xvfb-run", "-a", "-s", XVFB_ARGS, sys.executable, "uc_ts.py"],
                        env, TRY_TIMEOUT, tag="try " + px):
        print(f"[try] {px} 超时({TRY_TIMEOUT}s), 进程组已清理")
    tok = ""
    if os.path.exists("ts_token.txt"):
        tok = open("ts_token.txt").read().strip()
    if not tok:
        # 失败截图留档: uc_ts v2 每轮失败都截一张(uc_debug_rN.png), 最终失败再截
        # uc_debug.png —— 但下一个候选会覆盖它们, 按序号改名保留, workflow 收尾
        # 统一上传成 artifact 供人工看「卡在哪一步」。
        import glob
        for f in sorted(glob.glob("uc_debug*.png")):
            suf = f[len("uc_debug"):].lstrip("_") or "fail"
            try: os.rename(f, f"uc_debug_{idx:02d}_{suf}")
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
    # ---- ipapi.is 画像采集(09-26): 只对「新 IP」判死+排序, 复验 IP 不加新 kill 路径 ----
    dropped_p0 = []
    keyed = []
    risk_new = {}   # 09-25: 风险率持久化(px -> risk%), 随过盾写进 good_pool_risk.json 给面板展示
    ipis_prefetch(new_first[:P0_PROFILE_MAX])   # 09-26: 一次批量查完所有候选画像(纯 HTTP, 无盾)
    for px in new_first[:P0_PROFILE_MAX]:
        prof = ipis_profile(px)
        if isinstance(prof.get("risk"), int):
            risk_new[px] = prof["risk"]
        if p0_is_idc(prof):
            lab = " ".join(prof.get("labels") or []) or str(prof.get("iptype") or "?")
            dropped_p0.append(px)
            print(f"[ipis] ❌ {px} {lab} -> 跳过试盾直接判死", flush=True)
            continue
        sk = p0_sortkey(prof)
        red = any("代理" in lb for lb in (prof.get("labels") or []))
        tag = f"折分={prof.get('risk')}" if isinstance(prof.get("risk"), int) else "无画像"
        extra = " 代理红标" if red else ""
        native = prof.get("native") or ""
        if native: extra += f" {native}"
        org = (prof.get("org") or "")[:30]
        if org: extra += f" {org}"
        print(f"[ipis] {'⚠️' if red else '▫️'} {px} {tag}{extra} 排序{sk}", flush=True)
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
        print(f"[ipis] ipapi.is 机房/IDC 判死 {len(dropped_p0)} 个: " + ", ".join(dropped_p0), flush=True)
    if reverify:
        print(f"[stage2] 复验 {len(reverify)} 个殿后, 新 IP {len(new_first)} 个优先(已按折分排序)", flush=True)
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
                        # 09-25: 风险率随增量落盘(job 中途挂掉也不丢)
                        if px in risk_new:
                            try:
                                _rr = json.loads(gist_file("good_pool_risk.json") or "{}")
                            except Exception:
                                _rr = {}
                            _rr[px] = risk_new[px]
                            inc["good_pool_risk.json"] = {"content": json.dumps(_rr)}
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
def _vote_alive(cands, rounds=3, timeout=6, workers=24):
    """三轮 TCP 连通投票（09-25 新增）。

    动机: 旧逻辑「超 DEMOTE_HOURS 未复验 -> 直接降级」是凭时间戳猜死活。
    实测 09-25: 被降级的 15 个里 13 个三轮都能连通 —— 时间戳不能 predict 可用性。
    成本: 22 个并发 x 3 轮 = 18s; 对比真实试盾 35s/个 = 13 分钟, 便宜 40+ 倍。
    判据: >= 2/3 轮连通记为存活（单轮噪声大, 实测同批两次跑能差 3 个）。
    """
    import socket as _sk
    import concurrent.futures as _cf

    def _one(p):
        try:
            h, pt = p.rsplit(":", 1)
            c = _sk.create_connection((h, int(pt)), timeout=timeout)
            c.close()
            return p, True
        except Exception:
            return p, False

    score = {}
    for _ in range(max(1, rounds)):
        try:
            with _cf.ThreadPoolExecutor(max_workers=min(workers, max(1, len(cands)))) as ex:
                for p, ok in ex.map(_one, list(cands)):
                    if ok:
                        score[p] = score.get(p, 0) + 1
        except Exception as e:
            print("[vote] round err %s" % e)
    need = max(1, (rounds + 1) // 2)
    alive = [p for p in cands if score.get(p, 0) >= need]
    dead = [p for p in cands if score.get(p, 0) < need]
    return alive, dead


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
    stale = []
    for p in good:
        ts = meta.get(p)
        if ts and now_ts - ts > DEMOTE_HOURS * 3600:
            stale.append(p)
        else:
            if not ts:
                meta[p] = now_ts   # 旧条目没时间戳, 从现在起算宽限期
            fresh.append(p)
    # ===== 09-25 用户决策: 超期不再凭时间戳直接降级, 先做三轮连通投票实测 =====
    #   活 -> 留在 good_pool 并刷新 last_ok（不再被反复降级）
    #   死 -> 降 reserve（仍不进 dead, 家宽不拉黑）
    revived = []
    if stale:
        _alive, _dead = _vote_alive(stale)
        revived, demoted = _alive, _dead
        for p in revived:
            meta[p] = now_ts       # 刷新, 下轮不会立刻再超期
        print("[stage2] 超%d h %d 个 -> 三轮投票: 复活 %d / 降级 %d"
              % (DEMOTE_HOURS, len(stale), len(revived), len(demoted)))
        if revived:
            print("[stage2]   复活: " + ", ".join(revived[:8]))
        if demoted:
            print("[stage2]   降级: " + ", ".join(demoted[:8]))
    # 复验失败的好 IP 一并降级(合并去重), 从 good/meta 里摘掉
    demoted = list(dict.fromkeys(demoted + fail_good))
    fresh = [p for p in fresh if p not in set(demoted)]
    for p in fail_good:
        meta.pop(p, None)
    # ===== 09-25 用户决策: dead_pool 语义收窄 =====
    # 旧语义: 任何「试盾没过」的候选都进 dead → 家宽瞬断被永久拉黑, 且 good_pool 里
    #         40 个 IP 与 dead 重叠, 好 IP 被自己的黑名单锁死(实测 09-25)。
    # 新语义: dead_pool 只收「画像判为机房/广播」的 IP(= dropped_p0, 唯一合法来源)。
    #         家宽无论死活都不进 dead, 失败一律 → reserve, 保留复活通道。
    new_dead = [p for p in dropped_p0 if p not in dead]
    # 家宽新候选失败 → reserve(不再判死); 排除已是 good/reserve/dead 的
    new_fail_to_reserve = [p for p in tested
                           if p not in passed and p not in dead
                           and p not in set(demoted) and p not in reserve_set
                           and p not in new_dead]
    if new_fail_to_reserve:
        print(f"[stage2] 家宽失败 {len(new_fail_to_reserve)} 个 → reserve(不拉黑): "
              + ", ".join(new_fail_to_reserve[:6]))
    if fail_good:
        print("[stage2] 复验失败, 降级 reserve(不拉黑): " + ", ".join(fail_good))
    files = {}
    if new_good or demoted or restore or revived:
        # 09-12 fix: 空 content PATCH「已存在」的文件 = GitHub 直接删除该文件(实测 200),
        # 整个 good_pool 会消失; 而 content 为空 PATCH「不存在」的文件 = 422 整个 patch 全丢。
        # 触发场景: 一轮内所有 good 全超 DEMOTE_HOURS 降级且无新过盾 -> new_good+fresh 为空。
        # 兜底写 "\n" 保留文件(panel._patch_gist 早有同样兜底, 这里原先漏了)。
        for p in restore:            # 09-12: 复活的 IP 也要刷新 meta 计时, 否则立刻又被降级
            meta[p] = now_ts
        for p in revived:            # 09-25: 投票复活的同理(上文已刷新, 这里幂等保障)
            meta[p] = now_ts
        # 09-12 卫生: good_pool 全量去重(输入 good 可能已含历史重复, 复验/增量写攒出来的),
        # 重复条目会稀释注册机置顶权重, 也没必要。去重保序。
        # 09-25 卫生: good_pool 全量去重(输入 good 可能已含历史重复, 复验/增量写攒出来的),
        # 重复条目会稀释注册机置顶权重, 也没必要。去重保序。
        gfinal = list(dict.fromkeys(new_good + restore + fresh + revived))   # 09-25: revived 也要写回(否则投票复活的 IP 会凭空丢失)
        files["good_pool.txt"] = {"content": ("\n".join(gfinal)) or "\n"}   # 无上限(09-07 用户要求), 面板翻页展示
        # 09-25/26: 风险率持久化 —— ipapi.is 折算分(0~98)落进 good_pool_risk.json(面板每个 IP 展示)
        # 只对「新 IP」采画像, 所以老 IP 无值显示「—」; 池外成员一律剪掉防膨胀。
        try:
            _rr = json.loads(gist_file("good_pool_risk.json") or "{}")
        except Exception:
            _rr = {}
        for p in new_good:
            if p in risk_new:
                _rr[p] = risk_new[p]
        files["good_pool_risk.json"] = {"content": json.dumps({p: _rr[p] for p in gfinal if p in _rr})}
        reserve = [l.strip() for l in gist_file("reserve_pool.txt").splitlines() if l.strip()]
        # 09-12 卫生: 已经回到 good 的 IP 不再留在 reserve(清掉跨池重复)
        reserve = [p for p in reserve if p not in set(new_good + fresh + restore + revived)]
        new_reserve = [p for p in demoted if p not in reserve]
        reserve_all = (new_reserve + new_fail_to_reserve + reserve)[:500]
        # 09-08 fix: Gist PATCH 里新建空文件(content="")会 422 且整个 patch 被静默丢弃,
        # 此前导致所有"通过 N 个"的新 IP 从未入库。空 reserve 就不写这个文件。
        if reserve_all:
            files["reserve_pool.txt"] = {"content": "\n".join(reserve_all)}
        files["good_pool_meta.json"] = {"content": json.dumps(meta)}
    # 09-25: 家宽失败入 reserve 的场景可能不经过 good 段(本轮无 new_good/demoted/restore),
    # 需要独立补写, 否则这轮的家宽失败 IP 会凭空消失。
    if new_fail_to_reserve and "reserve_pool.txt" not in files:
        reserve = [l.strip() for l in gist_file("reserve_pool.txt").splitlines() if l.strip()]
        reserve_all = list(dict.fromkeys(new_fail_to_reserve + reserve))[:500]
        if reserve_all:
            files["reserve_pool.txt"] = {"content": "\n".join(reserve_all)}
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
