"""Tests for server/workers/shot.py — pure mock, no GPU, no ffmpeg.

Run: .venv/bin/python tests/test_shot.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.workers import image as image_w  # noqa: E402
from server.workers import music as music_w  # noqa: E402
from server.workers import shot  # noqa: E402
from server.workers import video as video_w  # noqa: E402
from server.workers import voice as voice_w  # noqa: E402


class Stage:
    """Fake stage worker: records calls, writes a placeholder output file."""
    kind = "generic"

    def __init__(self, fail=False, kind="generic"):
        self.kind = kind
        self.calls = []
        self.fail = fail

    def run(self, params, job_dir, progress, cancel):
        self.calls.append({"params": params, "job_dir": str(job_dir)})
        if self.fail:
            raise RuntimeError(f"{self.kind} stage failed")
        out = Path(job_dir) / f"{self.kind}.out"
        out.write_text("x")
        return {"output_path": str(out)}


def patch_stages(image=None, video=None, voice=None, music=None, muxRecorder=None):
    saved = (image_w.run, video_w.run, voice_w.run, music_w.run, shot.render)
    image_w.run = image.run if image else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("image called")))
    video_w.run = video.run if video else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("video called")))
    voice_w.run = voice.run if voice else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("voice called")))
    music_w.run = music.run if music else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("music called")))
    if muxRecorder is not None:
        class _M:
            def __getattr__(self, name):
                if name == "extract_last_frame":  # P10 tail-frame: stub, no ffmpeg
                    return lambda video, out: out
                assert name == "mux", name
                return muxRecorder
        shot.render = _M()
    return saved


def restore(saved):
    image_w.run, video_w.run, voice_w.run, music_w.run, shot.render = saved


def run_shot(params):
    with tempfile.TemporaryDirectory() as d:
        return shot.run(params, Path(d), progress=lambda r, p="": None,
                        cancel=lambda: False)


def test_requires_at_least_one_stage():
    try:
        run_shot({})
        assert False, "empty spec must fail"
    except ValueError as e:
        assert "at least one" in str(e)


def test_stage_order_and_paths():
    img, vid, voi, mus = (Stage(kind=k) for k in ("image", "video", "voice", "music"))
    calls = []
    saved = patch_stages(img, vid, voi, mus, muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        with tempfile.TemporaryDirectory() as d:
            result = shot.run(
                {"image": {"prompt": "p"}, "video": {"prompt": "v"},
                 "voice": {"text": "t"}, "music": {"prompt": "m"}},
                Path(d), progress=lambda r, p="": None, cancel=lambda: False)
            assert result["output_path"].endswith("shot.mp4")
            jd = Path(d)
            assert img.calls[0]["job_dir"] == str(jd / "image")
            # Ref2VA 顺序:voice 在 video 之前完成,video 收到 audio ref
            assert vid.calls[0]["params"]["refs"] == [
                {"kind": "audio", "path": f"{jd}/voice/voice.out"}]
            assert vid.calls[0]["job_dir"] == str(jd / "video")
            assert voi.calls[0]["job_dir"] == str(jd / "voice")
            assert mus.calls[0]["job_dir"] == str(jd / "music")
            kw = calls[0]
            assert Path(kw["video"]).name == "video.out"
            assert [Path(t["path"]).name for t in kw["audio_tracks"]] == ["voice.out", "music.out"]
            assert kw["audio_tracks"][1]["loop"] is True
            assert kw["mute_source_audio"] is True  # 有 voice/music → h3 音轨丢弃
            assert result["stages"]["image"]["output_path"].endswith("image.out")
    finally:
        restore(saved)


def test_first_frame_auto_wiring():
    img, vid = Stage(kind="image"), Stage(kind="video")
    saved = patch_stages(image=img, video=vid,
                         muxRecorder=lambda **kw: None)
    try:
        run_shot({"image": {"prompt": "p"},
                  "video": {"prompt": "v", "first_frame": "auto"}})
        ff = vid.calls[0]["params"]["first_frame"]
        assert ff.endswith("image.out"), ff
    finally:
        restore(saved)


def test_first_frame_auto_without_image_fails():
    vid = Stage()
    saved = patch_stages(video=vid, muxRecorder=lambda **kw: None)
    try:
        try:
            run_shot({"video": {"prompt": "v", "first_frame": "auto"}})
            assert False, "auto without image stage must fail"
        except ValueError as e:
            assert "auto" in str(e)
    finally:
        restore(saved)


def test_no_video_returns_stage_output_no_mux():
    img = Stage(kind="image")
    mux_called = []
    saved = patch_stages(image=img, muxRecorder=lambda **kw: mux_called.append(kw))
    try:
        result = run_shot({"image": {"prompt": "p"}})
        assert result["output_path"].endswith("image.out")
        assert not mux_called, "mux must be skipped without a video stage"
    finally:
        restore(saved)


def test_stage_failure_propagates():
    vid = Stage(fail=True)
    saved = patch_stages(video=vid, muxRecorder=lambda **kw: None)
    try:
        try:
            run_shot({"video": {"prompt": "v"}})
            assert False, "stage failure must raise"
        except RuntimeError as e:
            assert "failed" in str(e)
    finally:
        restore(saved)


def test_progress_monotonic():
    class P:
        last = 0.0
        vals = []

        @classmethod
        def cb(cls, ratio, phase=""):
            assert ratio >= cls.last - 1e-9, f"progress went backwards: {ratio} < {cls.last}"
            cls.last = ratio
            cls.vals.append(ratio)
    stages = {k: Stage() for k in ("image", "video", "voice")}
    saved = patch_stages(stages["image"], stages["video"], stages["voice"],
                         muxRecorder=lambda **kw: None)
    try:
        with tempfile.TemporaryDirectory() as d:
            shot.run({"image": {"prompt": "1"}, "video": {"prompt": "2"},
                      "voice": {"text": "3"}}, Path(d),
                     progress=P.cb, cancel=lambda: False)
        assert P.vals and max(P.vals) <= 0.99
    finally:
        restore(saved)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
