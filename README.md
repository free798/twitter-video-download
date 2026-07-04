# Twitter 视频下载器 (twitter-video-download)

一个免费的在线 Twitter/X 视频下载工具。粘贴推文链接即可解析出视频的多种清晰度直链,并支持一键下载。

## 功能特性

- 支持 `twitter.com` 和 `x.com` 推文链接
- 基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 解析,稳定可靠
- 提供多档分辨率(含原始画质)选择
- 后端代理下载,浏览器可直接保存为 mp4 文件
- 深色现代化界面,移动端自适应
- 无需登录、无需安装客户端

## 技术栈

- 后端:Python + FastAPI + yt-dlp
- 前端:原生 HTML / CSS / JavaScript(无构建步骤)

## 快速开始

```bash
# 1. 安装依赖(建议使用虚拟环境)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 启动服务
uvicorn main:app --host 0.0.0.0 --port 8000
```

打开浏览器访问 <http://localhost:8000> 即可使用。

## API 说明

### `POST /api/parse`

解析推文中的视频信息。

请求体:

```json
{ "url": "https://x.com/用户名/status/1234567890" }
```

响应示例:

```json
{
  "title": "视频标题",
  "uploader": "作者",
  "thumbnail": "https://pbs.twimg.com/...",
  "duration": 30.5,
  "formats": [
    {
      "format_id": "http-2176",
      "resolution": "1280x720",
      "ext": "mp4",
      "url": "https://video.twimg.com/...",
      "filesize": 1234567
    }
  ]
}
```

### `GET /api/download?url=...&filename=...`

代理下载视频文件(仅允许 `*.twimg.com` 域名,防止 SSRF)。

## 免责声明

本工具仅供个人学习与研究使用,请尊重视频原作者的版权,勿用于任何商业或侵权用途。
