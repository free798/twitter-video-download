# 推特(X)视频下载器

粘贴推文链接,一键解析出视频下载直链,支持**在线播放**和**多清晰度下载**的 Web 工具。

**默认模式下服务器只负责解析直链(每次仅几 KB 的 JSON 流量),播放和下载都由
浏览器直连推特 CDN(`video.twimg.com`)完成,不消耗服务器的视频流量。页面上
提供"服务器中转"开关,供无法直连推特 CDN(如国内无 VPN)的用户切换到服务器
中转线路观看和下载。**

## 功能

- 解析 `x.com` / `twitter.com` / `mobile.twitter.com` 视频、GIF 推文链接
- 展示推文作者、正文、视频封面、时长
- 列出全部清晰度(如 1280x720 / 640x360 / 480x270)及文件大小
- 页面内置播放器在线播放,可切换任意清晰度
- **双线路开关**(记忆在浏览器本地,切换后立即生效):
  - 直连模式(默认):播放/下载由浏览器直连推特 CDN,服务器零视频流量,
    要求用户网络能访问 `video.twimg.com`
  - 服务器中转模式:播放走 `/api/stream`(支持 Range 拖动进度条),下载走
    `/api/download`(自动弹出"另存为"),国内无需 VPN,但消耗服务器流量
- 复制视频 CDN 直链,可粘贴到任意播放器/下载工具使用
- 粘贴链接后自动解析,无需手动点击
- 白天/黑夜主题切换(默认跟随系统,记忆用户选择)
- 中文/英文界面切换(默认中文,记忆用户选择)

## 快速开始

```bash
cd twitter-video-downloader
pip install -r requirements.txt
python app.py
```

打开浏览器访问 `http://127.0.0.1:5000`,粘贴推文链接即可。

> 注意:服务器需要能正常访问 twitter.com / x.com。如在受限网络环境,
> 可通过环境变量走代理,例如:
>
> ```bash
> HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 python app.py
> ```

## API

### `POST /api/parse`

请求体:`{"url": "https://x.com/用户名/status/1234567890"}`

成功返回:

```json
{
  "ok": true,
  "data": {
    "tweet_id": "1234567890",
    "uploader": "作者昵称",
    "uploader_id": "作者ID",
    "description": "推文正文",
    "videos": [
      {
        "title": "视频标题",
        "thumbnail": "封面图URL",
        "duration": 30.5,
        "best": { "url": "最高清 MP4 直链", "resolution": "1280x720" },
        "formats": [
          { "url": "...", "resolution": "1280x720", "ext": "mp4", "filesize": 1048576, "is_hls": false }
        ]
      }
    ]
  }
}
```

失败返回 `{"ok": false, "error": "错误原因"}`(HTTP 400/422/500)。

### `GET /api/stream?url=<视频直链>`

播放中转代理(开关开启时前端播放器使用)。转发 `Range` 请求头,支持进度条拖动。

### `GET /api/download?url=<视频直链>&filename=<保存文件名>`

下载中转代理(开关开启时下载按钮使用),附带 `Content-Disposition` 触发"另存为"。

两个中转接口均只允许 `video.twimg.com` / `pbs.twimg.com` 域名,防止被当作任意 URL 代理滥用。

## 技术实现

- **后端**:Flask + [yt-dlp](https://github.com/yt-dlp/yt-dlp)(解析推特 GraphQL 接口拿到各码率 MP4/HLS 地址),
  中转接口为流式转发(64KB 分块),不占用服务器磁盘
- **前端**:原生 HTML/CSS/JS 单页,无构建依赖;全局 `no-referrer`(推特 CDN 对带第三方
  Referer 的请求返回 403)

## 限制

- 受限/敏感(NSFW)推文:推特游客接口会隐藏这类视频,本工具会自动回退到
  fxtwitter 公开 API 再解析一次,绝大多数情况可以成功
- 直连模式要求**用户自己的网络**能访问 `video.twimg.com`(境内网络需开启代理/VPN,
  或改用服务器中转开关)
- 纯图片推文会提示"没有找到视频"
