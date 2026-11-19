"""Isolated CPU/CUDA voice worker; stdout is reserved for JSON lines."""
import contextlib
import io
import json
import sys
import re
import base64
import os

def split_text(text, limit=18):
    words = text.split()
    chunks = []
    current = []
    for word in words:
        current.append(word)
        if len(current) >= limit or re.search(r'[.!?;…]$', word):
            chunks.append(' '.join(current))
            current = []
    if current:
        chunks.append(' '.join(current))
    return chunks


def create_engine(factory, reference=None):
    device = os.environ.get('VIENEU_DEVICE', 'cpu')
    threads = max(1, min(8, int(os.environ.get('VIENEU_CPU_THREADS', '2'))))
    if device == 'cuda':
        # Pascal has no fast FP16/BF16 kernels. Keep the registered voice in FP32.
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA runtime unavailable; refusing a silent CPU fallback')
        torch.set_num_threads(threads)
        options = {}
        if reference is not None:
            from pathlib import Path
            from huggingface_hub import constants
            profile = json.loads(Path(reference).with_name('profile.json').read_text(encoding='utf-8'))
            hub = Path(constants.HF_HUB_CACHE)
            backbone = hub / 'models--pnnbao-ump--VieNeu-TTS-v3-Turbo' / 'snapshots' / profile['model_revision']
            codec = hub / 'models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano'
            codec_revision = (codec / 'refs' / 'main').read_text().strip()
            options = {'backbone_repo': str(backbone), 'moss_tokenizer': str(codec / 'snapshots' / codec_revision)}
            if not (backbone / 'update' / 'model.safetensors').is_file():
                raise RuntimeError('Pinned GPU voice model missing')
        return factory(device='cuda', backend='pytorch', dtype='float32', max_batch_size=1, **options)
    if device != 'cpu':
        raise ValueError('Unsupported VieNeu device')
    return factory(device='cpu', threads=threads)

def main():
    if os.environ.get('HF_HUB_OFFLINE') == '1':
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from src.security.offline import install_offline_guard
        install_offline_guard()
    import numpy as np
    import soundfile as sf
    import onnxruntime as ort
    session = ort.InferenceSession
    def low_memory(path, sess_options=None, **kwargs):
        opts = sess_options or ort.SessionOptions()
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        opts.add_session_config_entry('session.disable_prepacking', '1')
        return session(path, sess_options=opts, **kwargs)
    ort.InferenceSession = low_memory
    with contextlib.redirect_stdout(sys.stderr):
        from vieneu import Vieneu
        tts = create_engine(Vieneu, sys.argv[1])
        from pathlib import Path
        encoded = Path(sys.argv[1]).with_name('encoded.npz')
        if '--enroll' in sys.argv:
            speaker_emb, codes = tts.encode_reference(sys.argv[1], denoise=False)
            np.savez(encoded, speaker_emb=speaker_emb, codes=codes)
            return
        with np.load(encoded, allow_pickle=False) as data:
            voice = {'speaker_emb': data['speaker_emb'], 'codes': data['codes']}
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if 'items' in request:
                if os.environ.get('VIENEU_DEVICE')!='cuda' or int(os.environ.get('VIENEU_BATCH_SIZE','1'))<=1:
                    raise ValueError('Batch protocol disabled')
                from src.tts.vieneu_batching import synthesize_request
                import gc
                import torch
                def emit(result): print(json.dumps(result),flush=True)
                def trace(result): print('VIENEU_BATCH '+json.dumps(result),file=sys.stderr,flush=True)
                def cleanup():
                    gc.collect()
                    torch.cuda.empty_cache()
                with contextlib.redirect_stdout(sys.stderr):
                    # Emit protocol responses to the original stdout, not diagnostics.
                    protocol=sys.__stdout__
                    def emit(result): print(json.dumps(result),file=protocol,flush=True)
                    response=synthesize_request(tts,voice,request['items'],int(os.environ['VIENEU_BATCH_SIZE']),
                        int(os.environ.get('VIENEU_BATCH_MAX_CHARS','1200')),split_text,emit,trace,
                        torch.cuda.OutOfMemoryError,cleanup,request['request_id'])
                print(json.dumps(response),flush=True)
                continue
            chunks = []
            with contextlib.redirect_stdout(sys.stderr):
                for text in split_text(request['text']):
                    wav = np.asarray(tts.infer(text, voice=voice), dtype=np.float32)
                    if not wav.size or not np.isfinite(wav).all() or np.max(np.abs(wav)) < 1e-5:
                        raise ValueError('Invalid synthesized audio')
                    chunks.append(wav)
            pause = np.zeros(int(.08*tts.sample_rate), dtype=np.float32)
            audio = np.concatenate([item for i, chunk in enumerate(chunks) for item in ([chunk, pause] if i < len(chunks)-1 else [chunk])])
            audio *= min(1., .95/max(float(np.max(np.abs(audio))), 1e-9))
            buffer = io.BytesIO()
            sf.write(buffer, audio, tts.sample_rate, format='WAV', subtype='PCM_16')
            response = {'ok': True, 'audio':base64.b64encode(buffer.getvalue()).decode('ascii')}
        except Exception as exc:
            response = {'ok': False, 'error': type(exc).__name__}
            if isinstance(locals().get('request'),dict) and 'items' in request:
                response.update(type='complete',message=str(exc)[:500])
        print(json.dumps(response), flush=True)

if __name__ == '__main__':
    # Direct execution must not shadow the installed vieneu package with our adapter.
    from pathlib import Path
    sys.path = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
    main()
