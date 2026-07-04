#!/usr/bin/env python3
"""推特(X)视频解析下载器 - Flask 后端

接口:
  GET  /              前端页面
  POST /api/parse     解析推文链接, 返回视频信息与各清晰度 CDN 直链
  GET  /api/stream    (可选)播放中转代理, 供无法直连推特 CDN 的用户使用
  GET  /api/download  (可选)下载中转代理, 附带 Content-Disposition 触发另存为

默认模式下播放/下载由浏览器直连 video.twimg.com, 不消耗服务器流量;
用户在页面上打开"服务器中转"开关后才会走 /api/stream 和 /api/download。
"""
import re
import urllib.parse

import requests
from flask import Flask, Response, abort, jsonify, render_template, request, stream_with_context
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

# 支持的推文链接格式: twitter.com / x.com / mobile.twitter.com / vxtwitter 等镜像统一归一化
TWEET_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter|x|vxtwitter|fxtwitter|fixupx)\.com"
    r"/(?P<user>\w+)/status(?:es)?/(?P<id>\d+)",
    re.IGNORECASE,
)

# 中转代理只允许推特官方 CDN, 防止接口被当作任意 URL 代理滥用
ALLOWED_PROXY_HOSTS = ("video.twimg.com", "pbs.twimg.com")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


def normalize_tweet_url(raw_url: str):
    """校验并归一化推文链接, 返回 (标准链接, 推文ID); 不合法返回 (None, None)。"""
    match = TWEET_URL_RE.search(raw_url.strip())
    if not match:
        return None, None
    return f"https://x.com/{match.group('user')}/status/{match.group('id')}", match.group("id")


RESOLUTION_IN_URL_RE = re.compile(r"/(\d{2,4})x(\d{2,4})/")


def extract_via_fxtwitter(tweet_url: str) -> dict:
    """回退方案: 推特游客接口会隐藏受限(NSFW)推文的视频, yt-dlp 未登录时
    解析不到; fxtwitter 的公开 API 仍能返回这类推文的视频直链。"""
    api_url = tweet_url.replace("https://x.com/", "https://api.fxtwitter.com/", 1)
    resp = requests.get(api_url, headers=REQUEST_HEADERS, timeout=20)
    data = resp.json()
    if data.get("code") != 200 or not data.get("tweet"):
        raise DownloadError(f"备用接口返回 {data.get('code')}: {data.get('message')}")

    tweet = data["tweet"]
    author = tweet.get("author") or {}
    media_videos = (tweet.get("media") or {}).get("videos") or []

    videos = []
    for mv in media_videos:
        formats = []
        for f in mv.get("formats") or []:
            url = f.get("url") or ""
            if not url:
                continue
            is_hls = (f.get("container") == "m3u8") or ".m3u8" in url
            res_match = RESOLUTION_IN_URL_RE.search(url)
            width = int(res_match.group(1)) if res_match else None
            height = int(res_match.group(2)) if res_match else None
            formats.append({
                "format_id": f"{'hls' if is_hls else 'http'}-{(f.get('bitrate') or 0) // 1000}",
                "url": url,
                "ext": "m3u8" if is_hls else "mp4",
                "is_hls": is_hls,
                "width": width,
                "height": height,
                "resolution": f"{width}x{height}" if width and height else ("HLS" if is_hls else "未知"),
                "filesize": None,
                "tbr": (f.get("bitrate") or 0) / 1000 or None,
            })
        formats.sort(key=lambda f: ((f["height"] or 0), not f["is_hls"]), reverse=True)
        if not formats:
            continue
        best_mp4 = next((f for f in formats if not f["is_hls"]), formats[0])
        videos.append({
            "title": tweet.get("text") or "推特视频",
            "thumbnail": mv.get("thumbnail_url"),
            "duration": mv.get("duration"),
            "formats": formats,
            "best": best_mp4,
        })

    if not videos:
        raise DownloadError("该推文中没有找到可下载的视频")

    return {
        "tweet_url": tweet_url,
        "uploader": author.get("name") or author.get("screen_name"),
        "uploader_id": author.get("screen_name"),
        "description": tweet.get("text"),
        "videos": videos,
    }


def extract_video_info(tweet_url: str) -> dict:
    """用 yt-dlp 解析推文, 返回前端需要的结构化数据。"""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # 推文可能包含多个视频, 解析全部
        "playlist_items": "1-10",
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(tweet_url, download=False)

    entries = info.get("entries") or [info]
    videos = []
    for entry in entries:
        if not entry:
            continue
        formats = []
        for f in entry.get("formats") or []:
            url = f.get("url") or ""
            if not url:
                continue
            protocol = f.get("protocol") or ""
            is_hls = "m3u8" in protocol or url.endswith(".m3u8")
            formats.append({
                "format_id": f.get("format_id"),
                "url": url,
                "ext": "m3u8" if is_hls else (f.get("ext") or "mp4"),
                "is_hls": is_hls,
                "width": f.get("width"),
                "height": f.get("height"),
                "resolution": (
                    f"{f['width']}x{f['height']}"
                    if f.get("width") and f.get("height") else (f.get("format_id") or "未知")
                ),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "tbr": f.get("tbr"),
            })
        # 按清晰度从高到低排序, MP4 优先
        formats.sort(key=lambda f: ((f["height"] or 0), not f["is_hls"]), reverse=True)
        if not formats:
            continue
        best_mp4 = next((f for f in formats if not f["is_hls"]), formats[0])
        videos.append({
            "title": entry.get("title") or info.get("title") or "推特视频",
            "thumbnail": entry.get("thumbnail"),
            "duration": entry.get("duration"),
            "formats": formats,
            "best": best_mp4,
        })

    if not videos:
        raise DownloadError("该推文中没有找到可下载的视频")

    return {
        "tweet_url": tweet_url,
        "uploader": info.get("uploader") or info.get("uploader_id"),
        "uploader_id": info.get("uploader_id"),
        "description": info.get("description") or info.get("title"),
        "videos": videos,
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/parse")
def api_parse():
    data = request.get_json(silent=True) or {}
    raw_url = data.get("url", "")
    tweet_url, tweet_id = normalize_tweet_url(raw_url)
    if not tweet_url:
        return jsonify({"ok": False, "error": "链接格式不正确, 请输入形如 https://x.com/用户名/status/12345 的推文链接"}), 400

    try:
        result = extract_video_info(tweet_url)
    except DownloadError as e:
        msg = str(e)
        # 游客接口对受限/敏感内容推文会隐藏视频, 回退到 fxtwitter 再试一次
        try:
            result = extract_via_fxtwitter(tweet_url)
        except Exception:  # noqa: BLE001
            if "No video" in msg or "没有找到" in msg:
                friendly = "该推文中没有找到视频, 请确认链接对应的是视频帖子"
            elif "NSFW" in msg or "age" in msg.lower() or "login" in msg.lower() or "authorization" in msg.lower():
                friendly = "该推文需要登录才能查看(可能是受限/敏感内容), 暂时无法解析"
            elif "suspended" in msg.lower() or "not found" in msg.lower() or "deleted" in msg.lower():
                friendly = "推文不存在或已被删除"
            else:
                friendly = f"解析失败: {msg}"
            return jsonify({"ok": False, "error": friendly}), 422
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"解析出错: {e}"}), 500

    result["tweet_id"] = tweet_id
    return jsonify({"ok": True, "data": result})


def check_cdn_host(video_url: str):
    host = urllib.parse.urlparse(video_url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_PROXY_HOSTS):
        abort(403, "仅允许中转推特官方 CDN(video.twimg.com)上的视频")


@app.get("/api/stream")
def api_stream():
    """播放中转: 服务器从推特 CDN 取流再转发给浏览器。
    转发 Range 请求头以支持进度条拖动(206 分段响应)。"""
    video_url = request.args.get("url", "")
    check_cdn_host(video_url)

    upstream_headers = dict(REQUEST_HEADERS)
    if request.headers.get("Range"):
        upstream_headers["Range"] = request.headers["Range"]

    upstream = requests.get(video_url, headers=upstream_headers, stream=True, timeout=30)
    if upstream.status_code not in (200, 206):
        abort(502, f"上游返回 {upstream.status_code}")

    headers = {"Accept-Ranges": "bytes"}
    for h in ("Content-Type", "Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            headers[h] = upstream.headers[h]
    headers.setdefault("Content-Type", "video/mp4")

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
        status=upstream.status_code,
        headers=headers,
    )


@app.get("/api/download")
def api_download():
    """下载中转: 附带 Content-Disposition 响应头, 浏览器直接弹出另存为。"""
    video_url = request.args.get("url", "")
    filename = request.args.get("filename", "twitter_video.mp4")
    filename = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", filename)[:120] or "twitter_video.mp4"
    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"

    check_cdn_host(video_url)

    upstream = requests.get(video_url, headers=REQUEST_HEADERS, stream=True, timeout=30)
    if upstream.status_code != 200:
        abort(502, f"上游返回 {upstream.status_code}")

    quoted = urllib.parse.quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
    }
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
        headers=headers,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
