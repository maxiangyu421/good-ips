"""SeleniumBase UC Mode 顺路采集 ping0.cc 出口画像(09-12)。
背景: stage1 的无头 socks5 隧道已被 ping0 Turnstile 全量挑战封死(实测直连/代理 100% 挑战页),
改由 stage2 真浏览器采集 —— 浏览器环境挑战自动通过并 reload(与人工 Firefox 一致, 无需点击)。
用法: SINGLE_PROXY=host:port xvfb-run python3 uc_ping0.py → 写 ping0_profile.json
字段(全部来自服务端直出, 无 JS 渲染数据): exit_ip(window.ip) / labels(类型标签) /
risk(风控值%) / native(原生|广播) / usecount(共享人数)。失败写 {"error":...} 绝不抛异常。"""
import sys, time, os, json

URL = "https://ping0.cc/"

def extract(sb):
    js = """
    var q = s => { var e = document.querySelector(s); return e ? e.textContent.trim() : ''; };
    var labels = [].slice.call(document.querySelectorAll('.line-iptype .label'))
                  .map(e => e.textContent.trim());
    var uc = document.querySelector('.usecountbar');
    var risk = null;
    var rv = q('.riskbar .riskcurrent .value');
    if (rv) { var m = rv.match(/(\\d+)%/); if (m) risk = parseInt(m[1]); }
    return JSON.stringify({
      exit_ip: window.ip || '',
      labels: labels,
      risk: risk,
      native: q('.line-nativeip .label'),
      usecount: uc ? uc.getAttribute('usecount') : ''
    });
    """
    try:
        return json.loads(sb.execute_script(js))
    except Exception as e:
        return {"error": str(e)[:120]}

def main():
    px = os.environ.get("SINGLE_PROXY", "").replace("socks5://", "")
    if not px:
        print("[p0] 缺 SINGLE_PROXY"); sys.exit(2)
    out = {}
    try:
        from seleniumbase import SB
        with SB(uc=True, locale="en", proxy="socks5://" + px,
                chromium_arg="--ignore-certificate-errors") as sb:
            sb.uc_open_with_reconnect(URL, reconnect_time=6)
            prof = {}
            for i in range(9):   # ~54s: 挑战自动过 → reload → 服务端直出字段出现
                time.sleep(6)
                prof = extract(sb)
                if prof.get("risk") is not None or prof.get("labels"):
                    break
                if i == 4:       # 中途卡挑战兜底点一下
                    try:
                        sb.uc_gui_handle_captcha()
                        print("[p0] attempt uc_gui_handle_captcha() OK", flush=True)
                    except Exception as e:
                        print("[p0] handle_captcha err:", str(e)[:90], flush=True)
            out = prof
    except Exception as e:
        out = {"error": str(e)[:150]}
    out["proxy"] = px
    with open("ping0_profile.json", "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print("[p0]", json.dumps(out, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
