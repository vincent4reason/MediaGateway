#!/usr/bin/env bash
# concat.sh — 把多個鏡頭 mp4 拼接成一段視頻
#
# 用法:
#   concat.sh <鏡頭目錄|list.txt> <輸出.mp4>
#
# 可選環境變數:
#   TRANSITION=  (預設空) 非空時啟用 xfade 過渡（需重編碼），如 fade/smoothleft
#   DUR=0.5     過渡時長（秒）
#
# 無 TRANSITION: concat demuxer，不重編碼（無損），要求所有鏡頭解析度/編碼一致
# 有 TRANSITION: xfade + acrossfade 逐段串接（視頻+音軌同步淡入淡出），需重編碼
set -euo pipefail

SHOT_LIST="${1:?用法: concat.sh <鏡頭目錄|list.txt> <輸出.mp4>}"
OUT="${2:?缺輸出路徑}"
TRANSITION="${TRANSITION:-}"
DUR="${DUR:-0.5}"

command -v ffmpeg  >/dev/null || { echo "錯誤: 缺少 ffmpeg"  >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "錯誤: 缺少 ffprobe" >&2; exit 1; }

# 收集鏡頭列表（目錄按文件名排序，shot id 字典序即時間序；或直接給 list.txt）
SHOTS=()
if [ -d "$SHOT_LIST" ]; then
    for f in "$SHOT_LIST"/*.mp4; do
        [ -f "$f" ] && SHOTS+=("$f")
    done
    [ "${#SHOTS[@]}" -gt 0 ] || { echo "錯誤: 目錄中沒有 mp4: $SHOT_LIST" >&2; exit 1; }
elif [ -f "$SHOT_LIST" ]; then
    while IFS= read -r f; do
        [ -n "$f" ] && SHOTS+=("$f")
    done < <(sed -n "s/^file '\(.*\)'$/\1/p" "$SHOT_LIST")
    [ "${#SHOTS[@]}" -gt 0 ] || { echo "錯誤: list.txt 沒有有效條目" >&2; exit 1; }
else
    echo "錯誤: 輸入既不是目錄也不是文件: $SHOT_LIST" >&2
    exit 1
fi

[ "${#SHOTS[@]}" -ge 2 ] || { echo "錯誤: 需要至少 2 個鏡頭" >&2; exit 1; }

if [ -z "$TRANSITION" ]; then
    # 無損拼接：concat demuxer 不重編碼；鏡頭參數不一致時報錯，改用 TRANSITION=fade
    LIST_FILE="$(mktemp)"
    trap 'rm -f "$LIST_FILE"' EXIT
    for f in "${SHOTS[@]}"; do
        printf "file '%s'\n" "$f" >> "$LIST_FILE"
    done
    ffmpeg -y -f concat -safe 0 -i "$LIST_FILE" -c copy "$OUT"
else
    # xfade 過渡：需所有鏡頭同解析度/幀率；音軌必需
    for f in "${SHOTS[@]}"; do
        ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$f" \
            | grep -q . || { echo "錯誤: 鏡頭無音軌（xfade 需要）: $f" >&2; exit 1; }
    done

    DURS=()
    for f in "${SHOTS[@]}"; do
        d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
        DURS+=("$d")
    done

    # 串接 xfade/acrossfade 鏈，offset = 上一段起點 + 上段時長 - 過渡時長
    FC=""
    PREV_V="[0:v]"
    PREV_A="[0:a]"
    OFF=0
    N=${#SHOTS[@]}
    for ((i=1; i<N; i++)); do
        OFF=$(awk -v s="$OFF" -v d="${DURS[i-1]}" -v D="$DUR" \
            'BEGIN{printf "%.3f", s + d - D}')
        FC+="${PREV_V}[${i}:v]xfade=transition=${TRANSITION}:duration=${DUR}:offset=${OFF}[v${i}];"
        FC+="${PREV_A}[${i}:a]acrossfade=d=${DUR}[a${i}];"
        PREV_V="[v${i}]"
        PREV_A="[a${i}]"
    done

    ARGS=()
    for f in "${SHOTS[@]}"; do ARGS+=(-i "$f"); done
    ffmpeg -y "${ARGS[@]}" -filter_complex "$FC" \
        -map "[v$((N-1))]" -map "[a$((N-1))]" \
        -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k "$OUT"
fi

echo "完成: $OUT"
