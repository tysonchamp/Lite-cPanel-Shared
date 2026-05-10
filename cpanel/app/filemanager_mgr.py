import os
import shutil
import mimetypes
import subprocess

def get_root_dir():
    try:
        from flask import session
        if session.get('role') == 'admin':
            return '/'
        return f"/home/{session.get('username')}"
    except Exception:
        return '/'

FORBIDDEN = ['/proc', '/sys', '/dev', '/run', '/boot']

def _safe_path(path):
    path = os.path.realpath(os.path.abspath(path))
    root = get_root_dir()
    if not path.startswith(root):
        return None
    for f in FORBIDDEN:
        if path.startswith(f):
            return None
    return path

def list_dir(path):
    safe = _safe_path(path)
    if not safe or not os.path.isdir(safe):
        return None, "Invalid or inaccessible path."
    try:
        entries = []
        for name in sorted(os.listdir(safe)):
            full = os.path.join(safe, name)
            try:
                stat = os.stat(full)
                is_dir = os.path.isdir(full)
                entries.append({
                    'name': name,
                    'path': full,
                    'is_dir': is_dir,
                    'size': stat.st_size if not is_dir else None,
                    'mtime': stat.st_mtime,
                    'perms': oct(stat.st_mode)[-3:],
                })
            except Exception:
                continue
        return {'path': safe, 'entries': entries, 'parent': os.path.dirname(safe)}, None
    except PermissionError:
        return None, "Permission denied."

def read_file(path):
    safe = _safe_path(path)
    if not safe or not os.path.isfile(safe):
        return None, "File not found."
    if os.path.getsize(safe) > 2 * 1024 * 1024:
        return None, "File too large to edit (>2MB)."
    mime = mimetypes.guess_type(safe)[0] or ''
    if not (mime.startswith('text/') or mime in ('application/json', 'application/xml', 'application/javascript')):
        # Try reading as text anyway for common config/code extensions
        ext = os.path.splitext(safe)[1].lower()
        if ext not in ('.txt','.py','.php','.js','.ts','.html','.htm','.css','.sh','.conf',
                       '.cfg','.ini','.env','.json','.xml','.yaml','.yml','.md','.sql','.log','.htaccess'):
            return None, "Binary file — cannot edit."
    try:
        with open(safe, 'r', errors='replace') as f:
            return f.read(), None
    except PermissionError:
        return None, "Permission denied."

def write_file(path, content):
    safe = _safe_path(path)
    if not safe:
        return False, "Invalid path."
    try:
        with open(safe, 'w') as f:
            f.write(content)
        return True, "File saved."
    except PermissionError:
        return False, "Permission denied."
    except Exception as e:
        return False, str(e)

def create_folder(parent, name):
    safe_parent = _safe_path(parent)
    if not safe_parent:
        return False, "Invalid path."
    name = os.path.basename(name)
    target = os.path.join(safe_parent, name)
    try:
        os.makedirs(target, exist_ok=True)
        return True, "Folder created."
    except PermissionError:
        return False, "Permission denied."
    except Exception as e:
        return False, str(e)

def rename_entry(path, new_name):
    safe = _safe_path(path)
    if not safe:
        return False, "Invalid path."
    new_name = os.path.basename(new_name)
    dest = os.path.join(os.path.dirname(safe), new_name)
    try:
        os.rename(safe, dest)
        return True, "Renamed successfully."
    except PermissionError:
        return False, "Permission denied."
    except Exception as e:
        return False, str(e)

def delete_entry(path):
    safe = _safe_path(path)
    if not safe or safe == '/':
        return False, "Invalid path."
    try:
        if os.path.isdir(safe):
            shutil.rmtree(safe)
        else:
            os.remove(safe)
        return True, "Deleted successfully."
    except PermissionError:
        return False, "Permission denied."
    except Exception as e:
        return False, str(e)

def save_upload(parent, file_storage):
    safe_parent = _safe_path(parent)
    if not safe_parent:
        return False, "Invalid path."
    filename = os.path.basename(file_storage.filename)
    if not filename:
        return False, "Invalid filename."
    dest = os.path.join(safe_parent, filename)
    try:
        file_storage.save(dest)
        return True, f"'{filename}' uploaded."
    except PermissionError:
        return False, "Permission denied."
    except Exception as e:
        return False, str(e)

def compress_entries(parent, names, archive_name, fmt):
    """Compress selected files/folders into an archive inside parent dir."""
    safe_parent = _safe_path(parent)
    if not safe_parent:
        return False, "Invalid path."

    archive_name = os.path.basename(archive_name)
    if not archive_name:
        return False, "Invalid archive name."

    # Validate all targets
    targets = []
    for name in names:
        full = os.path.join(safe_parent, os.path.basename(name))
        safe = _safe_path(full)
        if not safe or not os.path.exists(safe):
            return False, f"Invalid or missing: {name}"
        targets.append(os.path.basename(safe))

    dest = os.path.join(safe_parent, archive_name)

    try:
        if fmt == 'zip':
            if not archive_name.endswith('.zip'):
                dest += '.zip'
            cmd = ['zip', '-r', dest] + targets
        elif fmt == 'tar.gz':
            if not archive_name.endswith('.tar.gz'):
                dest += '.tar.gz'
            cmd = ['tar', '-czf', dest] + targets
        elif fmt == 'tar.bz2':
            if not archive_name.endswith('.tar.bz2'):
                dest += '.tar.bz2'
            cmd = ['tar', '-cjf', dest] + targets
        elif fmt == 'tar.xz':
            if not archive_name.endswith('.tar.xz'):
                dest += '.tar.xz'
            cmd = ['tar', '-cJf', dest] + targets
        else:
            return False, "Unsupported format."

        res = subprocess.run(cmd, capture_output=True, text=True, cwd=safe_parent)
        if res.returncode != 0:
            return False, res.stderr.strip() or "Compression failed."
        return True, f"Archive created: {os.path.basename(dest)}"
    except Exception as e:
        return False, str(e)


def decompress_entry(path, dest_dir=None):
    """Extract an archive into dest_dir (defaults to same directory)."""
    safe = _safe_path(path)
    if not safe or not os.path.isfile(safe):
        return False, "File not found."

    extract_to = _safe_path(dest_dir) if dest_dir else os.path.dirname(safe)
    if not extract_to:
        return False, "Invalid destination."

    name = safe.lower()
    try:
        if name.endswith('.zip'):
            cmd = ['unzip', '-o', safe, '-d', extract_to]
        elif name.endswith('.tar.gz') or name.endswith('.tgz'):
            cmd = ['tar', '-xzf', safe, '-C', extract_to]
        elif name.endswith('.tar.bz2'):
            cmd = ['tar', '-xjf', safe, '-C', extract_to]
        elif name.endswith('.tar.xz'):
            cmd = ['tar', '-xJf', safe, '-C', extract_to]
        elif name.endswith('.tar'):
            cmd = ['tar', '-xf', safe, '-C', extract_to]
        elif name.endswith('.gz'):
            cmd = ['gzip', '-dk', safe]
        elif name.endswith('.bz2'):
            cmd = ['bzip2', '-dk', safe]
        elif name.endswith('.xz'):
            cmd = ['xz', '-dk', safe]
        else:
            return False, "Unsupported archive format."

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            return False, res.stderr.strip() or "Extraction failed."
        return True, "Extracted successfully."
    except Exception as e:
        return False, str(e)


ARCHIVE_EXTS = ('.zip', '.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar', '.gz', '.bz2', '.xz')

def is_archive(name):
    n = name.lower()
    return any(n.endswith(ext) for ext in ARCHIVE_EXTS)
