#!/usr/bin/env bash
# mux.sh — 混音 + 燒錄字幕，合成最終 MP4
#
# 用法:
#   mux.sh <視頻.mp4> <字幕.srt> <輸出.mp4>
#
# 可選環境變數:
#   DIALOGUE_WAVS="a.wav@1.5 b.wav@3.2"  台詞 wav 列表，@ 後為起始秒（預設 0），逐段對齊混入
#   MUSIC_WAV=bgm.wav                   墊底音軌，自動循環到視頻結束
#   MUSIC_VOLUME=0.15   墊底音量（預設 0.15）
#   DIALOGUE_VOLUME=1.0 台詞音量（預設 1.0）
#   SUBTITLE_STYLE=     字幕 force_style（預設苹方/白字黑邊）
set -euo pipefail

VIDEO="${1:?用法: mux.sh <視頻.mp4> <字幕.srt> <輸出.mp4>}"
SRT="${2:?缺字幕文件}"
OUT="${3:?缺輸出路徑}"
DIALOGUE_WAVS="${DIALOGUE_WAVS:-}"
MUSIC_WAV="${MUSIC_WAV:-}"
MUSIC_VOLUME="${MUSIC_VOLUME:-0.15}"
DIALOGUE_VOLUME="${DIALOGUE_VOLUME:-1.0}"
SUBTITLE_STYLE="${SUBTITLE_STYLE:-FontName=PingFang SC,FontSize=18,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=1}"

command -v ffmpeg  >/dev/null || { echo "錯誤: 缺少 ffmpeg"  >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "錯誤: 缺少 ffprobe" >&2; exit 1; }

for f in "$VIDEO" "$SRT"; do
    [ -f "$f" ] || { echo "錯誤: 文件不存在: $f" >&2; exit 1; }
done

# 視頻時長，用於裁剪循環音軌和超長台詞
VDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO")

# 組裝輸入: 0=視頻, 1=墊底(如有), 2..=台詞
INPUTS=(-i "$VIDEO")
FC=""

if [ -n "$MUSIC_WAV" ]; then
    [ -f "$MUSIC_WAV" ] || { echo "錯誤: MUSIC_WAV 不存在: $MUSIC_WAV" >&2; exit 1; }
    INPUTS+=(-stream_loop -1 -i "$MUSIC_WAV")
    FC="[1:a]volume=${MUSIC_VOLUME},atrim=0:${VDUR},asetpts=PTS-STARTPTS[bg];"
fi

if [ -n "$MUSIC_WAV" ]; then
    MIX_IN="[0:a][bg]"
    N=2
else
    MIX_IN="[0:a]"
    N=1
fi
DLG_IDX=0
if [ -n "$DIALOGUE_WAVS" ]; then
    for item in $DIALOGUE_WAVS; do
        WAV="${item%%@*}"
        OFF="${item#*@}"          # 無 @ 時 OFF=item 本身，退化成非數字
        [ "$OFF" != "$WAV" ] || OFF=0
        [ -f "$WAV" ] || { echo "錯誤: 台詞 wav 不存在: $WAV" >&2; exit 1; }
        MS=$(awk -v s="$OFF" 'BEGIN{printf "%d", s*1000}')
        IDX=$N                  # 首個台詞輸入號 = 2(有墊底) 或 1(無墊底)
        INPUTS+=(-i "$WAV")
        FC+="[${IDX}:a]volume=${DIALOGUE_VOLUME},adelay=${MS}:all=1,atrim=0:${VDUR},asetpts=PTS-STARTPTS[d${DLG_IDX}];"
        MIX_IN+="[d${DLG_IDX}]"
        N=$((N+1))
        DLG_IDX=$((DLG_IDX+1))
    done
fi

# normalize=0 保持各軌原音量（默認會按輸入數均分，台詞會被削掉）
FC+="${MIX_IN}amix=inputs=${N}:normalize=0:dropout_transition=0[aout];"
FC+="[0:v]subtitles=filename='${SRT}':force_style='${SUBTITLE_STYLE}'[vout]"

ffmpeg -y "${INPUTS[@]}" -filter_complex "$FC" \
    -map "[vout]" -map "[aout]" \
    -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart "$OUT"

echo "完成: $OUT"
