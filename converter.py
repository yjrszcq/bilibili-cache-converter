#!/usr/bin/env python3
"""Convert Bilibili desktop or mobile caches into playable media files."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Mapping, Optional, Sequence
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
WINDOWS_ENV_VAR = re.compile(r"%([^%]+)%")
COURSE_INFO_LOCK = threading.Lock()


@dataclass(frozen=True)
class Config:
    source: str
    input_dir: Path
    output_dir: Path
    ffmpeg: str
    workers: int
    skip_existing: bool
    folder: bool
    danmaku: bool
    nfo: bool
    danmaku2ass: Optional[str]
    dry_run: bool


@dataclass(frozen=True)
class ConversionJob:
    source: str
    item_dir: Path
    order: int
    title: str
    part: str
    bvid: str
    group_title: str
    course_link: str
    video_files: tuple[Path, ...]
    audio_file: Optional[Path]
    metadata: dict


def load_dotenv(path: Path) -> dict[str, str]:
    """Read a small, dependency-free subset of dotenv syntax."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number} 不是有效的 KEY=VALUE 配置")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"{path}:{line_number} 的配置名为空")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"{name} 必须是 true/false、yes/no、on/off 或 1/0")


def expand_path(
    value: str, environ: Mapping[str, str], base_dir: Optional[Path] = None
) -> Path:
    def replace_windows_var(match: re.Match[str]) -> str:
        return environ.get(match.group(1), match.group(0))

    expanded = WINDOWS_ENV_VAR.sub(replace_windows_var, value)
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    path = Path(expanded)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将 Bilibili Windows 电脑端或手机端缓存转换为 MP4"
    )
    parser.add_argument(
        "--env-file", type=Path, help=".env 文件路径，默认读取当前目录的 .env"
    )
    parser.add_argument(
        "-s",
        "--source",
        choices=("auto", "desktop", "mobile"),
        help="缓存来源；auto 自动识别，desktop=Windows 电脑端，mobile=手机端；默认 auto",
    )
    parser.add_argument("-i", "--input", dest="input_dir", help="Bilibili 缓存目录")
    parser.add_argument("-o", "--output", dest="output_dir", help="MP4 输出目录")
    parser.add_argument("-f", "--ffmpeg", help="FFmpeg 可执行文件路径或命令名")
    parser.add_argument("-t", "--threads", type=int, help="并发转换数量")
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="跳过已存在的输出文件（可用 --no-skip-existing 关闭）",
    )
    parser.add_argument(
        "--folder",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="手机端按视频标题创建独立输出文件夹",
    )
    parser.add_argument(
        "--danmaku",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="手机端将 danmaku.xml 转换为 ASS",
    )
    parser.add_argument(
        "--nfo",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="手机端生成 NFO 元数据",
    )
    parser.add_argument("--danmaku2ass", help="danmaku2ass.py 或可执行文件路径")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="仅显示将要执行的任务，不调用 FFmpeg",
    )
    return parser


def _pick(cli_value, env: Mapping[str, str], name: str, default=None):
    return cli_value if cli_value is not None else env.get(name, default)


def build_config(
    argv: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
) -> Config:
    argv = list(argv if argv is not None else sys.argv[1:])
    process_env = dict(os.environ if environ is None else environ)
    working_dir = Path.cwd() if cwd is None else cwd

    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument("--env-file", type=Path)
    env_args, _ = env_parser.parse_known_args(argv)
    env_path = env_args.env_file or working_dir / ".env"

    settings = load_dotenv(env_path)
    settings.update(process_env)
    args = create_parser().parse_args(argv)

    source = _pick(args.source, settings, "BILIBILI_SOURCE", "auto")
    if source not in {"auto", "desktop", "mobile"}:
        raise ValueError("BILIBILI_SOURCE 必须是 auto、desktop 或 mobile")

    input_value = _pick(args.input_dir, settings, "BILIBILI_CACHE_DIR")
    output_value = _pick(args.output_dir, settings, "OUTPUT_MP4_DIR")
    if not input_value:
        raise ValueError("缺少缓存目录：请使用 -i/--input 或 BILIBILI_CACHE_DIR")
    if not output_value:
        raise ValueError("缺少输出目录：请使用 -o/--output 或 OUTPUT_MP4_DIR")

    workers_value = _pick(
        args.threads, settings, "MAX_WORKERS", str(multiprocessing.cpu_count())
    )
    try:
        workers = int(workers_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("MAX_WORKERS/--threads 必须是整数") from exc
    if workers < 1:
        raise ValueError("MAX_WORKERS/--threads 必须大于 0")

    def configured_bool(
        cli_value: Optional[bool], env_name: str, default: bool
    ) -> bool:
        if cli_value is not None:
            return cli_value
        if env_name not in settings:
            return default
        return parse_bool(settings[env_name], env_name)

    ffmpeg = str(_pick(args.ffmpeg, settings, "FFMPEG_PATH", "ffmpeg"))
    danmaku2ass = _pick(args.danmaku2ass, settings, "DANMAKU2ASS_PATH")
    return Config(
        source=source,
        input_dir=expand_path(str(input_value), settings, working_dir),
        output_dir=expand_path(str(output_value), settings, working_dir),
        ffmpeg=ffmpeg,
        workers=workers,
        skip_existing=configured_bool(args.skip_existing, "SKIP_EXISTING", True),
        folder=configured_bool(args.folder, "MOBILE_FOLDER", False),
        danmaku=configured_bool(args.danmaku, "MOBILE_DANMAKU", False),
        nfo=configured_bool(args.nfo, "MOBILE_NFO", False),
        danmaku2ass=str(danmaku2ass) if danmaku2ass else None,
        dry_run=configured_bool(args.dry_run, "DRY_RUN", False),
    )


def validate_config(config: Config) -> None:
    if not config.input_dir.is_dir():
        raise ValueError(f"缓存目录不存在：{config.input_dir}")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    if not shutil.which(config.ffmpeg):
        raise ValueError(f"找不到 FFmpeg：{config.ffmpeg}")


def clean_name(name: object, fallback: str = "未命名视频", max_length: int = 120) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', " ", str(name or fallback))
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    return (cleaned or fallback)[:max_length]


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取元数据 {path}：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"元数据不是 JSON 对象：{path}")
    return data


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def discover_desktop_jobs(input_dir: Path) -> list[ConversionJob]:
    item_dirs = {
        metadata_path.parent
        for metadata_name in ("videoInfo.json", ".videoInfo")
        for metadata_path in input_dir.rglob(metadata_name)
    }
    jobs: list[ConversionJob] = []
    for item_dir in sorted(item_dirs):
        metadata_path = next(
            (
                item_dir / name
                for name in ("videoInfo.json", ".videoInfo")
                if (item_dir / name).is_file()
            ),
            None,
        )
        if metadata_path is None:
            continue
        data = read_json(metadata_path)

        video_file = None
        audio_file = None
        for media_file in sorted(item_dir.glob("*.m4s")):
            marker = media_file.stem.split("-")[-1]
            if video_file is None and "300" in marker:
                video_file = media_file
            if audio_file is None and "302" in marker:
                audio_file = media_file
        if video_file is None or audio_file is None:
            continue

        bvid = clean_name(data.get("bvid"), "UnknownBV")
        jobs.append(
            ConversionJob(
                source="desktop",
                item_dir=item_dir,
                order=_as_int(data.get("p")),
                title=clean_name(data.get("title"), item_dir.name),
                part=clean_name(
                    data.get("tabName", data.get("title")), item_dir.name
                ),
                bvid=bvid,
                group_title=clean_name(data.get("groupTitle"), "未命名课程"),
                course_link=(
                    f"https://www.bilibili.com/video/{bvid}"
                    if bvid != "UnknownBV"
                    else ""
                ),
                video_files=(video_file,),
                audio_file=audio_file,
                metadata=data,
            )
        )
    return sorted(jobs, key=lambda job: (job.bvid, job.order, job.part))


def discover_mobile_jobs(input_dir: Path) -> list[ConversionJob]:
    jobs: list[ConversionJob] = []
    for metadata_path in sorted(input_dir.rglob("entry.json")):
        item_dir = metadata_path.parent
        data = read_json(metadata_path)
        page_data = data.get("page_data")
        if not isinstance(page_data, dict):
            page_data = {}

        videos = tuple(sorted(item_dir.rglob("video.m4s")))
        audio_file = next(iter(sorted(item_dir.rglob("audio.m4s"))), None)
        if not videos:
            legacy_parts: list[Path] = []
            for legacy_dir in sorted(item_dir.rglob("lua.*")):
                if legacy_dir.is_dir():
                    legacy_parts.extend(sorted(legacy_dir.rglob("*.blv")))
            videos = tuple(legacy_parts)
            audio_file = None
        if not videos:
            continue

        title = clean_name(data.get("title"), item_dir.name)
        bvid_value = data.get("bvid")
        if not bvid_value and data.get("avid") is not None:
            bvid_value = f"av{data['avid']}"
        jobs.append(
            ConversionJob(
                source="mobile",
                item_dir=item_dir,
                order=_as_int(page_data.get("page", data.get("page", 0))),
                title=title,
                part=clean_name(page_data.get("part"), title),
                bvid=clean_name(bvid_value, "UnknownBV"),
                group_title=title,
                course_link="",
                video_files=videos,
                audio_file=audio_file,
                metadata=data,
            )
        )
    return sorted(jobs, key=lambda job: (job.title, job.order, job.part))


def detect_source(input_dir: Path) -> str:
    has_desktop = any(input_dir.rglob("videoInfo.json")) or any(
        input_dir.rglob(".videoInfo")
    )
    has_mobile = any(input_dir.rglob("entry.json"))
    if has_desktop and has_mobile:
        raise ValueError(
            "输入目录同时包含电脑端和手机端缓存，请使用 --source desktop 或 "
            "--source mobile 明确指定"
        )
    if has_desktop:
        return "desktop"
    if has_mobile:
        return "mobile"
    raise ValueError(
        "无法自动识别缓存类型：未找到 videoInfo.json、.videoInfo 或 entry.json；"
        "请检查 -i 路径或手动指定 --source"
    )


def resolve_source(config: Config) -> str:
    return detect_source(config.input_dir) if config.source == "auto" else config.source


def discover_jobs(config: Config, source: Optional[str] = None) -> list[ConversionJob]:
    resolved_source = source or resolve_source(config)
    if resolved_source == "desktop":
        return discover_desktop_jobs(config.input_dir)
    return discover_mobile_jobs(config.input_dir)


def output_path_for(job: ConversionJob, config: Config) -> Path:
    if job.source == "desktop":
        return config.output_dir / job.bvid / f"{job.part}.mp4"
    if config.folder:
        return config.output_dir / job.title / f"{job.part}.mp4"
    filename = job.title if job.part == job.title else f"{job.title}-{job.part}"
    return config.output_dir / f"{filename}.mp4"


def write_course_info(job: ConversionJob, output_file: Path) -> None:
    if job.source != "desktop":
        return
    info_file = output_file.parent / "00_课程信息.txt"
    with COURSE_INFO_LOCK:
        if info_file.exists():
            return
        lines = [job.group_title]
        if job.course_link:
            lines.append(f"课程链接：{job.course_link}")
        info_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def download_cover(url: str, destination: Path) -> bool:
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        return False
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            destination.write_bytes(response.read())
        return True
    except (OSError, ValueError):
        return False


def write_mobile_nfo(job: ConversionJob, output_file: Path, folder_mode: bool) -> None:
    movie = ET.Element("movie")
    ET.SubElement(movie, "title").text = job.title
    owner_name = job.metadata.get("owner_name")
    owner_id = job.metadata.get("owner_id")
    if owner_name:
        credits = str(owner_name)
        if owner_id:
            credits += f" [{owner_id}]"
        ET.SubElement(movie, "credits").text = credits
    if job.bvid != "UnknownBV":
        ET.SubElement(movie, "uniqueid", {"type": "bilibili"}).text = job.bvid

    timestamp = job.metadata.get("time_update_stamp")
    if timestamp is not None:
        try:
            updated = datetime.fromtimestamp(float(timestamp) / 1000)
            ET.SubElement(movie, "year").text = str(updated.year)
            ET.SubElement(movie, "aired").text = updated.strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            pass

    nfo_file = output_file.with_suffix(".nfo")
    ET.ElementTree(movie).write(nfo_file, encoding="utf-8", xml_declaration=True)

    cover_url = job.metadata.get("cover")
    if isinstance(cover_url, str) and cover_url:
        cover_file = (
            output_file.parent / "cover.jpg"
            if folder_mode
            else output_file.with_name(f"{output_file.stem}-cover.jpg")
        )
        if not cover_file.exists():
            download_cover(cover_url, cover_file)


def resolve_danmaku_command(config: Config) -> list[str]:
    candidate = config.danmaku2ass
    if candidate:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_file():
            return [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
        executable = shutil.which(candidate)
        if executable:
            return [executable]
        raise ValueError(f"找不到 danmaku2ass：{candidate}")

    project_dir = Path(__file__).resolve().parent
    for name in ("danmaku2ass.exe", "danmaku2ass.py"):
        path = project_dir / name
        if path.is_file():
            return [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
    executable = shutil.which("danmaku2ass")
    if executable:
        return [executable]
    raise ValueError("启用了弹幕转换，但找不到 danmaku2ass；请配置 --danmaku2ass")


def convert_mobile_danmaku(
    job: ConversionJob, config: Config, output_file: Path
) -> None:
    danmaku_file = next(iter(sorted(job.item_dir.rglob("danmaku.xml"))), None)
    if danmaku_file is None:
        raise ValueError(f"未找到 danmaku.xml：{job.item_dir}")
    page_data = job.metadata.get("page_data")
    if not isinstance(page_data, dict):
        page_data = {}
    width = _as_int(page_data.get("width"), 1920)
    height = _as_int(page_data.get("height"), 1080)
    command = resolve_danmaku_command(config) + [
        str(danmaku_file),
        "-s",
        f"{width}x{height}",
        "-fn",
        "微软雅黑",
        "-fs",
        "48",
        "-a",
        "0.8",
        "-dm",
        "5",
        "-ds",
        "5",
        "-o",
        str(output_file.with_suffix(".ass")),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        error = result.stderr.strip()[-1000:] or f"退出码 {result.returncode}"
        raise ValueError(f"弹幕转换失败：{error}")


def repair_desktop_media(source: Path, destination: Path) -> None:
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        prefix = input_file.read(9)
        if len(prefix) != 9:
            raise ValueError(f"电脑端缓存文件不足 9 字节：{source}")
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)


def _write_concat_list(parts: Sequence[Path], list_path: Path) -> None:
    lines = []
    for part in parts:
        escaped = str(part).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_mobile_command(
    job: ConversionJob, config: Config, output_file: Path, temp_dir: Path
) -> list[str]:
    command = [config.ffmpeg, "-hide_banner", "-loglevel", "error"]
    if len(job.video_files) > 1 and job.audio_file is None:
        concat_file = temp_dir / "concat.txt"
        _write_concat_list(job.video_files, concat_file)
        command.extend(["-f", "concat", "-safe", "0", "-i", str(concat_file)])
    else:
        command.extend(["-i", str(job.video_files[0])])
        if job.audio_file is not None:
            command.extend(["-i", str(job.audio_file)])
    command.extend(["-c", "copy", "-y", str(output_file)])
    return command


def build_desktop_command(
    job: ConversionJob, config: Config, output_file: Path, temp_dir: Path
) -> list[str]:
    if job.audio_file is None:
        raise ValueError(f"电脑端缓存缺少音频：{job.item_dir}")
    repaired_video = temp_dir / "video.m4s"
    repaired_audio = temp_dir / "audio.m4s"
    repair_desktop_media(job.video_files[0], repaired_video)
    repair_desktop_media(job.audio_file, repaired_audio)
    return [
        config.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(repaired_video),
        "-i",
        str(repaired_audio),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-y",
        str(output_file),
    ]


def convert_job(job: ConversionJob, config: Config) -> tuple[bool, str]:
    output_file = output_path_for(job, config)
    if config.skip_existing and output_file.exists():
        return True, f"跳过已存在：{output_file}"
    if config.dry_run:
        inputs = ", ".join(str(path) for path in job.video_files)
        if job.audio_file:
            inputs += f", {job.audio_file}"
        return True, f"计划转换：{inputs} -> {output_file}"

    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="bilibili-converter-") as temp_name:
            temp_dir = Path(temp_name)
            if job.source == "desktop":
                command = build_desktop_command(job, config, output_file, temp_dir)
            else:
                command = build_mobile_command(job, config, output_file, temp_dir)
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
    except (OSError, ValueError) as exc:
        return False, f"转换失败：{job.part}：{exc}"

    if result.returncode != 0:
        error = result.stderr.strip()[-1000:] or f"退出码 {result.returncode}"
        return False, f"转换失败：{job.part}：{error}"
    if not output_file.is_file():
        return False, f"转换失败：FFmpeg 未生成 {output_file}"

    write_course_info(job, output_file)
    if job.source == "mobile":
        try:
            if config.nfo:
                write_mobile_nfo(job, output_file, config.folder)
            if config.danmaku:
                convert_mobile_danmaku(job, config, output_file)
        except (OSError, ValueError) as exc:
            return False, f"视频已生成，但附加文件失败：{job.part}：{exc}"
    return True, f"转换成功：{output_file}"


def run_conversions(jobs: Sequence[ConversionJob], config: Config) -> int:
    failures = 0
    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        futures = [executor.submit(convert_job, job, config) for job in jobs]
        for future in as_completed(futures):
            try:
                success, message = future.result()
            except Exception as exc:
                success, message = False, f"转换任务异常：{exc}"
            print(message, file=sys.stdout if success else sys.stderr)
            if not success:
                failures += 1
    return failures


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        config = build_config(argv)
        validate_config(config)
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    try:
        source = resolve_source(config)
    except ValueError as exc:
        print(f"识别错误：{exc}", file=sys.stderr)
        return 2
    source_label = "电脑端" if source == "desktop" else "手机端"
    mode_note = "（自动识别）" if config.source == "auto" else "（手动指定）"
    print(
        f"来源={source_label}{mode_note} 输入={config.input_dir} "
        f"输出={config.output_dir} 并发={config.workers}"
    )
    jobs = discover_jobs(config, source)
    if not jobs:
        print("未找到可转换的缓存项目", file=sys.stderr)
        return 1
    print(f"找到 {len(jobs)} 个可转换项目")
    failures = run_conversions(jobs, config)
    if failures:
        print(f"完成，但有 {failures} 个项目失败", file=sys.stderr)
        return 1
    print("全部任务执行完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
