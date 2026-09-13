#!/usr/bin/env python3
"""Turnstile 诊断探针(09-12 挑战前夜): 对单个代理打开注册页, 周期性 dump 控件状态。
回答一个问题: 失败 IP 的 Turnstile 到底处于什么状态 ——
  A. 控件根本没渲染(widgets=[] -> 是页面/站点问题, 不是盾)
  B. 控件在但点击后无挑战(token 不来 -> 非交互通道被拒)
  C. 点击后出现挑战面板(交互式 -> 需要解图片/选择类挑战)
只读不写任何池子; 由 probe.yml 通过 uc_probe.py 的 PROBE_SCRIPT=uc_diag.py 调用。
截图命名 probe_diag_*.png 以命中既有 artifact 上传路径 probe_*.png。"""
import os, sys, time, json

from cfg_open import load as _cfg
_CFG = _cfg()
PAGE = _CFG.get("TARGET_URL", "")
TOKEN_INPUT = _CFG.get("TOKEN_INPUT", "cf-turnstile-response")
if not PAGE:
    print("[diag] 缺 TARGET_URL"); sys.exit(2)

SNAP_JS = r"""
() => {
  const out = {iframes: [], widgets: [], tokenLen: 0, inputs: []};
  const seen = new Set();
  function walk(root, depth) {
    if (!root || depth > 8 || seen.has(root)) return;
    seen.add(root);
    let els = [];
    try { els = root.querySelectorAll('*'); } catch(e) { return; }
    els.forEach(el => {
      let cls = ''; try { cls = (typeof el.className === 'string' ? el.className : ''); } catch(e) {}
      const cl = cls.toLowerCase(), id = (el.id || '').toLowerCase();
      if (el.tagName === 'IFRAME') {
        let r = [0,0,0,0]; try { const b = el.getBoundingClientRect(); r = [b.x|0, b.y|0, b.width|0, b.height|0]; } catch(e) {}
        out.iframes.push({src: (el.src || '').slice(0, 110), rect: r});
      }
      if (cl.indexOf('turnstile') >= 0 || id.indexOf('turnstile') >= 0) {
        let r = [0,0,0,0]; try { const b = el.getBoundingClientRect(); r = [b.x|0, b.y|0, b.width|0, b.height|0]; } catch(e) {}
        out.widgets.push({tag: el.tagName, cls: cls.slice(0, 70), id: el.id, rect: r,
                          hasShadow: !!el.shadowRoot});
      }
      if (el.tagName === 'INPUT') out.inputs.push({name: el.name || '', type: el.type || '', len: (el.value||'').length});
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    });
  }
  walk(document, 0);
  try {
    const ti = document.querySelector('input[name="%s"]');
    out.tokenLen = ti ? (ti.value || '').length : -1;
  } catch(e) { out.tokenLen = -2; }
  out.url = (location.href || '').slice(0, 120);
  try { out.title = (document.title || '').slice(0, 90); } catch(e) {}
  try { out.bodyLen = document.body ? document.body.innerHTML.length : -1; } catch(e) { out.bodyLen = -2; }
  return out;
}
""" % TOKEN_INPUT

CROP_JS = r"""
(sel) => {
  function find(root, depth) {
    if (!root || depth > 8) return null;
    let els = []; try { els = root.querySelectorAll('*'); } catch(e) { return null; }
    for (const el of els) {
      let cls = ''; try { cls = (typeof el.className === 'string' ? el.className : ''); } catch(e) {}
      if ((cls.toLowerCase().indexOf('turnstile') >= 0 || (el.id||'').toLowerCase().indexOf('turnstile') >= 0)) {
        const b = el.getBoundingClientRect();
        if (b.width > 10 && b.height > 10) return [b.x|0, b.y|0, b.width|0, b.height|0];
      }
      if (el.shadowRoot) { const r = find(el.shadowRoot, depth+1); if (r) return r; }
    }
    return null;
  }
  return find(document, 0);
}
"""

def snap(sb):
    try:
        r = sb.execute_script(SNAP_JS)
        if isinstance(r, dict):
            return r
        return {"err": "execute_script 返回 %r" % (r,)}
    except Exception as e:
        return {"err": str(e)[:120]}

def main():
    px = os.environ.get("SINGLE_PROXY", "").replace("socks5://", "")
    total = int(os.environ.get("DIAG_TOTAL", "120"))
    click_at = int(os.environ.get("DIAG_CLICK_AT", "15"))
    print("[diag] 代理 %s | 总观测 %ds | %ds 时点击一次" % (px, total, click_at), flush=True)
    from seleniumbase import SB
    kw = {"page_load_strategy": "eager"}
    with SB(uc=True, locale="en", proxy="socks5://" + px,
            chromium_arg="--ignore-certificate-errors", **kw) as sb:
        t0 = time.time()
        sb.uc_open_with_reconnect(PAGE, reconnect_time=6)
        print("[diag] 页面打开耗时 %.1fs url=%s" % (time.time()-t0, getattr(sb, "current_url", "?")[:80]), flush=True)
        t0 = time.time()  # 观测计时从页面打开后才起算, 慢代理开页时间不吃掉观测窗口
        clicked = False
        i = 0
        while time.time() - t0 < total:
            i += 1
            s = snap(sb)
            errtag = (" url=%s bodyLen=%s ERR=%s" % (s.get("url", "?"), s.get("bodyLen", "?"), s.get("err", ""))) if (s.get("err") or s.get("url")) else ""
            print("[diag] t+%03ds snap%d: iframes=%d widgets=%d tokenLen=%s%s" % (
                time.time()-t0, i, len(s.get("iframes", [])), len(s.get("widgets", [])),
                s.get("tokenLen"), errtag[:260]), flush=True)
            for w in s.get("widgets", []):
                print("      widget: %s cls=%s rect=%s shadow=%s" % (
                    w.get("tag"), (w.get("cls") or "")[:50], w.get("rect"), w.get("hasShadow")), flush=True)
            for f in s.get("iframes", [])[:6]:
                print("      iframe: src=%s rect=%s" % (f.get("src"), f.get("rect")), flush=True)
            if not clicked and time.time() - t0 >= click_at:
                clicked = True
                try:
                    sb.uc_gui_click_captcha()
                    print("[diag] t+%03ds uc_gui_click_captcha() OK" % (time.time()-t0), flush=True)
                except Exception as e:
                    print("[diag] click err: %s" % str(e)[:120], flush=True)
                try: sb.save_screenshot("probe_diag_%s_mid.png" % px.replace(":", "_"))
                except Exception: pass
            time.sleep(10)
        # 收尾: 控件局部图 + 全页图
        try:
            box = sb.execute_script(CROP_JS)
            print("[diag] 控件盒:", box, flush=True)
            sb.save_screenshot("probe_diag_%s_full.png" % px.replace(":", "_"))
        except Exception as e:
            print("[diag] 收尾 err: %s" % str(e)[:120], flush=True)
        s = snap(sb)
        print("[diag] 最终 tokenLen=%s widgets=%d iframes=%d" % (
            s.get("tokenLen"), len(s.get("widgets", [])), len(s.get("iframes", []))), flush=True)
        print("[diag] DONE %s" % ("TOKEN_OK" if (s.get("tokenLen") or 0) > 50 else "NO_TOKEN"), flush=True)

if __name__ == "__main__":
    main()
