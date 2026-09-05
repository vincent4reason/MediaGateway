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


def _filter_escape_path(path: str) -> str:
    """ffmpeg filtergraph option values choke on ' : \\ — reject early with a
    clear error instead of a cryptic filter parse failure. Generated asset
    paths never contain these; only user-supplied paths can."""
    bad = [c for c in ("'", ":", "\\") if c in path]
    if bad:
        raise RenderError(f"路径含 filtergraph 非法字符 {bad}: {path}")
    return path


def _concat_escape(path: str) -> str:
    """Escape for concat demuxer list files (file '<path>')."""
    return path.replace("'", "'\\''")


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
                fh.write(f"file '{_concat_escape(f)}'\n")
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

    if duration >= min(durs):
        raise RenderError(f"transition duration ({duration}s) 必须短于最短镜头 ({min(durs)}s)")
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
    subtitles: str | None = None,  # None = 不烧字幕
    subtitle_style: str = DEFAULT_SUBTITLE_STYLE,
    dialogue_volume: float = 1.0,
    music_volume: float = 0.15,
    mute_source_audio: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> List[str]:
    """mute_source_audio: 丢弃视频自带音轨（h3 Ref2VA 工作流里音轨是模型渲染版，
    成片改铺 Voice Worker 原声）。有附加音轨时混音底换 anullsrc，无附加音轨则成片静音。"""
    _check_file(video)
    if subtitles:
        _check_file(subtitles)
        _filter_escape_path(subtitles)

    # 视频是否自带音轨：h3 输出总有音轨，但外部输入的视频可能没有；
    # 无音轨时用 anullsrc 作为 amix 的起底，否则 filter 引用 [0:a] 直接失败
    has_own_audio = bool(_probe(video, select_audio=True))

    # 視頻時長（原始字符串，透傳給 atrim），用於裁剪循環音軌和超長台詞
    vdur = _probe(video, select_audio=False)
    try:
        float(vdur)
    except ValueError:
        raise RenderError(f"無法解析視頻時長: {video} → {vdur!r}")

    inputs = ["-i", video]
    fc = ""
    if has_own_audio and not mute_source_audio:
        n = 1          # 輸入序號：0=視頻, 1=墊底(如有), 2..=台詞
        mix_in = "[0:a]"
    else:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        # anullsrc 是无限源：不 trim 的话 amix 永不结束（ffmpeg 挂死到超时）
        fc += "[1:a]atrim=0:" + vdur + ",asetpts=PTS-STARTPTS[mutbase];"
        n = 2          # 0=視頻, 1=anullsrc 起底, 2..=音軌
        mix_in = "[mutbase]"
    dlg = 0        # 台詞標籤序號，獨立於是否有墊底

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
    has_audio = n > 1  # n 起始為 1（視頻自身），>1 說明有額外音軌
    if subtitles:
        fc += f"[0:v]subtitles=filename='{subtitles}':force_style='{subtitle_style}'[vout]"
        vmap = "[vout]"
    else:
        vmap = "0:v"
    if has_audio:
        # amix 路数 = mix_in 里实际拼接的标签数（mute 分支下不等于输入索引 n）
        amix_n = mix_in.count("[")
        fc += f"{mix_in}amix=inputs={amix_n}:normalize=0:dropout_transition=0[aout];"
        amap = "[aout]"
    else:
        amap = "0:a"  # 無額外音軌：保留視頻自帶音軌

    cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y"] + inputs
    if fc:
        cmd += ["-filter_complex", fc]
    cmd += ["-map", vmap, "-map", amap,
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output]
    if not dry_run:
        _run(cmd, timeout)
    return cmd


# --- freeze.sh: 在指定時刻定格畫面（後期卡點）---
# points: "T1:D1;T2:D2" 字符串或 [(t, d), ...] — 在 T 秒處定格 D 秒（可多個）
# 假定 24fps 輸入（h3 固定 24fps）。已知限制：音轨原样保留（-map 0:a?），
# 定格延长的尾段是静音 — 需要卡点后仍有声时先 mux 再 freeze。

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
    for t, d in pts:
        if t < 0 or d <= 0:
            raise RenderError(f"卡點時間需 t>=0 且 d>0，得到 t={t} d={d}")

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
    # -map 0:a? : 保留音轨（原长，定格尾段静音）；不加 -map 会连音轨一起丢
    cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y", "-v", "error", "-i", video,
           "-filter_complex", fc, "-map", final, "-map", "0:a?",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", output]
    if not dry_run:
        _run(cmd, timeout)
    return cmd


def extract_last_frame(video: str, output: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """抓取视频结尾附近一帧，作下一镜头 first_frame（剪辑接缝用，非像素级）。

    -sseof -0.1 定位到结尾前 0.1s 取首帧：VFR 下可能差一两帧，接缝场景够用。
    """
    _check_file(video)
    cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y", "-v", "error",
           "-sseof", "-0.1", "-i", video, "-frames:v", "1", "-update", "1", output]
    _run(cmd, timeout)
    _check_file(output)
    return output


# --- bgm: 多段音樂 crossfade 鏈成連續音軌，墊到視頻音軌下 ---
# segments: [{"path": str, "duration_s": float}, ...]（有序；duration_s 僅為調用方
# 提示值，鏈長以文件真實時長為準）。音樂短於視頻 aloop 循環補齊，長則 atrim。

def bgm(
    video: str,
    segments: List[dict],
    output: str,
    gain_db: float = -6.0,
    timeout: float = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> List[str]:
    _check_file(video)
    if not segments:
        raise RenderError("需要至少 1 段音樂")
    for s in segments:
        _check_file(s["path"] if isinstance(s, dict) else s)

    vdur = _probe(video, select_audio=False)
    try:
        float(vdur)
    except ValueError:
        raise RenderError(f"無法解析視頻時長: {video} → {vdur!r}")
    has_own_audio = bool(_probe(video, select_audio=True))

    inputs = ["-i", video]
    for seg in segments:
        inputs += ["-i", seg["path"] if isinstance(seg, dict) else seg]
    # aformat 統一取樣率/聲道——acrossfade/amix 遇到混合取樣率的輸入會直接報錯
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    fc = ""
    prev = ""
    for i in range(len(segments)):
        fc += f"[{i + 1}:a]{fmt}[g{i}];"
        if prev:
            fc += f"{prev}[g{i}]acrossfade=d=1:c1=tri:c2=tri[x{i}];"
            prev = f"[x{i}]"
        else:
            prev = f"[g{i}]"
    # aloop+atrim：不足視頻長自動從頭循環補齊，超長則裁掉——兩種情況同一條濾鏡
    fc += f"{prev}aloop=loop=-1:size=1000000000,atrim=0:{vdur},asetpts=PTS-STARTPTS,"
    fc += f"volume={gain_db}dB[bgv]"
    if has_own_audio:
        fc += f";[0:a]{fmt}[v0a];[v0a][bgv]amix=inputs=2:normalize=0[aout]"
        amap = "[aout]"  # normalize=0：原音軌全量，bgm 只吃 gain_db
    else:
        amap = "[bgv]"  # 無源音軌：音樂直接作輸出音軌

    cmd = [_bin("ffmpeg", "FFMPEG_BIN"), "-y"] + inputs
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", amap,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", output]
    if not dry_run:
        _run(cmd, timeout)
    return cmd
