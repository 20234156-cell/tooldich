"""Isolated, single-model CPU worker. JSON-lines protocol; no model in API process."""
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

def main():
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
        tts = Vieneu(device='cpu', threads=max(1, min(8, int(os.environ.get('VIENEU_CPU_THREADS', '2')))))
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
        print(json.dumps(response), flush=True)

if __name__ == '__main__':
    # Direct execution must not shadow the installed vieneu package with our adapter.
    from pathlib import Path
    sys.path = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
    main()
