# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['quack.providers.copilot_sdk', 'quack.providers.github_models']
tmp_ret = collect_all('quack')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Without this every frozen exe reports the same version regardless of the
# source it was built from, so a stale bundled exe looks current.
import subprocess
from datetime import datetime

try:
    _commit = subprocess.check_output(
        ['git', 'rev-parse', '--short', 'HEAD'], text=True
    ).strip()
    if subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip():
        _commit += '-dirty'
except Exception:
    _commit = 'unknown'

print(f'quack.spec: stamping build commit {_commit}')
with open('src/quack/_build_info.py', 'w', encoding='utf-8') as _f:
    _f.write(
        '"""Build provenance stamped in by quack.spec at freeze time.\n\n'
        'The values checked into git mark a source (non-frozen) run; PyInstaller\n'
        'overwrites this module with the real commit and build date.\n"""\n\n'
        f'BUILD_COMMIT = {_commit!r}\n'
        f'BUILD_DATE = {datetime.now().isoformat(timespec="seconds")!r}\n'
    )


a = Analysis(
    ['src/quack/__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='quack',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
