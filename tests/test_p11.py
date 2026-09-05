"""Tests for P11 audio protocol surface — /v1/videos audio refs, mix worker,
/v1/music routes, speech duration, concat music_segments. No GPU, no ffmpeg.

Run: .venv/bin/python tests/test_p11.py
"""
from __future__ import annotations

import base64
import os
import struct
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="mg_p11_")
os.environ["MG_DB"] = os.path.join(_TMP, "gateway.db")
os.environ["MG_ASSETS"] = os.path.join(_TMP, "assets")
os.environ["H3CWEB_COMPAT_BASE_DIR"] = _TMP
os.environ["H3CWEB_COMPAT_REFS_DIR"] = os.path.join(_TMP, "refs")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from server import compat_h3cweb, core, render  # noqa: E402
from server.main import app  # noqa: E402
from server.workers import concat as concat_worker  # noqa: E402
from server.workers import mix as mix_worker  # noqa: E402


def _wav_bytes(rate=48000, channels=1, bits=16, seconds=0.1) -> bytes:
    data = b"\x00" * int(rate * channels * bits // 8 * seconds)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * bits // 8,
                      channels * bits // 8, bits)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


class FakeJob:
    """Replaces core.create_job with an instantly-completed job."""

    def __init__(self, output_file):
        self.output_file = output_file
        self.calls = []
        self.counter = 0

    def create(self, jtype, params, priority=0):
        self.counter += 1
        self.calls.append((jtype, params))
        if not Path(self.output_file).exists():
            Path(self.output_file).write_bytes(_wav_bytes())
        return {"id": f"fake_{self.counter:04d}", "type": jtype, "status": "completed",
                "progress": 1.0, "output_path": self.output_file, "error": None}

    def get(self, job_id):
        return {"id": job_id, "status": "completed", "progress": 1.0,
                "output_path": self.output_file, "error": None}


class _Patched:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self.saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = self.fake.create, self.fake.get
        return self.fake

    def __exit__(self, *a):
        core.create_job, core.get_job = self.saved


def test_v1_videos_accepts_audio_refs():
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "local.wav"
        wav.write_bytes(b"RIFFxxxxWAVE")
        fake = FakeJob(str(Path(d) / "o.mp4"))
        with _Patched(fake):
            client = TestClient(app)
            png = "data:image/png;base64," + base64.b64encode(b"PNG").decode()
            mp3 = "data:audio/mpeg;base64," + base64.b64encode(b"MP3DATA").decode()
            r = client.post("/v1/videos", json={
                "prompt": "x", "size": "864x480", "input_reference": [png],
                "audios": [mp3], "audio_url": str(wav)})
            assert r.status_code == 200, r.text
            refs = fake.calls[0][1]["refs"]
            assert [x["kind"] for x in refs] == ["image", "audio", "audio"]  # 先圖後音頻
            assert Path(refs[1]["path"]).read_bytes() == b"MP3DATA"
            assert refs[2]["path"] == str(wav)
            # video refs still rejected
            r = client.post("/v1/videos", json={"prompt": "x", "videos": ["a.mp4"]})
            assert r.status_code == 400, r.text
            r = client.post("/v1/videos", json={"prompt": "x", "reference_videos": "v"})
            assert r.status_code == 400, r.text
            # audio value that is neither data URL nor existing abs path
            r = client.post("/v1/videos", json={"prompt": "x", "audio_url": "http://x/a.mp3"})
            assert r.status_code == 400, r.text


def test_mix_worker_sfx_and_validation():
    with tempfile.TemporaryDirectory() as d:
        assets = Path(d) / "assets"
        (assets / "sfx" / "wind").mkdir(parents=True)
        (assets / "sfx" / "wind" / "a.wav").write_bytes(b"x")
        (assets / "sfx" / "wind" / "notes.md").write_text("skip non-audio")
        saved = mix_worker.core.ASSET_ROOT
        mix_worker.core.ASSET_ROOT = assets
        try:
            assert mix_worker._pick_sfx("wind").endswith("a.wav")
            try:
                mix_worker._pick_sfx("nope")
                raise AssertionError("expected ValueError for missing tag")
            except ValueError as e:
                assert "no sfx files" in str(e)
        finally:
            mix_worker.core.ASSET_ROOT = saved
        vid = Path(d) / "v.mp4"
        vid.write_bytes(b"0")
        for params, msg in [
            ({}, "video_path not found"),
            ({"video_path": str(vid), "tracks": "x"}, "tracks must be a list"),
            ({"video_path": str(vid), "tracks": [{"path": "/no.mp3"}]},
             "track file not found"),
            ({"video_path": str(vid), "tracks": [{"sfx_tag": "nope"}]}, "no sfx files"),
            ({"video_path": str(vid), "tracks": [{}]}, "needs path or sfx_tag"),
        ]:
            try:
                mix_worker.run(params, Path(d), lambda *a: None, lambda: False)
                raise AssertionError(f"expected error for {params}")
            except ValueError as e:
                assert msg in str(e), (params, str(e))
        # filter graph (pure command builder, ffmpeg never runs)
        cmd = mix_worker._build_cmd(str(vid), [
            {"path": "s1.wav", "gain_db": -3.0, "start_s": 1.5},
            {"path": "s2.wav"}], Path(d) / "o.mp4")
        assert cmd.count("-i") == 3 and cmd[cmd.index("-i", 1) + 1] == str(vid)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "[1:a]adelay=1500:all=1,volume=-3.0dB[a0];" in fc
        assert "[2:a]adelay=0:all=1,volume=0.0dB[a1];" in fc
        assert fc.endswith("[a0][a1]amix=inputs=2:normalize=0[aout]")
        assert cmd[cmd.index("-map") + 1] == "0:v" and "-c:v" in cmd
        cmd2 = mix_worker._build_cmd(str(vid), [], Path(d) / "o.mp4")
        assert "-an" in cmd2 and "-filter_complex" not in cmd2


def test_v1_music_route():
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.wav"))
        with _Patched(fake):
            client = TestClient(app)
            r = client.post("/v1/music", json={"prompt": "epic", "duration_s": 30})
            assert r.status_code == 200, r.text
            assert fake.calls[0][0] == "music"
            assert fake.calls[0][1] == {"prompt": "epic", "duration_s": 30}
            body = r.json()
            assert body["url"].endswith("/content"), body
            assert abs(body["duration_seconds"] - 0.1) < 0.01
            jid = body["url"].rsplit("/", 2)[-2]
            c = client.get(f"/v1/music/jobs/{jid}/content")
            assert c.status_code == 200 and c.content == _wav_bytes(), c.status_code
            r = client.post("/v1/music", json={"prompt": "x", "duration_s": 5})
            assert r.status_code == 400, r.text
            r = client.post("/v1/music", json={"prompt": "  "})
            assert r.status_code == 400, r.text


def test_speech_duration_seconds():
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.wav"))
        with _Patched(fake):
            client = TestClient(app)
            r = client.post("/v1/audio/speech", json={"input": "你好"})
            assert r.status_code == 200, r.text
            assert abs(r.json()["duration_seconds"] - 0.1) < 0.01


def test_concat_music_segments():
    with tempfile.TemporaryDirectory() as d:
        a, b, seg = (Path(d) / n for n in ("a.mp4", "b.mp4", "m.wav"))
        for pth in (a, b, seg):
            pth.write_bytes(b"0")
        try:
            concat_worker.run({"shots": [str(a), str(b)], "music_segments": "x"},
                              Path(d), lambda *x: None, lambda: False)
            raise AssertionError("expected ValueError for bad music_segments")
        except ValueError as e:
            assert "music_segments" in str(e)
        # bgm command shape — stub _probe so no real ffprobe runs
        saved_probe = render._probe
        render._probe = lambda path, select_audio: "12.0" if not select_audio else "0"
        try:
            cmd = render.bgm(str(a), [{"path": str(seg), "duration_s": 10},
                                      {"path": str(seg)}], str(Path(d) / "o.mp4"),
                             dry_run=True)
            fc = cmd[cmd.index("-filter_complex") + 1]
            assert "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[g0];" in fc
            assert "[g0][g1]acrossfade=d=1:c1=tri:c2=tri[x1];" in fc
            assert "aloop=loop=-1:size=1000000000,atrim=0:12.0" in fc
            assert "volume=-6.0dB[bgv]" in fc
            assert "[v0a][bgv]amix=inputs=2:normalize=0[aout]" in fc
            assert cmd[cmd.index("-c:v") + 1] == "copy"
            # video without its own audio track: music becomes the output track
            render._probe = lambda path, select_audio: "12.0" if not select_audio else ""
            cmd = render.bgm(str(a), [{"path": str(seg)}], str(Path(d) / "o.mp4"),
                             dry_run=True)
            fc = cmd[cmd.index("-filter_complex") + 1]
            assert "amix" not in fc and cmd[cmd.index("-map") + 3] == "[bgv]"
        finally:
            render._probe = saved_probe


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
