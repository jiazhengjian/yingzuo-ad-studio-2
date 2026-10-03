"""Local deterministic media operations; subprocesses always receive argument arrays."""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


class MediaCanceled(MediaError):
    pass


def run(command, *, cwd=None, canceled=None, timeout=300):
    process = subprocess.Popen([str(x) for x in command], cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    import time
    started = time.monotonic()
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if canceled and canceled():
                    raise MediaCanceled("合成已停止；已生成素材仍保留")
                if time.monotonic() - started > timeout:
                    raise MediaError("媒体处理超时")
        if process.returncode:
            # Do not expose arbitrary local filesystem paths or long FFmpeg output to the UI.
            raise MediaError("媒体处理失败，请检查文件是否可解码、编码器与中文字幕字体是否可用")
        return stdout, stderr
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def probe(path: Path, ffmpeg_path, canceled=None):
    path = Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise MediaError("媒体文件不存在或为空")
    stdout, stderr = run([ffmpeg_path, "-hide_banner", "-nostdin", "-v", "info", "-i", path,
                          "-map", "0:v:0?", "-map", "0:a:0?", "-progress", "pipe:1",
                          "-nostats", "-f", "null", "-"], canceled=canceled)
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    video = next((line for line in stderr.splitlines() if "Stream #0:" in line and "Video:" in line), "")
    dims = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video)
    fps = re.search(r"\b(\d+(?:\.\d+)?) fps\b", video)
    frames = re.findall(r"^frame=(\d+)$", stdout, re.MULTILINE)
    result = {
        "duration_seconds": sum(float(v) * factor for v, factor in zip(duration.groups(), (3600, 60, 1))) if duration else 0,
        "has_video": bool(video), "has_audio": bool(re.search(r"Stream #0:.*Audio:", stderr)),
        "width": int(dims[1]) if dims else None, "height": int(dims[2]) if dims else None,
        "fps": float(fps[1]) if fps else None, "frames": int(frames[-1]) if frames else None,
    }
    if result["has_video"] and result["frames"] and result["fps"]:
        result["video_duration_seconds"] = result["frames"] / result["fps"]
    if result["duration_seconds"] <= 0:
        raise MediaError("无法确定媒体实际时长")
    return result


def escape_ass(text):
    # Backslashes and braces cannot introduce ASS override tags. Newline is the only escape we emit.
    return str(text).replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", "").replace("\n", r"\N")


def ass_time(frame, fps):
    centiseconds = round(frame * 100 / fps)
    hours, remaining = divmod(centiseconds, 360000)
    minutes, remaining = divmod(remaining, 6000)
    seconds, cents = divmod(remaining, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{cents:02}"


def caption_cues(script, audio_seconds, fps=24):
    """Conservative sentence timing fallback, explicitly NOT word-level forced alignment."""
    parts = [x.strip() for x in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", script) if x.strip()]
    parts = [part[i:i + 24] for part in parts for i in range(0, len(part), 24)]
    if not parts:
        return []
    total_frames = max(1, round(audio_seconds * fps))
    weights = [max(1, len(re.sub(r"\s", "", part))) for part in parts]
    cues, accumulated, cursor = [], 0, 0
    for index, (part, weight) in enumerate(zip(parts, weights)):
        accumulated += weight
        end = total_frames if index == len(parts) - 1 else round(total_frames * accumulated / sum(weights))
        if end > cursor:
            cues.append({"start_frame": cursor, "end_frame": end, "text": part})
        cursor = end
    return cues


def write_ass(path, cues, total_frames, fps=24, font="PingFang SC"):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},38,&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,45,45,175,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for cue in cues:
        start, end = int(cue["start_frame"]), min(total_frames, int(cue["end_frame"]))
        if start < end:
            lines.append(f"Dialogue: 0,{ass_time(start, fps)},{ass_time(end, fps)},Caption,,0,0,0,,{escape_ass(cue['text'])}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def permits_missing_native_audio(spec):
    """Only an explicit approved no-speech choice permits a silent source."""
    production = spec.get("production")
    return (spec.get("audio_strategy") == "native" and isinstance(production, dict)
            and production.get("speech_mode") == "none")


def compose(manifest, asset_paths, destination: Path, ffmpeg_path, canceled=None, font="PingFang SC"):
    """Always render to a fresh directory; no global props, filters, or mutable outputs."""
    # Retired end-card metadata in historical renders must not create new overlays.
    manifest = {key: value for key, value in manifest.items() if key != "cta"}
    destination = Path(destination).resolve()
    if destination.exists():
        raise MediaError("输出文件已经存在；请使用新的版本路径")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = destination.parent / "compose_work"
    work.mkdir(exist_ok=False)
    fps = int(manifest["fps"])
    if fps != 24 or manifest["width"] != 720 or manifest["height"] != 1280:
        raise MediaError("P0仅支持720×1280 / 24fps")
    if not manifest["segments"]:
        raise MediaError("没有可合成片段")
    normalized, total, local_silence_assets = [], 0, []
    for i, segment in enumerate(manifest["segments"]):
        if canceled and canceled():
            raise MediaCanceled("制作已取消")
        frames = int(segment["source_out_frame"]) - int(segment["source_in_frame"])
        if frames <= 0 or int(segment["timeline_in_frame"]) != total or int(segment["timeline_out_frame"]) != total + frames:
            raise MediaError("时间清单存在缺口或长度错误")
        source = Path(asset_paths[segment["asset_id"]]).resolve()
        info = probe(source, ffmpeg_path, canceled)
        if not info["has_video"] or info.get("video_duration_seconds", info["duration_seconds"]) + 1 / fps < segment["source_out_frame"] / fps:
            raise MediaError("视频片段比计划短，不能补假画面或静帧凑数")
        name = f"segment_{i:03d}.mp4"
        path = work / name
        # Convert cadence before scaling: this FFmpeg build can otherwise discard the
        # last input frame when scale feeds fps at EOF (verified with a 24-frame clip).
        vf = "fps=24,scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p"
        command = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-i", source]
        add_silence = manifest["audio_strategy"] == "native" and not info["has_audio"]
        if add_silence:
            if not permits_missing_native_audio(manifest):
                raise MediaError("原生口播片段缺少音轨")
            # This deterministic silent track only makes the concat inputs
            # compatible. It is never a replacement for expected speech.
            command += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
            local_silence_assets.append(segment["asset_id"])
        command += ["-ss", str(segment["source_in_frame"] / fps), "-t", str(frames / fps),
                   "-vf", vf, "-frames:v", str(frames), "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "2"]
        if manifest["audio_strategy"] == "native":
            if add_silence:
                command += ["-map", "0:v:0", "-map", "1:a:0"]
            command += ["-c:a", "aac", "-ar", "44100", "-ac", "2"]
        else:
            command += ["-an"]
        run([*command, path], canceled=canceled)
        normalized.append(name)
        total += frames
    if total != int(manifest["total_frames"]):
        raise MediaError("成片帧数与清单不一致")
    write_ass(work / "captions.ass", manifest.get("captions", []), total, fps, font)
    command = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error"]
    if manifest["audio_strategy"] == "voiceover":
        # These silent intermediate clips have no AAC priming timestamps.
        (work / "concat.txt").write_text("\n".join(f"file '{name}'" for name in normalized) + "\n")
        run([ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-f", "concat", "-safe", "1",
             "-i", "concat.txt", "-c", "copy", "base.mp4"], cwd=work, canceled=canceled)
        audio_path = Path(asset_paths[manifest["audio_asset_id"]]).resolve()
        audio_info = probe(audio_path, ffmpeg_path, canceled)
        if not audio_info["has_audio"] or audio_info["duration_seconds"] > total / fps + 0.05:
            raise MediaError("配音超过视频时间线；请缩短脚本后重新规划，不能截断台词")
        command += ["-i", "base.mp4", "-i", audio_path, "-filter_complex", f"[1:a]apad,atrim=duration={total / fps}[a]",
                    "-map", "0:v:0", "-map", "[a]", "-vf", "ass=filename=captions.ass"]
    else:
        # Decode each AAC track separately so encoder priming is removed before
        # concatenation. Stream-copy concatenation offsets the first video frame
        # and can drop it at the requested endpoint or accumulate audio drift.
        filters, streams = [], []
        for i, (name, segment) in enumerate(zip(normalized, manifest["segments"])):
            command += ["-i", name]
            duration = (int(segment["source_out_frame"]) - int(segment["source_in_frame"])) / fps
            filters += [f"[{i}:v]setpts=PTS-STARTPTS[v{i}]",
                        f"[{i}:a]atrim=duration={duration},asetpts=PTS-STARTPTS[a{i}]"]
            streams.append(f"[v{i}][a{i}]")
        filters += [f"{''.join(streams)}concat=n={len(normalized)}:v=1:a=1[video][native_audio]",
                    "[video]ass=filename=captions.ass[v]",
                    "[native_audio]alimiter=limit=0.794328:level=false:latency=true[a]"]
        command += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]"]
    command += ["-t", str(total / fps), "-frames:v", str(total), "-r", str(fps), "-fps_mode", "cfr",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-movflags", "+faststart", destination]
    run(command, cwd=work, canceled=canceled)
    info = probe(destination, ffmpeg_path, canceled)
    if (not info["has_audio"] or info["frames"] != total or (info["width"], info["height"]) != (720, 1280)
            or abs((info.get("fps") or 0) - fps) > .01):
        raise MediaError("导出未通过音视频规格检查")
    if permits_missing_native_audio(manifest):
        # Source IDs identify exactly which missing tracks were supplemented;
        # neither their original files nor their provider metadata is rewritten.
        info["local_silence_asset_ids"] = local_silence_assets
        manifest["local_silence_asset_ids"] = local_silence_assets
    (destination.parent / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return info
