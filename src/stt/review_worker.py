"""Isolated local speech review with one model for a batch of bounded windows."""
import json
import os
from contextlib import nullcontext
from pathlib import Path
import sys

if __name__ == '__main__':
    os.environ['OMP_NUM_THREADS'] = '4'
    os.environ['MKL_NUM_THREADS'] = '4'
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.stt.whisper_provider import WhisperSTTProvider
from src.stt.base import TranscriptResult
from src.jobs.logger import PipelineException, SystemErrorCodes


def make_provider(model, cpu_threads=4, *, device='cpu', compute_type='int8'):
    provider = WhisperSTTProvider(model_size=model, device=device, compute_type=compute_type)
    provider.cpu_threads = cpu_threads
    provider.local_files_only = True
    provider.review_chunk_length = 10
    return provider


def recognize(provider, audio, language):
    try:
        return provider.transcribe(audio, language=language)
    except PipelineException as exc:
        if 'Không nhận dạng được nội dung lời thoại nào' not in str(exc):
            raise
        return TranscriptResult(language=language, segments=[])


def main():
    if sys.argv[1] == '--batch':
        request = json.loads(Path(sys.argv[2]).read_text(encoding='utf8'))
        audios = request['audios']
        if not isinstance(audios, list) or not audios:
            raise ValueError('Speech review requires at least one window')
        device = request.get('device', 'cpu')
        compute = request.get('compute_type', 'int8')
        provider = (make_provider(request['model'], 4) if (device, compute) == ('cpu', 'int8')
                    else make_provider(request['model'], 4, device=device, compute_type=compute))
        result_path = Path(sys.argv[2]).with_suffix('.results.jsonl')
        output = result_path.open('w', encoding='utf-8') if request.get('stream_results') else nullcontext(sys.stdout)
        with output as stream:
            for index, audio in enumerate(audios):
                code = None
                try:
                    result = recognize(provider, audio, request['language'])
                    row = dict(index=index, result=result.model_dump(mode='json'))
                except PipelineException as exc:
                    memory_failure = any(marker in str(exc).lower() for marker in
                                         ('mkl_malloc', 'failed to allocate', 'out of memory', 'memoryerror'))
                    code = (SystemErrorCodes.STT_MODEL_UNAVAILABLE
                            if memory_failure or device == 'cuda' else exc.code)
                    row = dict(index=index, error=dict(code=code, message=str(exc)))
                print(json.dumps(row), file=stream, flush=True)
                if code == SystemErrorCodes.STT_MODEL_UNAVAILABLE:
                    break
        return
    request = dict(model=sys.argv[1], audio=sys.argv[2], language=sys.argv[3])
    provider = make_provider(request['model'])
    result = recognize(provider, request['audio'], request['language'])
    print(result.model_dump_json(), flush=True)


if __name__ == '__main__':
    main()
