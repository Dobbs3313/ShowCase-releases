#!/usr/bin/env python3
"""Verify chroma and frame timing for the precise macOS BGR0 clip path."""

import argparse
from hashlib import sha256 as sha256_bytes
import json
from pathlib import Path
import platform
import subprocess

WIDTH, HEIGHT = 64, 48
MARKERS = [10 * WIDTH + 5 + bit * 7 for bit in range(8)]
SIGNED_TOOLS = {
    "ffmpeg": "ab79bb0b86615203cc0e0af94a6632ac2fde6316b29f6b4e4f8a5bd4df7d87d6",
    "ffprobe": "c621f88a65b3349ceadf21e07df4532678aa1fdc1f9fde72a70104dae193f019",
}


def sha256(path):
    return sha256_bytes(path.read_bytes()).hexdigest()


def generate_rgb(path):
    with path.open("wb") as stream:
        for number in range(120):
            frame = bytearray([112, 112, 112] * (WIDTH * HEIGHT))
            for bit in range(8):
                level = 240 if number & (1 << bit) else 12
                for y in range(8, 14):
                    for x in range(3 + bit * 7, 8 + bit * 7):
                        offset = (y * WIDTH + x) * 3
                        frame[offset:offset + 3] = bytes([level] * 3)
            stream.write(frame)


def markers(raw, channels, expected):
    frame_bytes = WIDTH * HEIGHT * channels
    if len(raw) % frame_bytes:
        raise ValueError(f"Raw readback has {len(raw)} bytes, not whole {frame_bytes}-byte frames")
    levels = []
    indices = []
    for start in range(0, len(raw), frame_bytes):
        samples = [
            sum(raw[start + point * channels:start + point * channels + min(3, channels)])
            for point in MARKERS
        ]
        threshold = 128 if channels == 1 else 400
        indices.append(sum(1 << bit for bit, value in enumerate(samples) if value > threshold))
        levels.append(samples)
    flattened = [value for frame in levels for value in frame]
    return {
        "frames": len(indices),
        "matches": indices == expected,
        "markerIds": indices,
        "firstFrameMarkerLevels": levels[0] if levels else [],
        "markerLevelRange": [min(flattened), max(flattened)] if flattened else [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="Exact unpacked signed ShowCase.app")
    parser.add_argument("output", type=Path, help="New or empty directory for synthetic evidence")
    parser.add_argument("--arch", choices=("arm64", "x86_64"))
    parser.add_argument("--inspect", type=Path, help="Read an existing synthetic diagnostic directory without encoding")
    args = parser.parse_args()
    binary_dir = args.app.resolve() / "Contents/Resources/bin"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("The output directory is not empty.")
    for name, digest in SIGNED_TOOLS.items():
        path = binary_dir / name
        if sha256(path) != digest:
            parser.error(f"{name} does not match the signed 1.1.6 candidate.")
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)], check=True)

    prefix = ["/usr/bin/arch", f"-{args.arch}"] if args.arch else []

    def tool(name, *flags):
        return subprocess.check_output([*prefix, str(binary_dir / name), *flags])

    def probe(path):
        return json.loads(tool(
            "ffprobe", "-v", "error", "-count_packets",
            "-show_entries", "format=duration:stream=codec_name,codec_type,pix_fmt,width,height,time_base,start_time,nb_read_packets",
            "-of", "json", str(path),
        ))

    def decode(path, format_name, channels, expected, *, filter_name=None, ignore_editlist=False, scalar=False):
        flags = ["-v", "error", "-nostdin"]
        if scalar:
            flags.extend(["-cpuflags", "0"])
        if ignore_editlist:
            flags.extend(["-ignore_editlist", "1"])
        flags.extend(["-i", str(path), "-map", "0:v:0"])
        if filter_name:
            flags.extend(["-vf", filter_name])
        flags.extend(["-fps_mode:v", "passthrough", "-pix_fmt", format_name, "-f", "rawvideo", "pipe:1"])
        return markers(tool("ffmpeg", *flags), channels, expected)

    def summarize(label, result):
        ids = result["markerIds"]
        print(
            f"{label}: {result['frames']} frames, first/last IDs {ids[:5]}/{ids[-5:]}, "
            f"marker levels {result['markerLevelRange']}, match={result['matches']}",
            flush=True,
        )

    expected_source = list(range(120))
    expected_clip = list(range(67, 86))
    media = args.inspect.resolve() if args.inspect else output
    raw, source, clip = (media / name for name in ("numbered.rgb", "source.avi", "clip.mp4"))
    if not args.inspect:
        generate_rgb(raw)
    generator = markers(raw.read_bytes(), 3, expected_source)
    if not generator["matches"]:
        raise RuntimeError("The generated raw RGB data differs before FFmpeg encodes it.")
    if not args.inspect:
        tool(
            "ffmpeg", "-v", "error", "-nostdin", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", "64x48", "-framerate", "30", "-i", str(raw),
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=4",
            "-t", "4", "-c:v", "ffv1", "-ar", "48000", "-c:a", "pcm_s16le", str(source),
        )
    source_format = probe(source)
    source_video = next(stream for stream in source_format["streams"] if stream["codec_type"] == "video")
    if source_video["codec_name"] != "ffv1" or source_video["time_base"] != "1/30":
        raise RuntimeError(f"Unexpected source fixture format: {source_video}")
    print(f"Source format: ffv1 {source_video['pix_fmt']} at {source_video['time_base']}")
    source_reads = {
        "luma": decode(source, "gray", 1, expected_source, filter_name="format=yuv444p,extractplanes=y"),
        "rgb": decode(source, "rgb24", 3, expected_source),
    }
    if source_video["pix_fmt"] == "bgr0":
        source_reads["nativeBgr0"] = decode(source, "bgr0", 4, expected_source)
    for label, result in source_reads.items():
        summarize(f"Source {label}", result)
    report = {
        "macOS": subprocess.check_output(["/usr/bin/sw_vers", "-productVersion"], text=True).strip(),
        "hostArchitecture": platform.machine(),
        "requestedArchitecture": args.arch,
        "readbackOnly": bool(args.inspect),
        "toolSha256": SIGNED_TOOLS,
        "source": {"sha256": sha256(source), "format": source_format, "rawGenerator": generator, **source_reads},
        "selectionSeconds": [2.206, 2.857],
    }

    if not any(result["matches"] for result in source_reads.values()):
        outcome = "source-FFV1-round-trip-failed-before-export"
    elif args.inspect and not clip.is_file():
        outcome = "source-verified-saved-clip-unavailable"
    else:
        if not args.inspect:
            tool(
                "ffmpeg", "-v", "error", "-nostdin", "-copyts", "-start_at_zero", "-ss", "1",
                "-noaccurate_seek", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-map_chapters", "-1",
                "-vf", "settb=1/1000000000,trim=start=2.206000:end=2.857000,setpts=PTS-2.206000/TB,"
                "scale=iw:ih:flags=bilinear+accurate_rnd+bitexact,format=yuv420p",
                "-filter:a:0", "atrim=start_pts=105888:end_pts=137136,asetpts=PTS-2.206000/TB",
                "-fps_mode:v", "passthrough", "-enc_time_base:v", "1:60000", "-c:v", "mpeg4", "-b:v", "8M",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(clip),
            )
        saved_format = probe(clip)
        saved_video = next(stream for stream in saved_format["streams"] if stream["codec_type"] == "video")
        print(
            f"Saved format: {saved_video['codec_name']} {saved_video['pix_fmt']} "
            f"at {saved_video['time_base']}, {saved_video['nb_read_packets']} video packets"
        )
        planar = tool(
            "ffmpeg", "-v", "error", "-nostdin", "-ignore_editlist", "1", "-i", str(clip),
            "-map", "0:v:0", "-fps_mode:v", "passthrough", "-pix_fmt", "yuv420p",
            "-f", "rawvideo", "pipe:1",
        )
        luma_bytes = WIDTH * HEIGHT
        frame_bytes = luma_bytes * 3 // 2
        assert len(planar) == frame_bytes * len(expected_clip)
        native_luma = b"".join(
            planar[start:start+luma_bytes] for start in range(0,len(planar),frame_bytes)
        )
        chroma = b"".join(
            planar[start+luma_bytes:start+frame_bytes] for start in range(0,len(planar),frame_bytes)
        )
        raw_yuv_ok = markers(native_luma, 1, expected_clip)["matches"] and min(chroma) >= 126 and max(chroma) <= 130
        print(f"Native YUV planes: expected numbered luma; neutral chroma range {min(chroma)} to {max(chroma)}, match={raw_yuv_ok}")
        saved_reads = {
            "nativeLuma": decode(clip, "gray", 1, expected_clip, filter_name="extractplanes=y", ignore_editlist=True),
            "rgb": decode(clip, "rgb24", 3, expected_clip, ignore_editlist=True),
        }
        if not saved_reads["rgb"]["matches"]:
            saved_reads["scalarRgb"] = decode(clip, "rgb24", 3, expected_clip, ignore_editlist=True, scalar=True)
        if not saved_reads["nativeLuma"]["matches"]:
            saved_reads["scalarLuma"] = decode(
                clip, "gray", 1, expected_clip, filter_name="extractplanes=y", ignore_editlist=True, scalar=True
            )
        for label, result in saved_reads.items():
            summarize(f"Saved {label}", result)
        report["saved"] = {"sha256": sha256(clip), "format": saved_format, **saved_reads}
        if saved_video["nb_read_packets"] != str(len(expected_clip)):
            outcome = "saved-video-packet-count-discrepancy"
        elif not any(result["matches"] for result in saved_reads.values()):
            outcome = "saved-image-or-shared-decoder-failed"
        elif not saved_reads["nativeLuma"]["matches"]:
            outcome = "luma-readback-discrepancy"
        elif not raw_yuv_ok:
            outcome = "chroma-readback-discrepancy"
        elif not source_reads["rgb"]["matches"] or not saved_reads["rgb"]["matches"]:
            outcome = "RGB-readback-discrepancy"
        elif not all(result["matches"] for result in source_reads.values()):
            outcome = "source-readback-discrepancy"
        else:
            outcome = "all-marker-readbacks-exact"
    report["outcome"] = outcome
    (output / "diagnostic.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Outcome: {outcome}")
    return int(outcome != "all-marker-readbacks-exact")


if __name__ == "__main__":
    raise SystemExit(main())
