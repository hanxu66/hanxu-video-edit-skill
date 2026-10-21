"""Read-only native Mac/Windows discovery; never installs or opens applications."""
import os
from pathlib import Path
import platform
import plistlib
import shutil


WINDOWS_APPS = {
    'ChatCut': ('ChatCut', 'ChatCut.exe'),
    'Jianying': ('JianyingPro', 'JianyingPro.exe'),
    'CapCut': ('CapCut', 'CapCut.exe'),
}


def host_info():
    system = platform.system()
    release = Path('/proc/sys/kernel/osrelease')
    wsl = system == 'Linux' and (bool(os.environ.get('WSL_DISTRO_NAME')) or
          (release.is_file() and 'microsoft' in release.read_text(encoding='utf-8').lower()))
    return {'system': system, 'architecture': platform.machine(), 'wsl': wsl,
            'native_desktop_platform': system in ('Darwin', 'Windows') and not wsl}


def windows_registry_apps():
    if platform.system() != 'Windows':
        return []
    import winreg
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for keyname in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                        r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
            try:
                with winreg.OpenKey(hive, keyname) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as item:
                                def value(name):
                                    try: return str(winreg.QueryValueEx(item, name)[0])
                                    except OSError: return ''
                                name = value('DisplayName')
                                label = next((k for k, words in {
                                    'ChatCut': ('chatcut',), 'Jianying': ('jianying', '剪映'),
                                    'CapCut': ('capcut',)}.items()
                                    if any(word in name.lower() for word in words)), None)
                                if label:
                                    found.append((label, value('InstallLocation'), value('DisplayIcon'), value('DisplayVersion')))
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def application_candidates(system=None):
    system = system or platform.system()
    result = {label: [] for label in WINDOWS_APPS}
    if system == 'Darwin':
        names = {'ChatCut': ('ChatCut.app',), 'Jianying': ('VideoFusion-macOS.app', 'JianyingPro.app', '剪映专业版.app'), 'CapCut': ('CapCut.app',)}
        for label, leaves in names.items():
            result[label] = [base / leaf for base in (Path('/Applications'), Path.home()/'Applications') for leaf in leaves]
    elif system == 'Windows':
        roots = [Path(os.environ[k]) for k in ('LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)') if os.environ.get(k)]
        for label, (folder, executable) in WINDOWS_APPS.items():
            located = shutil.which(executable)
            if located: result[label].append(Path(located))
            for root in roots:
                for base in (root/folder, root/'Programs'/folder):
                    result[label].extend((base/executable, base/'Apps'/executable))
                    for parent in (base, base/'Apps'):
                        if parent.is_dir(): result[label].extend(sorted(parent.glob('*/'+executable)))
        for label, location, icon, version in windows_registry_apps():
            if location:
                base = Path(os.path.expandvars(location.strip('"')))
                exe = WINDOWS_APPS[label][1]
                result[label].append(base/exe)
                if base.is_dir(): result[label].extend(sorted(base.glob('*/'+exe)))
            if icon:
                result[label].append(Path(os.path.expandvars(icon.split(',')[0].strip('"'))))
    return result


def observe_apps():
    system = platform.system()
    found = {label: [] for label in WINDOWS_APPS}
    for label, candidates in application_candidates().items():
        seen = set()
        for p in candidates:
            exists = p.is_dir() if system == 'Darwin' else p.is_file()
            if not exists or str(p.resolve()).casefold() in seen: continue
            seen.add(str(p.resolve()).casefold())
            version = None
            if system == 'Darwin':
                try:
                    with (p/'Contents/Info.plist').open('rb') as f:
                        version = plistlib.load(f).get('CFBundleShortVersionString')
                except (OSError, ValueError): pass
            found[label].append({'path': str(p.resolve()), 'version': version})
    return {'status': 'observed' if system in ('Darwin','Windows') else 'not_checked_on_this_platform',
            'installed': found, 'connection': 'not_checked',
            'scope': 'Read-only common paths/Windows uninstall entries/PATH; custom installs may require explicit paths. Existence is not tool or GUI access.'}


def binary_candidates(name, root, explicit=None):
    if explicit:
        return [Path(explicit).expanduser()]
    suffix = '.exe' if platform.system() == 'Windows' else ''
    candidates = [Path(root)/'.video-agent/runtime/bin'/(name+suffix)]
    located = shutil.which(name)
    if located: candidates.append(Path(located))
    for row in observe_apps()['installed']['ChatCut']:
        p = Path(row['path'])
        base = p/'Contents/Resources' if platform.system() == 'Darwin' else p.parent/'resources'
        package = base/'app.asar.unpacked/node_modules/ffmpeg-ffprobe-static'
        candidates.extend((package/(name+suffix), package/'bin'/(name+suffix)))
    return candidates


def font_candidates():
    system = platform.system()
    if system == 'Darwin':
        bases = [Path.home()/'Library/Fonts', Path('/Library/Fonts')]
    elif system == 'Windows':
        bases = [Path(os.environ['WINDIR'])/'Fonts'] if os.environ.get('WINDIR') else []
        if os.environ.get('LOCALAPPDATA'): bases.append(Path(os.environ['LOCALAPPDATA'])/'Microsoft/Windows/Fonts')
    else:
        bases = []
    paths = [p for base in bases if base.is_dir() for p in base.iterdir()
             if p.suffix.lower() in ('.ttf','.otf','.ttc') and '三极宋黑体' in p.name]
    if system == 'Windows':
        import winreg
        for hive, base in ((winreg.HKEY_LOCAL_MACHINE, bases[0] if bases else Path.home()),
                           (winreg.HKEY_CURRENT_USER, Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Microsoft/Windows/Fonts')):
            try:
                with winreg.OpenKey(hive,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        label, raw, _ = winreg.EnumValue(key,i)
                        if '三极宋黑体' in label and isinstance(raw,str):
                            p = Path(os.path.expandvars(raw)); paths.append(p if p.is_absolute() else base/p)
            except OSError: pass
    return sorted({str(p.resolve()) for p in paths if p.is_file()})
