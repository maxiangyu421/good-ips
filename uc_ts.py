"""SeleniumBase UC Mode 过盾(2026-08-29 实测成功路线)。
目标站与 token 字段名来自解密配置(TARGET_URL / TOKEN_INPUT env), 不写死在公开代码里。
支持 SINGLE_PROXY 单代理模式(供 ip-sift 阶段2逐个实测)。
成功后写 ts_token.txt + ts_proxy.txt(token 5分钟有效, 注册必须走同一代理)。"""
import sys, time, os, random

from cfg_open import load as _cfg
_CFG = _cfg()
PAGE = _CFG.get("TARGET_URL", "")
TOKEN_INPUT = _CFG.get("TOKEN_INPUT", "cf-turnstile-response")
if not PAGE:
    print("[uc] 缺 TARGET_URL(config 未解密或字段缺失)"); sys.exit(2)

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

def read_token(sb):
    try:
        return sb.execute_script(
            'var i=document.querySelector(\'input[name="%s"]\');' % TOKEN_INPUT +
            "return i ? i.value : '';") or ""
    except Exception:
        return ""

def try_one(px):
    from seleniumbase import SB
    with SB(uc=True, locale="en", proxy=px,
            chromium_arg="--ignore-certificate-errors") as sb:
        sb.uc_open_with_reconnect(PAGE, reconnect_time=6)
        time.sleep(10)
        tok = read_token(sb)
        print("[uc] 初始 token_len", len(tok), flush=True)
        # 坏代理试再多轮也解不出(实测), 2 轮就换
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
