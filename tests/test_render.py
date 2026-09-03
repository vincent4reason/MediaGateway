"""Tests for server/render.py — stub ffmpeg/ffprobe, no real binaries.

Run: .venv/bin/python tests/test_render.py
Equivalence is checked against vendor/render/{concat,mux,freeze}.sh, arg by arg.
"""
import contextlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import render  # noqa: E402

FAKE_FFMPEG = r"""#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ["FAKE_FFMPEG_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_FFMPEG_FAIL"):
    sys.stderr.write("fake ffmpeg exploded: filter parse error\n")
    sys.exit(int(os.environ["FAKE_FFMPEG_FAIL"]))
if os.environ.get("FAKE_FFMPEG_SLEEP"):
    time.sleep(float(os.environ["FAKE_FFMPEG_SLEEP"]))
open(sys.argv[-1], "w").close()  # touch output (last argv)
"""

FAKE_FFPROBE = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if "-select_streams" in args:
    print("" if os.environ.get("FAKE_NO_AUDIO") else "0")
else:
    print(os.environ.get("FAKE_DURATION", "5.000000"))
"""

# --- fixtures ---

D = Path(tempfile.mkdtemp(prefix="render_test_"))
FFMPEG = D / "fake_ffmpeg"
FFPROBE = D / "fake_ffprobe"
LOG = D / "ffmpeg_log"

for script in (FFMPEG, FFPROBE):
    script.write_text(FAKE_FFMPEG if script is FFMPEG else FAKE_FFPROBE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

for name in ("a.mp4", "b.mp4", "c.mp4", "v.mp4", "bg.wav", "d1.wav", "sub.srt"):
    (D / name).write_bytes(b"")


@contextlib.contextmanager
def env(**kw):
    kw.setdefault("FFMPEG_BIN", str(FFMPEG))
    kw.setdefault("FFPROBE_BIN", str(FFPROBE))
    kw.setdefault("FAKE_FFMPEG_LOG", str(LOG))
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update(kw)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fresh_log():
    LOG.write_text("")
    return LOG


def p(name):
    return str(D / name)


# --- concat ---

def test_concat_copy_equivalent_and_executes():
    with env(FFMPEG_BIN=str(FFMPEG)):
        cmd = render.concat([p("a.mp4"), p("b.mp4")], p("out.mp4"), dry_run=True)
        # 逐参数对照 concat.sh: ffmpeg -y -f concat -safe 0 -i LIST -c copy OUT
        assert cmd[0] == str(FFMPEG), "FFMPEG_BIN override"
        assert cmd[1:6] == ["-y", "-f", "concat", "-safe", "0"]
        assert cmd[6] == "-i"
        assert cmd[8:10] == ["-c", "copy"]
        assert cmd[10] == p("out.mp4")
        list_file = Path(cmd[7])
        assert list_file.read_text() == f"file '{p('a.mp4')}'\nfile '{p('b.mp4')}'\n"
        list_file.unlink()

        # 真实执行：假 ffmpeg touch 输出，argv（不含二进制本身）落日志
        log = fresh_log()
        out = D / "real_out.mp4"
        render.concat([p("a.mp4"), p("b.mp4")], str(out))
        assert out.exists(), "ffmpeg must run and produce output"
        argv = json.loads(log.read_text().splitlines()[0])  # == cmd[1:]，list 路径每次新建
        assert argv[:4] == cmd[1:5]
        assert argv[5] == "-i" and argv[7:9] == ["-c", "copy"]
        assert argv[9] == str(out)  # 输出路径为真实执行目标


def test_concat_needs_two_inputs():
    with env(FFMPEG_BIN=str(FFMPEG)):
        try:
            render.concat([p("a.mp4")], p("out.mp4"))
            assert False, "must reject <2 inputs"
        except render.RenderError as e:
            assert "2" in str(e)


def test_concat_xfade_equivalent():
    with env(FFMPEG_BIN=str(FFMPEG), FAKE_DURATION="5.000000"):
        # 2 镜头: offset = 5.0 - 0.5 = 4.500（.sh 的 FC 每段末尾都带 ';'，含最后一段）
        cmd = render.concat([p("a.mp4"), p("b.mp4")], p("out.mp4"),
                            transition="fade", duration=0.5, dry_run=True)
        fc = ("[0:v][1:v]xfade=transition=fade:duration=0.5:offset=4.500[v1];"
              "[0:a][1:a]acrossfade=d=0.5[a1];")
        assert cmd == [str(FFMPEG), "-y", "-i", p("a.mp4"), "-i", p("b.mp4"),
                       "-filter_complex", fc,
                       "-map", "[v1]", "-map", "[a1]",
                       "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "192k", p("out.mp4")], cmd

        # 3 镜头: offset 累加 = 上一起点 + 上段时长 - 过渡时长 (对照 .sh 的 awk)
        cmd = render.concat([p("a.mp4"), p("b.mp4"), p("c.mp4")], p("out.mp4"),
                            transition="fade", dry_run=True)
        fc = ("[0:v][1:v]xfade=transition=fade:duration=0.5:offset=4.500[v1];"
              "[0:a][1:a]acrossfade=d=0.5[a1];"
              "[v1][2:v]xfade=transition=fade:duration=0.5:offset=9.000[v2];"
              "[a1][2:a]acrossfade=d=0.5[a2];")
        assert cmd[9] == fc and cmd[10:14] == ["-map", "[v2]", "-map", "[a2]"], cmd


def test_concat_xfade_requires_audio():
    with env(FFMPEG_BIN=str(FFMPEG), FFPROBE_BIN=str(FFPROBE), FAKE_NO_AUDIO="1"):
        try:
            render.concat([p("a.mp4"), p("b.mp4")], p("out.mp4"),
                          transition="fade", dry_run=True)
            assert False, "xfade without audio must fail"
        except render.RenderError as e:
            assert "音軌" in str(e)


# --- mux ---

STYLE = ("FontName=PingFang SC,FontSize=18,PrimaryColour=&HFFFFFF,"
         "OutlineColour=&H000000,Outline=1")


def test_mux_equivalent():
    with env(FFMPEG_BIN=str(FFMPEG), FFPROBE_BIN=str(FFPROBE),
             FAKE_DURATION="5.000000"):
        # 对照 mux.sh: 0=视频 1=垫底循环 2..=台词，d 标签独立编号
        cmd = render.mux(
            p("v.mp4"),
            [{"path": p("bg.wav"), "loop": True},
             {"path": p("d1.wav"), "start": 1.5}],
            p("out.mp4"), p("sub.srt"), dry_run=True)
        fc = ("[1:a]volume=0.15,atrim=0:5.000000,asetpts=PTS-STARTPTS[bg];"
              "[2:a]volume=1.0,adelay=1500:all=1,atrim=0:5.000000,"
              "asetpts=PTS-STARTPTS[d0];"
              "[0:a][bg][d0]amix=inputs=3:normalize=0:dropout_transition=0[aout];"
              f"[0:v]subtitles=filename='{p('sub.srt')}':force_style='{STYLE}'[vout]")
        assert cmd == [str(FFMPEG), "-y",
                       "-i", p("v.mp4"),
                       "-stream_loop", "-1", "-i", p("bg.wav"),
                       "-i", p("d1.wav"),
                       "-filter_complex", fc,
                       "-map", "[vout]", "-map", "[aout]",
                       "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "192k",
                       "-movflags", "+faststart", p("out.mp4")], cmd

        # 无垫底、无台词：mix_in 只有 [0:a]，amix inputs=1（对照 .sh N=1 分支）
        cmd = render.mux(p("v.mp4"), [], p("out.mp4"), p("sub.srt"), dry_run=True)
        fc = ("[0:a]amix=inputs=1:normalize=0:dropout_transition=0[aout];"
              f"[0:v]subtitles=filename='{p('sub.srt')}':force_style='{STYLE}'[vout]")
        assert cmd[2:4] == ["-i", p("v.mp4")] and cmd[4] == "-filter_complex"
        assert cmd[5] == fc and "-stream_loop" not in cmd, cmd


def test_mux_missing_track_file():
    with env(FFMPEG_BIN=str(FFMPEG), FFPROBE_BIN=str(FFPROBE)):
        try:
            render.mux(p("v.mp4"), [{"path": p("nope.wav")}],
                       p("out.mp4"), p("sub.srt"))
            assert False
        except render.RenderError as e:
            assert "nope.wav" in str(e)


# --- freeze ---

def _freeze_fc(points):
    """对照 freeze.sh 手工展开的 filter_complex。"""
    filters, offset, label = [], 0.0, "0:v"
    for idx, (t, d) in enumerate(points):
        cur = f"{t + offset:.3f}"
        nxt = f"{float(cur) + 0.042:.3f}"
        filters += [
            f"[{label}]split=3[v{idx}a][v{idx}b][v{idx}c]",
            f"[v{idx}a]trim=start=0:end={cur},setpts=PTS-STARTPTS[v{idx}a1]",
            f"[v{idx}b]trim=start={cur}:end={nxt},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={d}[v{idx}b1]",
            f"[v{idx}c]trim=start={nxt},setpts=PTS-STARTPTS[v{idx}c1]",
            f"[v{idx}a1][v{idx}b1][v{idx}c1]concat=n=3:v=1:a=0[out{idx}]",
        ]
        offset += d
        label = f"out{idx}"
    return ";".join(filters)


def test_freeze_equivalent():
    with env(FFMPEG_BIN=str(FFMPEG)):
        # 卡點時間用「當前流時間線」: 原始 3s、7s → 流上 3.000 / 9.000 (7+2 偏移)
        expected = [
            str(FFMPEG), "-y", "-v", "error", "-i", p("v.mp4"),
            "-filter_complex", _freeze_fc([(3.0, 2.0), (7.0, 1.5)]),
            "-map", "[out1]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", p("out.mp4"),
        ]
        # 字符串形式（与 .sh 的 "T1:D1;T2:D2" 完全同接口）
        cmd = render.freeze(p("v.mp4"), "3:2;7:1.5", p("out.mp4"), dry_run=True)
        assert cmd == expected, cmd
        # tuple 形式等价
        cmd2 = render.freeze(p("v.mp4"), [(3.0, 2.0), (7.0, 1.5)],
                             p("out.mp4"), dry_run=True)
        assert cmd2 == cmd, cmd2


def test_freeze_bad_points():
    with env(FFMPEG_BIN=str(FFMPEG)):
        for bad in ("3", "abc:2", ""):
            try:
                render.freeze(p("v.mp4"), bad, p("out.mp4"), dry_run=True)
                assert False, f"must reject points={bad!r}"
            except render.RenderError:
                pass


# --- 通用行为 ---

def test_missing_inputs_rejected():
    with env(FFMPEG_BIN=str(FFMPEG)):
        for fn, kwargs in [
            (render.concat, {"inputs": [p("nope.mp4"), p("nope2.mp4")], "output": p("o.mp4")}),
            (render.mux, {"video": p("nope.mp4"), "audio_tracks": [],
                          "output": p("o.mp4"), "subtitles": p("sub.srt")}),
            (render.mux, {"video": p("v.mp4"), "audio_tracks": [],
                          "output": p("o.mp4"), "subtitles": p("nope.srt")}),
            (render.freeze, {"video": p("nope.mp4"), "points": "1:1", "output": p("o.mp4")}),
        ]:
            try:
                fn(**kwargs)
                assert False, f"{fn.__name__} must check input existence"
            except render.RenderError as e:
                assert "不存在" in str(e)


def test_dry_run_does_not_execute():
    log = fresh_log()
    out = D / "dry_out.mp4"
    with env(FFMPEG_BIN=str(FFMPEG)):
        render.mux(p("v.mp4"), [], str(out), p("sub.srt"), dry_run=True)
    assert not out.exists(), "dry_run must not produce output"
    assert log.read_text() == "", "dry_run must not invoke ffmpeg"


def test_failure_raises_with_stderr_tail():
    fresh_log()
    with env(FFMPEG_BIN=str(FFMPEG), FAKE_FFMPEG_FAIL="1"):
        try:
            render.freeze(p("v.mp4"), "1:1", p("out.mp4"))
            assert False, "nonzero exit must raise"
        except render.RenderError as e:
            assert "fake ffmpeg exploded: filter parse error" in str(e)


def test_timeout_kills():
    fresh_log()
    out = D / "slow_out.mp4"
    import time
    t0 = time.time()
    with env(FFMPEG_BIN=str(FFMPEG), FAKE_FFMPEG_SLEEP="30"):
        try:
            render.concat([p("a.mp4"), p("b.mp4")], str(out), timeout=0.5)
            assert False, "must raise on timeout"
        except render.RenderError as e:
            assert "超时" in str(e)
    assert time.time() - t0 < 10, "timeout must kill promptly"
    assert not out.exists()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
