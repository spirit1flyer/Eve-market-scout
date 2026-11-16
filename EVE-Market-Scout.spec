# -*- mode: python ; coding: utf-8 -*-
# EVE Market Scout - PyInstaller Spec File
#
# Build with: pyinstaller EVE-Market-Scout.spec
#
# This creates a folder distribution (not single file) for faster startup.
# Output lands in dist/EVE-Market-Scout/.
#
# TWO-BUILD NOTE
# --------------
# Some optional tabs are loaded at startup via a try-import and degrade to a
# disabled placeholder when their files are absent (see the gitignored
# "Local-only modules" block in .gitignore). PyInstaller bundles whatever is
# present in the build tree, so:
#   * Personal build (those local files present): build from this checkout and
#     the optional tab(s) are included.
#   * Public/distributable build: build from a fresh `git clone` of the repo so
#     the gitignored files aren't present, and the bundle ships without them.
# Nothing in this spec needs to change between the two builds.

from PyInstaller.utils.hooks import collect_submodules

# Collect all aiohttp submodules (it has many hidden imports)
aiohttp_hiddenimports = collect_submodules('aiohttp')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ESI_DATA_USAGE.md', '.'),
        # User databases, caches, and sound remain in the local data directory.
    ],
    hiddenimports=[
        # aiohttp and its dependencies
        *aiohttp_hiddenimports,
        'aiohttp',
        'asyncio',
        'multidict',
        'yarl',
        'async_timeout',
        'charset_normalizer',
        'aiosignal',
        'frozenlist',
        # requests and dependencies
        'requests',
        'urllib3',
        'certifi',
        'idna',
        # tkinter (usually auto-detected but be safe)
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        # Standard library that might be missed
        'sqlite3',
        'bz2',
        'statistics',
        'json',
        'csv',
        'hashlib',
        'secrets',
        'base64',
        'dataclasses',
        'enum',
        'pathlib',
        'webbrowser',
        'http.server',
        # Matplotlib for price history charts (numpy is pulled in automatically)
        'matplotlib',
        'matplotlib.pyplot',
        'matplotlib.backends.backend_tkagg',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary modules to reduce size
        # NOTE: numpy is required by matplotlib - do not exclude
        'pandas',
        'PIL',
        'scipy',
        'pytest',
        'unittest',
        'IPython',
        'notebook',
        'sphinx',
        'docutils',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EVE-Market-Scout',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # Compress executable (ignored if upx isn't on PATH)
    console=False,  # Set to True if you want to see print() output for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Uncomment and set path to add an icon (Windows .ico, Linux .png)
    # icon='icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EVE-Market-Scout',
)
