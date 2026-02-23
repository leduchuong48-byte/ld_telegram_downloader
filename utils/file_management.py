"""Utility functions to handle downloaded files."""
import glob
import os
import pathlib
import shutil
from hashlib import md5
from typing import Iterable, Tuple


def get_next_name(file_path: str) -> str:
    """
    Get next available name to download file.

    Parameters
    ----------
    file_path: str
        Absolute path of the file for which next available name to
        be generated.

    Returns
    -------
    str
        Absolute path of the next available name for the file.
    """
    posix_path = pathlib.Path(file_path)
    counter: int = 1
    new_file_name: str = os.path.join("{0}", "{1}-copy{2}{3}")
    while os.path.isfile(
        new_file_name.format(
            posix_path.parent,
            posix_path.stem,
            counter,
            "".join(posix_path.suffixes),
        )
    ):
        counter += 1
    return new_file_name.format(
        posix_path.parent,
        posix_path.stem,
        counter,
        "".join(posix_path.suffixes),
    )


def manage_duplicate_file(file_path: str):
    """
    Check if a file is duplicate.

    Compare the md5 of files with copy name pattern
    and remove if the md5 hash is same.

    Parameters
    ----------
    file_path: str
        Absolute path of the file for which duplicates needs to
        be managed.

    Returns
    -------
    str
        Absolute path of the duplicate managed file.
    """
    # pylint: disable = R1732
    posix_path = pathlib.Path(file_path)
    file_base_name: str = "".join(posix_path.stem.split("-copy")[0])
    name_pattern: str = f"{posix_path.parent}/{file_base_name}*"
    # Reason for using `str.translate()`
    # https://stackoverflow.com/q/22055500/6730439
    old_files: list = glob.glob(
        name_pattern.translate({ord("["): "[[]", ord("]"): "[]]"})
    )
    if file_path in old_files:
        old_files.remove(file_path)
    current_file_md5: str = md5(open(file_path, "rb").read()).hexdigest()
    for old_file_path in old_files:
        old_file_md5: str = md5(open(old_file_path, "rb").read()).hexdigest()
        if current_file_md5 == old_file_md5:
            os.remove(file_path)
            return old_file_path
    return file_path


def get_dir_size(path: str) -> int:
    """Get total size of files under the given path."""
    total = 0
    if not path or not os.path.exists(path):
        return total
    for root, _, files in os.walk(path):
        for name in files:
            file_path = os.path.join(root, name)
            try:
                total += os.path.getsize(file_path)
            except FileNotFoundError:
                continue
    return total


def _iter_files_by_mtime(path: str) -> Iterable[Tuple[str, int, float]]:
    files = []
    if not path or not os.path.exists(path):
        return files
    for root, _, filenames in os.walk(path):
        for name in filenames:
            file_path = os.path.join(root, name)
            try:
                stat_info = os.stat(file_path)
            except FileNotFoundError:
                continue
            files.append((file_path, stat_info.st_size, stat_info.st_mtime))
    files.sort(key=lambda item: item[2])
    return files


def cleanup_dir_by_freeing(path: str, target_free_bytes: int) -> int:
    """Delete oldest files until freed bytes reach target."""
    if target_free_bytes <= 0:
        return 0
    freed = 0
    for file_path, file_size, _ in _iter_files_by_mtime(path):
        try:
            os.remove(file_path)
            freed += file_size
        except FileNotFoundError:
            continue
        if freed >= target_free_bytes:
            break

    for root, dirs, files in os.walk(path, topdown=False):
        if root == path:
            continue
        if not dirs and not files:
            try:
                os.rmdir(root)
            except OSError:
                pass

    return freed


def clear_dir_contents(path: str) -> int:
    """Remove all contents under the path, keep the directory itself."""
    freed = 0
    if not path or not os.path.exists(path):
        return freed
    for name in os.listdir(path):
        file_path = os.path.join(path, name)
        if os.path.isfile(file_path) or os.path.islink(file_path):
            try:
                freed += os.path.getsize(file_path)
            except FileNotFoundError:
                pass
            try:
                os.remove(file_path)
            except FileNotFoundError:
                pass
        elif os.path.isdir(file_path):
            freed += get_dir_size(file_path)
            shutil.rmtree(file_path, ignore_errors=True)
    return freed
