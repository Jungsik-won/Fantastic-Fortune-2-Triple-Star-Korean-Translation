"""FF2 v0.9.4 offline desktop patcher. Select an ISO to start automatically."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from patch_engine import apply, Cancelled, PatchError, next_output, load_patch

PATCH_NAME = 'ff2-ko-full-reviewed-v094.ff2patch.gz'

def resource_path():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / PATCH_NAME


def friendly_error(error):
    if isinstance(error, PatchError):
        return str(error)
    if isinstance(error, PermissionError):
        return '파일을 읽거나 저장할 권한이 없습니다. ‘저장 폴더 변경’으로 쓰기 가능한 폴더를 선택해 주세요.'
    if isinstance(error, OSError) and error.errno == 28:
        return '저장 공간이 부족합니다. 다른 저장 폴더를 선택해 주세요.'
    if isinstance(error, OSError):
        return '파일 작업을 완료하지 못했습니다. ISO 연결 상태와 저장 폴더를 확인해 주세요.\n\n' + str(error)
    return '패치 중 오류가 발생했습니다. 꾸러미를 다시 받아 주세요.\n\n' + str(error)


class Patcher:
    def __init__(self, root, patch_path=None, engine_options=None):
        self.root = root
        self.patch = patch_path or resource_path()
        self.options = engine_options or {}
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.folder = None
        self.output = None
        self.result = None
        self.error = None
        self.progress = tk.DoubleVar(value=0)
        self.status = tk.StringVar(value='원본 ISO를 선택하면 자동으로 시작합니다.')
        self.iso_text = tk.StringVar(value='선택한 ISO가 없습니다')
        self.output_text = tk.StringVar(value='원본 ISO와 같은 폴더에 새 파일로 저장합니다.')
        root.title('판타스틱 포츈 2 · 한글 패치 v0.9.4')
        root.geometry('680x435')
        root.minsize(600, 420)
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='판타스틱 포츈 2: 트리플 스타', font=('', 17, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='한글 검수판 v0.9.4 · GUI 1.4', padding=(0, 5)).pack(anchor='w')
        ttk.Label(frame, text='일본판 원본 ISO를 선택하면 확인·패치·검증까지 자동으로 진행합니다.\n원본 파일은 그대로 두고 한글판 ISO를 새로 만듭니다.', wraplength=620).pack(anchor='w', pady=(8, 18))
        self.select_button = ttk.Button(frame, text='원본 ISO 선택 → 자동 패치', command=self.choose_iso)
        self.select_button.pack(anchor='w', ipady=7)
        ttk.Entry(frame, textvariable=self.iso_text, state='readonly').pack(fill='x', pady=(10, 5))
        ttk.Entry(frame, textvariable=self.output_text, state='readonly').pack(fill='x', pady=(0, 10))
        self.bar = ttk.Progressbar(frame, maximum=100, variable=self.progress)
        self.bar.pack(fill='x', pady=6)
        ttk.Label(frame, textvariable=self.status, wraplength=620).pack(anchor='w', pady=5)
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', side='bottom', pady=(10, 0))
        self.folder_button = ttk.Button(buttons, text='저장 폴더 변경', command=self.choose_folder)
        self.folder_button.pack(side='left')
        self.cancel_button = ttk.Button(buttons, text='취소', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='right')
        self.open_button = ttk.Button(buttons, text='완성 파일 폴더 열기', command=self.open_folder, state='disabled')
        self.open_button.pack(side='right', padx=8)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(80, self.poll)

    def choose_folder(self):
        chosen = filedialog.askdirectory(parent=self.root, title='한글판 ISO 저장 폴더')
        if chosen:
            self.folder = Path(chosen)
            self.output_text.set('저장 폴더: ' + str(self.folder))

    def choose_iso(self):
        selected = filedialog.askopenfilename(parent=self.root, title='일본판 원본 ISO 선택 (SLPS-25396)',
                                             filetypes=[('PS2 ISO', '*.iso'), ('모든 파일', '*')])
        if selected:
            self.start(Path(selected))

    def start(self, source):
        if self.busy:
            return
        self.output = next_output(source, self.folder)
        self.iso_text.set('원본: ' + str(source))
        self.output_text.set('완성 파일: ' + str(self.output))
        self.progress.set(0)
        self.status.set('패치를 준비하고 있습니다')
        self.result = self.error = None
        self.busy = True
        self.cancel_event.clear()
        for widget in (self.select_button, self.folder_button, self.open_button):
            widget.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        threading.Thread(target=self.worker, args=(source, self.output), daemon=True).start()

    def worker(self, source, output):
        try:
            result = apply(source, self.patch, output, self.cancel_event,
                           lambda stage, pct: self.events.put(('progress', stage, pct)), **self.options)
            self.events.put(('done', result))
        except Exception as e:
            self.events.put(('error', e))

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'progress':
                    self.status.set(event[1] + ' · ' + str(int(event[2])) + '%')
                    self.progress.set(event[2])
                else:
                    self.busy = False
                    self.select_button.configure(state='normal')
                    self.folder_button.configure(state='normal')
                    self.cancel_button.configure(state='disabled')
                    if event[0] == 'done':
                        self.result = event[1]
                        self.progress.set(100)
                        self.status.set('완료! 생성된 ISO를 PCSX2에서 새로 부팅해 주세요.')
                        self.open_button.configure(state='normal')
                    else:
                        self.error = event[1]
                        self.status.set(friendly_error(self.error))
                        self.progress.set(0)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def cancel(self):
        self.cancel_event.set()
        self.cancel_button.configure(state='disabled')
        self.status.set('취소하고 있습니다. 잠시 기다려 주세요.')

    def close(self):
        if self.busy:
            self.cancel()
            self.status.set('취소 후 창을 닫아 주세요. 원본 ISO는 그대로입니다.')
            return
        self.root.destroy()

    def open_folder(self):
        if not self.result:
            return
        try:
            folder = str(Path(self.result).parent)
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
        except OSError as e:
            messagebox.showerror('폴더 열기', friendly_error(e), parent=self.root)


def smoke_test(report):
    """Exercise bundled patch loading, Tk event loop and threaded completion."""
    import gzip
    import hashlib
    import tempfile
    import time
    bundled = load_patch(resource_path())
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        before, after = b'A'*4096, b'A'*1024+b'KOREAN'+b'A'*(4096-1030)
        src=folder/'source.iso';src.write_bytes(before)
        patch=folder/'tiny.gz'
        with gzip.open(patch, 'wt', encoding='utf-8') as f:
            json.dump({'format':'ff2-ko-patch-v1','source_size':len(before),
                       'source_sha256':hashlib.sha256(before).hexdigest(),
                       'hunks':[{'iso_offset':1024,'before':before[1024:1030].hex(),'after':b'KOREAN'.hex()}]}, f)
        root=tk.Tk();root.withdraw()
        app=Patcher(root,patch,{'patch_digest':hashlib.sha256(patch.read_bytes()).hexdigest(),
                                'output_digest':hashlib.sha256(after).hexdigest()})
        # Test the same selection callback as the user, without showing a picker.
        original= filedialog.askopenfilename
        filedialog.askopenfilename=lambda **kw:str(src)
        try:app.select_button.invoke()
        finally:filedialog.askopenfilename=original
        deadline=time.monotonic()+20
        while app.busy and time.monotonic()<deadline:
            root.update();time.sleep(0.02)
        assert not app.busy and app.result and not app.error
        assert app.progress.get()==100 and Path(app.result).read_bytes()==after
        assert src.read_bytes()==before
        assert app.open_button.instate(['!disabled'])
        result={'ok':True,'platform':sys.platform,'tk_version':root.tk.call('info','patchlevel'),
                'bundled_patch_spans':len(bundled['spans']), 'iso_selection_autostarts':True,
                'gui_thread_completion':True,'source_preserved':True}
        root.destroy()
        Path(report).write_text(json.dumps(result,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke-test', metavar='REPORT')
    args=parser.parse_args()
    if args.smoke_test:
        smoke_test(args.smoke_test)
        return
    root=tk.Tk()
    Patcher(root)
    root.mainloop()

if __name__ == '__main__':
    main()
