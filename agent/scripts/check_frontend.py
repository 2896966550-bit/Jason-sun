"""前端静态检查（D3）

为什么需要：前端没有构建工具，改名/删文件/漏引资源都不会在启动时报错，
只会在浏览器里静默失败（本轮就踩过：改了 JS 却被缓存、SVG 无尺寸撑破布局）。
这个脚本把几类"只有打开浏览器才能发现"的问题变成**可执行检查**，可进 CI。

检查项：
  1. HTML 标签配平（div/button/svg/span/section/aside/nav/table…）
  2. HTML 引用的 /static/ 资源是否真实存在
  3. 前端是否出现境外域名（合规红线：白名单只有 dashscope.aliyuncs.com，
     前端本不该出现任何外部域名）
  4. UI 代码里是否还有 emoji 当图标（注释内的不计）
  5. CSS 花括号配平

运行：<解释器> scripts/check_frontend.py
退出码：0 通过；1 有任何一项失败。
"""
import os
import re
import sys

AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HTML_FILES = ["equis.html", "equis-doubao.html"]
CSS_DIR = os.path.join(AGENT, "static", "css")
JS_DIR = os.path.join(AGENT, "static", "js")

BALANCE_TAGS = ["div", "button", "svg", "span", "section", "aside", "nav", "table", "tr", "td", "th"]

# 允许出现的外部域名（前端应为空；这里只白名单本地与相对路径）
ALLOWED_HOSTS = ("127.0.0.1", "localhost")

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print("  [%s] %-44s %s" % ("OK " if ok else "FAIL", name, detail))
    return ok


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def strip_comments(text, kind="html"):
    if kind == "html":
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def main():
    print("=" * 78)
    print("前端静态检查")
    print("=" * 78)

    # ── 1. 标签配平 ──
    print("\n1) HTML 标签配平")
    for fn in HTML_FILES:
        p = os.path.join(AGENT, fn)
        if not os.path.exists(p):
            check(fn, False, "文件不存在")
            continue
        html = read(p)
        ok = True
        detail = []
        for tag in BALANCE_TAGS:
            o = len(re.findall(r"<%s[\s>]" % tag, html))
            c = len(re.findall(r"</%s>" % tag, html))
            if o != c:
                ok = False
                detail.append("%s %d/%d" % (tag, o, c))
        check("%s 标签配平" % fn, ok, "；".join(detail) if detail else "全部平衡")

    # ── 2. 静态资源存在性 ──
    print("\n2) 静态资源可达性")
    missing = []
    refs = set()
    for fn in HTML_FILES:
        p = os.path.join(AGENT, fn)
        if not os.path.exists(p):
            continue
        html = read(p)
        for m in re.finditer(r'(?:href|src)="(/static/[^"?#]+)', html):
            refs.add(m.group(1))
    for r in JS_DIR, CSS_DIR:
        if not os.path.isdir(r):
            continue
        for name in os.listdir(r):
            if name.endswith((".js", ".css")):
                txt = read(os.path.join(r, name))
                for m in re.finditer(r'["\'](/static/[^"\'?#]+)["\']', txt):
                    refs.add(m.group(1))
    for ref in sorted(refs):
        fp = os.path.join(AGENT, ref.lstrip("/").replace("/", os.sep))
        if not os.path.exists(fp):
            missing.append(ref)
    check("引用的 /static 资源均存在", not missing,
          "缺失: %s" % ", ".join(missing[:5]) if missing else "共 %d 个引用" % len(refs))

    # ── 3. 境外域名 ──
    print("\n3) 境外域名（合规红线）")
    foreign = []
    scan = [(fn, os.path.join(AGENT, fn)) for fn in HTML_FILES]
    for d in (CSS_DIR, JS_DIR):
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith((".css", ".js")):
                    scan.append((name, os.path.join(d, name)))
    for name, path in scan:
        if not os.path.exists(path):
            continue
        txt = strip_comments(read(path))
        for m in re.finditer(r"https?://([a-zA-Z0-9.\-]+)", txt):
            host = m.group(1).lower()
            if host in ALLOWED_HOSTS or host.endswith(".aliyuncs.com"):
                continue
            # XML 命名空间标识符不是网络请求（SVG 的 xmlns 就是这种）。
            # 实测：自绘 SVG 引入 xmlns="http://www.w3.org/2000/svg"
            # 会被误判成境外域名 → 门禁假阳性。
            if host in ("www.w3.org", "w3.org"):
                continue
            foreign.append("%s → %s" % (name, host))
    check("前端无境外域名引用", not foreign,
          "；".join(sorted(set(foreign))[:4]) if foreign else "未发现外部域名")

    # ── 4. 残留 emoji（UI 代码，注释已剔除）──
    print("\n4) UI 代码残留 emoji")
    # 只拦**彩色 emoji**（字形随平台变化、无法控制描边与颜色）。
    # ✓ ✗ ● 属排版符号：等线、单色、跨平台一致，不算 emoji，放行。
    ALLOWED_GLYPHS = set("✓✗●")
    # 主界面相关文件必须零彩色 emoji；
    # 2026-10-17：equis-doubao.html 的 12 处已清理，技术债清零，
    # 两个页面现在都纳入严格项（任何新增 emoji 都会让门禁失败）。
    STRICT_FILES = {"equis.html", "equis-doubao.html", "cards.js", "sse.js", "app.js", "palette.js"}
    emo_hits, debt = [], {}
    for name, path in scan:
        if not name.endswith((".html", ".js")):
            continue
        if not os.path.exists(path):
            continue
        txt = strip_comments(read(path), "js" if name.endswith(".js") else "html")
        for m in EMOJI_RE.finditer(txt):
            if m.group() in ALLOWED_GLYPHS:
                continue
            ln = txt.count("\n", 0, m.start()) + 1
            if name in STRICT_FILES:
                emo_hits.append("%s:L%d %s" % (name, ln, m.group()))
            else:
                debt[name] = debt.get(name, 0) + 1
    check("主界面无彩色 emoji", not emo_hits,
          "；".join(emo_hits[:4]) if emo_hits else "已全部替换为 SVG/状态点")
    if debt:
        print("  [debt] 遗留页待清理：%s（不阻断，已登记为技术债）"
              % "，".join("%s %d 处" % (k, v) for k, v in debt.items()))

    # ── 5. CSS 花括号 ──
    print("\n5) CSS 结构")
    bad = []
    if os.path.isdir(CSS_DIR):
        for name in sorted(os.listdir(CSS_DIR)):
            if not name.endswith(".css"):
                continue
            txt = read(os.path.join(CSS_DIR, name))
            o = txt.count("{")
            c = txt.count("}")
            if o != c:
                bad.append("%s %d/%d" % (name, o, c))
    check("CSS 花括号配平", not bad, "；".join(bad) if bad else "全部平衡")

    # ── 汇总 ──
    print("\n" + "=" * 78)
    n_ok = sum(1 for _n, ok, _d in results if ok)
    print("总结：%d/%d 项通过" % (n_ok, len(results)))
    for name, ok, detail in results:
        if not ok:
            print("  [FAIL] %s %s" % (name, detail))
    print("=" * 78)
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
