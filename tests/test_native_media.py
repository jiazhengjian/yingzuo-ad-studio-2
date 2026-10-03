"""Exercise real local FFmpeg: original sound survives the native export path."""
import json
import hashlib
import re
import array
import wave
from pathlib import Path
import pytest

from app import media
from app.config import Settings


@pytest.mark.parametrize("source_fps", [24, 30])
def test_native_export_preserves_source_sound_and_does_not_invent_subtitles(tmp_path, source_fps):
    ffmpeg = Settings().ffmpeg_path
    sources = {}
    for index, frequency in enumerate((440, 660)):
        source = tmp_path / f"source-{index}.mp4"
        media.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=72x128:r={source_fps}:d=2",
                   "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=2", "-shortest",
                   "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", source])
        sources[str(index)] = str(source)
    manifest = {"fps": 24, "width": 720, "height": 1280, "total_frames": 72,
                "audio_strategy": "native", "audio_asset_id": None, "captions": [],
                "caption_alignment": "native_audio_untranscribed", "cta": "legacy text must not return",
                "segments": [
                    {"asset_id": "0", "source_in_frame": 0, "source_out_frame": 48, "timeline_in_frame": 0, "timeline_out_frame": 48},
                    {"asset_id": "1", "source_in_frame": 0, "source_out_frame": 24, "timeline_in_frame": 48, "timeline_out_frame": 72}]}
    output = tmp_path / "native" / "output.mp4"
    result = media.compose(manifest, sources, output, ffmpeg)
    assert result["has_audio"] and result["frames"] == 72
    assert result["fps"] == 24 and abs(result["duration_seconds"] - 3) < .01
    _, volume = media.run([ffmpeg, "-v", "info", "-i", output, "-af", "volumedetect", "-vn", "-f", "null", "-"])
    assert float(re.search(r"mean_volume:\s*([-\d.]+) dB", volume)[1]) > -40
    # Distinct tones must survive on both sides of the edit, in their original
    # order. Merely checking that an audio stream exists could pass with silence
    # or an unrelated replacement voice track.
    decoded = tmp_path / "decoded.wav"
    media.run([ffmpeg, "-v", "error", "-i", output, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", decoded])
    with wave.open(str(decoded)) as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
    for start, frequency in ((0.3, 440), (2.2, 660)):
        part = samples[round(start * 16000):round((start + 0.5) * 16000)]
        crossings = sum(a <= 0 < b for a, b in zip(part, part[1:]))
        assert abs(crossings / 0.5 - frequency) < 10
    captions = (output.parent / "compose_work/captions.ass").read_text()
    assert "Dialogue:" not in captions and "Style: CTA" not in captions
    assert "cta" not in json.loads((output.parent / "manifest.json").read_text())
    assert manifest["cta"] == "legacy text must not return"  # Old input remains immutable.


@pytest.mark.parametrize("mixed_environment_track", [False, True])
def test_explicit_no_speech_can_add_only_missing_local_silence(tmp_path, mixed_environment_track):
    ffmpeg = Settings().ffmpeg_path
    sources = {}
    for index in range(2):
        source = tmp_path / f"silent-source-{index}.mp4"
        command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=72x128:r=24:d=2"]
        if index == 1 and mixed_environment_track:
            command += ["-f", "lavfi", "-i", "sine=frequency=660:duration=2", "-shortest", "-c:a", "aac"]
        command += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", source]
        media.run(command)
        sources[str(index)] = str(source)
    hashes = {key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key, path in sources.items()}
    manifest = {"fps": 24, "width": 720, "height": 1280, "total_frames": 48,
                "audio_strategy": "native", "production": {"speech_mode": "none"}, "captions": [],
                "segments": [
                    {"asset_id": "0", "source_in_frame": 12, "source_out_frame": 36, "timeline_in_frame": 0, "timeline_out_frame": 24},
                    {"asset_id": "1", "source_in_frame": 0, "source_out_frame": 24, "timeline_in_frame": 24, "timeline_out_frame": 48}]}
    output = tmp_path / "no-speech" / "output.mp4"
    result = media.compose(manifest, sources, output, ffmpeg)
    assert result["frames"] == 48 and result["has_audio"] and result["fps"] == 24
    assert abs(result["duration_seconds"] - 2) < .01
    expected = ["0"] if mixed_environment_track else ["0", "1"]
    assert result["local_silence_asset_ids"] == expected
    stored = json.loads((output.parent / "manifest.json").read_text())
    assert stored["local_silence_asset_ids"] == expected
    assert "local_silence_asset_ids" not in manifest
    assert "Dialogue:" not in (output.parent / "compose_work/captions.ass").read_text()
    decoded = tmp_path / "no-speech.wav"
    media.run([ffmpeg, "-v", "error", "-i", output, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", decoded])
    with wave.open(str(decoded)) as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
    assert max(abs(value) for value in samples[3200:12800]) <= 1
    second = samples[19200:28800]
    if mixed_environment_track:
        crossings = sum(a <= 0 < b for a, b in zip(second, second[1:]))
        assert abs(crossings / .6 - 660) < 10  # Existing environmental sound survives.
    else:
        assert max(abs(value) for value in second) <= 1
    assert hashes == {key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key, path in sources.items()}


@pytest.mark.parametrize("speech_mode", [None, "voiceover", "character_dialogue"])
def test_missing_source_audio_never_becomes_silent_speech_ad(tmp_path, speech_mode):
    ffmpeg = Settings().ffmpeg_path
    source = tmp_path / "missing-audio.mp4"
    media.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=72x128:r=24:d=1",
               "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", source])
    manifest = {"fps": 24, "width": 720, "height": 1280, "total_frames": 24, "audio_strategy": "native",
        "segments": [{"asset_id": "source", "source_in_frame": 0, "source_out_frame": 24,
                      "timeline_in_frame": 0, "timeline_out_frame": 24}]}
    if speech_mode is not None:
        manifest["production"] = {"speech_mode": speech_mode}
    output = tmp_path / "spoken" / "output.mp4"
    with pytest.raises(media.MediaError, match="缺少音轨"):
        media.compose(manifest, {"source": str(source)}, output, ffmpeg)
    assert not output.exists()
