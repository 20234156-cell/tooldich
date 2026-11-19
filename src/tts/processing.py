"""Versioned, duration-preserving voice colors and conservative RMS trimming."""
import numpy as np
from scipy import signal

VERSION = 'voice-color-rms-v2-pcm-padding'
PRESETS = {'default':'Mặc định','review':'Review Phim — dày, nén tiếng','night':'Kể Chuyện Đêm Khuya — ấm','news':'Tin Tức / Drama — sáng','mystery':'Khoa Học Bí Ẩn — trầm'}

def parse_voice(voice):
    base, sep, preset = (voice or '').partition('@color=')
    preset = preset if sep else 'default'
    if preset not in PRESETS:
        raise ValueError('Preset màu giọng không hợp lệ')
    return base or None, preset

def processing_identity(voice):
    _, preset = parse_voice(voice)
    return f'{VERSION}:{preset}'

def trim_rms(data, sr, floor):
    # An adaptive floor below the old peak cutoff retains quiet consonants.
    frame = max(1,int(sr*.01))
    padded = np.pad(data,(0,(-len(data))%frame))
    levels = np.sqrt(np.mean(padded.reshape(-1,frame)**2,axis=1))
    threshold = min(floor,max(1e-6,float(np.max(levels))*.01))
    active = np.flatnonzero(levels>=threshold)
    if not active.size:
        return data
    first = max(0,int(active[0]*frame)-int(sr*.04))
    last = min(len(data),int((active[-1]+1)*frame)+int(sr*.18))
    return data[first:last]


def trim_pcm_padding(data, sr, keep_ms):
    """Honor padding without deleting quiet speech or internal pauses.

    RMS trimming deliberately retains a conservative phonetic tail. Remove
    only surplus exact-zero PCM at the outer edges after that step. All-zero
    audio stays unchanged so the existing silent-voice validation still fails.
    """
    if not np.isfinite(sr) or sr <= 0 or not np.isfinite(keep_ms) or keep_ms < 0:
        raise ValueError('Invalid sample rate or silence padding')
    if data.ndim not in (1,2):
        raise ValueError('Expected mono or channel audio')
    active = np.flatnonzero(np.any(data != 0,axis=1) if data.ndim == 2 else data != 0)
    if not active.size:
        return data
    padding = int(round(sr*keep_ms/1000))
    first = max(0,int(active[0])-padding)
    last = min(len(data),int(active[-1])+1+padding)
    return data[first:last]

def apply_color(data, sr, preset):
    if preset == 'default':
        return data
    if preset not in PRESETS:
        raise ValueError('Unknown voice color')
    audio = signal.sosfilt(signal.butter(2,70 if preset!='news' else 120,fs=sr,btype='highpass',output='sos'),data)
    if preset in ('review','night','mystery'):
        bass = signal.sosfilt(signal.butter(2,350,fs=sr,output='sos'),audio)
        audio += bass*(.3 if preset!='mystery' else .5)
    if preset in ('night','mystery'):
        audio = signal.sosfilt(signal.butter(2,min(6500 if preset=='night' else 4500,sr*.4),fs=sr,output='sos'),audio)
    if preset == 'news':
        low = signal.sosfilt(signal.butter(2,min(2200,sr*.3),fs=sr,output='sos'),audio)
        audio += .2*(audio-low)
    if preset == 'review':
        # Soft static compression; no extra tempo/pitch manipulation.
        audio = .5*np.tanh(audio/.4)
    peak = float(np.max(np.abs(audio))) if audio.size else 0
    if peak>.95:
        audio *= .95/peak
    return np.asarray(audio,dtype=np.float32)
