# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['main.py'], pathex=[],
    datas=[('bin/ffmpeg.exe', 'bin'), ('assets/app.ico', 'assets'),
           ('LICENSE', '.'), ('THIRD_PARTY_NOTICES.txt', '.'), ('licenses', 'licenses')]
          + collect_data_files('webview'),
    hiddenimports=['webview.platforms.winforms', 'webview.platforms.edgechromium'],
    excludes=['numpy', 'matplotlib', 'scipy', 'pandas', 'PIL', 'Crypto', 'cryptography', 'OpenSSL',
              'PySide2', 'PySide6', 'PyQt5', 'PyQt6', 'cefpython3', 'gi', 'wx',
              'webview.platforms.qt', 'webview.platforms.gtk', 'webview.platforms.cocoa',
              'webview.platforms.cef', 'webview.platforms.android'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MOOCDownloader',
          console=False, debug=False, strip=False, upx=False, icon='assets/app.ico')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MOOCDownloader')
