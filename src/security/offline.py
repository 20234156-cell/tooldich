"""Process-wide offline policy. Local model HTTP remains available on loopback."""
import ipaddress
import os
import sys

_installed = False


def is_loopback(host):
    if host in {'localhost', b'localhost'}:
        return True
    try:
        return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
    except (ValueError, TypeError):
        return False


def _audit(event, args):
    if event == 'socket.getaddrinfo':
        host = args[0]
        if host is not None and not is_loopback(host):
            raise PermissionError('Chế độ offline: đã chặn DNS/kết nối Internet.')
    elif event in {'socket.connect', 'socket.sendto'}:
        address = args[1]
        if isinstance(address, tuple) and not is_loopback(address[0]):
            raise PermissionError('Chế độ offline: chỉ cho phép model trên máy này.')
    elif event == 'socket.bind':
        address = args[1]
        if isinstance(address, tuple) and not is_loopback(address[0]):
            raise PermissionError('Chế độ offline: không mở cổng LAN/Internet.')


def install_offline_guard():
    global _installed
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
