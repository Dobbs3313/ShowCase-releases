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
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist")" = 1.1.6
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$plist")" = 29
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

python3 - "$scratch/numbered.rgb" <<'PYFIXTURE'
from pathlib import Path
import sys

width, height = 64, 48
with Path(sys.argv[1]).open('wb') as stream:
    for frame_number in range(120):
        frame = bytearray([112, 112, 112] * (width * height))
        for bit in range(8):
            level = 240 if frame_number & (1 << bit) else 12
            for y in range(8, 14):
                for x in range(3 + bit * 7, 8 + bit * 7):
                    offset = (y * width + x) * 3
                    frame[offset:offset + 3] = bytes([level] * 3)
        stream.write(frame)
PYFIXTURE
"$tools/ffmpeg" -v error -f rawvideo -pixel_format rgb24 -video_size 64x48 -framerate 30 \
  -i "$scratch/numbered.rgb" -i "$scratch/voice.aiff" \
  -t 4 -c:v ffv1 -ar 48000 -c:a pcm_s16le "$scratch/source.avi"
"$tools/ffprobe" -v error -select_streams v:0 -show_entries stream=time_base -of json "$scratch/source.avi" |
  jq -e '.streams[0].time_base=="1/30"' >/dev/null
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
  -fps_mode:v passthrough -pix_fmt rgb24 -f rawvideo "$scratch/decoded.rgb"
python3 - "$scratch/decoded.rgb" <<'PYREAD'
from pathlib import Path
import sys

width, height = 64, 48
frame_bytes = width * height * 3
raw = Path(sys.argv[1]).read_bytes()
assert len(raw) % frame_bytes == 0
indices = []
for start in range(0, len(raw), frame_bytes):
    index = 0
    for bit in range(8):
        location = start + (10 * width + 5 + bit * 7) * 3
        if sum(raw[location:location + 3]) > 400:
            index |= 1 << bit
    indices.append(index)
assert indices == list(range(67, 86)), indices
print('Clip contained precisely the selected 19 source frames.')
PYREAD
echo 'Exact bundled tools verified timestamped offline transcription and precise clip extraction from AVI.'
