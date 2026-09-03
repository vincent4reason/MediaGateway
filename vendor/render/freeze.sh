#!/usr/bin/env bash
# freeze.sh — 在指定時刻定格畫面（後期卡點），重編碼輸出
#
# 用法:
#   freeze.sh <輸入.mp4> <卡點表> <輸出.mp4>
# 卡點表: "T1:D1;T2:D2" — 在 T 秒處定格 D 秒（可多個，分號分隔）
# 示例: freeze.sh in.mp4 "3:2;7:1.5" out.mp4
#
# 依賴: ffmpeg（freezeframe 需要 tpad/loop 方案，此腳本用 split+loop+concat）

set -euo pipefail

IN="${1:?用法: freeze.sh <輸入> <卡點表> <輸出>}"
POINTS="${2:?缺卡點表，格式 T1:D1;T2:D2}"
OUT="${3:?缺輸出路徑}"

command -v ffmpeg >/dev/null || { echo "錯誤: 缺少 ffmpeg" >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "錯誤: 缺少 ffprobe" >&2; exit 1; }

FPS=$(ffprobe -v error -select_streams v -show_entries stream=avg_frame_rate -of csv=p=0 "$IN" | cut -d/ -f1)
[ -n "$FPS" ] && [ "$FPS" -gt 0 ] || FPS=24

# 解析卡點
IFS=';' read -ra ITEMS <<< "$POINTS"
[ "${#ITEMS[@]}" -ge 1 ] || { echo "錯誤: 卡點表為空" >&2; exit 1; }

# 構建 filter：對每個卡點 split → trim 前段 + freeze 段 + 後段 → concat
# 卡點時間用「當前流時間線」：原始時間 + 已累積的定格偏移
OFFSET=0
INPUT_LABEL="0:v"
FILTERS=()
CHAIN=()
IDX=0
for item in "${ITEMS[@]}"; do
    T="${item%%:*}"
    D="${item##*:}"
    [ "$T" = "$item" ] && { echo "錯誤: 卡點格式需 T:D: $item" >&2; exit 1; }
    CUR=$(python3 -c "print(f'{float($T)+$OFFSET:.3f}')")   # 當前流時間線上的卡點
    NEXT=$(python3 -c "print(f'{float($CUR)+0.042:.3f}')")
    # 3 路 split：前段 + 定格幀（tpad 克隆）+ 後段
    A="v${IDX}a"; B="v${IDX}b"; C="v${IDX}c"
    A1="v${IDX}a1"; B1="v${IDX}b1"; C1="v${IDX}c1"
    FILTERS+=(
        "[$INPUT_LABEL]split=3[$A][$B][$C]"
        "[$A]trim=start=0:end=$CUR,setpts=PTS-STARTPTS[$A1]"
        "[$B]trim=start=$CUR:end=$NEXT,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=$D[$B1]"
        "[$C]trim=start=$NEXT,setpts=PTS-STARTPTS[$C1]"
    )
    CHAIN+=("[$A1][$B1][$C1]concat=n=3:v=1:a=0[out${IDX}]")
    OFFSET=$(python3 -c "print(f'{$OFFSET}+{$D}')")   # 累積定格偏移
    INPUT_LABEL="out${IDX}"
    IDX=$((IDX+1))
done

FINAL="[out$((IDX-1))]"
FILTER_COMPLEX="$(IFS=';'; echo "${FILTERS[*]};${CHAIN[*]}")"

ffmpeg -y -v error -i "$IN" -filter_complex "$FILTER_COMPLEX" -map "$FINAL" -c:v libx264 -pix_fmt yuv420p -c:a copy "$OUT"
echo "完成: $OUT ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")s)"
