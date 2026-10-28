"""HTTP 工具：兼容 chunked 编码的请求体读取。

NapCat 的 HTTP 上报不设 Content-Length（Node http 自动走
Transfer-Encoding: chunked），BaseHTTPRequestHandler 不会自动解码分块——
只按 Content-Length 读会把每条真实上报读成空字节串（致命丢消息 bug）。
"""
import sys


def read_body(handler):
    """从 BaseHTTPRequestHandler 读取请求体：支持 chunked（多块+trailer）与定长。"""
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        body = b""
        while True:
            line = handler.rfile.readline(65536).strip()
            if b";" in line:
                line = line.split(b";", 1)[0]
            try:
                size = int(line, 16)
            except ValueError:
                return body  # 非法块行：容错返回已读部分
            if size == 0:
                while True:  # 消费 trailer 直到空行
                    trailer = handler.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                return body
            body += handler.rfile.read(size)
            handler.rfile.readline(65536)  # 块尾 CRLF
        return body
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


def log_error(context, e):
    print(f"[glm-foxgirl] {context}: {e}", file=sys.stderr)
