"""推特(X)视频下载器后端。

- POST /api/parse     解析推文,返回作者、正文及每个视频的多清晰度直链
- GET  /api/stream    播放中转代理(转发 Range,支持进度条拖动)
- GET  /api/download  下载中转代理(Content-Disposition 触发"另存为")
- GET  /              前端单页

默认模式下服务器只承担解析流量,播放/下载由浏览器直连推特 CDN;
两个中转接口仅在前端"服务器中转"开关开启时使用,且只允许
video.twimg.com / pbs.twimg.com 域名,防止被当作任意 URL 代理滥用。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")

TWEET_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:twitter\.com|x\.com|mobile\.twitter\.com)"
    r"/(?:[^/]+)/status(?:es)?/(\d+)",
    re.IGNORECASE,
)

# 中转接口允许的 CDN 域名白名单
ALLOWED_HOSTS = {"video.twimg.com", "pbs.twimg.com"}

CHUNK_SIZE = 64 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

RESOLUTION_IN_URL_RE = re.compile(r"/(\d{2,4}x\d{2,4})/")


def _error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _formats_from_ytdlp(entry: dict) -> list[dict]:
    """从 yt-dlp 的 entry 里整理出前端需要的格式列表(高清晰度在前)。"""
    formats: list[dict] = []
    for f in entry.get("formats", []):
        if f.get("vcodec") == "none":  # 纯音频轨
            continue
        url = f.get("url")
        if not url:
            continue
        protocol = f.get("protocol") or ""
        is_hls = "m3u8" in protocol
        width, height = f.get("width"), f.get("height")
        resolution = f"{width}x{height}" if width and height else (f.get("format_note") or "unknown")
        formats.append(
            {
                "url": url,
                "resolution": resolution,
                "ext": f.get("ext") or "mp4",
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "is_hls": is_hls,
                "_height": height or 0,
            }
        )
    formats.sort(key=lambda f: (not f["is_hls"], f["_height"]), reverse=True)
    # 直链(非 HLS)优先、高清晰度在前;去掉内部排序字段
    formats.sort(key=lambda f: f["is_hls"])
    for f in formats:
        f.pop("_height", None)
    return formats


def _video_from_ytdlp(entry: dict) -> dict | None:
    formats = _formats_from_ytdlp(entry)
    if not formats:
        return None
    direct = [f for f in formats if not f["is_hls"]]
    best = direct[0] if direct else formats[0]
    return {
        "title": entry.get("title") or "video",
        "thumbnail": entry.get("thumbnail"),
        "duration": entry.get("duration"),
        "best": {"url": best["url"], "resolution": best["resolution"]},
        "formats": formats,
    }


def _parse_with_ytdlp(url: str, tweet_id: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries") if info.get("_type") == "playlist" else [info]
    videos = [v for v in (_video_from_ytdlp(e) for e in entries or [] if e) if v]
    if not videos:
        raise LookupError("no video")

    return {
        "tweet_id": tweet_id,
        "uploader": info.get("uploader") or "",
        "uploader_id": info.get("uploader_id") or "",
        "description": info.get("description") or "",
        "videos": videos,
    }


def _parse_with_fxtwitter(tweet_id: str) -> dict:
    """受限/NSFW 推文回退:走 fxtwitter 公开 API 再解析一次。"""
    resp = requests.get(
        f"https://api.fxtwitter.com/status/{tweet_id}",
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    data = resp.json()
    tweet = data.get("tweet") or {}
    raw_videos = ((tweet.get("media") or {}).get("videos")) or []
    if not raw_videos:
        raise LookupError("no video")

    videos = []
    for v in raw_videos:
        best_url = v.get("url")
        if not best_url:
            continue
        formats = []
        for variant in v.get("variants") or []:
            vurl = variant.get("url")
            if not vurl or ".m3u8" in vurl.split("?")[0]:
                continue
            match = RESOLUTION_IN_URL_RE.search(vurl)
            formats.append(
                {
                    "url": vurl,
                    "resolution": match.group(1) if match else "unknown",
                    "ext": "mp4",
                    "filesize": None,
                    "is_hls": False,
                }
            )
        if not formats:
            match = RESOLUTION_IN_URL_RE.search(best_url)
            formats = [
                {
                    "url": best_url,
                    "resolution": match.group(1) if match else "unknown",
                    "ext": "mp4",
                    "filesize": None,
                    "is_hls": False,
                }
            ]

        def _height(f: dict) -> int:
            try:
                return int(f["resolution"].split("x")[1])
            except (IndexError, ValueError):
                return 0

        formats.sort(key=_height, reverse=True)
        best = formats[0]
        videos.append(
            {
                "title": tweet.get("text") or "video",
                "thumbnail": v.get("thumbnail_url"),
                "duration": v.get("duration"),
                "best": {"url": best["url"], "resolution": best["resolution"]},
                "formats": formats,
            }
        )

    if not videos:
        raise LookupError("no video")

    author = tweet.get("author") or {}
    return {
        "tweet_id": tweet_id,
        "uploader": author.get("name") or "",
        "uploader_id": author.get("screen_name") or "",
        "description": tweet.get("text") or "",
        "videos": videos,
    }


@app.post("/api/parse")
def api_parse():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    match = TWEET_URL_RE.match(url)
    if not match:
        return _error("请输入有效的推文链接(x.com / twitter.com)", 400)
    tweet_id = match.group(1)

    try:
        data = _parse_with_ytdlp(url, tweet_id)
    except (yt_dlp.utils.DownloadError, LookupError):
        # 受限/敏感推文游客接口拿不到,回退 fxtwitter
        try:
            data = _parse_with_fxtwitter(tweet_id)
        except LookupError:
            return _error("没有找到视频(可能是纯图片推文)", 422)
        except (requests.RequestException, ValueError) as exc:
            return _error(f"解析失败:{exc}", 500)
    except Exception as exc:  # noqa: BLE001 解析库异常种类繁多,统一兜底
        return _error(f"解析失败:{exc}", 500)

    return jsonify({"ok": True, "data": data})


# ---------------------------------------------------------------------------
# 服务器中转(播放 / 下载)
# ---------------------------------------------------------------------------

def _validate_cdn_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS


def _proxy(url: str, extra_headers: dict[str, str]) -> Response:
    upstream_headers = {"User-Agent": USER_AGENT}
    if range_header := request.headers.get("Range"):
        upstream_headers["Range"] = range_header

    try:
        upstream = requests.get(url, headers=upstream_headers, stream=True, timeout=30)
    except requests.RequestException as exc:
        return _error(f"上游不可用:{exc}", 502)

    headers = dict(extra_headers)
    for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if value := upstream.headers.get(name):
            headers[name] = value
    headers.setdefault("Accept-Ranges", "bytes")

    def generate():
        try:
            yield from upstream.iter_content(CHUNK_SIZE)
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=headers,
    )


@app.get("/api/stream")
def api_stream():
    url = request.args.get("url", "")
    if not _validate_cdn_url(url):
        return _error("仅允许中转推特 CDN 地址", 400)
    return _proxy(url, {})


@app.get("/api/download")
def api_download():
    url = request.args.get("url", "")
    if not _validate_cdn_url(url):
        return _error("仅允许中转推特 CDN 地址", 400)

    filename = request.args.get("filename") or "twitter-video.mp4"
    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", filename).strip("._") or "twitter-video.mp4"
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"

    disposition = f"attachment; filename*=UTF-8''{quote(safe_name)}"
    return _proxy(url, {"Content-Disposition": disposition})


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_file(BASE_DIR / "static" / "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
