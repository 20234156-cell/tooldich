"""Voice routing shared by batch and individual speech generation."""
from src.tts.edge import EdgeTTSProvider

def provider_for_voice(voice):
    from src.tts.processing import parse_voice
    voice, _ = parse_voice(voice)
    if voice and voice.startswith('vieneu:'):
        from src.tts.vieneu import provider, VOICE_ID
        if voice != VOICE_ID:
            raise ValueError('Unknown local voice')
        return provider
    if voice and voice not in {v['id'] for v in EdgeTTSProvider.VOICES}:
        raise ValueError('Unknown voice')
    return EdgeTTSProvider()

def voice_identity(voice):
    if voice and voice.startswith('vieneu:'):
        return provider_for_voice(voice).cache_version
    return None
