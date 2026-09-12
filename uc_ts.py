"""SeleniumBase UC Mode 过盾 v2(2026-09-12 交互式 Turnstile 迭代)。
目标站与 token 字段名来自解密配置(TARGET_URL / TOKEN_INPUT env), 不写死在公开代码里。
支持 SINGLE_PROXY 单代理模式(供 ip-sift 阶段2逐个实测)。
成功后写 ts_token.txt + ts_proxy.txt(token 5分钟有效, 注册必须走同一代理)。

v1 失败形态(09-12 五轮日志实锤): 页面能打开(6~134s), uc_gui_click_captcha() 报 OK,
但 token 始终 0 —— 点击落了, 挑战没解掉。瓶颈在「点一次死等 8s 就算完」:
  1) 固定 sleep 8s 太短: 慢代理下 CF 「Verifying...」转圈 20s+ 才出结果,
     8s 后再点第二下 = 打断挑战, 直接进 "Verification expired" 死循环;
  2) 只点 2 轮, 废掉的挑战实例从不重开(交互式挑战点坏一次基本就废);
  3) 盲等: 干净代理 2~3s 就自动出 token, 却固定白等 10s。

v2 改法(核心 = 「别打断、多等、废了就换新实例」):
  - 打开页后轮询 token 15s(自动通过型直接摘走, 不点击);
  - 最多 4 轮: 点击/处理 → 轮询 25s(给转圈留足时间, 不打断);
  - 第 3 轮前整页重开(uc_open_with_reconnect)换新挑战实例, 跳出 expired 死态;
  - click 与 handle 交替: click 报 OK 但没出 token 时, 下一轮换 handle(回车流);
  - token 读取多源: 隐藏 input + window.turnstile.getResponse() + 任意 turnstile 输入框;
  - 每轮失败截图 uc_debug_rN.png; iframe 尺寸 / 页面 title 打进日志(诊断「控件没渲染」)。
设置 UC_TS_LEGACY=1 可回跑 v1 逻辑(灰度对照用)。"""
import sys, time, os, random

from cfg_open import load as _cfg
_CFG = _cfg()
PAGE = _CFG.get("TARGET_URL", "")
TOKEN_INPUT = _CFG.get("TOKEN_INPUT", "cf-turnstile-response")
if not PAGE:
    print("[uc] 缺 TARGET_URL(config 未解密或字段缺失)"); sys.exit(2)

POLL_OPEN = int(os.environ.get("UC_POLL_OPEN", "15"))    # 打开页后「自动通过」轮询秒数
POLL_CLICK = int(os.environ.get("UC_POLL_CLICK", "25"))  # 每轮点击后结果轮询秒数
ROUNDS = int(os.environ.get("UC_ROUNDS", "4"))
LEGACY = os.environ.get("UC_TS_LEGACY", "") == "1"       # 灰度对照: 跑回 v1 逻辑

def load_proxies():
    try:
        return [l.strip() for l in open("proxies.txt") if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []

def load_good():
    try:
        return [l.strip() for l in open("good_pool.txt") if l.strip()]
    except FileNotFoundError:
        return []

_READ_JS = (
    'var out="";'
    'try{if(window.turnstile&&window.turnstile.getResponse){var g=window.turnstile.getResponse();if(g)out=g;}}catch(e){}'
    'if(!out){var i=document.querySelector(\'input[name="%s"]\')||document.querySelector(\'input[name*=turnstile]\');'
    'if(i&&i.value)out=i.value;}'
    'return out||"";' % TOKEN_INPUT
)
_IFRAME_JS = (
    'var f=document.querySelector(\'iframe[src*="challenges.cloudflare.com"]\');'
    'if(!f) return "none";'
    'var r=f.getBoundingClientRect();'
    'return Math.round(r.width)+"x"+Math.round(r.height)+" @"+Math.round(r.x)+","+Math.round(r.y);'
)

def read_token(sb):
    try:
        return sb.execute_script(_READ_JS) or ""
    except Exception:
        return ""

def poll_token(sb, secs, tag=""):
    """每 2s 查一次 token; 别盲等, 也别提前打断。"""
    t0 = time.time()
    while time.time() - t0 < secs:
        tok = read_token(sb)
        if tok:
            print("[uc] %s %.1fs 出 token(len=%d)" % (tag, time.time() - t0, len(tok)), flush=True)
            return tok
        time.sleep(2)
    return ""

def diag(sb, tag):
    """把 Turnstile 控件状态打进日志(诊断「点击 OK 但没出 token」时控件到底在不在)。"""
    try:
        info = sb.execute_script(_IFRAME_JS) or "?"
        title = str(sb.get_page_title())[:60]
        print("[uc] %s iframe=%s title=%r" % (tag, info, title), flush=True)
    except Exception as e:
        print("[uc] %s diag 失败: %s" % (tag, str(e)[:80]), flush=True)

def _legacy_flow(sb, px):
    """v1 原逻辑(灰度对照), 保留原样不改。"""
    time.sleep(10)
    tok = read_token(sb)
    print("[uc] 初始 token_len", len(tok), flush=True)
    for attempt in range(1, 3):
        if tok: break
        for fn in ("uc_gui_click_captcha", "uc_gui_handle_captcha"):
            try:
                getattr(sb, fn)()
                print("[uc] attempt %d %s() OK" % (attempt, fn), flush=True)
                break
            except Exception as e:
                print("[uc] attempt %d %s() err: %s" % (attempt, fn, str(e)[:90]), flush=True)
        time.sleep(8)
        tok = read_token(sb)
        print("[uc] attempt %d token_len %d" % (attempt, len(tok)), flush=True)
    return tok

def try_one(px):
    from seleniumbase import SB
    # 09-12 灰度实测(probe run 34689810547): normal 会在慢代理上卡死开不出页面,
    # eager 只等 DOMContentLoaded 就返回 —— 这是此前多轮 0 产出的真凶, 默认 eager。
    pls = os.environ.get("UC_PLS", "").strip() or "eager"
    kw = {"page_load_strategy": pls} if pls in ("normal", "eager", "none") else {}
    with SB(uc=True, locale="en", proxy=px,
            chromium_arg="--ignore-certificate-errors --window-size=1280,900", **kw) as sb:
        print("[uc] page_load_strategy=%s v2=%s" % (pls, not LEGACY), flush=True)
        t0 = time.time()
        sb.uc_open_with_reconnect(PAGE, reconnect_time=6)
        print("[uc] 页面打开返回, 耗时 %.1fs" % (time.time() - t0), flush=True)
        if LEGACY:
            tok = _legacy_flow(sb, px)
        else:
            tok = poll_token(sb, POLL_OPEN, tag="auto")
            diag(sb, "open后")
            reloaded = False
            for r in range(1, ROUNDS + 1):
                if tok: break
                if r == 3 and not reloaded:
                    # 前两轮点过仍无 token: 大概率挑战实例已进 expired/failed 死态,
                    # 继续点没意义 —— 整页重开换个新挑战实例。
                    reloaded = True
                    print("[uc] 两轮未出 token, 重开页面换新挑战实例", flush=True)
                    try:
                        sb.uc_open_with_reconnect(PAGE, reconnect_time=5)
                        print("[uc] 重开返回, 耗时 %.1fs" % (time.time() - t0), flush=True)
                        tok = poll_token(sb, POLL_OPEN, tag="auto2")
                        diag(sb, "重开后")
                    except Exception as e:
                        print("[uc] 重开失败: %s" % str(e)[:100], flush=True)
                    if tok: break
                # click 与 handle 交替: click 落了没结果时换 handle 的回车流试一次
                fn = "uc_gui_click_captcha" if r in (1, 3, 4) else "uc_gui_handle_captcha"
                try:
                    getattr(sb, fn)()
                    print("[uc] round %d %s() OK" % (r, fn), flush=True)
                except Exception as e:
                    print("[uc] round %d %s() err: %s" % (r, fn, str(e)[:90]), flush=True)
                tok = poll_token(sb, POLL_CLICK, tag="round%d" % r)
                if not tok:
                    diag(sb, "round%d失败" % r)
                    try: sb.save_screenshot("uc_debug_r%d.png" % r)
                    except Exception: pass
        if tok:
            with open("ts_token.txt", "w") as f: f.write(tok)
            with open("ts_proxy.txt", "w") as f: f.write(px)
            print("[uc] OK via", px, flush=True)
        else:
            try: sb.save_screenshot("uc_debug.png")
            except Exception: pass
        return tok

if __name__ == "__main__":
    single = os.environ.get("SINGLE_PROXY", "")   # ip-sift 阶段2: 只测这一个
    if single:
        px_list = [single.replace("socks5://", "")]
    else:
        pl = load_proxies()
        good = [p for p in load_good() if p in pl]
        rest = [p for p in pl if p not in good]
        random.shuffle(rest)
        px_list = (good + rest)[:4]
    print("[uc] 代理队列:", px_list, flush=True)
    got = ""
    for px in px_list:
        print("[uc] === 代理", px, "===", flush=True)
        try:
            got = try_one("socks5://" + px)
        except Exception as e:
            print("[uc] exc:", str(e)[:120], flush=True)
        if got: break
    print("[uc] 最终:", "OK" if got else "FAIL", flush=True)
    sys.exit(0)
