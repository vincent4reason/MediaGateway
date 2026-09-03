#!/usr/bin/env python3
"""CosyVoice worker client — POST tts_server 的 /tts 生成台词 wav。

API 契约 (tts_server.py, 127.0.0.1:8001):
    POST /tts  JSON {text, prompt_text, prompt_wav, speed, out_path}
      400: text 空 / prompt_wav 不存在 / prompt_text 缺 <|endofprompt|>
      200: {ok, wav_b64, sample_rate}  (out_path 不传时) 或 {ok, path, sample_rate}
    GET  /health -> {ok, model_loaded}

声音克隆 = zero-shot: 每个 voiceId 对应一份参考音频 (prompt_wav) + 参考文本
(prompt_text, 必须含 <|endofprompt|>)。注册表在 voices.json;
relative 路径以 COSYVOICE_ROOT 为基准 (默认 /Users/vincent/tool/cosyvoice)。

用法:
    python client.py --text "你到底想点啊？" --voice C001 --out out.wav
    python client.py --text "..." --voice C001 --speed 1.1 --out out.wav
    python client.py --text "..." --prompt-wav /abs/ref.wav --prompt-text "参考文本<|endofprompt|>" --out out.wav
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = os.environ.get('COSYVOICE_URL', 'http://127.0.0.1:8001')
DEFAULT_ROOT = os.environ.get('COSYVOICE_ROOT', '/Users/vincent/tool/cosyvoice')
VOICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voices.json')

# ponytail: 最多尝试 3 次(含首次)。4xx 不重试(参数错重试无意义),网络/5xx/空结果才重试。
ATTEMPTS = 3
RETRY_DELAY_S = 2.0


def _load_voices():
    if not os.path.exists(VOICES_FILE):
        return {}
    with open(VOICES_FILE, encoding='utf-8') as f:
        return json.load(f)


def _resolve_ref(path: str, root: str) -> str:
    return path if os.path.isabs(path) else os.path.join(root, path)


def _request_once(url: str, payload: dict):
    """单次 POST, 返回 (status, json)。网络错误抛 urllib.error.URLError。"""
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.status, json.loads(resp.read().decode('utf-8'))


def synthesize(text: str, voice: str = None, out: str = 'out.wav',
               prompt_wav: str = None, prompt_text: str = None,
               speed: float = 1.0, base_url: str = DEFAULT_BASE_URL,
               root: str = DEFAULT_ROOT, attempts: int = ATTEMPTS):
    """台詞 text → wav 文件。voice 从 voices.json 取参考音频;
    也可直接传 prompt_wav/prompt_text 覆盖。返回 (out路径, sample_rate)。"""
    if not prompt_wav or not prompt_text:
        voices = _load_voices()
        if voice not in voices:
            known = ', '.join(voices) if voices else '(空, 直接传 --prompt-wav/--prompt-text)'
            raise SystemExit(f'voice 未注册: {voice!r} (可用: {known})')
        v = voices[voice]
        prompt_wav = prompt_wav or _resolve_ref(v['prompt_wav'], root)
        prompt_text = prompt_text or v['prompt_text']

    if '<|endofprompt|>' not in prompt_text:
        raise SystemExit('prompt_text 需含 <|endofprompt|> (CosyVoice 3 要求)')

    payload = {'text': text, 'prompt_text': prompt_text,
               'prompt_wav': prompt_wav, 'speed': speed, 'out_path': None}
    url = f'{base_url.rstrip("/")}/tts'

    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            status, data = _request_once(url, payload)
            if status == 200 and data.get('ok') and data.get('wav_b64'):
                wav = base64.b64decode(data['wav_b64'])
                with open(out, 'wb') as f:
                    f.write(wav)
                print(f'OK {out} ({len(wav)} bytes, sample_rate={data["sample_rate"]})')
                return out, data['sample_rate']
            last_err = f'HTTP {status}: {str(data)[:200]}'
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise SystemExit(f'HTTP {e.code}: {e.reason} (4xx 不重试)') from None
            last_err = f'HTTP {e.code}'
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = f'网络错误: {e}'
        if attempt < attempts:
            print(f'[{attempt}/{attempts}] 失败, {RETRY_DELAY_S}s 后重试: {last_err}', file=sys.stderr)
            time.sleep(RETRY_DELAY_S)
    raise SystemExit(f'重试 {attempts} 次均失败: {last_err}')


def main():
    p = argparse.ArgumentParser(description='CosyVoice 台词生成 client')
    p.add_argument('--text', required=True, help='台詞文本')
    p.add_argument('--voice', help='voices.json 中的 voiceId, 如 C001')
    p.add_argument('--prompt-wav', help='参考音频路径 (覆盖 voice)')
    p.add_argument('--prompt-text', help='参考音频对应文本, 需含 <|endofprompt|> (覆盖 voice)')
    p.add_argument('--speed', type=float, default=1.0)
    p.add_argument('--out', default='out.wav')
    p.add_argument('--url', default=DEFAULT_BASE_URL)
    a = p.parse_args()
    synthesize(a.text, voice=a.voice, out=a.out, prompt_wav=a.prompt_wav,
               prompt_text=a.prompt_text, speed=a.speed, base_url=a.url)


if __name__ == '__main__':
    main()
