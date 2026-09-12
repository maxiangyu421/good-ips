#!/usr/bin/env python3
"""阶段1: 免费socks5抓取 → ip-api 质量预筛(只留住宅/ISP) → SOCKS5握手+连通实测 → ipapi.is精筛 → sifted.txt
不依赖浏览器; 阶段2(sift_browser.py)再用 uc_ts.py 逐个真·试盾 + uc_ping0.py 采集 ping0 出口画像(09-12 迁入)。
排除名单/优质名单都存 Gist(dead_pool.txt / good_pool.txt), 跨 run 累积。"""
import os, sys, json, time, socket, struct, random, re, ssl
import urllib.request as U
from html import unescape
from concurrent.futures import ThreadPoolExecutor

from cfg_open import load as _cfg
_CFG = _cfg()
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_ID = _CFG["GIST_ID"]
BROWSER_N = int(os.environ.get("SIFT_COUNT", "15"))   # 交给阶段2浏览器实测的数量
GIST_FILE = "dead_pool.txt"  # 只读; good_pool.txt 由阶段2写

# ---- 源分级(09-12, 实测驱动): 免费列表的"大"与"活"完全不成正比 ----
# 实测各源真实存活率(每源抽 350 个唯一 IP, 沙盒):
#   monosans 41.1% / hookzof 40.5% / proxifly 38.1% / casals-ar 17.4% / TheSpeedX 15.4%
#   MuRongPIG  0.29%  <-- 71602 个唯一 IP, 占候选池 96%, 却几乎全是僵尸
# 后果: 原来对全池均匀随机抽 6000 个, 96% 的探测预算扔在僵尸列表上 —
#   实测 6004 个只活 154 -> 住宅白名单 2 / 普通 0;
#   而小源全量 8046 个活 409 -> 住宅白名单 78 / 普通 53 (可用候选 2 -> 131)。
# 现策略: 小源全量优先(排前面, 保证一定被 6000 预算吃到), 大源只做尾部填充。
SMALL_SOURCES = [
    # 小源: 体量小(~4k)但存活率高, 全量进采样队列
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks5",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=3000&country=all",
    "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/refs/heads/main/socks5_proxies.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt",
    "https://proxyspace.pro/socks5.txt",
]
BIG_SOURCES = [
    # 大源: 十万级僵尸列表, 只做尾部填充(候选不足时才轮到)
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt",
]
SOURCES = SMALL_SOURCES + BIG_SOURCES   # 兼容: 仍可按全量遍历

def jreq(url, method="GET", data=None, hdrs=None, timeout=20):
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

def _pull(url, raw):
    """拉一个源并解析进 raw。返回本源新增条数。
    09-12: 原代码先用 jreq(假定 JSON)拉一次再 urlopen 拉一次 —— 列表是纯文本,
    第一次注定失败, 11 个源白拉 11 次(含 9 万行大文件)。现在只拉一次。"""
    n0 = len(raw)
    try:
        with U.urlopen(U.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as res:
            txt = res.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"[src] {url.split('/')[2]} 失败: {str(e)[:60]}")
        return 0
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "://" in line: line = line.split("://", 1)[1]
        if "@" in line: line = line.split("@")[-1]      # user:pass@host:port
        line = line.split()[0]                           # 行尾带国家/延迟注释
        if ":" in line and line.replace(".", "").replace(":", "").isdigit():
            raw.add(line)
    return len(raw) - n0

def fetch_lists():
    """返回 (small_raw, big_raw): 小源集合 / 大源集合, 各自已去重。
    分级是为了让连通测预算优先吃高存活率的小源(见 SOURCES 上方说明)。"""
    small, big = set(), set()
    for url in SMALL_SOURCES:
        n = _pull(url, small)
        print(f"[src] {url.split('/')[2]} +{n}")
    for url in BIG_SOURCES:
        n = _pull(url, big)
        print(f"[src] {url.split('/')[2]} +{n}  (大源/尾部填充)")
    big -= small    # 小源已覆盖的从大源里摘掉(实测重叠 35.7%), 避免重复占位
    return small, big

def gist_file(name):
    st, d = jreq(f"https://api.github.com/gists/{GIST_ID}",
                 hdrs={"Authorization": "token " + GIST_TOKEN})
    return ((d.get("files") or {}).get(name, {}).get("content") or "")

# ===== 判据修正(08-30, 关键) =====
# 实测所有历史过盾 IP 的 ip-api 标记:
#   184.181.217.210 Cox Communications  proxy=True  hosting=False  ← 当前置顶
#   72.195.114.169  Cox Communications  proxy=True  hosting=False  ← 三次全成功
#   72.195.101.99   Cox Communications  proxy=True  hosting=False
#   199.66.183.251  NET2ATLANTA.COM     proxy=True  hosting=False
#   82.114.228.35   SCTS(俄)            proxy=False hosting=False
# → 免费 SOCKS5 出口天然被 ip-api 标 proxy=True, 按 proxy 过滤等于砍掉最能过盾的
#   住宅宽带代理。真正必砍的只有 hosting=True(机房/云) + ISP 名是云厂商。
# 优先级: 住宅ISP白名单 > 普通非机房 > 其他
DC_KW = ("alibaba", "aliyun", "tencent", "huawei cloud", "amazon", "google llc",
         "microsoft", "azure", "oracle", "digitalocean", "ovh", "hetzner", "linode",
         "vultr", "contabo", "choopa", "leaseweb", "m247", "zenlayer", "datacamp",
         "colocrossing", "psychz", "performive", "secured servers", "hostinger",
         "cloudflare", "gcore", "datacenter", "data center", "idc", " vps", "hosting",
         # 08-30 实测(ipapi.is is_datacenter=True, ip-api 却报 hosting=False)补入:
         "net2atlanta", "readydedis", "global connectivity solutions", "dedis",
         "server", "colo", "网络科技", "bandwidth", "ipvolume", "stark industries")

# 住宅宽带 ISP(过盾概率最高) —— Cox 是实测最强的一家
RES_KW = ("cox communications", "comcast", "charter", "spectrum", "verizon", "at&t",
          "centurylink", "frontier", "optimum", "cablevision", "rogers", "shaw",
          "telus", "bell canada", "virgin media", "bt ", "sky broadband", "talktalk",
          "orange", "telefonica", "movistar", "vodafone", "deutsche telekom",
          "telecom italia", "kpn", "telia", "telenor", "swisscom", "chinanet",
          "china unicom", "china mobile", "china telecom", "kddi", "ntt", "softbank",
          "korea telecom", "sk broadband", "lg dacom", "bsnl", "jio", "airtel",
          "telkom", "megafon", "mts", "beeline", "rostelecom", "komtel",
          "cincinnati bell", "cox ")
# 注: net2atlanta 曾在白名单, 08-30 实测 ipapi.is is_datacenter=True(机房) 已移除

def _has(s, kws):
    s = (s or "").lower()
    return any(k in s for k in kws)

def ip_quality(ip_list):
    """ip-api batch 质量筛。返回 (prime, normal, unknown) 三档:
      prime  = 住宅宽带 ISP 白名单命中(过盾概率最高, 优先吃浏览器预算)
      normal = 非机房、非白名单
      unknown= ip-api 查不到(只做填充)
    只按 hosting / ISP名 判机房; proxy=True 不再淘汰(见文件头判据说明)。
    限速真相: batch 端点 100 个/请求、15 请求/分钟 —— 旧版 15/批 会把 600 候选拆成
    40 请求直接撞限速, 于是大批 '整批放行' 让机房 IP 混进试盾队列。"""
    hosts = list(dict.fromkeys(p.split(":")[0] for p in ip_list))
    verdict, fails = {}, 0
    for i in range(0, len(hosts), 100):
        chunk = hosts[i:i+100]
        d = None
        for attempt, tmo in enumerate((25, 30, 35)):
            st, r = jreq("http://ip-api.com/batch?fields=query,proxy,hosting,isp,country",
                         "POST", chunk, timeout=tmo)
            if st == 200 and isinstance(r, list):
                d = r; break
            time.sleep(5 * (attempt + 1))
        if d is None:
            fails += 1
            print(f"[api] batch {i//100} 三次均失败, 该批标 unknown(不放行)")
        else:
            for x in d: verdict[x.get("query")] = x
        if i + 100 < len(hosts): time.sleep(4.5)   # 15 请求/分钟 -> 4.5s 安全间隔
    prime, normal, unk, dc, isp_dc = [], [], [], 0, 0
    for p in ip_list:
        info = verdict.get(p.split(":")[0])
        if info is None: unk.append(p); continue
        isp = info.get("isp")
        if info.get("hosting"): dc += 1
        elif _has(isp, DC_KW): isp_dc += 1
        elif _has(isp, RES_KW): prime.append(p)
        else: normal.append(p)
    print(f"[api] 质量筛: 住宅宽带{len(prime)} 普通{len(normal)} 机房{dc} "
          f"ISP名判机房{isp_dc} 未知{len(unk)} (失败批 {fails})")
    for p in prime[:10]:
        v = verdict.get(p.split(":")[0], {})
        print(f"   ★ {p} {v.get('country','')} {(v.get('isp') or '')[:38]}")
    return prime, normal, unk

# ===== 二级精筛: ipapi.is (08-30 用户实测打脸后加) =====
# ip-api 的 hosting 字段漏判严重。用户用 ping0 实测 178.130.47.21 是机房，
# ip-api 却报 hosting=False。换 ipapi.is 复核全池 11 个 IP:
#   178.130.47.21 / 45.95.232.35 / 147.45.60.139 / 193.25.215.182
#   / 199.66.182.243 / 199.66.183.226   → is_datacenter=True  ❌ 机房(ip-api 全漏判)
#   66.42.224.229 / 184.178.172.26 / 98.175.31.195 / 72.223.188.92 / 98.175.31.222 → 住宅 ✅
# 对照组(历史真过盾成功): 184.181.217.210 / 72.195.114.169 / 72.195.101.99 全部住宅 ✅
# → ipapi.is 的 is_datacenter 与「能否过盾」高度吻合, 作为送盾前最后一道闸。
# 免费额度 1000/天 单查(batch 端点要付费 key, 实测 POST 返回 403), ~1.6s/个。
# 每 45min 一轮 × 每轮 ≤25 个 = 800/天, 在配额内。
IPIS_MAX = int(os.environ.get("IPIS_MAX", "30"))   # 每轮精筛上限(护住 1000/天 配额)
# 09-01 ipapi.is 匿名层改版: 检测字段(is_datacenter 等)全部移到 key 层, 匿名层只剩地理信息,
# 且 asn 变字符串(旧代码 .get 崩 = 连续 3 轮 failure 的根因)。免费账号(无需付费)给 key,
# 1000 次/天, 与每轮 30 × 32 轮/天 = 960 配额匹配。key 存加密 config 的 IPAPI_KEY。
IPIS_KEY = (_CFG.get("IPAPI_KEY") or os.environ.get("IPAPI_KEY") or "").strip()

def _ipis_org(verdict):
    """兼容 ipapi.is 新旧两代返回: 旧 asn={org:...}, 新 asn="AS22773 Cox..."(字符串),
    company 也可能是 str 或 dict。永远返回字符串, 不再 .get 崩溃。"""
    asn = verdict.get("asn")
    if isinstance(asn, dict):
        org = asn.get("org") or asn.get("descr") or ""
    elif isinstance(asn, str):
        org = asn
    else:
        org = ""
    if not org:
        comp = verdict.get("company")
        if isinstance(comp, dict): org = comp.get("name") or ""
        elif isinstance(comp, str): org = comp
    return str(org)

def _ipis_url(h):
    u = f"https://api.ipapi.is/?q={h}"
    if IPIS_KEY: u += f"&key={IPIS_KEY}"
    return u

def ipis_check(pxs):
    """对候选逐个查 ipapi.is。返回 (residential, datacenter, unknown) 三组 host:port。
    查不到的进 unknown(保留但排在住宅之后), 不因单点故障丢掉整批。
    09-01 起匿名层没有 is_datacenter —— 没 key 或返回里缺该字段时, 降级为
    「不精筛, 全部归住宅组」并打警告, 宁可多送几个也别把整轮流程搞崩。"""
    if not IPIS_KEY:
        print("[ipis] ⚠️ 未配置 IPAPI_KEY, 跳过二级精筛(全部按 ip-api 结果送盾)")
        return list(pxs), [], []
    res, dc, unk = [], [], []
    seen = {}
    degraded = False
    for i, px in enumerate(pxs[:IPIS_MAX]):
        h = px.split(":")[0]
        if h in seen:                       # 同 IP 只查一次
            (res if seen[h] == 1 else dc if seen[h] == 0 else unk).append(px); continue
        verdict = None
        for attempt in range(2):
            try:
                r = U.Request(_ipis_url(h),
                              headers={"User-Agent": "Mozilla/5.0"})
                with U.urlopen(r, timeout=15) as resp:
                    d = json.loads(resp.read().decode())
                verdict = d; break
            except Exception as e:
                if attempt == 0: time.sleep(3)
                else: print(f"[ipis] {h} 查询失败: {str(e)[:50]}")
        if verdict is None:
            seen[h] = 2; unk.append(px); continue
        if "is_datacenter" not in verdict:
            # 返回的是阉割版(匿名层格式): 无判据可用, 降级放行 + 只警告一次
            if not degraded:
                print("[ipis] ⚠️ 返回缺 is_datacenter(key 失效或被降级?), 本次跳过精筛防误杀")
                degraded = True
            seen[h] = 2; unk.append(px); continue
        is_dc = bool(verdict.get("is_datacenter"))
        org = _ipis_org(verdict)
        if is_dc:
            seen[h] = 0; dc.append(px)
            print(f"[ipis] ❌ 机房 {h} {org[:34]}")
        else:
            seen[h] = 1; res.append(px)
            print(f"[ipis] ✅ 住宅 {h} {org[:34]}")
        time.sleep(1.2)   # 免费额度友好节流
    # 超出 IPIS_MAX 的部分不查, 直接归 unknown 尾部
    unk += [p for p in pxs[IPIS_MAX:]]
    print(f"[ipis] 精筛: 住宅 {len(res)} / 机房剔除 {len(dc)} / 未知 {len(unk)}")
    return res, dc, unk

# ===== 三级半精筛(ping0 真实出口画像)→ 09-12 迁往 stage2 浏览器(uc_ping0.py) =====
# stage1 无头隧道已被 ping0 Turnstile 全量挑战封死(实测直连/代理 100% 挑战页),
# 改由 stage2 真浏览器顺路采集(挑战自动过, 与人工 Firefox 一致), 见 sift_browser.py。

def socks5_ok(target, timeout=7):
    """完整握手 + 连通目标, 返回(是否可用, 延迟ms)。IP-ATYP 直连(实测这批代理拒域名ATYP)。"""
    host, port = target.split(":")[0], int(target.split(":")[1])
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00": s.close(); return False, 0
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("1.1.1.1") + struct.pack(">H", 80))
        r = s.recv(64)
        s.close()
        if len(r) < 2 or r[1] != 0: return False, 0
        return True, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0

if __name__ == "__main__":
    small_raw, big_raw = fetch_lists()
    all_px = small_raw | big_raw
    print(f"[sift] 抓到 {len(all_px)} 个(去重后; 小源 {len(small_raw)} + 大源 {len(big_raw)})")
    dead = set(l.strip() for l in gist_file("dead_pool.txt").splitlines() if l.strip())
    good = set(l.strip() for l in gist_file("good_pool.txt").splitlines() if l.strip())
    # ---- stage1 死 IP 缓存(09-12): dead_pool 只记 stage2 试盾失败, stage1 里连都连不上的
    # 那 5800+ 个 IP 哪都不记 -> 下一轮从 10.6 万里重抽又抽到, 反复空烧握手。
    # 现按 IP 记时间戳, S1_DEAD_TTL 内不再重复握手(默认 24h, 免费代理寿命本就小时级)。
    S1_DEAD_TTL = int(os.environ.get("S1_DEAD_TTL", "86400"))
    S1_DEAD_CAP = int(os.environ.get("S1_DEAD_CAP", "20000"))
    try:
        s1_dead = json.loads(gist_file("s1_dead.json") or "{}")
    except Exception:
        s1_dead = {}
    _t_now = int(time.time())
    s1_dead = {h: t for h, t in s1_dead.items() if _t_now - t < S1_DEAD_TTL}
    s1_fresh = set()   # 本轮新确认连不通的
    # ---- IP 级去重(关键): 免费列表里同一 IP 会挂几十个端口, 同 IP 不同端口的
    # Turnstile/CF 信誉几乎完全一致 -> 逐端口重测纯属白烧浏览器预算。
    # 实测: dead_pool 176 条只有 117 个唯一 IP, 118.145.128.100 一个 IP 烧了 23 个端口。
    # ---- 整段拉黑: 同 IP >=3 个端口在 dead 里就封整段(09-10 明星机制已删) ----
    # 已 good 的 IP 复验失败在 stage2 只降级 reserve 不进 dead, 好网段不会被误封。
    dead_hosts_cnt = {}
    for p in dead:
        h = p.split(":")[0]
        dead_hosts_cnt[h] = dead_hosts_cnt.get(h, 0) + 1
    ban_hosts = {h for h, n in dead_hosts_cnt.items() if n >= 3}
    good_hosts = {p.split(":")[0] for p in good}
    # 09-12 回炉通道: 删明星机制后 good IP 被 good_hosts 整体排除出候选,
    # 永远无法复验 -> meta 时间戳永不刷新 -> 12h 保鲜变全员死刑(good_pool 30→5 断崖根因)。
    # 现把「快到期」的 good IP 插队回炉, 过盾后 stage2 刷新 meta 续命;
    # 名额不挤占新 IP(stage2 名额只数新 IP)。窗口 (9h, 36h): 每轮测不完下轮继续, 不漏。
    REVERIFY_HOURS = int(os.environ.get("REVERIFY_HOURS", "9"))
    try:
        _meta = json.loads(gist_file("good_pool_meta.json") or "{}")
    except Exception:
        _meta = {}
    _now = int(time.time())
    # 09-12 修: 原来窗口上界卡死 (_demote_h + 24) = 36h —— 但 meta 时间戳只在「过盾成功」时
    # 刷新, 一个 IP 一旦掉进 reserve 就再没有事件能刷新它, 于是时间越久越出窗口, 36h 后
    # 永久失去回炉资格。实测 36 个带 meta 的 reserve IP 里 34 个已出窗口(最久 92h),
    # 全是曾经实打实过盾的 IP, 现在既不在 good 也回不来 —— 池子只出不进的第二个漏口。
    # 回炉成本很低(1 次握手 + 1 次试盾), 试失败也只是原地留 reserve, 所以取消上界:
    # 任何 reserve 成员只要 meta 超过 REVERIFY_HOURS 就有资格回炉, 按「最久没复验」优先。
    reverify = [p for p in good
                if _meta.get(p) and _now - _meta.get(p) > REVERIFY_HOURS * 3600]
    # reserve 成员也回炉: 它们都是曾经过盾的 IP, 12h 断崖期被无辜降级的可借此复活(09-12)
    _res = [l.strip() for l in gist_file("reserve_pool.txt").splitlines() if l.strip()]
    _resv = [p for p in _res if p not in reverify and _meta.get(p)
             and _now - _meta.get(p) > REVERIFY_HOURS * 3600]
    # 09-12: 每轮最多回炉 N 个(默认 6), 按「最久未复验」优先 —— 否则 30+ 个 reserve
    # 一次全塞进队列会挤占新 IP 的握手/试盾预算(它们优先级在新 IP 之上)。
    REVERIFY_MAX = int(os.environ.get("REVERIFY_MAX", "6"))
    _resv.sort(key=lambda p: _meta.get(p, 0))
    if len(_resv) > REVERIFY_MAX:
        print(f"[sift] reserve 待回炉 {len(_resv)} 个, 本轮取最久未复验的 {REVERIFY_MAX} 个"
              f"(其余下轮继续, 队列不挤爆)")
        _resv = _resv[:REVERIFY_MAX]
    if _resv:
        print(f"[sift] reserve 复活复验 {len(_resv)} 个: " + ", ".join(_resv))
    reverify += _resv
    if reverify:
        print(f"[sift] 回炉复验 {len(reverify)} 个(超{REVERIFY_HOURS}h未复验): "
              + ", ".join(reverify))
    def _ok(p):
        return (p not in dead and p not in good
                and p.split(":")[0] not in ban_hosts
                and p.split(":")[0] not in good_hosts
                and p.split(":")[0] not in s1_dead)
    cand_small = [p for p in small_raw if _ok(p)]
    cand_big = [p for p in big_raw if _ok(p)]
    cand = cand_small + cand_big
    print(f"[sift] stage1 死IP缓存命中 {len(all_px) - len(cand_small) - len(cand_big)} 条"
          f"(TTL {S1_DEAD_TTL//3600}h, 现存 {len(s1_dead)} 条)")
    # ---- 种子队列: 外部投喂的代理(如别人分享的住宅列表)插队优先试盾 ----
    # 用法: 往 Gist 的 seed_queue.txt 写 host:port 每行一个; 试过一轮即清空。
    seed = [l.strip() for l in gist_file("seed_queue.txt").splitlines() if l.strip()]
    seed = [p for p in seed if p not in good]
    if seed:
        print(f"[sift] 种子队列 {len(seed)} 个插队(外部投喂)")
    cand = seed + reverify + cand    # 种子 > 回炉复验 > 常规候选
    print(f"[sift] 排除 dead {len(dead & all_px)} / 已good {len(good & all_px)} / "
          f"IP级拉黑 {len(ban_hosts)} 段 + 已好 {len(good_hosts)} 段, 候选 {len(cand)}")
    if len(cand) < 40 and not (seed or reverify):   # 源没新货可跳过, 但有回炉复验/种子时必须照跑(09-12), 否则复验通道失效
        print(f"[sift] 新候选不足 40, 本轮跳过(不烧 ip-api 配额/浏览器预算)")
        open("sifted.txt", "w").close()
        sys.exit(0)
    # 优先队列(种子+回炉复验)不参与 shuffle/截断, 保证一定被测到
    prio = seed + reverify
    # ---- 采样顺序(09-12 关键改造) ----
    # 小源(高存活率)全量排前, 大源(僵尸列表)shuffle 后只做尾部填充:
    # 同样 6000 次握手预算, 可用候选从 ~2 个变成 ~130 个。
    prio_set0 = set(prio)
    small_rest = [p for p in cand_small if p not in prio_set0]
    big_rest = [p for p in cand_big if p not in prio_set0]
    random.shuffle(small_rest)
    random.shuffle(big_rest)
    # ---- 测前按 IP 去重(09-12): 原代码先测 6000 个再按 host 去重(白烧 ~30% 预算);
    # 列表里同 IP 多端口极普遍(全池 106362 行/74084 IP = 143%, 小源 208%)。
    def _dedup_ip(lst):
        seen, out = set(), []
        for p in lst:
            h = p.split(":")[0]
            if h in seen: continue
            seen.add(h); out.append(p)
        return out
    queue = _dedup_ip(prio) + _dedup_ip(small_rest) + _dedup_ip(big_rest)
    print(f"[sift] 测前队列: 优先 {len(prio)} + 小源 {len(small_rest)} + 大源 {len(big_rest)}"
          f" (已按 IP 去重)")
    # 流程反转(08-30): ip-api 是最贵一环(15 请求/分钟)。
    # 先用免费无限的 SOCKS5 握手把候选压到几十个活的, 再花 ip-api 配额查质量。
    cand = queue[:6000]
    t0 = time.time()
    with ThreadPoolExecutor(128) as ex:
        results = dict(zip(cand, ex.map(socks5_ok, cand)))
    alive_raw = sorted([(ms, p) for p, (ok, ms) in results.items() if ok])
    n_small_alive = sum(1 for _, p in alive_raw if p in set(cand_small))
    print(f"[sift] 连通测 {len(cand)} 个 -> 活 {len(alive_raw)}"
          f"(其中小源 {n_small_alive}), 耗时 {int(time.time()-t0)}s")
    # 记录本轮连不通的(供 s1_dead 缓存, 大源样本为主, 省下轮预算)
    for p in cand:
        if not results[p][0]:
            s1_fresh.add(p.split(":")[0])
    # 同 IP 只留最快端口, 再送去查质量(省配额)
    seen0, dedup = set(), []
    for ms, p in alive_raw:
        h = p.split(":")[0]
        if h not in seen0: dedup.append(p); seen0.add(h)
    print(f"[sift] IP 去重后 {len(dedup)} 个进质量筛")
    prime, normal, unk0 = ip_quality(dedup[:900])
    def _by_ms(lst): return [p for ms, p in sorted((results[p][1], p) for p in lst)]
    # 一级(ip-api)排序后, 交二级(ipapi.is)精筛剔机房 —— ip-api 的 hosting 漏判太多
    stage1 = _by_ms(prime) + _by_ms(normal) + _by_ms(unk0)
    ipis_res, ipis_dc, ipis_unk = ipis_check(stage1)
    prime_set, prio_set = set(prime), set(prio)
    # 排序: 优先队列(种子) > ISP白名单住宅 > 其他住宅 > 未知
    res_p = [p for p in ipis_res if p in prio_set]
    res_a = [p for p in ipis_res if p not in prio_set and p in prime_set]
    res_b = [p for p in ipis_res if p not in prio_set and p not in prime_set]
    pre_pick = res_p + res_a + res_b + ipis_unk      # 机房(ipis_dc)已彻底出局
    # 三级半(ping0 画像)已迁往 stage2 浏览器采集(uc_ping0.py), stage1 到此为止
    picked = pre_pick[:BROWSER_N]
    if res_p: print(f"[sift] 优先队列命中 {len(res_p)} 个进送盾队列头部")
    alive = picked
    print(f"[sift] 送盾队列 {len(picked)} 个(画像采集/风控排序在 stage2 浏览器内做)")
    if ipis_dc:
        print("[sift] 已剔除机房: " + ", ".join(ipis_dc[:10]))
    # ---- stage1 死 IP 缓存写回(09-12): 只进 dead 的不写(good 里出现过的不误伤) ----
    newly = [h for h in s1_fresh if h not in good_hosts]
    for h in newly:
        s1_dead[h] = _t_now
    if len(s1_dead) > S1_DEAD_CAP:
        s1_dead = dict(sorted(s1_dead.items(), key=lambda kv: -kv[1])[:S1_DEAD_CAP])
    if newly:
        try:
            st, _ = jreq(f"https://api.github.com/gists/{GIST_ID}", "PATCH",
                         {"files": {"s1_dead.json": {
                             "content": json.dumps(s1_dead)}}},
                         {"Authorization": "token " + GIST_TOKEN})
            print(f"[sift] s1_dead.json 写回 +{len(newly)} (共 {len(s1_dead)}, status {st})")
        except Exception as e:
            print(f"[sift] s1_dead 写回失败(不影响主流程): {str(e)[:80]}")
    with open("sifted.txt", "w") as f:
        f.write("\n".join(picked))
    for p in alive[:20]:
        print("  " + p)
    print(f"[sift] sifted.txt {len(picked)} 个, 交给阶段2真·试盾")
