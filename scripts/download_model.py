"""下载 Fun-CosyVoice3-0.5B 模型（备用）。

当前机器模型已由用户拷贝至 ../models/Fun-CosyVoice3-0.5B-2512，
本脚本仅在需要重新下载时使用：
    .venv\\Scripts\\python scripts\\download_model.py --source modelscope
    .venv\\Scripts\\python scripts\\download_model.py --source huggingface
"""

import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
MODEL_DIR = PROJECT.parent / 'models' / 'Fun-CosyVoice3-0.5B-2512'

MODEL_NS = 'FunAudioLLM/Fun-CosyVoice3-0.5B-2512'   # ModelScope
MODEL_HF = 'FunAudioLLM/Fun-CosyVoice3-0.5B-2512'   # HuggingFace


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['modelscope', 'huggingface'],
                    default='modelscope')
    ap.add_argument('--dest', default=str(MODEL_DIR))
    args = ap.parse_args()

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    print(f'下载 {args.source} 模型到：{dest}')

    if args.source == 'modelscope':
        from modelscope import snapshot_download
        snapshot_download(MODEL_NS, local_dir=str(dest))
    else:
        from huggingface_hub import snapshot_download
        snapshot_download(MODEL_HF, local_dir=str(dest))

    print('下载完成。验证关键文件：')
    for f in ('cosyvoice3.yaml', 'llm.pt', 'flow.pt', 'hift.pt',
              'speech_tokenizer_v3.onnx', 'campplus.onnx', 'spk2info.pt'):
        p = dest / f
        print(f'  {"✔" if p.exists() else "✗"} {f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
