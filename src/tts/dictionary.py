"""Validated, atomic user pronunciation dictionary shared by speech paths."""
import json
import os
import tempfile
from config.settings import settings

def dictionary_path():
    return settings.storage_path.parent/'tts'/'custom_dict.json'

def validate_dictionary(data):
    if not isinstance(data, dict) or len(data)>1000:
        raise ValueError('Từ điển phải là đối tượng JSON, tối đa 1.000 mục.')
    for key, value in data.items():
        if not isinstance(key,str) or not isinstance(value,str) or not key.strip() or not value.strip() or len(key)>100 or len(value)>300:
            raise ValueError('Mỗi mục cần từ gốc và cách đọc không rỗng (tối đa 100/300 ký tự).')
        if key != key.strip() or '=' in key or any(ord(c)<32 for c in key+value):
            raise ValueError('Mỗi từ và cách đọc phải nằm trên một dòng; từ gốc không chứa dấu = hoặc khoảng trắng ở hai đầu.')
    if len({key.casefold() for key in data}) != len(data):
        raise ValueError('Từ điển có mục trùng nhau khi bỏ qua hoa/thường.')
    return data

def load_dictionary():
    path = dictionary_path()
    return validate_dictionary(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else {}

def save_dictionary(data):
    data = validate_dictionary(data)
    path = dictionary_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',dir=path.parent,encoding='utf-8',delete=False) as handle:
        json.dump(data,handle,ensure_ascii=False,indent=2)
        temporary = handle.name
    try:
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
