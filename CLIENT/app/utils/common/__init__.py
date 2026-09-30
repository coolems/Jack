"""Common utilities package - modularized from the original monolithic common.py.

This package re-exports **every public name** from the sub-modules so that
existing import statements continue to work without changes::

    # Old style - still works exactly the same
    from app.utils.common import get_working_root, guard_path_inside_working_root

    # New explicit style (recommended for new code)
    from app.utils.common.working_root import get_working_root
    from app.utils.common.path_security import guard_path_inside_working_root

"""

# ---- Working root ----------------------------------------------------------
# SINGLE SOURCE OF TRUTH: the value lives ONLY in <CLIENT>/config/.working_root.json.
from .working_root import (
    _WORKING_ROOT_FILE,
    _load_persisted_working_root,
    set_working_root,
    get_working_root,
)

# Backward-compat: ``common.WORKING_ROOT`` attribute access still works via
# the ``__getattr__`` defined in working_root.py.  We re-expose it here so
# top-level package access also works.
def __getattr__(name):
    if name == "WORKING_ROOT":
        return get_working_root()
    raise AttributeError(f"module {__name__!r} has no attribute '{name}'")


# ---- Path security ---------------------------------------------------------
from .path_security import (
    _strip_extended_length_prefix,
    _is_path_inside_allowed,
    _denied_error,
    validate_filename,
    guard_path_inside_working_root,
    resolve_path_in_working_root,
    resolve_path_for_move,
    resolve_path_to_dir,
    is_safe_path,
    find_file,
)

# ---- File I/O --------------------------------------------------------------
from .file_io import (
    FILE_MAX_SIZE_BYTES,
    FILE_MAX_TEXT_CONTENT_BYTES,
    FILE_MAX_WRITE_BYTES,
    MAX_FILE_SIZE,
    MAX_TEXT_CONTENT_SIZE,
    MAX_WRITE_SIZE,
    TEXT_EXTENSIONS,
    EXCLUDED_FOLDERS,
    IMAGE_EXTENSIONS,
    is_text_file,
    is_image_file,
    encode_image_to_base64,
    read_text_file,
    get_file_info,
)


__all__ = [
    # working_root
    "_WORKING_ROOT_FILE",
    "_load_persisted_working_root",
    "set_working_root",
    "get_working_root",
    # path_security
    "_strip_extended_length_prefix",
    "_is_path_inside_allowed",
    "_denied_error",
    "validate_filename",
    "guard_path_inside_working_root",
    "resolve_path_in_working_root",
    "resolve_path_for_move",
    "resolve_path_to_dir",
    "is_safe_path",
    "find_file",
    # file_io
    "FILE_MAX_SIZE_BYTES",
    "FILE_MAX_TEXT_CONTENT_BYTES",
    "FILE_MAX_WRITE_BYTES",
    "MAX_FILE_SIZE",
    "MAX_TEXT_CONTENT_SIZE",
    "MAX_WRITE_SIZE",
    "TEXT_EXTENSIONS",
    "EXCLUDED_FOLDERS",
    "IMAGE_EXTENSIONS",
    "is_text_file",
    "is_image_file",
    "encode_image_to_base64",
    "read_text_file",
    "get_file_info",
]
