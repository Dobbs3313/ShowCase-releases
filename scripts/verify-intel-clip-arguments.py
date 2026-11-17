#!/usr/bin/env python3
"""Verify the app's exact generated clip argv with the signed Intel tools."""

import argparse
import json
import math
import platform
import struct
import subprocess
import wave
from hashlib import sha256
from pathlib import Path

from importlib import import_module
fixture = import_module("diagnose-intel-chroma")
FFPROBE_SHA256 = "c621f88a65b3349ceadf21e07df4532678aa1fdc1f9fde72a70104dae193f019"
RATE = 48000
START_SAMPLE, END_SAMPLE = 105888, 137136


def tone_fixture(path):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, RATE, 0, "NONE", ""))
        start, phase, pcm = 0, 0.0, bytearray()
        for end, hz in [(START_SAMPLE, 440), (END_SAMPLE, 880), (4 * RATE, 1320)]:
            increment = math.tau * hz / RATE
            for i in range(end - start):
                pcm.extend(struct.pack("<h", round(10000 * math.sin(phase + i * increment))))
            phase += (end - start) * increment
            start = end
        audio.writeframes(pcm)


def verify_audio(pcm):
    samples = list(struct.unpack(f"<{len(pcm) // 2}h", pcm))
    window_size = 960
    waveforms = {
        hz: [(math.sin(math.tau * hz * n / RATE), math.cos(math.tau * hz * n / RATE))
             for n in range(window_size)]
        for hz in (440, 880, 1320)
    }
    audible = 0
    min_ratio = math.inf
    if not END_SAMPLE - START_SAMPLE - 1024 <= len(samples) <= END_SAMPLE - START_SAMPLE + 2304:
        raise ValueError(f"Unexpected decoded AAC sample count {len(samples)}.")
    for start in range(0, len(samples) - window_size + 1, window_size):
        window = samples[start:start + window_size]
        if sum(abs(x) for x in window) < window_size * 140:
            continue
        audible += 1
        powers = {}
        for hz, function in waveforms.items():
            sin = sum(value * basis[0] for value, basis in zip(window, function))
            cos = sum(value * basis[1] for value, basis in zip(window, function))
            powers[hz] = sin * sin + cos * cos
        ratio = powers[880] / max(1, powers[440], powers[1320])
        min_ratio = min(min_ratio, ratio)
        if ratio < 3:
            raise ValueError(f"Unexpected audio at window {start}: 880 Hz ratio {ratio:.2f}.")
    if audible < 31:
        raise ValueError(f"Only {audible} audible 20 ms windows.")
    return {"samples": len(samples), "880HzWindows": audible, "minInsideToOutsidePower": min_ratio}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bin", type=Path)
    parser.add_argument("invocations", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--architecture", choices=["x86_64", "arm64"], required=True)
    opts = parser.parse_args()
    if platform.machine() != opts.architecture:
        raise ValueError("Native architecture does not match requested lane.")
    digest = {"ffmpeg": fixture.TOOL_SHA256, "ffprobe": FFPROBE_SHA256}
    binaries = {name: opts.bin.resolve() / name for name in digest}
    for name, path in binaries.items():
        if sha256(path.read_bytes()).hexdigest() != digest[name]:
            raise ValueError(f"Unexpected bundled bytes for {name}.")
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)], check=True)
    plan = json.loads(opts.invocations.read_text())
    invocations = plan["invocations"]
    base = fixture.TRIM
    expected = {"bgr0": base + fixture.SCALE, "yuv420p": base}
    if plan["selectionMicroseconds"] != [2206000, 2857000] or set(invocations) != set(expected):
        raise ValueError("Unexpected app source selection or probe formats.")
    for fmt, filters in expected.items():
        args = invocations[fmt]
        if args.count("@INPUT@") != 1 or args.count("@OUTPUT@") != 1 or args[args.index("-vf") + 1] != filters:
            raise ValueError(f"Unexpected sealed {fmt} arguments.")
    out = opts.output.resolve()
    out.mkdir()
    tones = out / "three-tones.wav"
    tone_fixture(tones)
    report = {
        "architecture": platform.machine(),
        "toolsSha256": digest,
        "sourceSha": plan["sourceSha"],
        "argumentSha256": sha256(opts.invocations.read_bytes()).hexdigest(),
        "selectionMicroseconds": plan["selectionMicroseconds"],
        "cases": {},
    }

    def ffmpeg(*args, data=None):
        return subprocess.run([str(binaries["ffmpeg"]), "-v", "error", "-nostdin", *map(str, args)],
                              input=data, capture_output=True, check=True, timeout=30).stdout

    def ffprobe(path):
        data = subprocess.check_output([
            str(binaries["ffprobe"]), "-v", "error", "-count_packets", "-show_streams",
            "-of", "json", str(path),
        ], timeout=30)
        return json.loads(data)["streams"]

    for name, fmt, colored in [("neutral-bgr0", "bgr0", False),
                                ("saturated-bgr0", "bgr0", True),
                                ("common-yuv420p", "yuv420p", True)]:
        source = out / f"{name}.avi"
        rgb = fixture.rgb_fixture(colored)
        data = fixture.planar_fixture(rgb) if fmt == "yuv420p" else rgb
        ffmpeg(
            "-f", "rawvideo", "-pixel_format", "yuv420p" if fmt == "yuv420p" else "rgb24",
            "-video_size", "64x48", "-framerate", "30", "-i", "pipe:0",
            "-i", tones, "-c:v", "ffv1", "-pix_fmt", fmt, "-c:a", "pcm_s16le", source, data=data,
        )
        original = ffprobe(source)
        assert original[0]["pix_fmt"] == fmt and original[0]["time_base"] == "1/30"
        assert int(original[1]["sample_rate"]) == RATE
        target = out / f"{name}.mp4"
        collected = []
        for i in range(opts.repeats):
            command = [
                str(source) if item == "@INPUT@" else str(target) if item == "@OUTPUT@" else item
                for item in invocations[fmt]
            ]
            subprocess.run([str(binaries["ffmpeg"]), *command], capture_output=True, check=True, timeout=30)
            streams = ffprobe(target)
            assert streams[0]["codec_name"] == "mpeg4" and streams[0]["pix_fmt"] == "yuv420p"
            assert (streams[0]["width"], streams[0]["height"]) == (64, 48)
            assert int(streams[0]["nb_read_packets"]) == len(fixture.SELECTED)
            assert streams[1]["codec_name"] == "aac" and int(streams[1]["sample_rate"]) == RATE
            observations = {}
            for ignored in (False, True):
                decode = ["-ignore_editlist", "1"] if ignored else []
                pixels = ffmpeg(*decode, "-i", target, "-map", "0:v:0",
                                "-fps_mode:v", "passthrough", "-pix_fmt", "yuv420p",
                                "-f", "rawvideo", "pipe:1")
                picture = fixture.inspect_yuv(pixels, colored)
                if not picture["ok"]:
                    raise ValueError(f"{name} repeat {i}, ignore edit list={ignored}: {picture}.")
                luma = [pixels[point] for point in fixture.MARKERS]
                assert any(16 <= value <= 35 for value in luma) and any(213 <= value <= 231 for value in luma), luma
                pcm = ffmpeg(*decode, "-i", target, "-map", "0:a:0", "-ac", "1",
                             "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1")
                observations[str(ignored)] = {"picture": picture, "lumaFirstFrame": luma, "audio": verify_audio(pcm)}
            collected.append(observations)
        report["cases"][name] = {
            "originalPixelFormat": fmt, "width": 64, "height": 48,
            "runs": collected, "lastOutputSHA256": sha256(target.read_bytes()).hexdigest(),
        }
        print(f"{name}: {opts.repeats}/{opts.repeats} exact {platform.machine()} clips, "
              "frame IDs 67–85, 64×48, color and 880 Hz verified with and without edit lists.",
              flush=True)
    (out / "verification.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
