"""Render library: ffmpeg wrappers replacing vendor/render/{concat,mux,freeze}.sh.

Not a worker — pure functions returning the ffmpeg command that was (or would be)
run. Each function validates inputs, runs ffmpeg via subprocess with a timeout
(kills the child on expiry), and raises RenderError with the last 500 chars of
ffmpeg stderr on failure. dry_run=True returns the command list without executing.

Env:
    FFMPEG_BIN  ffmpeg executable  (default "ffmpeg")
    FFPROBE_BIN ffprobe executable (default "ffprobe")
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import List, Sequence, Tuple

DEFAULT_TIMEOUT = 600.0

# mux.sh default force_style
DEFAULT_SUBTITLE_STYLE = (
    "FontName=PingFang SC,FontSize=18,"
    "PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=1"
)


class RenderError(RuntimeError):
    pass


def _bin(name: str, env: str) -> str:
    return os.environ.get(env, name)


def _check_file(path: str) -> None:
    if not os.path.isfile(path):
        raise RenderError(f"文件不存在: {path}")


def _run(cmd: Sequence[str], timeout: float) -> None:
    """Run a command; raise RenderError on timeout (child killed) or nonzero exit."""
    try:
        proc = subprocess.run(list(cmd), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RenderError(f"超时({timeout}s)已 kill: {' '.join(cmd[:2])} ...")
    except FileNotFoundError:
        raise RenderError(f"找不到可执行文件: {cmd[0]}")
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-500:]
        raise RenderError(f"ffmpeg 失败(exit {proc.returncode}): {' '.join(cmd)}\n{tail}")


def _probe(path: str, select_audio: bool) -> str:
    """ffprobe stdout (stripped). select_audio=True → audio stream index list."""
    cmd = [_bin("ffprobe", "FFPROBE_BIN"), "-v", "error"]
    if select_audio:
        cmd += ["-select_streams", "a", "-show_entries", "stream=index"]
    else:
        cmd += ["-show_entries", "format=duration"]
    cmd += ["-of", "csv=p=0", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except FileNotFoundError:
        raise RenderError(f"找不到可执行文件: {cmd[0]}")
    if proc.returncode != 0:
        raise RenderError(
            f"ffprobe 失败: {path}\n{proc.stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return proc.stdout.decode("utf-8", "replace").strip()


# --- concat.sh: 拼接多個鏡頭 ---
# 無 transition: concat demuxer + -c copy（無損，不重編碼）
# 有 transition: xfade + acrossfade 逐段串接（重編碼，要求音軌/解析度一致）

def concat(
    inputs: List[str],
    output: str,
    transition: str = "",
    duration: float = 0.5,
    timeout: float = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> List[str]:
    for f in inputs:
        _check_file(f)
    if len(inputs) < 2:
        raise RenderError("需要至少 2 个鏡頭")

    if not transition:
        # concat demuxer：临时 list 文件，格式 file '<path>'
        fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="concat_")
        with os.fdopen(fd, "w") as fh:
            for f in inputs:
                fh.write(f"file '{f}'\n")
        cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y", "-f", "concat", "-safe", "0",
               "-i", list_path, "-c", "copy", output]
        if dry_run:
            return cmd  # ponytail: dry_run 不删 list 文件，供测试核对内容
        try:
            _run(cmd, timeout)
        finally:
            os.unlink(list_path)
        return cmd

    # xfade：每段取时长，校验音軌存在，offset = 上段起點 + 上段時長 - 過渡時長
    durs: List[float] = []
    for f in inputs:
        if not _probe(f, select_audio=True):
            raise RenderError(f"鏡頭無音軌（xfade 需要）: {f}")
        raw = _probe(f, select_audio=False)
        try:
            durs.append(float(raw))
        except ValueError:
            raise RenderError(f"無法解析時長: {f} → {raw!r}")

    n = len(inputs)
    fc = ""
    prev_v, prev_a = "[0:v]", "[0:a]"
    off = 0.0
    for i in range(1, n):
        off = round(off + durs[i - 1] - duration, 3)
        fc += (f"{prev_v}[{i}:v]xfade=transition={transition}"
               f":duration={duration}:offset={off:.3f}[v{i}];")
        fc += f"{prev_a}[{i}:a]acrossfade=d={duration}[a{i}];"
        prev_v, prev_a = f"[v{i}]", f"[a{i}]"

    cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y"]
    for f in inputs:
        cmd += ["-i", f]
    cmd += ["-filter_complex", fc,
            "-map", f"[v{n - 1}]", "-map", f"[a{n - 1}]",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", output]
    if not dry_run:
        _run(cmd, timeout)
    return cmd


# --- mux.sh: 混音 + 燒錄字幕 ---
# audio_tracks 条目: {"path": wav, "start": 起始秒(0), "loop": True=墊底循環音軌,
#                     "volume": 覆蓋同類默認(墊底=music_volume, 台詞=dialogue_volume)}

def mux(
    video: str,
    audio_tracks: List[dict],
    output: str,
    subtitles: str,
    subtitle_style: str = DEFAULT_SUBTITLE_STYLE,
    dialogue_volume: float = 1.0,
    music_volume: float = 0.15,
    timeout: float = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> List[str]:
    _check_file(video)
    _check_file(subtitles)

    # 視頻時長（原始字符串，透傳給 atrim），用於裁剪循環音軌和超長台詞
    vdur = _probe(video, select_audio=False)
    try:
        float(vdur)
    except ValueError:
        raise RenderError(f"無法解析視頻時長: {video} → {vdur!r}")

    inputs = ["-i", video]
    fc = ""
    n = 1          # 輸入序號：0=視頻, 1=墊底(如有), 2..=台詞
    dlg = 0        # 台詞標籤序號，獨立於是否有墊底
    mix_in = "[0:a]"

    for t in audio_tracks:
        path = t["path"]
        _check_file(path)
        if t.get("loop"):
            # 墊底音軌：無限循環，裁到視頻時長
            inputs += ["-stream_loop", "-1", "-i", path]
            vol = t.get("volume", music_volume)
            fc += f"[{n}:a]volume={vol},atrim=0:{vdur},asetpts=PTS-STARTPTS[bg];"
            mix_in += "[bg]"
        else:
            # 台詞：起始秒 對齊（adelay 毫秒），同樣裁到視頻時長
            start = float(t.get("start", 0) or 0)
            ms = int(start * 1000)
            inputs += ["-i", path]
            vol = t.get("volume", dialogue_volume)
            fc += (f"[{n}:a]volume={vol},adelay={ms}:all=1,"
                   f"atrim=0:{vdur},asetpts=PTS-STARTPTS[d{dlg}];")
            mix_in += f"[d{dlg}]"
            dlg += 1
        n += 1

    # normalize=0 保持各軌原音量（默認按輸入數均分，台詞會被削掉）
    fc += f"{mix_in}amix=inputs={n}:normalize=0:dropout_transition=0[aout];"
    fc += f"[0:v]subtitles=filename='{subtitles}':force_style='{subtitle_style}'[vout]"

    cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y"] + inputs + [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output]
    if not dry_run:
        _run(cmd, timeout)
    return cmd


# --- freeze.sh: 在指定時刻定格畫面（後期卡點）---
# points: "T1:D1;T2:D2" 字符串或 [(t, d), ...] — 在 T 秒處定格 D 秒（可多個）

def freeze(
    video: str,
    points: object,
    output: str,
    timeout: float = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> List[str]:
    _check_file(video)

    if isinstance(points, str):
        try:
            pts: List[Tuple[float, float]] = [
                (float(t), float(d))
                for t, d in (p.split(":", 1) for p in points.split(";") if p)
            ]
        except ValueError:
            raise RenderError(f"卡點格式需 T:D（分號分隔）: {points!r}")
    else:
        pts = [(float(t), float(d)) for t, d in points]
    if not pts:
        raise RenderError("卡點表為空")

    # 卡點時間用「當前流時間線」：原始時間 + 已累積的定格偏移
    # 3 路 split → trim 前段 + freeze 段（tpad 克隆）+ 後段 → concat
    filters: List[str] = []
    offset = 0.0
    label = "0:v"
    for idx, (t, d) in enumerate(pts):
        cur = f"{t + offset:.3f}"
        nxt = f"{float(cur) + 0.042:.3f}"
        a, b, c = f"v{idx}a", f"v{idx}b", f"v{idx}c"
        a1, b1, c1 = f"v{idx}a1", f"v{idx}b1", f"v{idx}c1"
        filters.append(f"[{label}]split=3[{a}][{b}][{c}]")
        filters.append(f"[{a}]trim=start=0:end={cur},setpts=PTS-STARTPTS[{a1}]")
        filters.append(f"[{b}]trim=start={cur}:end={nxt},setpts=PTS-STARTPTS,"
                       f"tpad=stop_mode=clone:stop_duration={d}[{b1}]")
        filters.append(f"[{c}]trim=start={nxt},setpts=PTS-STARTPTS[{c1}]")
        filters.append(f"[{a1}][{b1}][{c1}]concat=n=3:v=1:a=0[out{idx}]")
        offset += d
        label = f"out{idx}"

    fc = ";".join(filters)
    final = f"[out{len(pts) - 1}]"
    cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y", "-v", "error", "-i", video,
           "-filter_complex", fc, "-map", final,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", output]
    if not dry_run:
        _run(cmd, timeout)
    return cmd
