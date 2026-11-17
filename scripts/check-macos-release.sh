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

"$tools/ffmpeg" -v error -f lavfi -i 'testsrc2=size=192x128:rate=30' \
  -i "$scratch/voice.aiff" -t 4 -c:v mpeg4 -g 120 -bf 2 -c:a aac "$scratch/source.mp4"
"$tools/ffmpeg" -v error -nostdin -ss 1.135000 -accurate_seek -i "$scratch/source.mp4" \
  -map 0:v:0 -map '0:a?' -map_chapters -1 -vf trim=end=1.231000 -af atrim=end=1.231000 \
  -fps_mode:v passthrough -enc_time_base:v 1:60000 -c:v mpeg4 -b:v 8M -c:a aac -b:a 192k \
  -movflags +faststart "$scratch/clip.mp4"
"$tools/ffprobe" -v error -show_entries 'format=duration:stream=codec_type,codec_name' -of json "$scratch/clip.mp4" |
  jq -e 'any(.streams[]; .codec_type=="video" and .codec_name=="mpeg4")
    and any(.streams[]; .codec_type=="audio" and .codec_name=="aac")
    and ((.format.duration|tonumber) >= 1.2 and (.format.duration|tonumber) <= 1.3)' >/dev/null
"$tools/ffmpeg" -v error -i "$scratch/clip.mp4" -f null -
echo 'Exact bundled tools decoded a newly encoded clip and produced offline transcription with timestamps.'
