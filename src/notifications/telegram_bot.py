"""Single-owner long polling, at-most-once command intake and durable confirmations."""
import hashlib
import json
import os
import queue as mailbox
import sys
import threading
import time

from config.settings import settings
from src.notifications import assistant
from src.notifications.studio import StudioError


class FileLease:
    """OS lock survives races between multiple local server processes; crash releases it."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open('a+b')
        if not path.stat().st_size:
            self.stream.write(b'0'); self.stream.flush()
        try:
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


class TelegramBotService:
    def __init__(self, transport=None, *, testing=False):
        self._transport = transport
        self._testing = testing and transport is not None and 'pytest' in sys.modules
        self._stop_event = threading.Event()
        self._thread = self._command_thread = self._fast_thread = None
        self._restart_thread = None
        self._lifecycle_generation = 0
        self._commands = mailbox.Queue(maxsize=100)
        self._fast_commands = mailbox.Queue(maxsize=100)
        self._lock = threading.RLock()
        self._lease = None
        self._last_update_id = None
        self._offset_loaded = False
        self._error = None
        self._metrics_at = 0
        self._metrics_running = False

    @property
    def stopping(self):
        return self._stop_event.is_set()

    def permitted(self):
        from src.notifications.telegram import network_allowed
        return (not settings.OFFLINE_MODE and settings.TELEGRAM_NOTIFY_ENABLED
                and bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)
                and ('pytest' not in sys.modules or self._testing)
                and (network_allowed() or self._testing))

    def call(self, method, data, files=None, timeout=10):
        if not self.permitted():
            raise RuntimeError('Telegram đang tắt hoặc bị cô lập.')
        if self._transport:
            return self._transport(method, data, files=files, timeout=timeout)
        from src.notifications.telegram import _request
        return _request(method, data, files=files, timeout=timeout)

    def message(self, text, markup=None):
        if self.stopping:
            return None
        from src.notifications.telegram import safe_text
        text = safe_text(str(text))
        reply = None
        for offset in range(0, max(1, len(text)), 3800):
            data = dict(chat_id=settings.TELEGRAM_CHAT_ID, text=text[offset:offset+3800])
            if markup and offset == 0:
                data['reply_markup'] = json.dumps(markup, ensure_ascii=False)
            current = self.call('sendMessage', data)
            if offset == 0:
                reply = current
                self.remember_keyboard(current, markup)
        return reply

    def _action_state(self, change):
        from src.notifications.state import durable_json as atomic_json
        path = assistant.private_path('telegram_actions.json')
        # Use both a process lock and an OS lock, because notification workers
        # may be running on a second local FastAPI instance.
        with self._lock:
            deadline = time.monotonic()+3
            while True:
                try:
                    lease = FileLease(path.with_suffix('.lock'))
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Chưa khóa được hồ sơ duyệt Telegram.') from None
                    time.sleep(.02)
            try:
                state = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
                result = change(state)
                atomic_json(path, state)
                return result
            finally:
                lease.close()

    def remember_keyboard(self, reply, markup):
        if not reply or not markup or type(reply.get('message_id')) is not int:
            return
        from src.pipeline import auto_workflow as auto
        rows = markup.get('inline_keyboard', [])
        bindings = {}
        task_bindings = {}
        for row in rows:
            for button in row:
                data = button.get('callback_data', '')
                if data.startswith(('res:', 'skip:', 'stop:')):
                    project_id = data.split(':', 1)[1]
                    if data.startswith('stop:'):
                        from src.api.app import queue
                        task = queue.get_by_project(project_id)
                        if task:
                            task_bindings[project_id] = task.task_id
                    profile = auto.read(project_id)
                    if profile:
                        bindings[project_id] = profile.get('generation')
        if not bindings and not task_bindings:
            return
        def save(state):
            messages = state.setdefault('messages', {})
            messages[str(reply['message_id'])] = dict(bindings=bindings, tasks=task_bindings, expires=time.time()+7*86400)
            # Bound journal growth and expire buttons instead of accepting stale approval.
            state['messages'] = dict(list(messages.items())[-500:])
        self._action_state(save)

    def verify_keyboard(self, action, project_id, message_id):
        if action not in {'res', 'skip', 'stop'}:
            return
        from src.pipeline import auto_workflow as auto
        with auto.lock:
            profile = auto.read(project_id)
            def verify(state):
                saved = state.get('messages', {}).get(str(message_id), {})
                if action == 'stop':
                    from src.api.app import queue
                    task = queue.get_by_project(project_id)
                    if (saved.get('expires', 0) < time.time() or not task or
                        saved.get('tasks', {}).get(project_id) != task.task_id):
                        raise ValueError('Nút hủy thuộc tác vụ cũ; chọn tác vụ hiện tại.')
                    return
                if (not profile or saved.get('expires', 0) < time.time() or
                    saved.get('bindings', {}).get(project_id) != profile.get('generation')):
                    raise ValueError('Nút thuộc lượt Auto cũ; mở hồ sơ mới trên Web.')
            self._action_state(verify)

    def confirm(self, project_id, message_id, identity):
        if type(message_id) is not int:
            raise ValueError('Telegram chưa xác nhận tin nhắn xóa; chưa cho phép xóa.')
        def save(state):
            state['deletions'] = {k: v for k, v in state.get('deletions', {}).items()
                                  if v.get('expires', 0) >= time.time()}
            pending = state['deletions']
            pending[project_id] = dict(message_id=message_id, identity=identity,
                                       expires=time.time()+600)
        self._action_state(save)

    def clear_confirmation(self, project_id):
        self._action_state(lambda state: state.setdefault('deletions', {}).pop(project_id, None))

    def take_confirmation(self, project_id, message_id):
        def take(state):
            pending = state.setdefault('deletions', {}).get(project_id)
            if not pending or pending['message_id'] != message_id or pending['expires'] < time.time():
                raise ValueError('Xác nhận xóa không hợp lệ hoặc hết hạn. Bấm yêu cầu xóa lần nữa.')
            return state['deletions'].pop(project_id)
        return self._action_state(take)

    def _load_offset(self):
        if self._offset_loaded:
            return
        path = assistant.private_path('telegram_offset.json')
        if path.is_file():
            saved = json.loads(path.read_text(encoding='utf-8'))
            value = saved.get('last_update_id')
            token_hash = hashlib.sha256(settings.TELEGRAM_BOT_TOKEN.encode()).hexdigest()
            if type(value) is not int or value < 0:
                raise ValueError('Offset Telegram không hợp lệ; chưa nhận lệnh.')
            if saved.get('bot_identity', token_hash) == token_hash:
                self._last_update_id = value
        self._offset_loaded = True

    def _checkpoint(self, update_id):
        from src.notifications.state import durable_json as atomic_json
        atomic_json(assistant.private_path('telegram_offset.json'), dict(last_update_id=update_id,
            bot_identity=hashlib.sha256(settings.TELEGRAM_BOT_TOKEN.encode()).hexdigest()))
        self._last_update_id = update_id

    def process_update(self, update, *, enqueue=False):
        """Reserve durably BEFORE side effects. A crashed command is never replayed automatically."""
        if not self.permitted() or self.stopping or not isinstance(update, dict):
            return False
        identifier = update.get('update_id')
        if type(identifier) is not int or identifier < 0:
            return False
        with self._lock:
            self._load_offset()
            if self._last_update_id is not None and identifier <= self._last_update_id:
                return False
            self._checkpoint(identifier)  # failed persistence must never execute a command
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback, dict) else update.get('message', {})
        if not isinstance(message, dict) or str(message.get('chat', {}).get('id')) != str(settings.TELEGRAM_CHAT_ID):
            return False
        if enqueue:
            try:
                # Status/cancel/approval never wait behind a ten-second AI call
                # or a disk inventory/receipt check in the conversational lane.
                data = callback.get('data', '') if isinstance(callback, dict) else ''
                text = message.get('text', '')
                fast = data.split(':', 1)[0] in {'stop', 'res', 'skip', 'delx'} if callback else (
                    isinstance(text, str) and not assistant.extract_video_url(text) and
                    assistant.intent(text) in {'help', 'status', 'list', 'stop'})
                if callback and isinstance(data,str) and data.startswith('ui:'):
                    from src.notifications.studio import fast_action
                    fast = fast_action(self,data)
                (self._fast_commands if fast else self._commands).put_nowait(update)
            except mailbox.Full:
                self.message('Hàng đợi lệnh Telegram đang đầy. Hãy gửi lại sau.')
                return False
        else:
            self._dispatch(update)
        return True

    def _dispatch(self, update):
        if self.stopping:
            return
        try:
            callback = update.get('callback_query')
            if callback:
                try:
                    self.call('answerCallbackQuery', {'callback_query_id': callback['id']}, timeout=2)
                except Exception:
                    pass  # A late/sporadic ACK must not discard the authorized action.
                data = callback.get('data', '')
                message_id = callback.get('message', {}).get('message_id')
                if isinstance(data, str) and ':' in data:
                    action, project_id = data.split(':', 1)
                    # Hold Auto's generation lock through validation and submission.
                    from src.pipeline import auto_workflow as auto
                    if action in {'res', 'skip', 'stop'}:
                        with auto.lock:
                            self.verify_keyboard(action, project_id, message_id)
                            assistant.handle_callback(data, self, message_id)
                    else:
                        assistant.handle_callback(data, self, message_id)
            else:
                text = update.get('message', {}).get('text')
                if isinstance(text, str):
                    assistant.handle_message(text[:8000], self)
        except StudioError as error:
            try:
                self.message(str(error)+'\nGửi /menu để mở bảng điều khiển mới.')
            except Exception:
                pass
        except Exception:
            # Never echo provider exceptions (URLs, tokens and credentials may be embedded).
            try:
                self.message('Chưa thực hiện được yêu cầu. Gửi /menu → Dự án để kiểm tra trạng thái và lấy nút mới. Nếu đang xử lý, hãy đợi hoặc yêu cầu dừng trước khi sửa/xóa.')
            except Exception:
                pass

    def _poll(self):
        try:
            try:
                from src.notifications.studio import register_menu
                register_menu(self)
            except Exception:
                pass  # Menu registration must not prevent authorized command intake.
            self._load_offset()
            if self._last_update_id is None:
                # First installation/new bot: discard the old backlog, not execute it.
                rows = self.call('getUpdates', {'offset': -1, 'timeout': 0,
                    'allowed_updates': json.dumps(['message', 'callback_query'])}, timeout=3) or []
                latest = max((r['update_id'] for r in rows if type(r.get('update_id')) is int), default=0)
                self._checkpoint(latest)
            while not self.stopping and self.permitted():
                try:
                    rows = self.call('getUpdates', {'offset': self._last_update_id+1, 'timeout': 2,
                        'allowed_updates': json.dumps(['message', 'callback_query'])}, timeout=4) or []
                    for update in sorted(rows, key=lambda item: item.get('update_id', -1)):
                        if self.stopping:
                            break
                        self.process_update(update, enqueue=True)
                    self.schedule_refresh()
                    self._error = None
                except Exception:
                    self._error = 'Kết nối Telegram gián đoạn; sẽ thử lại khi ứng dụng còn mở.'
                self._stop_event.wait(settings.TELEGRAM_POLL_INTERVAL_SECONDS)
        except Exception:
            self._error = 'Không đọc/ghi được offset Telegram; dừng nhận lệnh để tránh chạy lặp.'
        finally:
            self._stop_event.set()

    def _consume(self, commands):
        while not self.stopping:
            try:
                update = commands.get(timeout=.2)
            except mailbox.Empty:
                continue
            try:
                self._dispatch(update)
            finally:
                commands.task_done()

    def start(self):
        with self._lock:
            if any(t and t.is_alive() for t in (self._thread, self._command_thread, self._fast_thread)):
                if self.stopping and settings.TELEGRAM_POLLING_ENABLED and self.permitted():
                    self._restart_when_idle()
                return False
            # A slow command may finish after stop()'s bounded join. Reap its
            # old lease before restarting, without admitting duplicate workers.
            if self._lease:
                self._lease.close()
                self._lease = None
            self._thread = self._command_thread = self._fast_thread = None
            if not settings.TELEGRAM_POLLING_ENABLED or not self.permitted():
                return False
            try:
                self._lease = FileLease(assistant.private_path('telegram_poll.lock'))
            except OSError:
                self._error = 'Một phiên ứng dụng khác đang nhận lệnh Telegram.'
                return False
            self._stop_event.clear()
            self._last_update_id = None
            self._offset_loaded = False
            self._commands = mailbox.Queue(maxsize=100)
            self._fast_commands = mailbox.Queue(maxsize=100)
            self._thread = threading.Thread(target=self._poll, name='telegram-poll', daemon=True)
            self._command_thread = threading.Thread(target=self._consume, args=(self._commands,), name='telegram-command', daemon=True)
            self._fast_thread = threading.Thread(target=self._consume, args=(self._fast_commands,), name='telegram-control', daemon=True)
            self._command_thread.start()
            self._fast_thread.start()
            self._thread.start()
            self.schedule_refresh()
            return True

    def _restart_when_idle(self):
        if self._restart_thread and self._restart_thread.is_alive():
            return
        generation = self._lifecycle_generation
        previous = (self._thread, self._command_thread, self._fast_thread)

        def wait_previous():
            pause = threading.Event()
            while any(t and t.is_alive() for t in previous):
                with self._lock:
                    if generation != self._lifecycle_generation:
                        return
                pause.wait(.1)
            with self._lock:
                self._restart_thread = None
                if generation == self._lifecycle_generation and self.stopping:
                    self.start()

        self._restart_thread = threading.Thread(target=wait_previous, name='telegram-restart', daemon=True)
        self._restart_thread.start()

    def stop(self, *, notify=True):
        with self._lock:
            self._lifecycle_generation += 1
            self._stop_event.set()
            threads = (self._thread, self._command_thread, self._fast_thread)
            started = bool(self._thread)
        if notify and started and self.permitted():
            try:
                self.call('sendMessage', dict(chat_id=settings.TELEGRAM_CHAT_ID,
                    text='💤 Hệ thống DuanLayLink trên máy tính đã tắt. Trợ lý Telegram tạm nghỉ ngơi.'), timeout=2)
            except Exception:
                pass
        deadline = time.monotonic()+5
        for thread in threads:
            if thread and thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline-time.monotonic()))
        # Never allow a second poller while the previous thread is still exiting.
        with self._lock:
            if not any(t and t.is_alive() for t in (self._thread, self._command_thread, self._fast_thread)):
                if self._lease:
                    self._lease.close(); self._lease = None
                self._thread = self._command_thread = self._fast_thread = None

    def schedule_refresh(self):
        if settings.OFFLINE_MODE or 'pytest' in sys.modules:
            return
        with self._lock:
            if self._metrics_running or time.monotonic()-self._metrics_at < 30:
                return
            self._metrics_at = time.monotonic()
            self._metrics_running = True
        def refresh():
            try:
                from src.notifications.studio import update_progress
                update_progress(self)
                assistant.refresh_usage()
            except Exception:
                pass
            finally:
                with self._lock:
                    self._metrics_running = False
        try:
            threading.Thread(target=refresh, name='telegram-disk-metrics', daemon=True).start()
        except Exception:
            with self._lock:
                self._metrics_running = False
            raise

    def status(self):
        return dict(enabled=bool(settings.TELEGRAM_NOTIFY_ENABLED),
                    configured=bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
                    polling_enabled=settings.TELEGRAM_POLLING_ENABLED,
                    listening=bool(self._thread and self._thread.is_alive() and not self.stopping),
                    isolated=not self.permitted(), message=self._error,
                    last_update_id=self._last_update_id,
                    disk_guard_enabled=settings.AUTO_DISK_GUARD_ENABLED,
                    disk_guard_min_free_gb=settings.AUTO_DISK_GUARD_MIN_FREE_GB)


bot_service = TelegramBotService()
