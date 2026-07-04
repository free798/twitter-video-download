"""Twitter/X 视频下载器后端服务。

基于 FastAPI + yt-dlp:
- POST /api/parse    解析推文链接,返回视频信息与各清晰度直链
- GET  /api/download 代理下载视频(限制为 Twitter CDN 域名,避免 SSRF)
- GET  /             前端页面
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Twitter Video Download", version="1.0.0")

TWEET_URL_RE = re.compile(
    r"^https?://(www\.)?(twitter\.com|x\.com|mobile\.twitter\.com)/[^/]+/status/\d+",
    re.IGNORECASE,
)

# 允许代理下载的 CDN 域名白名单
ALLOWED_DOWNLOAD_HOSTS = (".twimg.com",)


class ParseRequest(BaseModel):
    url: str


class VideoFormat(BaseModel):
    format_id: str
    resolution: str
    ext: str
    url: str
    filesize: int | None = None


class ParseResponse(BaseModel):
    title: str
    uploader: str
    thumbnail: str | None = None
    duration: float | None = None
    formats: list[VideoFormat]


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not TWEET_URL_RE.match(url):
        raise HTTPException(status_code=400, detail="请输入有效的 Twitter/X 推文链接")
    # x.com 统一转为 twitter.com,yt-dlp 两者都支持,这里保持原样即可
    return url


@app.post("/api/parse", response_model=ParseResponse)
def parse_video(req: ParseRequest) -> ParseResponse:
    url = _normalize_url(req.url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=422, detail=f"解析失败:{exc}") from exc

    # 推文包含多个视频时取第一个
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise HTTPException(status_code=404, detail="该推文中未找到视频")
        info = entries[0]

    formats: list[VideoFormat] = []
    for f in info.get("formats", []):
        # 只保留 http(s) 直链,过滤 m3u8 分片流;vcodec == "none" 表示纯音频轨
        if f.get("vcodec") == "none":
            continue
        if f.get("protocol") not in ("http", "https"):
            continue
        height = f.get("height")
        width = f.get("width")
        resolution = f"{width}x{height}" if width and height else (f.get("format_note") or "未知")
        formats.append(
            VideoFormat(
                format_id=str(f.get("format_id")),
                resolution=resolution,
                ext=f.get("ext") or "mp4",
                url=f["url"],
                filesize=f.get("filesize") or f.get("filesize_approx"),
            )
        )

    if not formats:
        raise HTTPException(status_code=404, detail="该推文中未找到可下载的视频")

    # 按分辨率从高到低排列
    def _height(fmt: VideoFormat) -> int:
        try:
            return int(fmt.resolution.split("x")[1])
        except (IndexError, ValueError):
            return 0

    formats.sort(key=_height, reverse=True)

    return ParseResponse(
        title=info.get("title") or "Twitter 视频",
        uploader=info.get("uploader") or info.get("uploader_id") or "未知作者",
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        formats=formats,
    )


@app.get("/api/download")
def download_video(url: str, filename: str = "twitter-video.mp4"):
    """代理下载,解决浏览器跨域无法直接触发下载的问题。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="非法的下载地址")
    if not any(parsed.hostname.endswith(host) for host in ALLOWED_DOWNLOAD_HOSTS):
        raise HTTPException(status_code=400, detail="仅支持下载 Twitter CDN 上的视频")

    # 文件名清洗,防止响应头注入
    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", filename) or "twitter-video.mp4"
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(request, timeout=30)  # noqa: S310 已校验域名白名单
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"下载源不可用:{exc}") from exc

    def stream():
        try:
            while chunk := resp.read(64 * 1024):
                yield chunk
        finally:
            resp.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{urllib.parse.quote(safe_name)}"',
    }
    if length := resp.headers.get("Content-Length"):
        headers["Content-Length"] = length

    return StreamingResponse(stream(), media_type="video/mp4", headers=headers)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
