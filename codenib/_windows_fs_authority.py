# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared Win32 HANDLE and FILE_ID filesystem primitives.

This module owns only kernel bindings and handle-level metadata operations.
Publication, source-containment, and staging policy remain in their respective
callers.  Child paths are deliberately absent from this interface: callers
enumerate an already-open parent and reopen entries by filesystem identity.
"""

from __future__ import annotations

import ctypes
import errno
import ntpath
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterator

INVALID_HANDLE_VALUE = -1
FILE_LIST_DIRECTORY = 0x0001
FILE_READ_DATA = 0x0001
FILE_READ_ATTRIBUTES = 0x0080
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ID_BOTH_DIRECTORY_INFO = 10
FILE_ID_BOTH_DIRECTORY_RESTART_INFO = 11
FILE_STANDARD_INFO = 1
FILE_ATTRIBUTE_TAG_INFO = 9
FILE_ID_INFO = 18
FILE_ID_EXTD_DIRECTORY_INFO = 19
FILE_ID_EXTD_DIRECTORY_RESTART_INFO = 20
FILE_RENAME_INFO = 3
FILE_ID_TYPE = 0
EXTENDED_FILE_ID_TYPE = 2
FSCTL_GET_REPARSE_POINT = 0x000900A8
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
SYMLINK_FLAG_RELATIVE = 0x00000001
MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024
OBJ_DONT_REPARSE = 0x00001000
FILE_OPEN = 0x00000001
FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_OPEN_REPARSE_POINT = 0x00200000
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_NO_MORE_FILES = 18
ERROR_FILE_EXISTS = 80
ERROR_ALREADY_EXISTS = 183
ERROR_INVALID_FUNCTION = 1
ERROR_NOT_SUPPORTED = 50
DIRECTORY_QUERY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class WindowsFileId:
    """One volume-scoped 128-bit Windows filesystem identity."""

    volume_serial: int
    value: bytes


@dataclass(frozen=True, slots=True)
class WindowsHandleMetadata:
    """Windows-native metadata retained by handle-bound policy layers."""

    st_dev: int
    st_ino: int
    st_mode: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int
    st_nlink: int
    st_file_attributes: int
    file_id_128: bytes = b""
    reparse_tag: int = 0
    delete_pending: bool = False

    @property
    def file_identity(self) -> WindowsFileId:
        """Return the exact volume-scoped identity reported for this handle."""

        return WindowsFileId(self.st_dev, self.file_id_128)


@dataclass(frozen=True, slots=True)
class WindowsDirectoryEntry:
    """One enumerated child name and its filesystem identity."""

    name: str
    file_id: int
    attributes: int
    file_id_128: bytes = b""
    reparse_tag: int = 0


@dataclass(frozen=True, slots=True)
class WindowsReparsePoint:
    """Parsed data for one no-follow reparse-point handle."""

    tag: int
    substitute_name: str | None
    print_name: str | None
    flags: int = 0


def parse_windows_reparse_data(payload: bytes) -> WindowsReparsePoint:
    """Strictly decode one FSCTL_GET_REPARSE_POINT response buffer."""

    if not isinstance(payload, bytes) or len(payload) < 8:
        raise RuntimeError("Windows reparse-point data is truncated")
    tag = int.from_bytes(payload[0:4], "little")
    data_length = int.from_bytes(payload[4:6], "little")
    data_end = 8 + data_length
    if data_end > len(payload) or data_end > MAXIMUM_REPARSE_DATA_BUFFER_SIZE:
        raise RuntimeError("Windows reparse-point data length is invalid")
    if tag != IO_REPARSE_TAG_SYMLINK:
        return WindowsReparsePoint(tag, None, None)
    if data_length < 12 or data_end < 20:
        raise RuntimeError("Windows symbolic-link reparse data is truncated")
    substitute_offset = int.from_bytes(payload[8:10], "little")
    substitute_length = int.from_bytes(payload[10:12], "little")
    print_offset = int.from_bytes(payload[12:14], "little")
    print_length = int.from_bytes(payload[14:16], "little")
    flags = int.from_bytes(payload[16:20], "little")
    path_start = 20

    def decode_name(offset: int, length: int) -> str:
        if offset % 2 or length % 2:
            raise RuntimeError("Windows symbolic-link name is misaligned")
        start = path_start + offset
        end = start + length
        if start < path_start or end > data_end:
            raise RuntimeError("Windows symbolic-link name is out of bounds")
        try:
            return payload[start:end].decode("utf-16-le", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                "Windows symbolic-link name is not valid UTF-16"
            ) from exc

    return WindowsReparsePoint(
        tag=tag,
        substitute_name=decode_name(substitute_offset, substitute_length),
        print_name=decode_name(print_offset, print_length),
        flags=flags,
    )


def windows_file_id_is_reliable(value: object) -> bool:
    """Return whether *value* is one non-zero 128-bit FILE_ID."""

    return isinstance(value, bytes) and len(value) == 16 and any(value)


def windows_metadata_identity(
    metadata: WindowsHandleMetadata,
) -> tuple[object, ...]:
    """Return the exact version token used by HANDLE authority checks."""

    return (
        metadata.st_dev,
        metadata.file_id_128,
        metadata.st_mode,
        metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
        metadata.st_file_attributes,
        metadata.reparse_tag,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
        metadata.delete_pending,
    )


def windows_handle_ownership_identity(
    metadata: WindowsHandleMetadata,
) -> tuple[object, ...]:
    """Return the stable kernel-object identity used only for HANDLE cleanup.

    Directory timestamps, link counts, attributes, and delete-pending state are
    version/authentication data. They may change while the retained HANDLE still
    names the same kernel object and therefore must not decide close ownership.
    """

    return (metadata.st_dev, metadata.file_id_128)


def windows_extended_path(path: Path) -> str:
    """Return one canonical extended-length path for opening a root authority."""

    absolute = str(path.absolute())
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def windows_mode_from_attributes(attributes: int) -> int:
    """Mirror Windows readonly semantics without inventing execute bits."""

    readonly = bool(attributes & FILE_ATTRIBUTE_READONLY)
    if attributes & FILE_ATTRIBUTE_DIRECTORY:
        permissions = 0o555 if readonly else 0o777
        return stat.S_IFDIR | permissions
    permissions = 0o444 if readonly else 0o666
    return stat.S_IFREG | permissions


class WindowsKernelApi:
    """Small HANDLE API shared by filesystem authority policy layers.

    The higher-level methods make the ABI replaceable in simulation tests.
    Future source-containment and staging work can extend this class with
    reparse queries and root-relative opens without duplicating ctypes.
    """

    __slots__ = (
        "kernel32",
        "ntdll",
        "wintypes",
        "BY_HANDLE_FILE_INFORMATION",
        "FILE_BASIC_INFO",
        "FILE_STANDARD_INFO",
        "FILE_ATTRIBUTE_TAG_INFO",
        "FILE_ID_128",
        "FILE_ID_INFO",
        "FILE_ID_DESCRIPTOR",
        "FILE_ID_BOTH_DIR_INFO",
        "FILE_ID_EXTD_DIR_INFO",
        "FILE_RENAME_INFO",
        "UNICODE_STRING",
        "OBJECT_ATTRIBUTES",
        "IO_STATUS_BLOCK",
        "invalid_handle",
    )

    def __init__(self) -> None:
        if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
            raise RuntimeError("Windows HANDLE filesystem API is unavailable")
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        class FILE_BASIC_INFO(ctypes.Structure):
            _fields_ = [
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
            ]

        class FILE_STANDARD_INFO_STRUCT(ctypes.Structure):
            _fields_ = [
                ("AllocationSize", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("NumberOfLinks", wintypes.DWORD),
                ("DeletePending", wintypes.BOOLEAN),
                ("Directory", wintypes.BOOLEAN),
            ]

        class FILE_ATTRIBUTE_TAG_INFO_STRUCT(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            ]

        class FILE_ID_128_STRUCT(ctypes.Structure):
            _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

        class FILE_ID_INFO_STRUCT(ctypes.Structure):
            _fields_ = [
                ("VolumeSerialNumber", ctypes.c_ulonglong),
                ("FileId", FILE_ID_128_STRUCT),
            ]

        class FILE_ID_VALUE(ctypes.Union):
            _fields_ = [
                ("FileId", ctypes.c_longlong),
                ("ObjectId", ctypes.c_ubyte * 16),
                ("ExtendedFileId", ctypes.c_ubyte * 16),
            ]

        class FILE_ID_DESCRIPTOR(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("Type", ctypes.c_int),
                ("value", FILE_ID_VALUE),
            ]

        class FILE_ID_EXTD_DIR_INFO_STRUCT(ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", wintypes.DWORD),
                ("FileIndex", wintypes.DWORD),
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("AllocationSize", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
                ("FileNameLength", wintypes.DWORD),
                ("EaSize", wintypes.DWORD),
                ("ReparsePointTag", wintypes.DWORD),
                ("FileId", FILE_ID_128_STRUCT),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class FILE_ID_BOTH_DIR_INFO_STRUCT(ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", wintypes.DWORD),
                ("FileIndex", wintypes.DWORD),
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong),
                ("AllocationSize", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
                ("FileNameLength", wintypes.DWORD),
                ("EaSize", wintypes.DWORD),
                ("ShortNameLength", ctypes.c_byte),
                ("ShortName", wintypes.WCHAR * 12),
                ("FileId", ctypes.c_longlong),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class FILE_RENAME_INFO_STRUCT(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        class UNICODE_STRING_STRUCT(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class OBJECT_ATTRIBUTES_STRUCT(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UNICODE_STRING_STRUCT)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        class IO_STATUS_BLOCK_STRUCT(ctypes.Structure):
            _fields_ = [
                ("Status", ctypes.c_void_p),
                ("Information", ctypes.c_size_t),
            ]

        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
        )
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.OpenFileById.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FILE_ID_DESCRIPTOR),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.OpenFileById.restype = wintypes.HANDLE
        kernel32.ReadFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.DeviceIoControl.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.DuplicateHandle.restype = wintypes.BOOL

        ntdll.NtCreateFile.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(OBJECT_ATTRIBUTES_STRUCT),
            ctypes.POINTER(IO_STATUS_BLOCK_STRUCT),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        )
        ntdll.NtCreateFile.restype = ctypes.c_long
        ntdll.RtlNtStatusToDosError.argtypes = (wintypes.ULONG,)
        ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

        self.kernel32 = kernel32
        self.ntdll = ntdll
        self.wintypes = wintypes
        self.BY_HANDLE_FILE_INFORMATION = BY_HANDLE_FILE_INFORMATION
        self.FILE_BASIC_INFO = FILE_BASIC_INFO
        self.FILE_STANDARD_INFO = FILE_STANDARD_INFO_STRUCT
        self.FILE_ATTRIBUTE_TAG_INFO = FILE_ATTRIBUTE_TAG_INFO_STRUCT
        self.FILE_ID_128 = FILE_ID_128_STRUCT
        self.FILE_ID_INFO = FILE_ID_INFO_STRUCT
        self.FILE_ID_DESCRIPTOR = FILE_ID_DESCRIPTOR
        self.FILE_ID_BOTH_DIR_INFO = FILE_ID_BOTH_DIR_INFO_STRUCT
        self.FILE_ID_EXTD_DIR_INFO = FILE_ID_EXTD_DIR_INFO_STRUCT
        self.FILE_RENAME_INFO = FILE_RENAME_INFO_STRUCT
        self.UNICODE_STRING = UNICODE_STRING_STRUCT
        self.OBJECT_ATTRIBUTES = OBJECT_ATTRIBUTES_STRUCT
        self.IO_STATUS_BLOCK = IO_STATUS_BLOCK_STRUCT
        self.invalid_handle = ctypes.c_void_p(INVALID_HANDLE_VALUE).value

    def _raise_last_error(self, context: str) -> None:
        error = ctypes.get_last_error()
        if error in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
            raise FileNotFoundError(error, context)
        if error in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS}:
            raise FileExistsError(error, context)
        if error in {ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED}:
            raise RuntimeError(
                "filesystem does not support HANDLE-bound directory authority"
            )
        raise ctypes.WinError(error)

    def _raise_ntstatus(self, status: int, context: str) -> None:
        error = int(self.ntdll.RtlNtStatusToDosError(status & 0xFFFFFFFF))
        ctypes.set_last_error(error)
        self._raise_last_error(context)

    @staticmethod
    def _handle_value(handle: object) -> int:
        value = getattr(handle, "value", handle)
        return 0 if value is None else int(value)

    def create_directory_handle(self, path: Path) -> int:
        handle = self.kernel32.CreateFileW(
            windows_extended_path(path),
            FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        value = self._handle_value(handle)
        if value == self.invalid_handle:
            self._raise_last_error(f"could not open filesystem authority: {path}")
        return value

    def duplicate_handle(self, handle: int) -> int:
        duplicate = self.wintypes.HANDLE()
        process = self.kernel32.GetCurrentProcess()
        if not self.kernel32.DuplicateHandle(
            process,
            handle,
            process,
            ctypes.byref(duplicate),
            0,
            False,
            0x00000002,  # DUPLICATE_SAME_ACCESS
        ):
            self._raise_last_error("could not duplicate filesystem authority handle")
        return self._handle_value(duplicate)

    def close(self, handle: int) -> None:
        if handle not in {0, self.invalid_handle}:
            if not self.kernel32.CloseHandle(handle):
                self._raise_last_error("could not close filesystem authority HANDLE")

    def metadata(self, handle: int) -> WindowsHandleMetadata:
        information = self.BY_HANDLE_FILE_INFORMATION()
        if not self.kernel32.GetFileInformationByHandle(
            handle,
            ctypes.byref(information),
        ):
            self._raise_last_error("could not read HANDLE identity")
        basic = self.FILE_BASIC_INFO()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle,
            0,  # FileBasicInfo
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            self._raise_last_error("could not read HANDLE version metadata")
        standard = self.FILE_STANDARD_INFO()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle,
            FILE_STANDARD_INFO,
            ctypes.byref(standard),
            ctypes.sizeof(standard),
        ):
            self._raise_last_error("could not read HANDLE standard metadata")
        identity = self.FILE_ID_INFO()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle,
            FILE_ID_INFO,
            ctypes.byref(identity),
            ctypes.sizeof(identity),
        ):
            self._raise_last_error("could not read HANDLE 128-bit identity")
        tag = self.FILE_ATTRIBUTE_TAG_INFO()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle,
            FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(tag),
            ctypes.sizeof(tag),
        ):
            self._raise_last_error("could not read HANDLE reparse metadata")
        attributes = int(tag.FileAttributes)
        file_id_128 = bytes(identity.FileId.Identifier)
        legacy_file_id = (int(information.nFileIndexHigh) << 32) | int(
            information.nFileIndexLow
        )
        return WindowsHandleMetadata(
            st_dev=int(identity.VolumeSerialNumber),
            st_ino=legacy_file_id,
            st_mode=windows_mode_from_attributes(attributes),
            st_size=int(standard.EndOfFile),
            st_mtime_ns=int(basic.LastWriteTime) * 100,
            st_ctime_ns=int(basic.ChangeTime) * 100,
            st_nlink=int(standard.NumberOfLinks),
            st_file_attributes=attributes,
            file_id_128=file_id_128,
            reparse_tag=(
                int(tag.ReparseTag) if attributes & FILE_ATTRIBUTE_REPARSE_POINT else 0
            ),
            delete_pending=bool(standard.DeletePending),
        )

    def iter_directory(self, handle: int) -> Iterator[WindowsDirectoryEntry]:
        """Yield children incrementally from an already-open directory HANDLE."""

        information_class = FILE_ID_EXTD_DIRECTORY_RESTART_INFO
        while True:
            buffer = ctypes.create_string_buffer(DIRECTORY_QUERY_BYTES)
            if not self.kernel32.GetFileInformationByHandleEx(
                handle,
                information_class,
                buffer,
                len(buffer),
            ):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    break
                self._raise_last_error("could not enumerate directory HANDLE")
            information_class = FILE_ID_EXTD_DIRECTORY_INFO
            offset = 0
            while True:
                entry = self.FILE_ID_EXTD_DIR_INFO.from_buffer(buffer, offset)
                name_offset = offset + self.FILE_ID_EXTD_DIR_INFO.FileName.offset
                name_bytes = bytes(
                    buffer[name_offset : name_offset + int(entry.FileNameLength)]
                )
                try:
                    name = name_bytes.decode("utf-16-le", errors="strict")
                except UnicodeDecodeError as exc:
                    raise RuntimeError(
                        "Windows directory name is not valid UTF-16"
                    ) from exc
                if name not in {".", ".."}:
                    file_id_128 = bytes(entry.FileId.Identifier)
                    yield WindowsDirectoryEntry(
                        name=name,
                        file_id=int.from_bytes(file_id_128[:8], "little"),
                        attributes=int(entry.FileAttributes),
                        file_id_128=file_id_128,
                        reparse_tag=(
                            int(entry.ReparsePointTag)
                            if int(entry.FileAttributes) & FILE_ATTRIBUTE_REPARSE_POINT
                            else 0
                        ),
                    )
                next_offset = int(entry.NextEntryOffset)
                if not next_offset:
                    break
                offset += next_offset
                if offset >= len(buffer):
                    raise RuntimeError("Windows directory enumeration is malformed")

    def enumerate_directory(self, handle: int) -> tuple[WindowsDirectoryEntry, ...]:
        """Return all children for compatibility with publication callers."""

        output: list[WindowsDirectoryEntry] = []
        information_class = FILE_ID_BOTH_DIRECTORY_RESTART_INFO
        while True:
            buffer = ctypes.create_string_buffer(DIRECTORY_QUERY_BYTES)
            if not self.kernel32.GetFileInformationByHandleEx(
                handle,
                information_class,
                buffer,
                len(buffer),
            ):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    break
                self._raise_last_error("could not enumerate directory HANDLE")
            information_class = FILE_ID_BOTH_DIRECTORY_INFO
            offset = 0
            while True:
                entry = self.FILE_ID_BOTH_DIR_INFO.from_buffer(buffer, offset)
                name_offset = offset + self.FILE_ID_BOTH_DIR_INFO.FileName.offset
                name_bytes = bytes(
                    buffer[name_offset : name_offset + int(entry.FileNameLength)]
                )
                try:
                    name = name_bytes.decode("utf-16-le", errors="strict")
                except UnicodeDecodeError as exc:
                    raise RuntimeError(
                        "Windows directory name is not valid UTF-16"
                    ) from exc
                if name not in {".", ".."}:
                    output.append(
                        WindowsDirectoryEntry(
                            name=name,
                            file_id=int(entry.FileId) & ((1 << 64) - 1),
                            attributes=int(entry.FileAttributes),
                        )
                    )
                next_offset = int(entry.NextEntryOffset)
                if not next_offset:
                    break
                offset += next_offset
                if offset >= len(buffer):
                    raise RuntimeError("Windows directory enumeration is malformed")
        return tuple(output)

    def open_by_extended_id(
        self,
        volume_hint: int,
        file_id_128: bytes,
        *,
        desired_access: int,
        is_directory: bool,
    ) -> int:
        """Open one object by its exact 128-bit FILE_ID without following it."""

        if not windows_file_id_is_reliable(file_id_128):
            raise ValueError("Windows extended FILE_ID is not reliable")
        descriptor = self.FILE_ID_DESCRIPTOR()
        descriptor.dwSize = ctypes.sizeof(descriptor)
        descriptor.Type = EXTENDED_FILE_ID_TYPE
        ctypes.memmove(
            ctypes.addressof(descriptor.ExtendedFileId),
            file_id_128,
            len(file_id_128),
        )
        flags = FILE_FLAG_OPEN_REPARSE_POINT
        if is_directory:
            flags |= FILE_FLAG_BACKUP_SEMANTICS
        handle = self.kernel32.OpenFileById(
            volume_hint,
            ctypes.byref(descriptor),
            desired_access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            flags,
        )
        value = self._handle_value(handle)
        if value == self.invalid_handle:
            self._raise_last_error("could not open directory entry by extended FILE_ID")
        return value

    def open_relative(
        self,
        parent_handle: int,
        name: str,
        *,
        desired_access: int,
        is_directory: bool,
        allow_reparse: bool,
    ) -> int:
        """Open one simple child relative to a retained directory HANDLE."""

        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "\\" in name
            or "/" in name
            or "\x00" in name
        ):
            raise ValueError("Windows relative open requires one simple child name")
        encoded = name.encode("utf-16-le", errors="strict")
        if len(encoded) > 0xFFFE:
            raise ValueError("Windows relative child name is too long")
        name_buffer = ctypes.create_unicode_buffer(name)
        unicode_name = self.UNICODE_STRING()
        unicode_name.Length = len(encoded)
        unicode_name.MaximumLength = len(encoded) + 2
        unicode_name.Buffer = ctypes.cast(name_buffer, self.wintypes.LPWSTR)
        attributes = self.OBJECT_ATTRIBUTES()
        attributes.Length = ctypes.sizeof(attributes)
        attributes.RootDirectory = parent_handle
        attributes.ObjectName = ctypes.pointer(unicode_name)
        attributes.Attributes = 0 if allow_reparse else OBJ_DONT_REPARSE
        attributes.SecurityDescriptor = None
        attributes.SecurityQualityOfService = None
        io_status = self.IO_STATUS_BLOCK()
        opened = self.wintypes.HANDLE()
        options = FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT
        options |= FILE_DIRECTORY_FILE if is_directory else FILE_NON_DIRECTORY_FILE
        status = int(
            self.ntdll.NtCreateFile(
                ctypes.byref(opened),
                desired_access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                FILE_OPEN,
                options,
                None,
                0,
            )
        )
        if status < 0:
            self._raise_ntstatus(status, "could not open HANDLE-relative child")
        return self._handle_value(opened)

    def query_reparse_point(self, handle: int) -> WindowsReparsePoint:
        """Read and strictly parse one already-open reparse point."""

        buffer = ctypes.create_string_buffer(MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
        returned = self.wintypes.DWORD()
        if not self.kernel32.DeviceIoControl(
            handle,
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buffer,
            len(buffer),
            ctypes.byref(returned),
            None,
        ):
            self._raise_last_error("could not read HANDLE reparse-point data")
        payload = bytes(buffer[: int(returned.value)])
        return parse_windows_reparse_data(payload)

    def open_by_id(
        self,
        volume_hint: int,
        file_id: int,
        *,
        desired_access: int,
        is_directory: bool,
    ) -> int:
        descriptor = self.FILE_ID_DESCRIPTOR()
        descriptor.dwSize = ctypes.sizeof(descriptor)
        descriptor.Type = FILE_ID_TYPE
        signed_id = file_id if file_id < (1 << 63) else file_id - (1 << 64)
        descriptor.FileId = signed_id
        flags = FILE_FLAG_OPEN_REPARSE_POINT
        if is_directory:
            flags |= FILE_FLAG_BACKUP_SEMANTICS
        handle = self.kernel32.OpenFileById(
            volume_hint,
            ctypes.byref(descriptor),
            desired_access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            flags,
        )
        value = self._handle_value(handle)
        if value == self.invalid_handle:
            self._raise_last_error("could not open directory entry by FILE_ID")
        return value

    def read(self, handle: int, size: int) -> bytes:
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        read = self.wintypes.DWORD()
        if not self.kernel32.ReadFile(
            handle,
            buffer,
            size,
            ctypes.byref(read),
            None,
        ):
            self._raise_last_error("could not read FILE_ID-bound file")
        return bytes(buffer[: int(read.value)])

    def rename_noreplace(
        self,
        source_handle: int,
        parent_handle: int,
        destination: str,
    ) -> None:
        encoded = destination.encode("utf-16-le", errors="strict")
        structure_type = self.FILE_RENAME_INFO
        size = structure_type.FileName.offset + len(encoded)
        buffer = ctypes.create_string_buffer(size)
        information = structure_type.from_buffer(buffer)
        information.ReplaceIfExists = False
        information.RootDirectory = parent_handle
        information.FileNameLength = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + structure_type.FileName.offset,
            encoded,
            len(encoded),
        )
        if not self.kernel32.SetFileInformationByHandle(
            source_handle,
            FILE_RENAME_INFO,
            buffer,
            size,
        ):
            self._raise_last_error("could not perform no-replace HANDLE rename")


@dataclass(frozen=True, slots=True)
class _WindowsAuthorityObservation:
    parent_handle: int
    parent_identity: tuple[object, ...]
    name: str
    child_handle: int
    child_identity: tuple[object, ...]
    child_file_id: bytes


class WindowsDirectoryAuthority:
    """Retained no-follow HANDLE chain for one lexical Windows directory."""

    __slots__ = (
        "api",
        "path",
        "handles",
        "observations",
        "anchor_identity",
        "closed",
    )

    def __init__(
        self,
        *,
        api: WindowsKernelApi,
        path: Path,
        handles: list[int],
        observations: list[_WindowsAuthorityObservation],
        anchor_identity: tuple[object, ...],
    ) -> None:
        self.api = api
        self.path = path
        self.handles = handles
        self.observations = observations
        self.anchor_identity = anchor_identity
        self.closed = False

    @property
    def handle(self) -> int:
        if self.closed or not self.handles:
            raise RuntimeError("Windows directory authority is closed")
        return self.handles[-1]

    @property
    def identity(self) -> tuple[object, ...]:
        return windows_metadata_identity(self.api.metadata(self.handle))

    def _find_exact_child(
        self,
        parent_handle: int,
        name: str,
    ) -> WindowsDirectoryEntry | None:
        iterator = getattr(self.api, "iter_directory", None)
        entries = (
            iterator(parent_handle)
            if callable(iterator)
            else self.api.enumerate_directory(parent_handle)
        )
        matches = [entry for entry in entries if entry.name == name]
        if len(matches) > 1:
            raise RuntimeError(
                "Windows authority directory has duplicate exact child names"
            )
        return matches[0] if matches else None

    def verify(self) -> None:
        if self.closed or not self.handles:
            raise RuntimeError("Windows directory authority is closed")
        anchor = self.api.metadata(self.handles[0])
        if (
            windows_metadata_identity(anchor) != self.anchor_identity
            or not stat.S_ISDIR(anchor.st_mode)
            or anchor.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT
            or not anchor.st_dev
            or not windows_file_id_is_reliable(anchor.file_id_128)
            or anchor.delete_pending
        ):
            raise RuntimeError("Windows lexical anchor changed")
        for observation in self.observations:
            if (
                windows_metadata_identity(self.api.metadata(observation.parent_handle))
                != observation.parent_identity
            ):
                raise RuntimeError("Windows lexical parent changed")
            entry = self._find_exact_child(
                observation.parent_handle,
                observation.name,
            )
            if entry is None or entry.file_id_128 != observation.child_file_id:
                raise RuntimeError("Windows lexical directory binding changed")
            opened = self.api.metadata(observation.child_handle)
            if (
                windows_metadata_identity(opened) != observation.child_identity
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT
                or not windows_file_id_is_reliable(opened.file_id_128)
                or opened.delete_pending
            ):
                raise RuntimeError("Windows lexical directory component changed")

    def close(self) -> None:
        if self.closed:
            return
        # A cancellation may land after the final HANDLE was confirmed closed
        # but before the prior call could publish ``closed``.  Empty ownership
        # is authoritative and makes the retry a finite state transition.
        if not self.handles:
            self.closed = True
            return
        expected = {self.handles[0]: self.anchor_identity[:2]}
        expected.update(
            {
                observation.child_handle: observation.child_identity[:2]
                for observation in self.observations
            }
        )
        failure = _close_windows_handles(self.api, self.handles, expected)
        self.closed = not self.handles
        if failure is not None:
            raise failure


def _close_windows_handles(
    api: WindowsKernelApi,
    handles: list[int],
    expected_identities: dict[int, tuple[object, ...]],
) -> BaseException | None:
    """Visit every HANDLE and retain the first delayed close failure."""

    deferred: BaseException | None = None
    for handle in reversed(tuple(handles)):
        closed = False
        while True:
            try:
                observed = api.metadata(handle)
                expected = expected_identities.get(handle)
                if (
                    expected is not None
                    and windows_handle_ownership_identity(observed) != expected
                ):
                    deferred = deferred or RuntimeError(
                        "Windows HANDLE ownership changed"
                    )
                    closed = True
                    break
            except KeyError:
                closed = True
                break
            except OSError as exc:
                if exc.errno == errno.EBADF or getattr(exc, "winerror", None) == 6:
                    closed = True
                    break
                deferred = deferred or exc
                break
            except BaseException as exc:  # noqa: B036 - confirm close result
                if isinstance(exc, Exception):
                    deferred = deferred or exc
                    break
                deferred = deferred or exc
                continue

            expected_at_close = windows_handle_ownership_identity(observed)
            try:
                api.close(handle)
                closed = True
            except BaseException as exc:  # noqa: B036 - confirm close result
                deferred = deferred or exc
                while True:
                    try:
                        visible = api.metadata(handle)
                    except KeyError:
                        closed = True
                        break
                    except OSError as probe_error:
                        if (
                            probe_error.errno == errno.EBADF
                            or getattr(probe_error, "winerror", None) == 6
                        ):
                            closed = True
                        else:
                            deferred = deferred or probe_error
                        break
                    except BaseException as probe_error:  # noqa: B036
                        if isinstance(probe_error, Exception):
                            deferred = deferred or probe_error
                            break
                        deferred = deferred or probe_error
                        continue
                    if windows_handle_ownership_identity(visible) != expected_at_close:
                        # The owned HANDLE closed and its numeric value was
                        # reused. Never close the replacement authority.
                        closed = True
                    break
                if closed or isinstance(exc, Exception):
                    break
                continue
            break

        if closed:
            while handle in handles:
                try:
                    handles.pop(handles.index(handle))
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        raise
                    deferred = deferred or exc
            while handle in expected_identities:
                try:
                    expected_identities.pop(handle, None)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        raise
                    deferred = deferred or exc
    return deferred


class _WindowsHandleCleanup:
    """Retryable owner installed before the first Windows HANDLE acquisition."""

    __slots__ = ("api", "handles", "expected_identities")

    def __init__(self, api: WindowsKernelApi) -> None:
        self.api = api
        self.handles: list[int] = []
        self.expected_identities: dict[int, tuple[object, ...]] = {}

    @property
    def closed(self) -> bool:
        return not self.handles

    def retain(
        self,
        handle: int,
        metadata: WindowsHandleMetadata | None = None,
    ) -> int:
        deferred = _append_owned_windows_handle(self.handles, handle)
        if deferred is not None:
            raise deferred
        if metadata is not None:
            self.expected_identities[handle] = windows_handle_ownership_identity(
                metadata
            )
        return handle

    def close(self) -> None:
        failure = _close_windows_handles(
            self.api,
            self.handles,
            self.expected_identities,
        )
        if failure is not None:
            raise failure


def _install_cleanup_resource(slot: object | None, resource: object) -> None:
    if slot is None:
        return
    own = getattr(slot, "own", None)
    if not callable(own):
        raise TypeError("source cleanup slot must provide own()")
    own(resource)


def _attach_cleanup_resource(failure: BaseException, resource: object) -> None:
    try:
        if bool(resource.closed):  # type: ignore[attr-defined]
            return
    except BaseException:  # noqa: B036 - uncertain ownership stays reachable
        pass
    try:
        failure.source_cleanup_owner = resource  # type: ignore[attr-defined]
    except BaseException:  # noqa: B036 - local ownership remains authoritative
        pass


def _append_owned_windows_handle(
    handles: list[int],
    handle: int,
    deferred: BaseException | None = None,
) -> BaseException | None:
    while handle not in handles:
        try:
            handles.append(handle)
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise
            deferred = deferred or exc
    return deferred


def _open_owned_windows_handle(
    operation: Callable[[], int],
    handles: list[int],
) -> int:
    """Open and register a HANDLE at the first Python-owned opportunity.

    A raw integer can still be lost if arbitrary opcode injection lands between
    the native API return and Python's result store. Full coverage of that
    interpreter boundary requires a native helper returning an owning object.
    """

    handle = 0
    try:
        handle = operation()
        deferred = _append_owned_windows_handle(handles, handle)
        if deferred is not None:
            raise deferred
        return handle
    except BaseException:  # noqa: B036 - commit HANDLE ownership
        if handle:
            try:
                _append_owned_windows_handle(handles, handle)
            except BaseException:  # noqa: B036 - preserve primary failure
                pass
        raise


def open_lexical_directory_authority(
    path: str | Path,
    *,
    api: WindowsKernelApi | None = None,
    cleanup_slot: object | None = None,
) -> WindowsDirectoryAuthority:
    """Pin every component of an absolute lexical Windows directory path."""

    selected = windows_kernel_api() if api is None else api
    if api is None:
        windows_require_source_api(selected)
    raw = str(path)
    if not raw or "\x00" in raw:
        raise ValueError("Windows lexical directory path is invalid")
    normalized = ntpath.normpath(ntpath.abspath(raw))
    pure = PureWindowsPath(normalized)
    if not pure.is_absolute() or not pure.anchor:
        raise ValueError("Windows lexical directory path must be absolute")
    components = pure.parts[1:]
    if (
        len(components) > 256
        or len(normalized.encode("utf-8", errors="strict")) > 4_096
    ):
        raise ValueError("Windows lexical directory path exceeds its limits")
    for component in components:
        if (
            not component
            or component in {".", ".."}
            or "\\" in component
            or "/" in component
            or "\x00" in component
        ):
            raise ValueError("Windows lexical directory component is invalid")

    cleanup = _WindowsHandleCleanup(selected)
    _install_cleanup_resource(cleanup_slot, cleanup)
    handles = cleanup.handles
    handle_identities = cleanup.expected_identities
    observations: list[_WindowsAuthorityObservation] = []
    try:
        anchor_handle = _open_owned_windows_handle(
            lambda: selected.create_directory_handle(Path(pure.anchor)),
            handles,
        )
        anchor = selected.metadata(anchor_handle)
        if (
            not stat.S_ISDIR(anchor.st_mode)
            or anchor.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT
            or not anchor.st_dev
            or not windows_file_id_is_reliable(anchor.file_id_128)
            or anchor.delete_pending
        ):
            raise RuntimeError("Windows lexical anchor is not identity-bound")
        handle_identities[anchor_handle] = windows_handle_ownership_identity(anchor)
        current_handle = anchor_handle
        for component in components:
            parent_identity = windows_metadata_identity(
                selected.metadata(current_handle)
            )

            def open_component(
                current_handle: int = current_handle,
                component: str = component,
            ) -> int:
                return selected.open_relative(
                    current_handle,
                    component,
                    desired_access=(
                        FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE
                    ),
                    is_directory=True,
                    allow_reparse=False,
                )

            child_handle = _open_owned_windows_handle(
                open_component,
                handles,
            )
            child = selected.metadata(child_handle)
            if (
                not stat.S_ISDIR(child.st_mode)
                or child.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT
                or child.st_dev != anchor.st_dev
                or not windows_file_id_is_reliable(child.file_id_128)
                or child.delete_pending
            ):
                raise RuntimeError(
                    "Windows lexical directory component is not identity-bound"
                )
            handle_identities[child_handle] = windows_handle_ownership_identity(child)
            iterator = getattr(selected, "iter_directory", None)
            entries = (
                iterator(current_handle)
                if callable(iterator)
                else selected.enumerate_directory(current_handle)
            )
            matches = [entry for entry in entries if entry.name == component]
            if len(matches) != 1 or matches[0].file_id_128 != child.file_id_128:
                raise RuntimeError("Windows lexical directory changed while opening")
            observations.append(
                _WindowsAuthorityObservation(
                    parent_handle=current_handle,
                    parent_identity=parent_identity,
                    name=component,
                    child_handle=child_handle,
                    child_identity=windows_metadata_identity(child),
                    child_file_id=child.file_id_128,
                )
            )
            current_handle = child_handle
        authority = WindowsDirectoryAuthority(
            api=selected,
            path=Path(normalized),
            handles=handles,
            observations=observations,
            anchor_identity=windows_metadata_identity(anchor),
        )
        authority.verify()
        _install_cleanup_resource(cleanup_slot, authority)
        return authority
    except BaseException as primary:  # noqa: B036 - preserve acquisition failure
        cleanup_failure: BaseException | None = None
        try:
            cleanup.close()
        except BaseException as exc:  # noqa: B036 - retain explicit retry owner
            cleanup_failure = exc
        _attach_cleanup_resource(primary, cleanup_slot or cleanup)
        if cleanup_failure is not None:
            raise primary from cleanup_failure
        raise


_KERNEL_API: WindowsKernelApi | None = None


def windows_kernel_api() -> WindowsKernelApi:
    """Return the process-wide Win32 filesystem binding."""

    global _KERNEL_API
    if _KERNEL_API is None:
        try:
            _KERNEL_API = WindowsKernelApi()
        except (AttributeError, OSError) as exc:
            raise RuntimeError(
                "Windows FILE_ID HANDLE filesystem APIs are unavailable"
            ) from exc
    return _KERNEL_API


def windows_require_publication_api(api: WindowsKernelApi | None = None) -> None:
    """Fail closed unless the no-replace publication primitives are callable."""

    selected = windows_kernel_api() if api is None else api
    required = (
        "GetFileInformationByHandleEx",
        "OpenFileById",
        "SetFileInformationByHandle",
    )
    if any(not hasattr(selected.kernel32, name) for name in required):
        raise RuntimeError(
            "atomic directory publication requires Windows FILE_ID HANDLE APIs"
        )


def windows_require_source_api(api: WindowsKernelApi | None = None) -> None:
    """Fail closed unless the Windows source-authority primitives are callable."""

    selected = windows_kernel_api() if api is None else api
    kernel_required = (
        "DeviceIoControl",
        "GetFileInformationByHandleEx",
        "OpenFileById",
    )
    if any(not hasattr(selected.kernel32, name) for name in kernel_required) or not (
        hasattr(selected.ntdll, "NtCreateFile")
        and hasattr(selected.ntdll, "RtlNtStatusToDosError")
    ):
        raise RuntimeError(
            "secure source reads require Windows 128-bit FILE_ID, relative-open, "
            "and reparse-point APIs"
        )


__all__ = [
    "DELETE",
    "FILE_ATTRIBUTE_DIRECTORY",
    "FILE_ATTRIBUTE_READONLY",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "FILE_LIST_DIRECTORY",
    "FILE_READ_ATTRIBUTES",
    "FILE_READ_DATA",
    "IO_REPARSE_TAG_MOUNT_POINT",
    "IO_REPARSE_TAG_SYMLINK",
    "SYNCHRONIZE",
    "WindowsDirectoryAuthority",
    "WindowsDirectoryEntry",
    "WindowsFileId",
    "WindowsHandleMetadata",
    "WindowsKernelApi",
    "WindowsReparsePoint",
    "parse_windows_reparse_data",
    "windows_extended_path",
    "windows_file_id_is_reliable",
    "windows_handle_ownership_identity",
    "windows_kernel_api",
    "windows_metadata_identity",
    "windows_mode_from_attributes",
    "open_lexical_directory_authority",
    "windows_require_publication_api",
    "windows_require_source_api",
]
