# Bilibili 电脑端 / 手机端缓存转换工具

一个 Python 脚本同时处理 Windows 电脑端和手机端 Bilibili 离线缓存，将视频与音频合并为常见的 MP4 文件。

## 功能

- 默认根据目录结构自动识别电脑端或手机端缓存。
- 可用 `--source desktop` 或 `--source mobile` 手动指定。
- 支持命令行参数、系统环境变量和 `.env`；命令行优先级最高。
- 电脑端按 BV 号归类，保留分集名称并生成 `00_课程信息.txt`。
- 电脑端仅在系统临时目录修复 M4S 前缀，不修改原始缓存，也不产生 `.bak` 文件。
- 手机端支持 `video.m4s + audio.m4s`，也支持旧版多段 `.blv` 缓存。
- 手机端可选生成 NFO、下载封面、转换 ASS 弹幕。
- 支持并发转换、跳过已有结果和 dry-run 预览。

## 支持的缓存结构

### Windows 电脑端

每个分集目录需要包含 `videoInfo.json` 或 `.videoInfo`，以及文件名标识中含 `300`（视频）和 `302`（音频）的 M4S：

```text
缓存目录/
└── 分集目录/
    ├── videoInfo.json        # 或 .videoInfo
    ├── ...-300....m4s        # 视频
    └── ...-302....m4s        # 音频
```

### 手机端

新版 M4S 缓存：

```text
缓存目录/
└── 视频目录/
    ├── entry.json
    ├── danmaku.xml           # 可选
    └── 80/                   # 清晰度编号可能不同
        ├── video.m4s
        └── audio.m4s
```

旧版 BLV 缓存也支持：

```text
视频目录/
├── entry.json
└── lua.*/
    ├── 0.blv
    └── 1.blv
```

输入路径可以指向单个视频目录，也可以指向包含多个视频目录的缓存根目录。

## 环境要求

- Python 3.9 或更高版本
- FFmpeg
- 可选：`danmaku2ass.py` 或 `danmaku2ass` 可执行文件（仅转换弹幕时需要）

脚本只使用 Python 标准库，不需要执行 `pip install`。

### Windows 安装 FFmpeg

从 [FFmpeg 官网](https://ffmpeg.org/download.html) 下载 Windows 版本，将 `ffmpeg.exe` 所在的 `bin` 目录加入系统 `Path`；也可以在 `.env` 的 `FFMPEG_PATH` 中填写完整路径。

## 快速开始（Windows 电脑端）

1. 将 `.env.example` 复制并改名为 `.env`。
2. 模板已使用 Windows Bilibili 客户端的默认缓存位置：

   ```dotenv
   BILIBILI_CACHE_DIR="%USERPROFILE%\Videos\bilibili"
   ```

3. 根据需要修改输出目录。
4. 在脚本目录运行：

   ```powershell
   python converter.py
   ```

`.env.example` 中提供了 Windows 路径，但 `converter.py` 本身没有硬编码缓存路径或输出路径。如果不使用 `.env`，必须通过 `-i` 和 `-o` 提供这两个路径。

## 自动识别与手动指定

不传 `--source`，或明确使用 `--source auto`，都会自动识别：

```bash
python converter.py -i "缓存目录" -o "输出目录"
python converter.py --source auto -i "缓存目录" -o "输出目录"
```

识别依据：

- `videoInfo.json` / `.videoInfo` → Windows 电脑端
- `entry.json` → 手机端

如果同一个输入根目录同时含有两种结构，自动模式会停止并要求明确指定：

```bash
python converter.py --source desktop -i "电脑端缓存目录" -o "输出目录"
python converter.py --source mobile -i "手机端缓存目录" -o "输出目录"
```

## 配置优先级

从高到低依次为：

1. 命令行参数
2. 当前系统环境变量
3. `.env` 文件
4. 程序的非路径默认值

例如 `.env` 中设置了 `MAX_WORKERS=3`，以下命令仍会使用 6 个并发任务：

```bash
python converter.py --threads 6
```

默认读取当前工作目录中的 `.env`。也可以指定其他文件：

```bash
python converter.py --env-file "D:\配置\bilibili.env"
```

## 命令行参数

```text
-s, --source auto|desktop|mobile  缓存类型，默认 auto
-i, --input PATH                 缓存目录
-o, --output PATH                MP4 输出目录
-f, --ffmpeg PATH                FFmpeg 路径或命令名
-t, --threads N                  并发任务数
--skip-existing                  跳过已有输出（默认）
--no-skip-existing               重新生成已有输出
--folder / --no-folder           手机端按标题建立文件夹
--danmaku / --no-danmaku         手机端转换 ASS 弹幕
--nfo / --no-nfo                 手机端生成 NFO 并尝试下载封面
--danmaku2ass PATH                danmaku2ass 路径
--dry-run / --no-dry-run         只预览任务，不调用 FFmpeg
--env-file PATH                  指定 .env 文件
```

随时可以查看脚本内置帮助：

```bash
python converter.py --help
```

## `.env` 配置项

| 配置项 | 对应参数 | 说明 |
|---|---|---|
| `BILIBILI_SOURCE` | `--source` | `auto`、`desktop` 或 `mobile` |
| `BILIBILI_CACHE_DIR` | `--input` | 缓存目录；无脚本内置路径 |
| `OUTPUT_MP4_DIR` | `--output` | 输出目录；无脚本内置路径 |
| `FFMPEG_PATH` | `--ffmpeg` | FFmpeg 路径，默认命令名 `ffmpeg` |
| `MAX_WORKERS` | `--threads` | 并发任务数 |
| `SKIP_EXISTING` | `--skip-existing` | 是否跳过已有输出 |
| `MOBILE_FOLDER` | `--folder` | 手机端是否建立标题文件夹 |
| `MOBILE_DANMAKU` | `--danmaku` | 是否转换弹幕 |
| `MOBILE_NFO` | `--nfo` | 是否生成 NFO 和封面 |
| `DANMAKU2ASS_PATH` | `--danmaku2ass` | 弹幕转换工具路径 |
| `DRY_RUN` | `--dry-run` | 是否只预览任务 |

布尔值可写为 `true/false`、`yes/no`、`on/off` 或 `1/0`。

## 使用示例

自动识别电脑端并转换：

```bash
python converter.py -i "D:\缓存\bilibili" -o "D:\视频\Bilibili转换"
```

手机端缓存，生成独立文件夹、NFO 和弹幕：

```bash
python converter.py --source mobile -i "D:\手机缓存" -o "D:\视频" --folder --nfo --danmaku --danmaku2ass "D:\工具\danmaku2ass.py"
```

只检查识别结果和待转换文件：

```bash
python converter.py -i "缓存目录" -o "输出目录" --dry-run
```

## 输出规则

电脑端按 BV 号整理：

```text
输出目录/
└── BVxxxxxxxxxx/
    ├── 00_课程信息.txt
    ├── 第一集.mp4
    └── 第二集.mp4
```

手机端默认输出 `标题-分集名.mp4`；标题和分集名相同时只输出 `标题.mp4`。启用 `--folder` 后输出到 `标题/分集名.mp4`。

## 安全与注意事项

- 转换不会修改原始缓存文件。
- 电脑端的 9 字节前缀修复只发生在系统临时目录，任务结束后自动清理。
- 不要在 Bilibili 客户端仍在下载同一视频时进行转换。
- NFO 封面下载需要网络；封面失败不会影响 MP4 和 NFO。
- 启用弹幕后，如果缺少 `danmaku.xml` 或 danmaku2ass，MP4 会保留，但该任务会报告附加文件失败。
- 工具仅用于转换你有权使用的本地离线缓存，请遵守版权和平台规则。

## 验证

运行内置测试：

```bash
python -m unittest -v
```

## 参考

- 手机端 M4S/BLV 缓存、弹幕、分文件夹及 NFO 功能参考并整合自 [kaixinol/BiliCache2MP4](https://github.com/kaixinol/BiliCache2MP4)。
- 电脑端缓存识别、BV 分组及九字节前缀处理行为参考了 [switch616/bilibiliConverted](https://github.com/switch616/bilibiliConverted)。

## 许可证

手机端参考项目 BiliCache2MP4 采用 GNU GPL v3。该整合项目按 GNU GPL v3 发布，完整条款见 `LICENSE`。
