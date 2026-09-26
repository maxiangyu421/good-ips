"""诊断: ping0.cc 从 GitHub runner 出口到底渲染出什么。
输出: ① subprocess curl 直连状态 ② 浏览器打开后的 title/URL/readyState/正文摘要/head 2000 字
③ 截图 probe_diag.png(workflow 会自动上传 artifact)。
用法: xvfb-run python3 uc_ping0_diag.py
"""
import subprocess, time, json, os

URL = "https://ping0.cc/"

def curl_probe():
    for ua in ["curl/8.0", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36"]:
        r = subprocess.run(["curl", "-sS", "-o", "/tmp/p0.html", "-w", "%{http_code}",
                            "-A", ua, "--max-time", "20", URL], capture_output=True, text=True)
        body = open("/tmp/p0.html", "rb").read()[:400].decode("utf-8", "ignore")
        print(f"[diag] curl UA={ua[:25]!r} -> status={r.stdout} err={r.stderr[:80]!r}", flush=True)
        print("[diag] body[:400]:", body.replace("\n", " ")[:400], flush=True)

def main():
    curl_probe()
    from seleniumbase import SB
    with SB(uc=True, locale="en", chromium_arg="--ignore-certificate-errors",
            page_load_strategy="eager") as sb:
        sb.uc_open_with_reconnect(URL, reconnect_time=6)
        head = ""
        for i in range(12):  # ~72s
            time.sleep(6)
            try:
                info = sb.execute_script("""
                  return JSON.stringify({t: document.title, u: location.href,
                    rs: document.readyState, len: document.body ? document.body.innerHTML.length : -1,
                    head: document.body ? document.body.outerHTML.slice(0,1500) : '',
                    cf: !!document.querySelector('[id*=challenge], [class*=challenge], #turnstile-wrapper, .cf-turnstile')});
                """)
            except Exception as e:
                info = json.dumps({"err": str(e)[:150]})
            print(f"[diag] t{i}:", info[:800], flush=True)
            d = json.loads(info) if info != -1 else {}
            if isinstance(d, dict) and d.get("len", -1) > 5000:
                head = json.loads(info)["head"]
                break
        print("[diag] FULLHEAD:", head[:2000], flush=True)
        try:
            sb.save_screenshot("probe_diag.png")
            print("[diag] screenshot saved", flush=True)
        except Exception as e:
            print("[diag] shot err", str(e)[:100], flush=True)

if __name__ == "__main__":
    main()
