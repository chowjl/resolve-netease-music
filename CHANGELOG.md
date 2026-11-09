# 更新日志

## [v1.0.6](https://github.com/chowjl/resolve-netease-music/releases/tag/v1.0.6) · 2026-11-09

- 安装程序支持读取同目录下的 FFmpeg ZIP。可在目标电脑上离线完成安装，避开下载超时或 504。

## [v1.0.5](https://github.com/chowjl/resolve-netease-music/releases/tag/v1.0.5) · 2026-11-09

- 双击歌曲时直接解码并启动试听，避免后台完成通知丢失后一直显示「准备中」。
- 试听用的 FFmpeg 解码超时时会显示错误提示。

## [v1.0.4](https://github.com/chowjl/resolve-netease-music/releases/tag/v1.0.4) · 2026-11-09

- 双击歌曲试听时，后台解码完成后直接通知播放器启动。进度刷新也可以接收结果，避免界面长时间停在「准备中」。

## [v1.0.3](https://github.com/chowjl/resolve-netease-music/releases/tag/v1.0.3) · 2026-11-09

- 打开试听面板、点击歌曲或开始试听时，将键盘焦点交给面板。
- Windows 仍将达芬奇主窗口视为活动窗口时，鼠标位于试听面板上也可以用空格控制试听。

## [v1.0.2](https://github.com/chowjl/resolve-netease-music/releases/tag/v1.0.2) · 2026-11-09

- 修正试听面板的空格键。面板有焦点时，按空格可开始试听、暂停或继续。长按只触发一次。
- 搜索框和路径输入框有焦点时，空格用于输入。

## [v1.0.1](https://github.com/chowjl/resolve-netease-music/releases/tag/v1.0.1) · 2026-11-09

- 刷新列表时扫描下载目录及各级子文件夹，所有歌曲按修改时间统一排序。
- 跳过默认的 `网易云配乐_MP3` 输出子文件夹，避免转码副本重复出现。

## [v1.0.0](https://github.com/chowjl/resolve-netease-music/releases/tag/v1.0.0) · 2026-11-09

- 首次发布 Windows 安装包，适用于 DaVinci Resolve Studio 21.2。
- 支持文件名搜索、多选、试听、按需加载波形及 A/B 选段。
- 支持 NCM 转 MP3、设置 MP3 保存目录，以及批量导入「网易云音乐」素材箱。
