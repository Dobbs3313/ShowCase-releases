#!/usr/bin/env python3
"""Measure chroma before and after encoding precise synthetic AVI clips."""

import argparse
import json
import platform
import subprocess
from hashlib import sha256
from pathlib import Path

WIDTH, HEIGHT = 64, 48
FRAME_COUNT = 120
SELECTED = list(range(67, 86))
MARKERS = [10 * WIDTH + 5 + bit * 7 for bit in range(8)]
LUMA_SIZE = WIDTH * HEIGHT
CHROMA_SIZE = LUMA_SIZE // 4
FRAME_SIZE = LUMA_SIZE + 2 * CHROMA_SIZE
TOOL_SHA256 = "ab79bb0b86615203cc0e0af94a6632ac2fde6316b29f6b4e4f8a5bd4df7d87d6"
TRIM = "settb=1/1000000000,trim=start=2.206000:end=2.857000,setpts=PTS-2.206000/TB"
SCALE = ",scale=iw:ih:flags=bilinear+accurate_rnd+bitexact,format=yuv420p"


def rgb_fixture(colored):
    result = bytearray()
    for number in range(FRAME_COUNT):
        frame = bytearray([112, 112, 112] * LUMA_SIZE)
        for bit in range(8):
            level = 240 if number & (1 << bit) else 12
            for y in range(8, 14):
                for x in range(3 + bit * 7, 8 + bit * 7):
                    at = (y * WIDTH + x) * 3
                    frame[at:at + 3] = bytes([level] * 3)
        if colored:
            for y in range(24, 44):
                for x in range(WIDTH):
                    color = (220, 32, 32) if x < 21 else ((32, 200, 32) if x < 43 else (32, 32, 220))
                    at = (y * WIDTH + x) * 3
                    frame[at:at + 3] = bytes(color)
        result.extend(frame)
    return bytes(result)


def planar_fixture(rgb):
    """Create studio-range BT.601 YUV independently of the FFmpeg scaler."""
    def convert(r, g, b):
        return ((66*r + 129*g + 25*b + 128) // 256 + 16,
                (-38*r - 74*g + 112*b + 128) // 256 + 128,
                (112*r - 94*g - 18*b + 128) // 256 + 128)

    output = bytearray()
    for start in range(0, len(rgb), LUMA_SIZE * 3):
        pixels = [convert(*rgb[i:i + 3]) for i in range(start, start + LUMA_SIZE * 3, 3)]
        output.extend(p[0] for p in pixels)
        for channel in (1, 2):
            for y in range(0, HEIGHT, 2):
                for x in range(0, WIDTH, 2):
                    p = y * WIDTH + x
                    neighbors = [pixels[p + offset][channel] for offset in (0, 1, WIDTH, WIDTH + 1)]
                    output.append((sum(neighbors) + 2) // 4)
    return bytes(output)


def inspect_yuv(raw, colored):
    if len(raw) != FRAME_SIZE * len(SELECTED):
        return {"ok": False, "bytes": len(raw), "expectedBytes": FRAME_SIZE * len(SELECTED)}
    ids, chroma, patches = [], bytearray(), []
    for start in range(0, len(raw), FRAME_SIZE):
        frame = raw[start:start + FRAME_SIZE]
        ids.append(sum(1 << bit for bit, at in enumerate(MARKERS) if frame[at] > 128))
        u = frame[LUMA_SIZE:LUMA_SIZE + CHROMA_SIZE]
        v = frame[LUMA_SIZE + CHROMA_SIZE:]
        chroma.extend(u)
        chroma.extend(v)
        # Read the interior of each colored area, away from subsampling edges.
        positions = [(36 // 2) * (WIDTH // 2) + x // 2 for x in (10, 32, 52)]
        patches.append([(u[p], v[p]) for p in positions])
    color_ok = (
        all(r[0] < 122 and r[1] >= 165 and
            g[0] < 112 and g[1] < 112 and
            b[0] >= 160 and b[1] < 145 for r, g, b in patches)
        if colored else min(chroma) >= 125 and max(chroma) <= 131
    )
    return {
        "ok": ids == SELECTED and color_ok,
        "frames": ids,
        "chromaRange": [min(chroma), max(chroma)],
        "centerPatchesFirstFrame": patches[0],
        "yuvSha256": sha256(raw).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ffmpeg", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repeats", type=int, default=6)
    options = parser.parse_args()
    output = options.output.resolve()
    output.mkdir()
    ffmpeg = options.ffmpeg.resolve()
    if sha256(ffmpeg.read_bytes()).hexdigest() != TOOL_SHA256:
        raise ValueError("Bundled FFmpeg does not match the reviewed release bytes.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(ffmpeg)], check=True)

    def tool(*args, data=None):
        p = subprocess.run(
            [str(ffmpeg), "-nostdin", "-v", "error", *map(str, args)],
            input=data, capture_output=True, timeout=20, check=False,
        )
        if p.returncode:
            raise RuntimeError(p.stderr.decode(errors="replace").strip())
        return p.stdout

    scenarios = {
        "default": (),
        "scalar": ("-cpuflags", "0"),
        "filter1": ("-filter_threads", "1"),
        "decoder1": (),
        "encoder1": (),
        "flags": ("-sws_flags", "bilinear+accurate_rnd+bitexact"),
        "explicit": (),
        "explicit_scalar": ("-cpuflags", "0"),
    }
    report = {"architecture": platform.machine(), "toolSha256": TOOL_SHA256, "cases": {}}
    retained = output / "samples"
    retained.mkdir()

    for name, colored, is_planar in [
        ("gray-bgr0", False, False), ("color-bgr0", True, False), ("gray-yuv420p", False, True)
    ]:
        source = output / f"{name}.avi"
        reference = rgb_fixture(colored)
        data = planar_fixture(reference) if is_planar else reference
        pix_fmt = "yuv420p" if is_planar else "rgb24"
        tool(
            "-f", "rawvideo", "-pixel_format", pix_fmt, "-video_size", "64x48",
            "-framerate", "30", "-i", "pipe:0", "-f", "lavfi", "-i",
            "sine=frequency=880:sample_rate=48000:duration=4", "-t", "4",
            "-c:v", "ffv1", "-pix_fmt", "yuv420p" if is_planar else "bgr0",
            "-c:a", "pcm_s16le", source, data=data,
        )
        saved_source = tool(
            "-i", source, "-map", "0:v:0", "-pix_fmt", pix_fmt,
            "-fps_mode:v", "passthrough", "-f", "rawvideo", "pipe:1",
        )
        if saved_source != data:
            raise ValueError(f"Source fixture failed its lossless {pix_fmt} readback: {name}.")

        for variant, options_before_input in scenarios.items():
            if is_planar and variant not in {"default", "encoder1", "scalar"}:
                continue
            config = TRIM + (SCALE if variant.startswith("explicit") else "")
            base = [
                *options_before_input, "-copyts", "-start_at_zero", "-ss", "1",
                "-noaccurate_seek",
                *(["-threads:v", "1"] if variant == "decoder1" else []),
                "-i", source, "-map", "0:v:0", "-vf", config,
            ]
            key = f"{name}/{variant}"
            sample = output / "current.mp4"
            rec = {"scaleBeforeMpeg4": [], "export": []}
            report["cases"][key] = rec
            for iteration in range(options.repeats):
                try:
                    if variant not in {"encoder1"}:
                        before = tool(
                            *base, "-fps_mode:v", "passthrough", "-an",
                            "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
                        )
                        rec["scaleBeforeMpeg4"].append(inspect_yuv(before, colored))
                    tool(
                        *base, "-map", "0:a?", "-map_chapters", "-1",
                        "-filter:a:0", "atrim=start_pts=105888:end_pts=137136,asetpts=PTS-2.206000/TB",
                        "-fps_mode:v", "passthrough", "-enc_time_base:v", "1:60000",
                        "-c:v", "mpeg4", "-b:v", "8M",
                        *(["-threads:v", "1"] if variant == "encoder1" else []),
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                        "-y", sample,
                    )
                    args = ["-ignore_editlist", "1", "-i", sample, "-map", "0:v:0",
                            "-fps_mode:v", "passthrough", "-pix_fmt", "yuv420p",
                            "-f", "rawvideo", "pipe:1"]
                    normal = inspect_yuv(tool(*args), colored)
                    scalar = inspect_yuv(tool("-cpuflags", "0", "-threads:v", "1", *args), colored)
                    rec["export"].append({"normal": normal, "scalarDecode": scalar})
                    suffix = "good" if normal["ok"] and scalar["ok"] else "bad"
                    destination = retained / f"{name}-{variant}-{suffix}.mp4"
                    if not destination.exists():
                        destination.write_bytes(sample.read_bytes())
                except RuntimeError as error:
                    rec["error"] = str(error)
                    break
            conversions = rec["scaleBeforeMpeg4"]
            encoded = rec["export"]
            print(
                f"{key}: conversion {sum(x['ok'] for x in conversions)}/{len(conversions)}; "
                f"MPEG4 {sum(x['normal']['ok'] for x in encoded)}/{len(encoded)}; "
                f"scalar decoder {sum(x['scalarDecode']['ok'] for x in encoded)}/{len(encoded)}; "
                f"error {rec.get('error', 'none')}",
                flush=True,
            )

    (output / "diagnostic.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
