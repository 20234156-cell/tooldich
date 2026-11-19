"""Voice routing shared by batch and individual speech generation."""
from src.tts.edge import EdgeTTSProvider

def provider_for_voice(voice):
    from config.settings import settings
    if not voice:
        voice = getattr(settings, 'DEFAULT_TTS_VOICE', settings.EDGE_TTS_VOICE)
    from src.tts.processing import parse_voice
    voice, _ = parse_voice(voice)
    if voice and voice.startswith('piper:'):
        from src.tts.piper import provider as piper_prov, PiperTTSProvider
        if voice not in {v['id'] for v in PiperTTSProvider.VOICES}:
            raise ValueError('Unknown Piper voice')
        return piper_prov
    if voice and voice.startswith('vieneu:'):
        from src.tts.vieneu import provider, VOICE_ID
        if voice != VOICE_ID:
            raise ValueError('Unknown local voice')
        return provider
    if voice and voice not in {v['id'] for v in EdgeTTSProvider.VOICES}:
        raise ValueError('Unknown voice')
    if settings.OFFLINE_MODE:
        raise ValueError('Giọng đã chọn cần Internet. Hãy chọn giọng Piper hoặc VieNeu trên máy.')
    return EdgeTTSProvider()

def voice_identity(voice):
    if voice and (voice.startswith('vieneu:') or voice.startswith('piper:')):
        provider = provider_for_voice(voice)
        if voice.startswith('piper:'):
            return provider.cache_version_for_voice(voice)
        return provider.cache_version
    return None
