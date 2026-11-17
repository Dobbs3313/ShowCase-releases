#!/bin/bash
set -euo pipefail

bundle="$1"
expected_arch="$2"
expected_major="$3"
runtime_version="$(sw_vers -productVersion)"
test "$(uname -m)" = "$expected_arch"
test "${runtime_version%%.*}" = "$expected_major"
printf 'Native runtime %s %s\n' "$expected_arch" "$runtime_version"

plist="$bundle/Contents/Info.plist"
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$plist")" = com.dobbs.showcase
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist")" = 1.1.7
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$plist")" = 30
xcrun stapler validate "$bundle"
codesign --verify --deep --strict "$bundle"
spctl --assess --type execute --verbose=2 "$bundle"

scratch="$(mktemp -d "${RUNNER_TEMP:-/private/tmp}/showcase-smoke.XXXXXX")"
process_id=
cleanup() {
  if [[ -n "$process_id" ]]; then
    kill -TERM "$process_id" 2>/dev/null || true
    wait "$process_id" 2>/dev/null || true
  fi
  find "$scratch" -depth -delete
}
trap cleanup EXIT
mkdir -p "$scratch/profile"/{Documents,'Library/Application Support','Library/Caches','Library/Preferences'}
cat > "$scratch/profile.sb" <<'SB'
(version 1)
(allow default)
(deny network*)
(deny file-read* (require-all (subpath (param "ACTUAL_PROFILE")) (require-not (subpath (param "SCRATCH"))) (require-not (subpath (param "BUNDLE")))))
(deny file-write* (require-all (require-not (subpath (param "SCRATCH"))) (require-not (literal "/dev/null"))))
(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.SecurityServer") (global-name "com.apple.cfprefsd.agent") (global-name "com.apple.cfprefsd.daemon"))
SB
cat > "$scratch/window.swift" <<'SWIFT'
import CoreGraphics
import Foundation

let pid = Int32(CommandLine.arguments[1])!
let windows = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
let visible = windows.contains { entry in
    (entry[kCGWindowOwnerPID as String] as? Int32) == pid
        && (entry[kCGWindowLayer as String] as? Int) == 0
        && (entry[kCGWindowIsOnscreen as String] as? Int) == 1
}
exit(visible ? 0 : 1)
SWIFT
xcrun swiftc "$scratch/window.swift" -o "$scratch/window"
/usr/bin/sandbox-exec -D "SCRATCH=$scratch" -D "ACTUAL_PROFILE=$HOME" -D "BUNDLE=$bundle" -f "$scratch/profile.sb" \
  /usr/bin/env CFFIXED_USER_HOME="$scratch/profile" TMPDIR="$scratch/profile" \
  "$bundle/Contents/MacOS/ShowCase" >"$scratch/app.log" 2>&1 &
process_id="$!"
launch_ok=false
for attempt in $(seq 1 50); do
  if ! kill -0 "$process_id" 2>/dev/null; then
    tail -40 "$scratch/app.log"
    break
  fi
  if "$scratch/window" "$process_id" && [[ -s "$scratch/profile/Documents/litigation_v1.isar" ]]; then
    launch_ok=true
    break
  fi
  sleep 1
done
test "$launch_ok" = true
echo 'Native app opened a visible window and created an isolated case database.'

tools="$bundle/Contents/Resources/bin"
model="$bundle/Contents/Resources/models/ggml-base.bin"
printf '%s  %s\n' 60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe "$model" | shasum -a 256 -c -
/usr/bin/say -o "$scratch/voice.aiff" 'The witness reviewed the delivery record. This is a fictional test exhibit.'
test -s "$scratch/voice.aiff"
"$tools/ffmpeg" -v error -i "$scratch/voice.aiff" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$scratch/voice.wav"
"$tools/whisper" --no-gpu -m "$model" -f "$scratch/voice.wav" -oj -of "$scratch/transcript" -t 2 >"$scratch/transcription.log" 2>&1
jq -e 'any(.transcription[]; (.text|length>0) and (.offsets.to>.offsets.from))' "$scratch/transcript.json" >/dev/null

python3 - "$scratch/numbered.yuv" <<'PYFIXTURE'
from pathlib import Path
import sys

width, height = 192, 128
chroma = bytes([128]) * (width * height // 2)
with Path(sys.argv[1]).open('wb') as stream:
    for frame_number in range(120):
        luma = bytearray([112]) * (width * height)
        for bit in range(8):
            level = 235 if frame_number & (1 << bit) else 16
            for y in range(12, 24):
                for x in range(6 + bit * 14, 18 + bit * 14):
                    luma[y * width + x] = level
        stream.write(luma)
        stream.write(chroma)
PYFIXTURE
cat > "$scratch/read-frames.py" <<'PYREAD'
from pathlib import Path
import sys

path, first, last = sys.argv[1:]
width, height = 192, 128
frame_bytes = width * height * 3 // 2
raw = Path(path).read_bytes()
assert len(raw) % frame_bytes == 0
indices = []
for start in range(0, len(raw), frame_bytes):
    index = 0
    for bit in range(8):
        location = start + 18 * width + 11 + bit * 14
        if raw[location] > 170:
            index |= 1 << bit
    indices.append(index)
expected = list(range(int(first), int(last) + 1))
assert indices == expected, f"{Path(path).name}: {indices}"
print(f'{Path(path).name} matched all {len(expected)} expected source frames.')
PYREAD
"$tools/ffmpeg" -v error -f rawvideo -pixel_format yuv420p -video_size 192x128 -framerate 30 \
  -i "$scratch/numbered.yuv" -i "$scratch/voice.aiff" \
  -t 4 -c:v ffv1 -pix_fmt yuv420p -ar 48000 -c:a pcm_s16le "$scratch/source.avi"
"$tools/ffprobe" -v error -select_streams v:0 -show_entries stream=time_base,pix_fmt -of json "$scratch/source.avi" |
  jq -e '.streams[0].time_base=="1/30" and .streams[0].pix_fmt=="yuv420p"' >/dev/null
"$tools/ffmpeg" -v error -i "$scratch/source.avi" -map 0:v:0 \
  -fps_mode:v passthrough -pix_fmt yuv420p -f rawvideo "$scratch/decoded-source.yuv"
python3 "$scratch/read-frames.py" "$scratch/decoded-source.yuv" 0 119
"$tools/ffmpeg" -v error -nostdin -copyts -start_at_zero -ss 1 -noaccurate_seek -i "$scratch/source.avi" \
  -map 0:v:0 -map '0:a?' -map_chapters -1 \
  -vf 'settb=1/1000000000,trim=start=2.206000:end=2.857000,setpts=PTS-2.206000/TB' \
  -filter:a:0 'atrim=start_pts=105888:end_pts=137136,asetpts=PTS-2.206000/TB' \
  -fps_mode:v passthrough -enc_time_base:v 1:60000 -c:v mpeg4 -b:v 8M -c:a aac -b:a 192k \
  -movflags +faststart "$scratch/clip.mp4"
"$tools/ffprobe" -v error -show_entries 'format=duration:stream=codec_type,codec_name' -of json "$scratch/clip.mp4" |
  jq -e 'any(.streams[]; .codec_type=="video" and .codec_name=="mpeg4")
    and any(.streams[]; .codec_type=="audio" and .codec_name=="aac")
    and ((.format.duration|tonumber) >= 0.63 and (.format.duration|tonumber) <= 0.70)' >/dev/null
"$tools/ffmpeg" -v error -ignore_editlist 1 -i "$scratch/clip.mp4" -map 0:v:0 \
  -fps_mode:v passthrough -pix_fmt yuv420p -f rawvideo "$scratch/decoded-clip.yuv"
python3 "$scratch/read-frames.py" "$scratch/decoded-clip.yuv" 67 85
echo 'Exact bundled tools verified timestamped offline transcription and precise clip extraction from AVI.'
