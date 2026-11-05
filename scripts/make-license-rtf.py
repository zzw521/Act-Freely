"""把 EULA.zh-CN.txt 转成 RTF（中文全部用 \\uN 转义）。

为什么要 RTF：Tauri 的 MSI 打包会把 .txt 协议原样塞进 \\ansicpg1252 的 RTF 壳里，
UTF-8 中文在 RichEdit 里会变乱码；而 .rtf 会被 WiX 直接使用。NSIS 的许可页按
文件内容前 5 字节 "{\\rtf" 判断 RTF，所以同一个文件两个安装器都能正确显示。

产物是纯 ASCII，避免任何编码歧义。改完 txt 重新跑一次即可。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "EULA.zh-CN.txt"
DST = ROOT / "EULA.zh-CN.rtf"

HEADER = (
    r"{\rtf1\ansi\ansicpg936\deff0\uc1"
    r"{\fonttbl{\f0\fnil\fcharset134 Microsoft YaHei;}}"
    r"\viewkind4\pard\sa160\sl240\slmult1\f0\fs20 "
)


def escape(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if ch in "\\{}":
            out.append("\\" + ch)
        elif code < 128:
            out.append(ch)
        else:
            # RTF 的 \uN 是带符号 16 位
            out.append("\\u%d?" % (code - 65536 if code > 32767 else code))
    return "".join(out)


def main() -> None:
    source = SRC.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    paragraphs = [escape(line.strip()) for line in source.split("\n")]
    body = "\\par\n".join(paragraphs)
    rtf = HEADER + body + "\\par }"
    data = rtf.encode("ascii")
    DST.write_bytes(data)
    print(f"{DST.name}: {len(data)} bytes, paragraph={len(paragraphs)}")


if __name__ == "__main__":
    main()
