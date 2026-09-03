"""Tests for server/workers/video.py — pure mock, no GPU, no real engine.

Run: .venv/bin/python tests/test_video.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.workers import video  # noqa: E402


class FakeEngine:
    """Records generate() kwargs, fires on_progress, returns canned meta."""

    def __init__(self, meta=None, progress_calls=(("denoise", 12, 48),)):
        self.calls = []
        self.closed = False
        self.meta = meta or {"width": 864, "height": 480, "frames": 48,
                             "fps": 24, "sample_rate": 48000, "seed": 7}
        self.progress_calls = progress_calls

    def close(self):
        self.closed = True

    def generate(self, prompt, *, output_path, refs=None, on_progress=None, **overrides):
        self.calls.append({"prompt": prompt, "output_path": output_path,
                           "refs": refs, "overrides": overrides})
        for phase, done, total in self.progress_calls:
            if on_progress:
                on_progress(phase, done, total)
        return dict(self.meta)


def run_with(params, engine):
    """Inject fake engine, run worker, restore singleton. Returns (result, calls)."""
    saved = video._engine
    video._engine = engine
    try:
        with tempfile.TemporaryDirectory() as d:
            result = video.run(params, Path(d), progress=lambda r, p="": None,
                               cancel=lambda: False)
        return result, engine.calls
    finally:
        video._engine = saved


def test_engine_released_after_job():
    eng = FakeEngine()
    saved = video._engine
    video._engine = eng
    try:
        with tempfile.TemporaryDirectory() as d:
            video.run({"prompt": "x"}, Path(d), progress=lambda r, p="": None,
                      cancel=lambda: False)
        assert eng.closed, "engine must be closed (memory released) after job"
        assert video._engine is None, "singleton must be reset after close"
    finally:
        video._engine = saved


def test_keep_loaded_keeps_engine():
    eng = FakeEngine()
    saved = video._engine
    video._engine = eng
    try:
        with tempfile.TemporaryDirectory() as d:
            video.run({"prompt": "x", "keep_loaded": True}, Path(d),
                      progress=lambda r, p="": None, cancel=lambda: False)
        assert not eng.closed, "keep_loaded=True must keep the engine resident"
        assert video._engine is eng
    finally:
        video._engine = saved


def test_defaults_and_param_mapping():
    eng = FakeEngine()
    _, calls = run_with({"prompt": "a cat"}, eng)
    ov = calls[0]["overrides"]
    assert ov == {"width": 864, "height": 480, "steps": 6, "denoise_reuse": 1,
                  "dit_layers": 45}, ov
    assert calls[0]["refs"] == []
    assert calls[0]["output_path"].endswith("output.mp4")


def test_seconds_to_frames_and_round32():
    eng = FakeEngine()
    _, calls = run_with({"prompt": "x", "seconds": 2.5, "width": 853, "height": 499}, eng)
    ov = calls[0]["overrides"]
    assert ov["frames"] == 60, ov  # 2.5s * 24
    assert ov["width"] == 832 and ov["height"] == 480, ov  # floor to multiple of 32


def test_frames_beats_nothing_seconds_wins():
    eng = FakeEngine()
    _, calls = run_with({"prompt": "x", "frames": 96}, eng)
    assert calls[0]["overrides"]["frames"] == 96
    _, calls = run_with({"prompt": "x", "seconds": 1, "frames": 96}, FakeEngine())
    assert calls[0]["overrides"]["frames"] == 24  # seconds wins, like h3cweb


def test_optional_flags_passthrough():
    eng = FakeEngine()
    _, calls = run_with({"prompt": "x", "core_reuse": 2, "token_reduction": True,
                         "ssd_streaming": True, "seed": 123,
                         "reference_image_size": 512}, eng)
    ov = calls[0]["overrides"]
    assert ov["core_reuse"] == 2 and ov["token_reduction"] == 1
    assert ov["ssd_streaming"] == 1 and ov["seed"] == 123
    assert ov["reference_image_size"] == 512
    _, calls = run_with({"prompt": "x"}, FakeEngine())
    assert "core_reuse" not in calls[0]["overrides"]
    assert "seed" not in calls[0]["overrides"]
    assert "token_reduction" not in calls[0]["overrides"]


def test_refs_kind_mapping(tmp_refs):
    eng = FakeEngine()
    _, calls = run_with({"prompt": "x", "refs": [
        {"kind": "image", "path": tmp_refs["img"]},
        {"kind": "video_audio", "path": tmp_refs["vid"],
         "audio_path": tmp_refs["wav"], "include_embedded_audio": True},
        {"kind": "audio", "path": tmp_refs["wav"]},
    ]}, eng)
    refs = calls[0]["refs"]
    assert refs[0] == {"kind": "image", "path": tmp_refs["img"],
                       "audio_path": None, "include_embedded_audio": False}
    assert refs[1]["kind"] == "video_audio"
    assert refs[1]["audio_path"] == tmp_refs["wav"]
    assert refs[1]["include_embedded_audio"] is True


def test_missing_ref_raises(tmp_refs):
    try:
        run_with({"prompt": "x", "refs": [{"kind": "image", "path": "/no/such.png"}]},
                 FakeEngine())
        raise AssertionError("expected VideoError for missing ref")
    except video.VideoError as e:
        assert "/no/such.png" in str(e)
    # bad kind too
    try:
        run_with({"prompt": "x", "refs": [{"kind": "gif", "path": tmp_refs["img"]}]},
                 FakeEngine())
        raise AssertionError("expected VideoError for bad kind")
    except video.VideoError:
        pass


def test_missing_first_frame_raises(tmp_refs):
    try:
        run_with({"prompt": "x", "first_frame": "/no/such.png"}, FakeEngine())
        raise AssertionError("expected VideoError")
    except video.VideoError:
        pass


def test_meta_passthrough():
    meta = {"width": 1024, "height": 576, "frames": 120, "fps": 24, "seed": 99}
    result, _ = run_with({"prompt": "x"}, FakeEngine(meta=meta))
    for k, v in meta.items():
        assert result[k] == v, result
    assert "output_path" in result
    assert "sample_rate" not in result  # not in the documented meta set


def test_progress_ratio():
    seen = []
    saved = video._engine
    video._engine = FakeEngine()  # fires on_progress("denoise", 12, 48)
    try:
        with tempfile.TemporaryDirectory() as d:
            video.run({"prompt": "x"}, Path(d),
                      progress=lambda r, p="": seen.append((r, p)),
                      cancel=lambda: False)
    finally:
        video._engine = saved
    assert seen == [(12 / 48, "denoise")], seen


def test_cancel_raises():
    for cancel in (lambda: True, lambda: False):
        engine = FakeEngine()
        saved = video._engine
        video._engine = engine
        try:
            with tempfile.TemporaryDirectory() as d:
                video.run({"prompt": "x"}, Path(d),
                          progress=lambda r, p="": None, cancel=cancel)
            assert not cancel(), "expected cancel to raise"
        except video.VideoError:
            assert cancel(), "raised without cancel set"
        finally:
            video._engine = saved


def _make_tmp_refs():
    d = tempfile.mkdtemp()
    paths = {}
    for name in ("img.png", "vid.mp4", "wav.wav"):
        p = Path(d) / name
        p.write_bytes(b"x")
        paths[name.split(".")[0]] = str(p)
    return paths


if __name__ == "__main__":
    refs = _make_tmp_refs()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t(refs) if t.__code__.co_argcount else t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} tests passed")
