"""生成内嵌展示字（思源宋体 = Noto Serif SC 的可变字重子集）。

    python scripts/build_display_font.py [--src C:\\Windows\\Fonts\\NotoSerifSC-VF.ttf]

产出 apps/server/kbserver/web_static/fonts/ 下两份 woff2，并生成 web_static/fonts.css
（两条 @font-face，含 unicode-range，tokens.css 里的 --font-display 引用这个族名）。
字表不写死，从仓库现算：

- core：界面固定文案（web_static 里 HTML 的文本节点 + JS 字符串字面量）。说明页与收件箱
  的品牌名、标题、按钮、空态引导语全在这里，保证这些字在任何设备都是思源宋体。
- wide：core 之外、docs/*.md 真实中文里出现过的汉字，作为增量一份，用 unicode-range
  声明，只有动态内容（条目标题等）真用到才下载。

改了界面文案或说明页诗句之后重跑一次即可（只想重算区间用 --css-only）。
源字体许可为 SIL OFL 1.1，随字带一份 fonts/OFL.txt。
"""
from __future__ import annotations

import argparse
import glob
import html
import os
import re
import sys

WEB = os.path.join("apps", "server", "kbserver", "web_static")
FONTS = os.path.join(WEB, "fonts")
FACES_CSS = os.path.join(WEB, "fonts.css")
FAMILY = "金蔷薇宋体"
AXIS = (400, 600)          # 界面只用到 400 与 600 两档字重，轴收窄到这一段省体积
ASCII = {chr(i) for i in range(0x20, 0x7F)}
EXTRA = set("—…·‘’“”–×→←✓±°、。《》【】‖")


def html_text(path):
    s = open(path, encoding="utf-8").read()
    s = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def js_strings(path):
    s = open(path, encoding="utf-8").read()
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", " ", s)
    out = []
    for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\\n]|\\.)*)`', s):
        out.append("".join(g for g in m.groups() if g))
    return " ".join(out)


def collect_chars() -> tuple[str, str]:
    ui = [html_text(p) for p in glob.glob(os.path.join(WEB, "*.html"))]
    ui += [js_strings(p) for p in glob.glob(os.path.join(WEB, "js", "*.js"))]
    # 汉字 + 中日韩标点（U+3000 段）+ 全角标点（U+FF00 段，，：（）都在这）
    def keep(c):
        return "一" <= c <= "鿿" or "　" <= c <= "〿" or "！" <= c <= "～"
    core = ASCII | EXTRA | {c for t in ui for c in t if keep(c)}
    prose = set()
    for p in glob.glob(os.path.join("docs", "*.md")):
        prose |= {c for c in open(p, encoding="utf-8").read() if "一" <= c <= "鿿"}
    wide = prose - core
    return "".join(sorted(core)), "".join(sorted(wide))


def unicode_range(text: str) -> str:
    """把字符集压成 CSS unicode-range，逗号分隔、十六进制大写。"""
    cps = sorted({ord(c) for c in text})
    runs, lo, prev = [], cps[0], cps[0]
    for cp in cps[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        runs.append((lo, prev))
        lo = prev = cp
    runs.append((lo, prev))
    return ", ".join("U+%X" % a if a == b else "U+%X-%X" % (a, b) for a, b in runs)


FACE_HEADER = """/* 内嵌展示字（思源宋体 = Noto Serif SC，OFL 1.1）——由 scripts/build_display_font.py 生成，别手改。
   两份子集同一族名，浏览器按字符挑面孔：core 覆盖全站固定文案，wide 只在动态内容
   （条目标题等）用到 core 之外的字时才下载。轴区间 wght %d-%d。
   界面文案或说明页诗句改了：重跑那个脚本，两份 woff2 和本文件一起更新。 */
"""

FACE_TMPL = """@font-face {{
  font-family: "{family}";
  src: url("/webstatic/fonts/{file}.woff2") format("woff2");
  font-weight: {lo} {hi};
  font-style: normal;
  font-display: swap;
  unicode-range: {rng};
}}"""


def write_faces_css(pairs) -> None:
    with open(FACES_CSS, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(FACE_HEADER % AXIS)
        for name, text in pairs:
            fh.write(FACE_TMPL.format(family=FAMILY, file=name, lo=AXIS[0], hi=AXIS[1],
                                      rng=unicode_range(text)) + "\n")
    print("已生成 %s（%d B）" % (FACES_CSS, os.path.getsize(FACES_CSS)))


def build(src: str, core: str, wide: str) -> int:
    import tempfile

    from fontTools.subset import Options, Subsetter
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    os.makedirs(FONTS, exist_ok=True)
    made = []
    # 限轴结果先落成临时文件再子集：可变字体在同一次加载里串这两步会踩到 gvar 的
    # 惰性表（KeyError: 'uni2016' 这类），中间落一次盘就没这问题
    with tempfile.TemporaryDirectory() as td:
        limited = os.path.join(td, "jr-limited.ttf")
        instantiateVariableFont(TTFont(src), {"wght": AXIS}, inplace=False).save(limited)
        for name, text in (("jr-serif", core), ("jr-serif-wide", wide)):
            font = TTFont(limited, lazy=False)
            o = Options()
            o.flavor = "woff2"
            o.ignore_missing_unicodes = True
            o.hinting = False
            ss = Subsetter(options=o)
            ss.populate(text=text)
            ss.subset(font)
            out = os.path.join(FONTS, name + ".woff2")
            font.save(out)
            font.close()
            saved = TTFont(out)
            got = {chr(c) for c in saved.getBestCmap()}
            saved.close()
            missing = [c for c in text if c not in got]
            # 区间按字体里真有的字写：源字体没有的字符（如 ✓ ‖）不认领，留给后面的系统字
            made.append((name, "".join(c for c in text if c in got), missing))
            print("%-18s %4d 字 -> %4d KB  %s" % (
                name + ".woff2", len(text), os.path.getsize(out) // 1024,
                "覆盖完整" if not missing else "缺字：" + "".join(missing)))
    write_faces_css([("jr-serif", core), ("jr-serif-wide", wide)])
    return 1 if any(m for _, _, m in made) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=r"C:\Windows\Fonts\NotoSerifSC-VF.ttf",
                    help="Noto Serif SC 可变字体（google/fonts 的 ofl/notoserifsc 同款）")
    ap.add_argument("--css-only", action="store_true",
                    help="只按现有字表重写 fonts.css，不重新裁字体（woff2 已够用、只想改区间时）")
    a = ap.parse_args()
    core, wide = collect_chars()
    print("core %d 字 / wide 增量 %d 字 / 轴 wght %d-%d" % (len(core), len(wide), *AXIS))
    if a.css_only:
        write_faces_css([("jr-serif", core), ("jr-serif-wide", wide)])
        return 0
    if not os.path.exists(a.src):
        sys.exit("找不到源字体：%s\n（从 https://github.com/google/fonts/tree/main/ofl/notoserifsc 下载后指给 --src）" % a.src)
    return build(a.src, core, wide)


if __name__ == "__main__":
    sys.exit(main())
