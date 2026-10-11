# 本地运行与打包

开发环境：64 位 Windows、Python 3.14、Inno Setup 6。

```powershell
python -m pip install -r requirements.txt
python main.py
```

源码仓库不包含 FFmpeg 二进制。可从 https://ffmpeg.org/download.html 选择 Windows 构建，
将 `ffmpeg.exe` 放入 `bin/`。源码运行也会检查 PATH。当前 2.0.2 完整安装包仍使用
Gyan FFmpeg 9.0.1 essentials，未采用此前尝试构建的精简组件。

安装 Inno Setup 6 后运行 `build.bat`。脚本安装构建依赖，生成文件夹版程序，
再生成 `release/MOOCDownloader-Setup-2.0.2-x64.exe`。打包必须存在 `bin/ffmpeg.exe`。

应用源码采用 MIT。FFmpeg 为独立进程组件，当前构建采用 GPL-3.0-or-later，
许可证、准确的构建版本与上游源码提交见 `THIRD_PARTY_NOTICES.txt` 和 `licenses/`。
这些来源说明不等于完整对应源码包：当前仍未收集 Gyan 构建中全部静态链接外部库的
对应源码与构建材料。公开分发完整安装包前，这项分发材料仍需发布者落实。

程序功能、源码整理和安装验证与上述分发材料是不同检查项；安装验证通过不表示
完整对应源码材料已经齐备。本次没有重新编译或替换 FFmpeg。
