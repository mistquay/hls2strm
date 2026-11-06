"""有限预检得到的容器信息；不解码或转换媒体。"""

from urllib.parse import urlsplit

CONTAINERS = frozenset({"m3u8", "mp4", "mov", "mkv", "webm", "ts"})
MEDIA_TYPES = {"m3u8": "application/vnd.apple.mpegurl", "mp4": "video/mp4", "mov": "video/quicktime",
               "mkv": "video/x-matroska", "webm": "video/webm", "ts": "video/mp2t"}
CONTENT_TYPES = {
    "application/vnd.apple.mpegurl": "m3u8", "application/x-mpegurl": "m3u8",
    "audio/mpegurl": "m3u8", "audio/x-mpegurl": "m3u8",
    "video/mp4": "mp4", "application/mp4": "mp4", "video/quicktime": "mov",
    "video/x-matroska": "mkv", "video/webm": "webm", "video/mp2t": "ts",
}


def media_container(url: str, content_type: str = "", sample: bytes = b"") -> str:
    if sample.lstrip().startswith(b"#EXTM3U"):
        return "m3u8"
    if b"ftyp" in sample[:32]:
        return "mp4"
    kind = CONTENT_TYPES.get(content_type.split(";", 1)[0].strip().lower())
    if kind:
        return kind
    extension = urlsplit(url).path.rsplit(".", 1)[-1].lower()
    if extension in CONTAINERS:
        return extension
    if sample.startswith(b"\x1a\x45\xdf\xa3"):
        return "mkv"
    return ""
