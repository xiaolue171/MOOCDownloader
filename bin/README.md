# FFmpeg

源码仓库不提交 FFmpeg 可执行文件。

从 https://ffmpeg.org/download.html 选择 Windows 构建，将其中的 `ffmpeg.exe`
复制到此目录，供 HLS 视频合并、分辨率检查和本地打包使用。也可以自行编译 FFmpeg。
源码运行还会检查系统 PATH；完整安装包的构建要求此目录存在 `ffmpeg.exe`。

应用通过独立进程调用 FFmpeg，不会对下载视频重新编码。公开分发包含 FFmpeg 的安装包时，
需同时准备所选构建的许可证及完整对应源码材料，包含适用的外部库和构建信息。

本地验证过的构建为 Gyan 的 FFmpeg 9.0.1 essentials，启用了 GPLv3。
版本和构建参数可用 `ffmpeg.exe -version`、`ffmpeg.exe -buildconf`、`ffmpeg.exe -L` 查看。
