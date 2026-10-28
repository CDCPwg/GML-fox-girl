"""凭据加密托管：Windows DPAPI（crypt32）按机器+当前用户绑定加密，纯标准库 ctypes。

密文只有在本机本用户下才能解开——配置文件被拷走也无法还原明文。
非 Windows 平台调用保护接口会抛错（明确拒绝明文落盘）。
"""
import base64
import ctypes
import sys
from ctypes import wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(plain: str) -> str:
    """加密明文 → base64 密文（CryptProtectData，本机当前用户可解）。"""
    if sys.platform != "win32":
        raise RuntimeError("凭据加密仅支持 Windows（DPAPI）；当前平台拒绝明文存储")
    blob_in, blob_out = _blob(plain.encode("utf-8")), _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), "glm-foxgirl", None, None, None, 0, ctypes.byref(blob_out)):
        raise RuntimeError("DPAPI 加密失败")
    try:
        return base64.b64encode(ctypes.string_at(blob_out.pbData, blob_out.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_unprotect(token: str) -> str:
    """解密 dpapi_protect 的密文（仅限同一台机器同一用户）。"""
    if sys.platform != "win32":
        raise RuntimeError("凭据解密仅支持 Windows（DPAPI）")
    blob_in, blob_out = _blob(base64.b64decode(token)), _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise RuntimeError("DPAPI 解密失败（需在同一台机器同一用户下执行）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
