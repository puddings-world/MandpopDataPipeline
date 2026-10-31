"""
Subprocess entry point for audio-separator.

audio-separator pulls numpy 2.x, which conflicts with the main env
(transformers / WhisperX). It therefore lives in an isolated venv
(`tools/venvs/separator/`, created by `setup_env.py --separator`) and the
main process calls this script with that venv's interpreter.

    python _separator_worker.py <input> <out_dir> <model> <FORMAT> <model_dir>

Prints the list of written files (paths relative to out_dir) as one JSON
line prefixed with `@@WRITTEN@@`, so the caller can ignore library logs.
"""

import json
import sys


def main() -> int:
    input_audio, out_dir, model, output_format, model_dir = sys.argv[1:6]
    from audio_separator.separator import Separator

    sep = Separator(
        output_dir=out_dir,
        output_format=output_format,
        model_file_dir=model_dir,
    )
    sep.load_model(model_filename=model)
    written = sep.separate(input_audio)
    print("@@WRITTEN@@" + json.dumps(written, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
