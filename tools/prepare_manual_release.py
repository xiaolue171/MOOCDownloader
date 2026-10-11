"""Export an allowlisted source tree and verified release attachments locally."""
import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'publish'
SOURCE = DEST / 'source' / 'MOOCDownloader'
ATTACHMENTS = DEST / 'release'
VERSION = '2.0.2'


def main():
    if SOURCE.exists() or ATTACHMENTS.exists():
        raise SystemExit('publish/source or publish/release already exists; preserve or move it before exporting again.')
    files = [ROOT / name for name in (
        '.gitignore', 'LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.txt',
        'main.py', 'run.pyw', 'build.bat', 'mooc.spec', 'requirements.txt', 'requirements-build.txt',
        'bin/README.md', 'docs/BUILDING.md', 'docs/RELEASE_2.0.md',
        'docs/RELEASE_2.0.1.md', 'docs/RELEASE_2.0.2.md',
        'tools/build_assets.py', 'tools/prepare_manual_release.py',
        'tests/test_v2.py', 'tests/task_removal_v2.py', 'tests/ui_v2.py', 'tests/layout_v2.py',
        'tests/packaged_v2.py', 'tests/verify_installer.ps1', 'tests/live_v2.py', 'tests/live_handouts.py',
    )]
    for folder in ('app', 'assets', 'installer', 'licenses'):
        files += [p for p in (ROOT / folder).rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.log')]
    for path in sorted(set(files)):
        relative = path.relative_to(ROOT)
        assert path.is_file(), relative
        assert path.stat().st_size < 2_000_000, 'Unexpected large source file: ' + str(relative)
        target = SOURCE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    archive = DEST / ('MOOCDownloader-source-' + VERSION + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(SOURCE.rglob('*')):
            if path.is_file():
                bundle.write(path, Path('MOOCDownloader') / path.relative_to(SOURCE))
    ATTACHMENTS.mkdir(parents=True)
    installer = ROOT / 'release' / ('MOOCDownloader-Setup-' + VERSION + '-x64.exe')
    for path in (installer, ROOT / 'release/SHA256SUMS.txt'):
        shutil.copy2(path, ATTACHMENTS / path.name)
    shutil.copy2(ROOT / 'release/RELEASE_NOTES.md', DEST / 'RELEASE_NOTES.md')
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    assert (ROOT / 'release/SHA256SUMS.txt').read_text().strip() == digest + '  ' + installer.name
    names = [p.relative_to(SOURCE).as_posix() for p in SOURCE.rglob('*') if p.is_file()]
    assert not any(name.startswith(('.github/', '.git/', '.tools/', '_backup/', '_test_tmp/'))
                   or name.endswith(('.exe', '.pyc', '.log')) or name == 'cookies.json' for name in names)
    (DEST / 'SOURCE_FILES.txt').write_text('\n'.join(sorted(names))+'\n', encoding='utf-8')
    print('Source files:', len(names))
    print('Source archive bytes:', archive.stat().st_size)
    print('Installer bytes:', installer.stat().st_size)
    print('Installer SHA256:', digest)


if __name__ == '__main__':
    main()
