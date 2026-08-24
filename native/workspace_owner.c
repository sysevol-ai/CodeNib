// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#ifdef __linux__
#include <sys/random.h>
#include <sys/syscall.h>
#endif

#ifndef O_CLOEXEC
#define O_CLOEXEC 0
#endif
#ifndef O_DIRECTORY
#define O_DIRECTORY 0
#endif
#ifndef O_NOFOLLOW
#define O_NOFOLLOW 0
#endif
#ifndef O_NONBLOCK
#define O_NONBLOCK 0
#endif
#ifndef AT_SYMLINK_NOFOLLOW
#define AT_SYMLINK_NOFOLLOW 0x100
#endif
#ifndef AT_FDCWD
#define AT_FDCWD -100
#endif
#ifndef RENAME_NOREPLACE
#define RENAME_NOREPLACE (1U << 0)
#endif
#ifndef RENAME_EXCHANGE
#define RENAME_EXCHANGE (1U << 1)
#endif
#ifndef KCMP_FILE
#define KCMP_FILE 0
#endif

#define CODENIB_MAX_PATH_BYTES 4096
#define CODENIB_MAX_ADOPTION_PATH_BYTES ((CODENIB_MAX_PATH_BYTES * 2) + 1)
#define CODENIB_MAX_COMPONENT_BYTES 255
#define CODENIB_MAX_DIRECTORIES 100000
#define CODENIB_MAX_COMPONENTS 256
#define CODENIB_MAX_SCRATCH_FDS ((CODENIB_MAX_COMPONENTS * 2) + 8)
#define CODENIB_ORPHAN_ATTEMPTS 64
#define CODENIB_MAX_FILE_WRITE_BYTES (8 * 1024 * 1024)
#define CODENIB_FD_SAFETY_MARGIN 64

enum workspace_namespace_state {
  WORKSPACE_EMPTY = 0,
  WORKSPACE_PROVISIONING = 1,
  WORKSPACE_PROVISIONED = 2,
  WORKSPACE_ADOPTED = 3,
  WORKSPACE_PUBLISHED_UNRECEIPTED = 4,
  WORKSPACE_RECEIPTED = 5,
  WORKSPACE_QUARANTINED = 6,
  WORKSPACE_DESTINATION_CAPTURED = 7,
  WORKSPACE_REPLACEMENT_PROVISIONING = 8,
  WORKSPACE_REPLACEMENT_PROVISIONED = 9,
  WORKSPACE_REPLACEMENT_ADOPTED = 10,
  WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED = 11,
  WORKSPACE_REPLACEMENT_RECEIPTED = 12,
  WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED = 13,
  WORKSPACE_DESTINATION_LEASED = 14,
};

enum workspace_receipt_kind {
  WORKSPACE_RECEIPT_PUBLISH = 1,
  WORKSPACE_RECEIPT_REPLACEMENT = 2,
};

enum workspace_replacement_mapping {
  WORKSPACE_REPLACEMENT_MAPPING_UNKNOWN = 0,
  WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE = 1,
  WORKSPACE_REPLACEMENT_MAPPING_EXCHANGED = 2,
};

typedef struct {
  char *path;
  int fd;
  int guard_fd;
  int exposed;
  mode_t mode;
  dev_t device;
  ino_t inode;
  int identity_known;
} WorkspaceDirectoryRecord;

typedef struct WorkspaceOwner {
  PyObject_HEAD pid_t owner_pid;
  uid_t allowed_root_euid;
  int allowed_root_policy_known;
  int closed;
  int provision_started;
  int destination_capture_started;
  int namespace_created;
  int namespace_sync_pending;
  enum workspace_namespace_state namespace_state;
  int anchor_fd;
  int anchor_guard_fd;
  int parent_fd;
  int parent_guard_fd;
  int parent_exposed;
  int replacement_lock_fd;
  int replacement_lock_guard_fd;
  int root_fd;
  int root_guard_fd;
  int root_exposed;
  int captured_destination_fd;
  int captured_destination_guard_fd;
  int captured_destination_exposed;
  dev_t anchor_device;
  ino_t anchor_inode;
  int anchor_identity_known;
  dev_t parent_device;
  ino_t parent_inode;
  int parent_identity_known;
  dev_t replacement_lock_device;
  ino_t replacement_lock_inode;
  int replacement_lock_identity_known;
  dev_t root_device;
  ino_t root_inode;
  int root_identity_known;
  dev_t captured_destination_device;
  ino_t captured_destination_inode;
  int captured_destination_identity_known;
  char *destination_name;
  char *destination_path;
  char *initial_stage_name;
  char *stage_name;
  char *replacement_slot_name;
  char *plan_digest;
  char *orphan_name;
  WorkspaceDirectoryRecord *directories;
  Py_ssize_t directory_count;
  WorkspaceDirectoryRecord scratch[CODENIB_MAX_SCRATCH_FDS];
  Py_ssize_t scratch_count;
  Py_ssize_t absolute_chain_count;
  Py_ssize_t relative_chain_start;
  Py_ssize_t relative_chain_count;
  int file_fd;
  int file_guard_fd;
  Py_ssize_t file_parent_index;
  char *file_name;
  dev_t file_device;
  ino_t file_inode;
  int file_identity_known;
  int file_active;
  int file_failed;
  off_t file_offset;
  int publish_permit_claimed;
  int publish_permit_active;
  int replacement_permit_claimed;
  int replacement_permit_active;
  int replacement_lock_active;
  uint64_t receipt_generation;
} WorkspaceOwner;

typedef struct {
  PyObject_HEAD WorkspaceOwner *owner;
  pid_t owner_pid;
  int consumed;
} WorkspacePublishPermit;

typedef struct {
  PyObject_HEAD WorkspaceOwner *owner;
  pid_t owner_pid;
  int consumed;
} WorkspaceReplacementPermit;

typedef struct {
  PyObject_HEAD WorkspaceOwner *owner;
  pid_t owner_pid;
  uint64_t generation;
  enum workspace_receipt_kind kind;
  int consumed;
} WorkspaceReceiptToken;

static PyObject *WorkspacePublishPermitTypeObject = NULL;
static PyObject *WorkspaceReplacementPermitTypeObject = NULL;
static PyObject *WorkspaceReceiptTokenTypeObject = NULL;

static PyObject *new_workspace_publish_permit(WorkspaceOwner *owner);
static PyObject *new_workspace_replacement_permit(WorkspaceOwner *owner);
static PyObject *new_workspace_receipt_token(WorkspaceOwner *owner,
                                             uint64_t generation,
                                             enum workspace_receipt_kind kind);

static int set_errno_error(int error) {
  errno = error;
  PyErr_SetFromErrno(PyExc_OSError);
  return -1;
}

static int require_linux(void) {
#ifdef __linux__
  return 0;
#else
  PyErr_SetString(PyExc_RuntimeError,
                  "native strict workspace provisioning requires Linux");
  return -1;
#endif
}

static int require_owner_pid(WorkspaceOwner *self) {
  if (getpid() == self->owner_pid) {
    return 0;
  }
  PyErr_SetString(PyExc_RuntimeError,
                  "native workspace owner cannot cross a PID boundary");
  return -1;
}

static int check_deadline(long long deadline_ns) {
  struct timespec now;
  long long current;

  if (deadline_ns <= 0) {
    return 0;
  }
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
    return set_errno_error(errno);
  }
  current = ((long long)now.tv_sec * 1000000000LL) + now.tv_nsec;
  if (current >= deadline_ns) {
    PyErr_SetString(PyExc_TimeoutError,
                    "native workspace provisioning deadline expired");
    return -1;
  }
  return 0;
}

static int verify_parent_binding(WorkspaceOwner *self);
static int verify_captured_destination_hidden_authority(WorkspaceOwner *self);
static int verify_captured_destination_local_authority(WorkspaceOwner *self);
static int verify_captured_destination_authority(WorkspaceOwner *self);
static int verify_captured_destination_binding(WorkspaceOwner *self);
static int verify_owner_authority(WorkspaceOwner *self);
static int verify_replacement_binding(WorkspaceOwner *self);
static int verify_replacement_lock_authority(WorkspaceOwner *self);
static int verify_descriptor(int descriptor, int guard, dev_t device,
                             ino_t inode);
static int verify_hidden_directory(int descriptor, dev_t device, ino_t inode);
static int quarantine_namespace_internal(WorkspaceOwner *self);
static int settle_replacement_namespace_internal(WorkspaceOwner *self);
static int workspace_state_is_replacement(WorkspaceOwner *self);

static int native_fsync_retry(int descriptor) {
  int attempts;
  for (attempts = 0; attempts < 64; ++attempts) {
    if (fsync(descriptor) == 0) {
      return 0;
    }
    if (errno != EINTR) {
      return -1;
    }
  }
  errno = EINTR;
  return -1;
}

static int native_flock_retry(int descriptor, int operation) {
#if defined(__linux__)
  int attempts;
  for (attempts = 0; attempts < 64; ++attempts) {
    if (flock(descriptor, operation) == 0) {
      return 0;
    }
    if (errno != EINTR) {
      return -1;
    }
  }
  errno = EINTR;
  return -1;
#else
  (void)descriptor;
  (void)operation;
  errno = ENOSYS;
  return -1;
#endif
}

static void enter_replacement_recovery(WorkspaceOwner *self) {
  self->namespace_state = WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED;
  if (self->replacement_lock_active) {
    self->namespace_sync_pending = 1;
  }
}

static int release_replacement_lock(WorkspaceOwner *self) {
  if (!self->replacement_lock_active) {
    return 0;
  }
  if (getpid() != self->owner_pid) {
    errno = EPERM;
    return -1;
  }
  if (verify_replacement_lock_authority(self) < 0 ||
      native_flock_retry(self->replacement_lock_fd, LOCK_UN) < 0) {
    return -1;
  }
  self->replacement_lock_active = 0;
  return 0;
}

static int native_fchmod_retry(int descriptor, mode_t mode) {
  int attempts;
  for (attempts = 0; attempts < 64; ++attempts) {
    if (fchmod(descriptor, mode) == 0) {
      return 0;
    }
    if (errno != EINTR) {
      return -1;
    }
  }
  errno = EINTR;
  return -1;
}

static int native_open_file_retry(int parent, const char *name, mode_t mode) {
  int attempts;
  int descriptor;
  for (attempts = 0; attempts < 64; ++attempts) {
    descriptor =
        openat(parent, name,
               O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode);
    if (descriptor >= 0 || errno != EINTR) {
      return descriptor;
    }
  }
  errno = EINTR;
  return -1;
}

static int native_fcntl_getfd_retry(int descriptor) {
  int attempts;
  int result;
  for (attempts = 0; attempts < 64; ++attempts) {
    result = fcntl(descriptor, F_GETFD);
    if (result >= 0 || errno != EINTR) {
      return result;
    }
  }
  errno = EINTR;
  return -1;
}

static int native_fcntl_getfl_retry(int descriptor) {
  int attempts;
  int result;
  for (attempts = 0; attempts < 64; ++attempts) {
    result = fcntl(descriptor, F_GETFL);
    if (result >= 0 || errno != EINTR) {
      return result;
    }
  }
  errno = EINTR;
  return -1;
}

static int native_fcntl_setfl_retry(int descriptor, int flags) {
  int attempts;
  for (attempts = 0; attempts < 64; ++attempts) {
    if (fcntl(descriptor, F_SETFL, flags) == 0) {
      return 0;
    }
    if (errno != EINTR) {
      return -1;
    }
  }
  errno = EINTR;
  return -1;
}

#ifndef F_DUPFD_CLOEXEC
static int native_fcntl_setfd_retry(int descriptor, int flags) {
  int attempts;
  for (attempts = 0; attempts < 64; ++attempts) {
    if (fcntl(descriptor, F_SETFD, flags) == 0) {
      return 0;
    }
    if (errno != EINTR) {
      return -1;
    }
  }
  errno = EINTR;
  return -1;
}
#endif

#ifdef F_DUPFD_CLOEXEC
static int native_fcntl_dupfd_cloexec_retry(int descriptor) {
  int attempts;
  int result;
  for (attempts = 0; attempts < 64; ++attempts) {
    result = fcntl(descriptor, F_DUPFD_CLOEXEC, 0);
    if (result >= 0 || errno != EINTR) {
      return result;
    }
  }
  errno = EINTR;
  return -1;
}
#endif

static int sync_parent(WorkspaceOwner *self) {
  if (!self->parent_identity_known ||
      verify_hidden_directory(self->parent_guard_fd, self->parent_device,
                              self->parent_inode) < 0) {
    return -1;
  }
  return native_fsync_retry(self->parent_guard_fd);
}

static int native_fstat_once(int descriptor, struct stat *metadata) {
#ifdef SYS_fstat
  return (int)syscall(SYS_fstat, descriptor, metadata);
#else
  (void)descriptor;
  (void)metadata;
  errno = ENOSYS;
  return -1;
#endif
}

static int native_fstat_retry(int descriptor, struct stat *metadata) {
  int attempts;
  for (attempts = 0; attempts < 64; ++attempts) {
    if (native_fstat_once(descriptor, metadata) == 0) {
      return 0;
    }
    if (errno != EINTR) {
      return -1;
    }
  }
  errno = EINTR;
  return -1;
}

static int native_fstatat(int parent, const char *name, struct stat *metadata,
                          int flags) {
#ifdef SYS_newfstatat
  return (int)syscall(SYS_newfstatat, parent, name, metadata, flags);
#else
  (void)parent;
  (void)name;
  (void)metadata;
  (void)flags;
  errno = ENOSYS;
  return -1;
#endif
}

static int native_fstatat_retry(int parent, const char *name,
                                struct stat *metadata, int flags) {
  int attempts;
  for (attempts = 0; attempts < 64; ++attempts) {
    if (native_fstatat(parent, name, metadata, flags) == 0) {
      return 0;
    }
    if (errno != EINTR) {
      return -1;
    }
  }
  errno = EINTR;
  return -1;
}

enum ofd_comparison {
  OFD_COMPARISON_ERROR = -1,
  OFD_DIFFERENT = 0,
  OFD_SAME = 1,
};

static int compare_open_file_description(int descriptor, int guard) {
#if defined(__linux__) && defined(SYS_kcmp)
  long result;
  int attempts;
  if (descriptor < 0 || guard < 0) {
    errno = EBADF;
    return OFD_COMPARISON_ERROR;
  }
  for (attempts = 0; attempts < 64; ++attempts) {
    result =
        syscall(SYS_kcmp, getpid(), getpid(), KCMP_FILE, descriptor, guard);
    if (result == 0) {
      return OFD_SAME;
    }
    if (result > 0) {
      return OFD_DIFFERENT;
    }
    if (errno != EINTR) {
      return OFD_COMPARISON_ERROR;
    }
  }
  errno = EINTR;
  return OFD_COMPARISON_ERROR;
#else
  (void)descriptor;
  (void)guard;
  errno = ENOSYS;
  return OFD_COMPARISON_ERROR;
#endif
}

static int compare_open_file_description_by_status_flags(int descriptor,
                                                         int guard) {
  int descriptor_initial;
  int guard_initial;
  int toggled_flags;
  int descriptor_toggled;
  int guard_toggled;
  int descriptor_restored;
  int guard_restored;
  int comparison = OFD_COMPARISON_ERROR;
  int error = 0;
  int guard_changed = 0;

  descriptor_initial = native_fcntl_getfl_retry(descriptor);
  guard_initial = native_fcntl_getfl_retry(guard);
  if (descriptor_initial < 0 || guard_initial < 0) {
    return OFD_COMPARISON_ERROR;
  }
  if (descriptor_initial != guard_initial) {
    return OFD_DIFFERENT;
  }

  /* File status flags belong to the open file description.  Toggling the
     otherwise inert O_NONBLOCK bit on the hidden directory guard gives
     cleanup a callback-free OFD challenge when a later seccomp policy denies
     kcmp.  A same-inode reopen has independent status flags and cannot pass.
     The GIL remains held throughout the challenge and the original flags are
     restored and re-read before the result is trusted. */
  toggled_flags = guard_initial ^ O_NONBLOCK;
  if (native_fcntl_setfl_retry(guard, toggled_flags) < 0) {
    return OFD_COMPARISON_ERROR;
  }
  guard_changed = 1;
  guard_toggled = native_fcntl_getfl_retry(guard);
  descriptor_toggled = native_fcntl_getfl_retry(descriptor);
  if (guard_toggled < 0 || descriptor_toggled < 0) {
    error = errno == 0 ? EIO : errno;
    goto restore;
  }
  if (guard_toggled != toggled_flags) {
    error = EOPNOTSUPP;
    goto restore;
  }
  comparison = descriptor_toggled == guard_toggled ? OFD_SAME : OFD_DIFFERENT;

restore:
  if (guard_changed && native_fcntl_setfl_retry(guard, guard_initial) < 0) {
    error = errno == 0 ? EIO : errno;
    comparison = OFD_COMPARISON_ERROR;
  }
  guard_restored = native_fcntl_getfl_retry(guard);
  descriptor_restored = native_fcntl_getfl_retry(descriptor);
  if (guard_restored != guard_initial || descriptor_restored < 0 ||
      (comparison == OFD_SAME && descriptor_restored != descriptor_initial)) {
    if (error == 0) {
      error = errno == 0 ? ESTALE : errno;
    }
    comparison = OFD_COMPARISON_ERROR;
  }
  if (comparison == OFD_COMPARISON_ERROR) {
    errno = error == 0 ? EIO : error;
  }
  return comparison;
}

static int compare_open_file_description_for_cleanup(int descriptor,
                                                     int guard) {
  int comparison = compare_open_file_description(descriptor, guard);
  int kcmp_error;

  if (comparison != OFD_COMPARISON_ERROR) {
    return comparison;
  }
  kcmp_error = errno == 0 ? EIO : errno;
  comparison = compare_open_file_description_by_status_flags(descriptor, guard);
  if (comparison == OFD_COMPARISON_ERROR && errno == 0) {
    errno = kcmp_error;
  }
  return comparison;
}

static int duplicate_cloexec(int descriptor);

static int raw_close_terminal(int descriptor) {
#if defined(__linux__) && defined(SYS_close)
  int result;
  if (descriptor < 0) {
    return 0;
  }
  result = (int)syscall(SYS_close, descriptor);
  return result == 0 ? 0 : (errno == 0 ? EIO : errno);
#else
  (void)descriptor;
  return ENOSYS;
#endif
}

static void discard_uncommitted_pair(int *descriptor_slot, int *guard_slot,
                                     int error) {
  if (*guard_slot >= 0) {
    (void)raw_close_terminal(*guard_slot);
    *guard_slot = -1;
  }
  if (*descriptor_slot >= 0) {
    (void)raw_close_terminal(*descriptor_slot);
    *descriptor_slot = -1;
  }
  errno = error == 0 ? EIO : error;
}

static int close_inflight_file(WorkspaceOwner *self) {
  int first_error = 0;
  int error;

  if (self->file_fd >= 0) {
    first_error = raw_close_terminal(self->file_fd);
    self->file_fd = -1;
  }
  if (self->file_guard_fd >= 0) {
    error = raw_close_terminal(self->file_guard_fd);
    self->file_guard_fd = -1;
    if (first_error == 0) {
      first_error = error;
    }
  }
  self->file_active = 0;
  self->file_identity_known = 0;
  self->file_parent_index = -2;
  self->file_offset = 0;
  free(self->file_name);
  self->file_name = NULL;
  return first_error;
}

static int probe_kcmp_support(void) {
#if defined(__linux__) && defined(SYS_kcmp) && defined(SYS_close)
  int original = -1;
  int duplicate = -1;
  int independent = -1;
  int error = 0;
  int same_result;
  int independent_result;

  original =
      open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
  if (original < 0) {
    return -1;
  }
  duplicate = duplicate_cloexec(original);
  independent =
      open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
  if (duplicate < 0 || independent < 0) {
    error = errno == 0 ? EIO : errno;
    goto done;
  }
  same_result = compare_open_file_description(original, duplicate);
  if (same_result != OFD_SAME) {
    error = same_result == OFD_COMPARISON_ERROR ? (errno == 0 ? EPERM : errno)
                                                : ESTALE;
    goto done;
  }
  independent_result = compare_open_file_description(original, independent);
  if (independent_result != OFD_DIFFERENT) {
    error = independent_result == OFD_COMPARISON_ERROR
                ? (errno == 0 ? EPERM : errno)
                : ESTALE;
    goto done;
  }

done:
  if (independent >= 0 && raw_close_terminal(independent) != 0 && error == 0) {
    error = EIO;
  }
  if (duplicate >= 0 && raw_close_terminal(duplicate) != 0 && error == 0) {
    error = EIO;
  }
  if (raw_close_terminal(original) != 0 && error == 0) {
    error = EIO;
  }
  if (error != 0) {
    errno = error;
    return -1;
  }
  return 0;
#else
  errno = ENOSYS;
  return -1;
#endif
}

static int close_one(WorkspaceOwner *self, int *slot, int *guard_slot,
                     dev_t *expected_device, ino_t *expected_inode,
                     int *identity_known, int exposed) {
  int fd = *slot;
  int guard = *guard_slot;
  int public_error = 0;
  int guard_error = 0;
  int verification_error = 0;
  struct stat metadata;

  (void)self;

  if (fd < 0) {
    if (guard >= 0) {
      guard_error = raw_close_terminal(guard);
      *guard_slot = -1;
    }
    return guard_error;
  }
  if (guard >= 0 && exposed) {
    int comparison = compare_open_file_description_for_cleanup(fd, guard);
    if (comparison == OFD_COMPARISON_ERROR) {
      int comparison_error = errno == 0 ? EIO : errno;
      int guard_flags;
      if (comparison_error == EBADF &&
          native_fstat_retry(guard, &metadata) == 0 &&
          S_ISDIR(metadata.st_mode) && metadata.st_dev == *expected_device &&
          metadata.st_ino == *expected_inode &&
          (guard_flags = native_fcntl_getfd_retry(guard)) >= 0 &&
          (guard_flags & FD_CLOEXEC) != 0) {
        /* The guarded original remains exact and the public number is gone. */
        *slot = -1;
        guard_error = raw_close_terminal(guard);
        *guard_slot = -1;
        return guard_error != 0 ? guard_error : ESTALE;
      }
      /* A comparison error proves neither ownership nor replacement.  Keep
         the complete pair so an explicit cleanup retry can authenticate it;
         dropping the guard here would make the exposed number permanently
         uncloseable.  The kcmp-denied case normally resolves through the
         status-flag challenge before reaching this branch. */
      return comparison_error;
    }
    if (comparison == OFD_DIFFERENT) {
      /* The caller-visible number no longer denotes our OFD.  Revoke it without
         touching the foreign replacement, then close only the hidden original.
       */
      *slot = -1;
      guard_error = raw_close_terminal(guard);
      *guard_slot = -1;
      return guard_error != 0 ? guard_error : ESTALE;
    }
  }
  if (guard < 0 && exposed) {
    /* A guardless exposed number cannot be authenticated safely.  All partial
       acquisition paths close their unexposed number immediately, so this is
       only reachable after a failed cleanup comparison retained the number. */
    return ESTALE;
  }
  if (!*identity_known) {
    if (native_fstat_retry(fd, &metadata) < 0) {
      verification_error = errno == 0 ? EIO : errno;
    } else if (!S_ISDIR(metadata.st_mode) || metadata.st_dev == 0 ||
               metadata.st_ino == 0) {
      verification_error = ESTALE;
    } else {
      *expected_device = metadata.st_dev;
      *expected_inode = metadata.st_ino;
      *identity_known = 1;
    }
  } else if (native_fstat_retry(fd, &metadata) < 0 ||
             !S_ISDIR(metadata.st_mode) ||
             metadata.st_dev != *expected_device ||
             metadata.st_ino != *expected_inode) {
    verification_error = errno == 0 ? ESTALE : errno;
  }

  /* Either the number was never exposed or its OFD matched the hidden guard.
     It is safe to close both even when the final metadata attestation reports
     corruption; retaining an authenticated pair on that error would leak it. */
  public_error = raw_close_terminal(fd);
  *slot = -1;
  if (guard >= 0) {
    guard_error = raw_close_terminal(guard);
    *guard_slot = -1;
  }
  if (verification_error != 0) {
    return verification_error;
  }
  if (public_error != 0) {
    return public_error;
  }
  if (guard_error != 0) {
    return guard_error;
  }
  /* Linux releases the descriptor number before reporting late close errors.
     Raw close is therefore terminal and is never retried through a reused
     integer slot. */
  return 0;
}

static int close_inherited_descriptors(WorkspaceOwner *self) {
  Py_ssize_t index;
  int first_error = close_inflight_file(self);
  int error;

  /* A child must not mutate or sync the inherited namespace.  Public
     descriptor numbers can nevertheless have been closed and reused in the
     child before this call, so authenticate exposed pairs exactly as normal
     cleanup does rather than raw-closing a possible foreign replacement. */
  for (index = self->directory_count; index > 0; --index) {
    error = close_one(self, &self->directories[index - 1].fd,
                      &self->directories[index - 1].guard_fd,
                      &self->directories[index - 1].device,
                      &self->directories[index - 1].inode,
                      &self->directories[index - 1].identity_known,
                      self->directories[index - 1].exposed);
    if (first_error == 0 && error != 0) {
      first_error = error;
    }
  }
  error = close_one(self, &self->captured_destination_fd,
                    &self->captured_destination_guard_fd,
                    &self->captured_destination_device,
                    &self->captured_destination_inode,
                    &self->captured_destination_identity_known,
                    self->captured_destination_exposed);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(self, &self->root_fd, &self->root_guard_fd,
                    &self->root_device, &self->root_inode,
                    &self->root_identity_known, self->root_exposed);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(
      self, &self->replacement_lock_fd, &self->replacement_lock_guard_fd,
      &self->replacement_lock_device, &self->replacement_lock_inode,
      &self->replacement_lock_identity_known, 0);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(self, &self->parent_fd, &self->parent_guard_fd,
                    &self->parent_device, &self->parent_inode,
                    &self->parent_identity_known, self->parent_exposed);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(self, &self->anchor_fd, &self->anchor_guard_fd,
                    &self->anchor_device, &self->anchor_inode,
                    &self->anchor_identity_known, 0);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  for (index = self->scratch_count; index > 0; --index) {
    error = close_one(
        self, &self->scratch[index - 1].fd, &self->scratch[index - 1].guard_fd,
        &self->scratch[index - 1].device, &self->scratch[index - 1].inode,
        &self->scratch[index - 1].identity_known, 0);
    if (first_error == 0 && error != 0) {
      first_error = error;
    }
  }
  self->closed = self->captured_destination_fd < 0 &&
                 self->captured_destination_guard_fd < 0 && self->root_fd < 0 &&
                 self->root_guard_fd < 0 && self->replacement_lock_fd < 0 &&
                 self->replacement_lock_guard_fd < 0 && self->parent_fd < 0 &&
                 self->parent_guard_fd < 0 && self->anchor_fd < 0 &&
                 self->anchor_guard_fd < 0;
  for (index = 0; index < self->directory_count; ++index) {
    if (self->directories[index].fd >= 0 ||
        self->directories[index].guard_fd >= 0) {
      self->closed = 0;
    }
  }
  for (index = 0; index < self->scratch_count; ++index) {
    if (self->scratch[index].fd >= 0 || self->scratch[index].guard_fd >= 0) {
      self->closed = 0;
    }
  }
  return first_error;
}

static int close_all(WorkspaceOwner *self) {
  Py_ssize_t index;
  int first_error = 0;
  int error;

  if (getpid() != self->owner_pid) {
    return close_inherited_descriptors(self);
  }
  error = close_inflight_file(self);
  if (error != 0) {
    first_error = error;
  }
  if (self->namespace_sync_pending) {
    if (self->parent_fd < 0 || sync_parent(self) < 0) {
      if (first_error == 0) {
        first_error = errno == 0 ? EIO : errno;
      }
    } else {
      self->namespace_sync_pending = 0;
    }
  }

  for (index = self->directory_count; index > 0; --index) {
    error = close_one(self, &self->directories[index - 1].fd,
                      &self->directories[index - 1].guard_fd,
                      &self->directories[index - 1].device,
                      &self->directories[index - 1].inode,
                      &self->directories[index - 1].identity_known,
                      self->directories[index - 1].exposed);
    if (first_error == 0 && error != 0) {
      first_error = error;
    }
  }
  error = close_one(self, &self->captured_destination_fd,
                    &self->captured_destination_guard_fd,
                    &self->captured_destination_device,
                    &self->captured_destination_inode,
                    &self->captured_destination_identity_known,
                    self->captured_destination_exposed);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(self, &self->root_fd, &self->root_guard_fd,
                    &self->root_device, &self->root_inode,
                    &self->root_identity_known, self->root_exposed);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(
      self, &self->replacement_lock_fd, &self->replacement_lock_guard_fd,
      &self->replacement_lock_device, &self->replacement_lock_inode,
      &self->replacement_lock_identity_known, 0);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(self, &self->parent_fd, &self->parent_guard_fd,
                    &self->parent_device, &self->parent_inode,
                    &self->parent_identity_known, self->parent_exposed);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  error = close_one(self, &self->anchor_fd, &self->anchor_guard_fd,
                    &self->anchor_device, &self->anchor_inode,
                    &self->anchor_identity_known, 0);
  if (first_error == 0 && error != 0) {
    first_error = error;
  }
  for (index = self->scratch_count; index > 0; --index) {
    error = close_one(
        self, &self->scratch[index - 1].fd, &self->scratch[index - 1].guard_fd,
        &self->scratch[index - 1].device, &self->scratch[index - 1].inode,
        &self->scratch[index - 1].identity_known, 0);
    if (first_error == 0 && error != 0) {
      first_error = error;
    }
  }
  self->closed = self->captured_destination_fd < 0 &&
                 self->captured_destination_guard_fd < 0 && self->root_fd < 0 &&
                 self->root_guard_fd < 0 && self->replacement_lock_fd < 0 &&
                 self->replacement_lock_guard_fd < 0 && self->parent_fd < 0 &&
                 self->parent_guard_fd < 0 && self->anchor_fd < 0 &&
                 self->anchor_guard_fd < 0;
  for (index = 0; index < self->directory_count; ++index) {
    if (self->directories[index].fd >= 0 ||
        self->directories[index].guard_fd >= 0) {
      self->closed = 0;
    }
  }
  for (index = 0; index < self->scratch_count; ++index) {
    if (self->scratch[index].fd >= 0 || self->scratch[index].guard_fd >= 0) {
      self->closed = 0;
    }
  }
  return first_error;
}

static void free_configuration(WorkspaceOwner *self) {
  Py_ssize_t index;

  for (index = 0; index < self->directory_count; ++index) {
    free(self->directories[index].path);
    self->directories[index].path = NULL;
  }
  free(self->directories);
  self->directories = NULL;
  self->directory_count = 0;
  for (index = 0; index < self->scratch_count; ++index) {
    free(self->scratch[index].path);
    self->scratch[index].path = NULL;
  }
  free(self->destination_name);
  self->destination_name = NULL;
  free(self->destination_path);
  self->destination_path = NULL;
  free(self->initial_stage_name);
  self->initial_stage_name = NULL;
  free(self->stage_name);
  self->stage_name = NULL;
  free(self->replacement_slot_name);
  self->replacement_slot_name = NULL;
  free(self->plan_digest);
  self->plan_digest = NULL;
  free(self->orphan_name);
  self->orphan_name = NULL;
  free(self->file_name);
  self->file_name = NULL;
}

static void free_unprovisioned_replacement_configuration(WorkspaceOwner *self) {
  Py_ssize_t index;

  for (index = 0; index < self->directory_count; ++index) {
    free(self->directories[index].path);
    self->directories[index].path = NULL;
  }
  free(self->directories);
  self->directories = NULL;
  self->directory_count = 0;
  free(self->replacement_slot_name);
  self->replacement_slot_name = NULL;
  free(self->plan_digest);
  self->plan_digest = NULL;
}

static char *copy_exact_bytes(PyObject *value, const char *label,
                              Py_ssize_t max_bytes) {
  char *source;
  char *copy;
  Py_ssize_t size;

  if (!PyBytes_CheckExact(value)) {
    PyErr_Format(PyExc_TypeError, "%s must be exact bytes", label);
    return NULL;
  }
  if (PyBytes_AsStringAndSize(value, &source, &size) < 0) {
    return NULL;
  }
  if (size <= 0 || size > max_bytes ||
      memchr(source, '\0', (size_t)size) != NULL) {
    PyErr_Format(PyExc_ValueError, "%s is empty, unbounded, or contains NUL",
                 label);
    return NULL;
  }
  copy = malloc((size_t)size + 1);
  if (copy == NULL) {
    PyErr_NoMemory();
    return NULL;
  }
  memcpy(copy, source, (size_t)size);
  copy[size] = '\0';
  return copy;
}

static int valid_component(const char *value) {
  size_t length = strlen(value);
  return length > 0 && length <= CODENIB_MAX_COMPONENT_BYTES &&
         strcmp(value, ".") != 0 && strcmp(value, "..") != 0 &&
         strchr(value, '/') == NULL;
}

static int valid_relative_path(const char *value) {
  const char *start = value;
  const char *cursor = value;
  size_t component_length;
  size_t components = 0;

  if (*value == '\0' || *value == '/' ||
      strlen(value) > CODENIB_MAX_PATH_BYTES) {
    return 0;
  }
  for (;;) {
    if (*cursor == '/' || *cursor == '\0') {
      ++components;
      component_length = (size_t)(cursor - start);
      if (components > CODENIB_MAX_COMPONENTS || component_length == 0 ||
          component_length > CODENIB_MAX_COMPONENT_BYTES ||
          (component_length == 1 && start[0] == '.') ||
          (component_length == 2 && start[0] == '.' && start[1] == '.')) {
        return 0;
      }
      if (*cursor == '\0') {
        return 1;
      }
      start = cursor + 1;
    }
    ++cursor;
  }
}

static char *join_destination_path(const char *allowed_root,
                                   const char *relative) {
  size_t root_length = strlen(allowed_root);
  size_t relative_length = strlen(relative);
  int needs_separator =
      root_length == 0 || allowed_root[root_length - 1] != '/';
  size_t total = root_length + (size_t)needs_separator + relative_length;
  char *joined;

  if (total == 0 || total > CODENIB_MAX_ADOPTION_PATH_BYTES) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace absolute destination is unbounded");
    return NULL;
  }
  joined = malloc(total + 1);
  if (joined == NULL) {
    PyErr_NoMemory();
    return NULL;
  }
  memcpy(joined, allowed_root, root_length);
  if (needs_separator) {
    joined[root_length] = '/';
  }
  memcpy(joined + root_length + (size_t)needs_separator, relative,
         relative_length);
  joined[total] = '\0';
  return joined;
}

static int open_directory_at(int parent, const char *name) {
  return openat(parent, name,
                O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
}

static int duplicate_cloexec(int descriptor) {
#ifdef F_DUPFD_CLOEXEC
  return native_fcntl_dupfd_cloexec_retry(descriptor);
#else
  int duplicate = dup(descriptor);
  if (duplicate >= 0 && native_fcntl_setfd_retry(duplicate, FD_CLOEXEC) < 0) {
    int error = errno;
    close(duplicate);
    errno = error;
    return -1;
  }
  return duplicate;
#endif
}

static int record_identity(int descriptor, dev_t *device, ino_t *inode,
                           mode_t expected_type);

static int retain_scratch_descriptor(WorkspaceOwner *self, int descriptor,
                                     const char *component) {
  WorkspaceDirectoryRecord *record;
  if (self->scratch_count >= CODENIB_MAX_SCRATCH_FDS) {
    errno = EMFILE;
    return -1;
  }
  record = &self->scratch[self->scratch_count++];
  record->path = NULL;
  record->fd = descriptor;
  record->guard_fd = -1;
  record->exposed = 0;
  record->identity_known = 0;
  if (component != NULL) {
    record->path = strdup(component);
    if (record->path == NULL) {
      discard_uncommitted_pair(&record->fd, &record->guard_fd, ENOMEM);
      return -1;
    }
  }
  record->guard_fd = duplicate_cloexec(descriptor);
  if (record->guard_fd < 0) {
    int error = errno;
    discard_uncommitted_pair(&record->fd, &record->guard_fd, error);
    return -1;
  }
  if (record_identity(descriptor, &record->device, &record->inode, S_IFDIR) <
      0) {
    int error = errno;
    discard_uncommitted_pair(&record->fd, &record->guard_fd, error);
    return -1;
  }
  record->identity_known = 1;
  return 0;
}

static int transfer_scratch_descriptor(WorkspaceOwner *self, int descriptor,
                                       int *guard, dev_t *device,
                                       ino_t *inode) {
  Py_ssize_t index;
  for (index = self->scratch_count; index > 0; --index) {
    if (self->scratch[index - 1].fd == descriptor) {
      if (!self->scratch[index - 1].identity_known) {
        errno = ESTALE;
        return -1;
      }
      *device = self->scratch[index - 1].device;
      *inode = self->scratch[index - 1].inode;
      *guard = self->scratch[index - 1].guard_fd;
      self->scratch[index - 1].fd = -1;
      self->scratch[index - 1].guard_fd = -1;
      return descriptor;
    }
  }
  errno = EBADF;
  return -1;
}

static int open_absolute_directory(WorkspaceOwner *self, const char *path) {
  int current = -1;
  int child = -1;
  const char *start;
  const char *cursor;
  char component[CODENIB_MAX_COMPONENT_BYTES + 1];
  size_t length;
  size_t components = 0;

  if (path[0] != '/' || strlen(path) > CODENIB_MAX_PATH_BYTES) {
    errno = EINVAL;
    return -1;
  }
  for (cursor = path + 1; *cursor != '\0'; ++cursor) {
    if (*cursor == '/') {
      ++components;
    }
  }
  if (path[1] != '\0' && ++components > CODENIB_MAX_COMPONENTS) {
    errno = EINVAL;
    return -1;
  }
  current =
      open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
  if (current < 0) {
    return -1;
  }
  if (retain_scratch_descriptor(self, current, "/") < 0) {
    return -1;
  }
  start = path + 1;
  if (*start == '\0') {
    return current;
  }
  for (cursor = start;; ++cursor) {
    if (*cursor != '/' && *cursor != '\0') {
      continue;
    }
    length = (size_t)(cursor - start);
    if (length == 0 || length > CODENIB_MAX_COMPONENT_BYTES) {
      errno = EINVAL;
      return -1;
    }
    memcpy(component, start, length);
    component[length] = '\0';
    if (!valid_component(component)) {
      errno = EINVAL;
      return -1;
    }
    child = open_directory_at(current, component);
    if (child < 0) {
      return -1;
    }
    if (retain_scratch_descriptor(self, child, component) < 0) {
      return -1;
    }
    current = child;
    if (*cursor == '\0') {
      return current;
    }
    start = cursor + 1;
  }
}

static int split_destination(const char *relative, char **parent_path,
                             char **name) {
  const char *slash = strrchr(relative, '/');
  size_t parent_length;

  if (!valid_relative_path(relative)) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace destination must be normalized relative POSIX");
    return -1;
  }
  if (slash == NULL) {
    *parent_path = strdup("");
    *name = strdup(relative);
  } else {
    parent_length = (size_t)(slash - relative);
    *parent_path = malloc(parent_length + 1);
    *name = strdup(slash + 1);
    if (*parent_path != NULL) {
      memcpy(*parent_path, relative, parent_length);
      (*parent_path)[parent_length] = '\0';
    }
  }
  if (*parent_path == NULL || *name == NULL) {
    free(*parent_path);
    free(*name);
    *parent_path = NULL;
    *name = NULL;
    PyErr_NoMemory();
    return -1;
  }
  return 0;
}

static int walk_relative_directory(WorkspaceOwner *self, int anchor,
                                   const char *relative) {
  int current = duplicate_cloexec(anchor);
  int child;
  char *copy = NULL;
  char *cursor;
  char *slash;

  if (current < 0) {
    return -1;
  }
  if (retain_scratch_descriptor(self, current, NULL) < 0) {
    return -1;
  }
  if (relative[0] == '\0') {
    return current;
  }
  copy = strdup(relative);
  if (copy == NULL) {
    errno = ENOMEM;
    return -1;
  }
  cursor = copy;
  for (;;) {
    slash = strchr(cursor, '/');
    if (slash != NULL) {
      *slash = '\0';
    }
    child = open_directory_at(current, cursor);
    if (child < 0) {
      free(copy);
      return -1;
    }
    if (retain_scratch_descriptor(self, child, cursor) < 0) {
      free(copy);
      return -1;
    }
    current = child;
    if (slash == NULL) {
      break;
    }
    cursor = slash + 1;
  }
  free(copy);
  return current;
}

static int record_identity(int descriptor, dev_t *device, ino_t *inode,
                           mode_t expected_type) {
  struct stat metadata;
  if (native_fstat_retry(descriptor, &metadata) < 0) {
    return -1;
  }
  if ((metadata.st_mode & S_IFMT) != expected_type || metadata.st_dev == 0 ||
      metadata.st_ino == 0) {
    errno = EINVAL;
    return -1;
  }
  *device = metadata.st_dev;
  *inode = metadata.st_ino;
  return 0;
}

static int same_identity_at(int parent, const char *name, dev_t device,
                            ino_t inode) {
  struct stat metadata;
  if (native_fstatat_retry(parent, name, &metadata, AT_SYMLINK_NOFOLLOW) < 0) {
    return -1;
  }
  if (!S_ISDIR(metadata.st_mode) || metadata.st_dev != device ||
      metadata.st_ino != inode) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static const char *candidate_namespace_name(WorkspaceOwner *self) {
  if (self->replacement_slot_name != NULL) {
    return self->replacement_slot_name;
  }
  return self->stage_name;
}

static int identify_created_root(WorkspaceOwner *self) {
  struct stat metadata;
  const char *name = candidate_namespace_name(self);
  if (!self->namespace_created || self->parent_guard_fd < 0 || name == NULL) {
    errno = EINVAL;
    return -1;
  }
  if (native_fstatat_retry(self->parent_guard_fd, name, &metadata,
                           AT_SYMLINK_NOFOLLOW) < 0) {
    return -1;
  }
  if (!S_ISDIR(metadata.st_mode) || metadata.st_dev == 0 ||
      metadata.st_ino == 0 ||
      (self->root_device != 0 && self->root_device != metadata.st_dev) ||
      (self->root_inode != 0 && self->root_inode != metadata.st_ino)) {
    errno = ESTALE;
    return -1;
  }
  self->root_device = metadata.st_dev;
  self->root_inode = metadata.st_ino;
  return 0;
}

static int compare_path_to_prefix(const char *candidate, const char *path,
                                  size_t path_length) {
  size_t candidate_length = strlen(candidate);
  size_t shared =
      candidate_length < path_length ? candidate_length : path_length;
  int comparison = memcmp(candidate, path, shared);

  if (comparison != 0) {
    return comparison;
  }
  if (candidate_length < path_length) {
    return -1;
  }
  if (candidate_length > path_length) {
    return 1;
  }
  return 0;
}

static Py_ssize_t find_directory_index(WorkspaceOwner *self, const char *path,
                                       size_t path_length,
                                       Py_ssize_t upper_bound) {
  Py_ssize_t lower = 0;
  Py_ssize_t upper = upper_bound;

  while (lower < upper) {
    Py_ssize_t middle = lower + ((upper - lower) / 2);
    int comparison = compare_path_to_prefix(self->directories[middle].path,
                                            path, path_length);
    if (comparison < 0) {
      lower = middle + 1;
    } else if (comparison > 0) {
      upper = middle;
    } else {
      return middle;
    }
  }
  return -1;
}

static int find_parent_descriptor(WorkspaceOwner *self, const char *path) {
  const char *slash = strrchr(path, '/');
  Py_ssize_t index;

  if (slash == NULL) {
    return self->root_guard_fd;
  }
  index = find_directory_index(self, path, (size_t)(slash - path),
                               self->directory_count);
  if (index >= 0) {
    return self->directories[index].guard_fd;
  }
  errno = EINVAL;
  return -1;
}

static const char *path_basename(const char *path) {
  const char *slash = strrchr(path, '/');
  return slash == NULL ? path : slash + 1;
}

static int parse_directories(WorkspaceOwner *self, PyObject *directories,
                             long long deadline_ns) {
  Py_ssize_t count;
  Py_ssize_t index;
  PyObject *item;
  PyObject *path_object;
  PyObject *mode_object;
  long mode;

  if (!PyTuple_CheckExact(directories)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace directories must be an exact tuple");
    return -1;
  }
  count = PyTuple_Size(directories);
  if (count < 0 || count > CODENIB_MAX_DIRECTORIES) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace directory count exceeds native owner budget");
    return -1;
  }
  if (count == 0) {
    return 0;
  }
  self->directories = calloc((size_t)count, sizeof(*self->directories));
  if (self->directories == NULL) {
    PyErr_NoMemory();
    return -1;
  }
  self->directory_count = count;
  for (index = 0; index < count; ++index) {
    if ((index & 63) == 0 && check_deadline(deadline_ns) < 0) {
      return -1;
    }
    self->directories[index].fd = -1;
    self->directories[index].guard_fd = -1;
    self->directories[index].exposed = 0;
  }
  for (index = 0; index < count; ++index) {
    item = PyTuple_GetItem(directories, index);
    if (item == NULL || !PyTuple_CheckExact(item) || PyTuple_Size(item) != 2) {
      PyErr_SetString(PyExc_TypeError,
                      "workspace directory records must be exact 2-tuples");
      return -1;
    }
    path_object = PyTuple_GetItem(item, 0);
    mode_object = PyTuple_GetItem(item, 1);
    self->directories[index].path = copy_exact_bytes(
        path_object, "workspace directory path", CODENIB_MAX_PATH_BYTES);
    if (self->directories[index].path == NULL) {
      return -1;
    }
    if (!valid_relative_path(self->directories[index].path)) {
      PyErr_SetString(
          PyExc_ValueError,
          "workspace directory path must be normalized relative POSIX");
      return -1;
    }
    if (!PyLong_CheckExact(mode_object)) {
      PyErr_SetString(PyExc_TypeError,
                      "workspace directory mode must be an exact integer");
      return -1;
    }
    mode = PyLong_AsLong(mode_object);
    if (mode == -1 && PyErr_Occurred()) {
      return -1;
    }
    if (mode < 0 || mode > 0777) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace directory mode is outside 0000..0777");
      return -1;
    }
    self->directories[index].mode = (mode_t)mode;
  }
  return 0;
}

static int validate_directory_plan(WorkspaceOwner *self,
                                   long long deadline_ns) {
  Py_ssize_t index;
  const char *path;
  const char *slash;

  for (index = 0; index < self->directory_count; ++index) {
    if ((index & 63) == 0 && check_deadline(deadline_ns) < 0) {
      return -1;
    }
    path = self->directories[index].path;
    if (index > 0) {
      int comparison = strcmp(self->directories[index - 1].path, path);
      if (comparison == 0) {
        PyErr_SetString(PyExc_ValueError,
                        "workspace directory plan contains a duplicate path");
        return -1;
      }
      if (comparison > 0) {
        PyErr_SetString(PyExc_ValueError,
                        "workspace directory plan is not in canonical order");
        return -1;
      }
    }
    slash = strrchr(path, '/');
    if (slash != NULL &&
        find_directory_index(self, path, (size_t)(slash - path), index) < 0) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace directory parent is absent from its plan");
      return -1;
    }
  }
  return 0;
}

static size_t relative_component_count(const char *path) {
  const char *cursor;
  size_t count = 0;

  if (*path == '\0') {
    return 0;
  }
  count = 1;
  for (cursor = path; *cursor != '\0'; ++cursor) {
    if (*cursor == '/') {
      ++count;
    }
  }
  return count;
}

static int count_open_descriptors(uintmax_t *count) {
  DIR *directory;
  struct dirent *entry;
  uintmax_t observed = 0;
  int scan_error = 0;

  directory = opendir("/proc/self/fd");
  if (directory == NULL) {
    return -1;
  }
  errno = 0;
  while ((entry = readdir(directory)) != NULL) {
    const char *cursor = entry->d_name;
    if (*cursor == '\0') {
      continue;
    }
    while (*cursor >= '0' && *cursor <= '9') {
      ++cursor;
    }
    if (*cursor == '\0') {
      ++observed;
    }
  }
  if (errno != 0) {
    scan_error = errno;
  }
  if (closedir(directory) < 0 && scan_error == 0) {
    scan_error = errno == 0 ? EIO : errno;
  }
  if (scan_error != 0) {
    errno = scan_error;
    return -1;
  }
  if (observed == 0) {
    errno = EIO;
    return -1;
  }
  /* The scan observes its own directory descriptor, which closedir released. */
  *count = observed - 1;
  return 0;
}

static int preflight_descriptor_budget(WorkspaceOwner *self,
                                       const char *allowed_root,
                                       const char *parent_path,
                                       size_t additional_owner_pairs) {
  struct rlimit limit;
  size_t absolute_records;
  size_t relative_records;
  size_t owner_pairs;
  size_t required;
  uintmax_t open_descriptors;

  absolute_records = 1 + relative_component_count(allowed_root + 1);
  relative_records = 1 + relative_component_count(parent_path);
  if (absolute_records > SIZE_MAX - relative_records - 1 ||
      absolute_records + relative_records + 1 >
          SIZE_MAX - (size_t)self->directory_count ||
      absolute_records + relative_records + 1 + (size_t)self->directory_count >
          SIZE_MAX - additional_owner_pairs) {
    PyErr_SetString(PyExc_OverflowError,
                    "workspace descriptor budget overflowed");
    return -1;
  }
  owner_pairs = absolute_records + relative_records + 1 +
                (size_t)self->directory_count + additional_owner_pairs;
  if (owner_pairs > (SIZE_MAX - 2 - CODENIB_FD_SAFETY_MARGIN) / (size_t)2) {
    PyErr_SetString(PyExc_OverflowError,
                    "workspace descriptor budget overflowed");
    return -1;
  }
  required = (owner_pairs * 2) + 2 + CODENIB_FD_SAFETY_MARGIN;
  if (getrlimit(RLIMIT_NOFILE, &limit) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    return -1;
  }
  if (count_open_descriptors(&open_descriptors) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    return -1;
  }
  if (limit.rlim_cur != RLIM_INFINITY &&
      (open_descriptors > (uintmax_t)limit.rlim_cur ||
       (uintmax_t)required > (uintmax_t)limit.rlim_cur - open_descriptors)) {
    errno = EMFILE;
    PyErr_SetFromErrno(PyExc_OSError);
    return -1;
  }
  return 0;
}

static int preflight_additional_descriptor_budget(size_t owner_pairs) {
  struct rlimit limit;
  size_t required;
  uintmax_t open_descriptors;

  if (owner_pairs > (SIZE_MAX - 2 - CODENIB_FD_SAFETY_MARGIN) / (size_t)2) {
    PyErr_SetString(PyExc_OverflowError,
                    "workspace descriptor budget overflowed");
    return -1;
  }
  required = (owner_pairs * 2) + 2 + CODENIB_FD_SAFETY_MARGIN;
  if (getrlimit(RLIMIT_NOFILE, &limit) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    return -1;
  }
  if (count_open_descriptors(&open_descriptors) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    return -1;
  }
  if (limit.rlim_cur != RLIM_INFINITY &&
      (open_descriptors > (uintmax_t)limit.rlim_cur ||
       (uintmax_t)required > (uintmax_t)limit.rlim_cur - open_descriptors)) {
    errno = EMFILE;
    PyErr_SetFromErrno(PyExc_OSError);
    return -1;
  }
  return 0;
}

static int verify_allowed_root_policy(WorkspaceOwner *self) {
  struct stat metadata;
  int flags;
  uid_t current_euid;

  if (!self->allowed_root_policy_known || !self->anchor_identity_known ||
      self->anchor_guard_fd < 0 ||
      native_fstat_retry(self->anchor_guard_fd, &metadata) < 0) {
    return -1;
  }
  flags = native_fcntl_getfd_retry(self->anchor_guard_fd);
  current_euid = geteuid();
  if (!S_ISDIR(metadata.st_mode) || metadata.st_dev != self->anchor_device ||
      metadata.st_ino != self->anchor_inode || flags < 0 ||
      (flags & FD_CLOEXEC) == 0) {
    errno = ESTALE;
    return -1;
  }
  if (current_euid != self->allowed_root_euid ||
      metadata.st_uid != current_euid ||
      (metadata.st_mode & (mode_t)07777) != (mode_t)0700) {
    errno = EPERM;
    return -1;
  }
  return 0;
}

static PyObject *WorkspaceOwner_new(PyTypeObject *type, PyObject *args,
                                    PyObject *keywords) {
  allocfunc allocate;
  WorkspaceOwner *self;

  if (PyTuple_Size(args) != 0 ||
      (keywords != NULL && PyDict_Size(keywords) != 0)) {
    PyErr_SetString(PyExc_TypeError, "WorkspaceOwner accepts no arguments");
    return NULL;
  }
  allocate = (allocfunc)PyType_GetSlot(type, Py_tp_alloc);
  if (allocate == NULL) {
    return NULL;
  }
  self = (WorkspaceOwner *)allocate(type, 0);
  if (self == NULL) {
    return NULL;
  }
  self->owner_pid = getpid();
  self->allowed_root_euid = (uid_t)-1;
  self->allowed_root_policy_known = 0;
  self->closed = 0;
  self->provision_started = 0;
  self->destination_capture_started = 0;
  self->namespace_created = 0;
  self->namespace_sync_pending = 0;
  self->namespace_state = WORKSPACE_EMPTY;
  self->anchor_fd = -1;
  self->anchor_guard_fd = -1;
  self->parent_fd = -1;
  self->parent_guard_fd = -1;
  self->parent_exposed = 0;
  self->replacement_lock_fd = -1;
  self->replacement_lock_guard_fd = -1;
  self->root_fd = -1;
  self->root_guard_fd = -1;
  self->root_exposed = 0;
  self->captured_destination_fd = -1;
  self->captured_destination_guard_fd = -1;
  self->captured_destination_exposed = 0;
  self->anchor_identity_known = 0;
  self->parent_identity_known = 0;
  self->replacement_lock_identity_known = 0;
  self->root_identity_known = 0;
  self->captured_destination_identity_known = 0;
  self->destination_name = NULL;
  self->destination_path = NULL;
  self->initial_stage_name = NULL;
  self->stage_name = NULL;
  self->replacement_slot_name = NULL;
  self->plan_digest = NULL;
  self->orphan_name = NULL;
  self->directories = NULL;
  self->directory_count = 0;
  self->scratch_count = 0;
  self->absolute_chain_count = 0;
  self->relative_chain_start = 0;
  self->relative_chain_count = 0;
  self->file_fd = -1;
  self->file_guard_fd = -1;
  self->file_parent_index = -2;
  self->file_name = NULL;
  self->file_identity_known = 0;
  self->file_active = 0;
  self->file_failed = 0;
  self->file_offset = 0;
  self->publish_permit_claimed = 0;
  self->publish_permit_active = 0;
  self->replacement_permit_claimed = 0;
  self->replacement_permit_active = 0;
  self->replacement_lock_active = 0;
  self->receipt_generation = 0;
  for (Py_ssize_t index = 0; index < CODENIB_MAX_SCRATCH_FDS; ++index) {
    self->scratch[index].path = NULL;
    self->scratch[index].fd = -1;
    self->scratch[index].guard_fd = -1;
    self->scratch[index].exposed = 0;
    self->scratch[index].identity_known = 0;
  }
  return (PyObject *)self;
}

static void WorkspaceOwner_dealloc(WorkspaceOwner *self) {
  freefunc release;
  PyObject *error_type;
  PyObject *error_value;
  PyObject *error_traceback;
  int settlement_failed = 0;

  PyErr_Fetch(&error_type, &error_value, &error_traceback);
  if (!self->closed && getpid() == self->owner_pid &&
      workspace_state_is_replacement(self)) {
    if (settle_replacement_namespace_internal(self) < 0 &&
        (self->replacement_lock_active ||
         self->namespace_state == WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED)) {
      /* Lease-held or recovery-required replacement ownership must not release
         its cooperative lease or close its pinned descriptors.  A pre-exchange
         contention failure acquired no lease and changed no namespace, so its
         descriptors can be closed without settling the sibling-owned names. */
      settlement_failed = 1;
    }
  } else if (!self->closed && getpid() == self->owner_pid &&
             self->namespace_created &&
             self->namespace_state != WORKSPACE_RECEIPTED) {
    (void)quarantine_namespace_internal(self);
  }
  if (!settlement_failed) {
    close_all(self);
  }
  free_configuration(self);
  release = (freefunc)PyType_GetSlot(Py_TYPE(self), Py_tp_free);
  if (release != NULL) {
    release((PyObject *)self);
  }
  PyErr_Restore(error_type, error_value, error_traceback);
}

static PyObject *
WorkspaceOwner_claim_publish_permit(WorkspaceOwner *self,
                                    PyObject *Py_UNUSED(ignored)) {
  PyObject *permit;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->closed || self->publish_permit_claimed || self->provision_started ||
      self->namespace_state != WORKSPACE_EMPTY || self->namespace_created ||
      self->replacement_permit_claimed || self->replacement_permit_active ||
      self->anchor_fd >= 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace publish permit is not claimable");
    return NULL;
  }
  permit = new_workspace_publish_permit(self);
  if (permit == NULL) {
    return NULL;
  }
  self->publish_permit_claimed = 1;
  self->publish_permit_active = 1;
  return permit;
}

static PyObject *
WorkspaceOwner_claim_replacement_permit(WorkspaceOwner *self,
                                        PyObject *Py_UNUSED(ignored)) {
  PyObject *permit;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->closed || self->namespace_state != WORKSPACE_DESTINATION_LEASED ||
      self->replacement_permit_claimed || self->replacement_permit_active ||
      self->publish_permit_claimed || self->publish_permit_active ||
      !self->replacement_lock_active || self->namespace_created ||
      self->root_fd >= 0 || self->root_guard_fd >= 0 ||
      verify_captured_destination_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace replacement permit is not claimable");
    return NULL;
  }
  permit = new_workspace_replacement_permit(self);
  if (permit == NULL) {
    return NULL;
  }
  self->replacement_permit_claimed = 1;
  self->replacement_permit_active = 1;
  return permit;
}

static PyObject *WorkspaceOwner_provision(WorkspaceOwner *self,
                                          PyObject *args) {
  PyObject *allowed_root_object;
  PyObject *destination_object;
  PyObject *stage_object;
  PyObject *digest_object;
  PyObject *root_mode_object;
  PyObject *directories_object;
  PyObject *deadline_object;
  char *allowed_root = NULL;
  char *destination = NULL;
  char *parent_path = NULL;
  char *destination_name = NULL;
  char *destination_path = NULL;
  char *stage_name = NULL;
  char *plan_digest = NULL;
  long root_mode;
  long long deadline_ns;
  struct stat metadata;
  dev_t opened_device;
  ino_t opened_inode;
  Py_ssize_t index;
  int parent;
  int acquisition_started = 0;
  const char *name;

  if (require_linux() < 0 || require_owner_pid(self) < 0) {
    return NULL;
  }
#if !defined(SYS_renameat2) || !defined(SYS_fstat) ||                          \
    !defined(SYS_newfstatat) || !defined(SYS_kcmp) || !defined(SYS_close)
  PyErr_SetString(PyExc_RuntimeError,
                  "native strict workspace provisioning requires Linux "
                  "descriptor and OFD-comparison syscalls");
  return NULL;
#else
  if (probe_kcmp_support() < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native strict workspace provisioning requires permitted "
                    "same-process kcmp OFD comparison");
    return NULL;
  }
#endif
  if (!self->publish_permit_claimed || !self->publish_permit_active) {
    PyErr_SetString(
        PyExc_RuntimeError,
        "native workspace provision requires its claimed publish permit");
    return NULL;
  }
  if (self->namespace_state != WORKSPACE_EMPTY || self->closed ||
      self->provision_started || self->replacement_permit_claimed ||
      self->replacement_permit_active || self->anchor_fd >= 0) {
    PyErr_SetString(PyExc_RuntimeError, "native workspace owner is not empty");
    return NULL;
  }
  if (!PyArg_UnpackTuple(args, "provision", 7, 7, &allowed_root_object,
                         &destination_object, &stage_object, &digest_object,
                         &root_mode_object, &directories_object,
                         &deadline_object)) {
    return NULL;
  }
  if (!PyLong_CheckExact(deadline_object)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace deadline must be an exact integer");
    return NULL;
  }
  deadline_ns = PyLong_AsLongLong(deadline_object);
  if (deadline_ns == -1 && PyErr_Occurred()) {
    return NULL;
  }
  if (deadline_ns <= 0) {
    PyErr_SetString(PyExc_ValueError, "workspace deadline is invalid");
    return NULL;
  }
  if (check_deadline(deadline_ns) < 0) {
    return NULL;
  }
  allowed_root = copy_exact_bytes(allowed_root_object, "allowed root",
                                  CODENIB_MAX_PATH_BYTES);
  destination = copy_exact_bytes(destination_object, "workspace destination",
                                 CODENIB_MAX_PATH_BYTES);
  stage_name = copy_exact_bytes(stage_object, "workspace stage name",
                                CODENIB_MAX_COMPONENT_BYTES);
  plan_digest = copy_exact_bytes(digest_object, "workspace plan digest", 128);
  if (allowed_root == NULL || destination == NULL || stage_name == NULL ||
      plan_digest == NULL) {
    goto error;
  }
  if (allowed_root[0] != '/' || !valid_component(stage_name) ||
      strlen(plan_digest) != 64) {
    PyErr_SetString(PyExc_ValueError,
                    "native workspace provision arguments are invalid");
    goto error;
  }
  for (index = 0; index < 64; ++index) {
    if (!((plan_digest[index] >= '0' && plan_digest[index] <= '9') ||
          (plan_digest[index] >= 'a' && plan_digest[index] <= 'f'))) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace plan digest must be lowercase hexadecimal");
      goto error;
    }
  }
  if (!PyLong_CheckExact(root_mode_object)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace root mode must be an exact integer");
    goto error;
  }
  root_mode = PyLong_AsLong(root_mode_object);
  if (root_mode == -1 && PyErr_Occurred()) {
    goto error;
  }
  if (root_mode < 0 || root_mode > 0777) {
    PyErr_SetString(PyExc_ValueError, "workspace root mode is invalid");
    goto error;
  }
  if (split_destination(destination, &parent_path, &destination_name) < 0 ||
      parse_directories(self, directories_object, deadline_ns) < 0 ||
      validate_directory_plan(self, deadline_ns) < 0 ||
      preflight_descriptor_budget(self, allowed_root, parent_path, 0) < 0) {
    goto error;
  }
  destination_path = join_destination_path(allowed_root, destination);
  if (destination_path == NULL) {
    goto error;
  }
  self->destination_name = destination_name;
  destination_name = NULL;
  self->destination_path = destination_path;
  destination_path = NULL;
  self->stage_name = stage_name;
  stage_name = NULL;
  self->initial_stage_name = strdup(self->stage_name);
  if (self->initial_stage_name == NULL) {
    PyErr_NoMemory();
    goto error;
  }
  self->plan_digest = plan_digest;
  plan_digest = NULL;
  if (strcmp(self->destination_name, self->stage_name) == 0) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace stage and destination must differ");
    goto error;
  }
  if (check_deadline(deadline_ns) < 0) {
    goto error;
  }

  acquisition_started = 1;
  self->provision_started = 1;
  self->anchor_fd = open_absolute_directory(self, allowed_root);
  self->absolute_chain_count = self->scratch_count;
  if (self->anchor_fd >= 0) {
    self->anchor_fd = transfer_scratch_descriptor(
        self, self->anchor_fd, &self->anchor_guard_fd, &self->anchor_device,
        &self->anchor_inode);
    if (self->anchor_fd >= 0) {
      self->anchor_identity_known = 1;
    }
  }
  if (self->anchor_fd < 0 ||
      record_identity(self->anchor_fd, &self->anchor_device,
                      &self->anchor_inode, S_IFDIR) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->allowed_root_euid = geteuid();
  self->allowed_root_policy_known = 1;
  if (verify_allowed_root_policy(self) < 0) {
    if (errno == EPERM) {
      PyErr_SetString(PyExc_PermissionError,
                      "workspace allowed root must be owned by the current "
                      "effective user with exact mode 0700");
    } else {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    goto error;
  }
  self->relative_chain_start = self->scratch_count;
  self->parent_fd = walk_relative_directory(self, self->anchor_fd, parent_path);
  self->relative_chain_count = self->scratch_count - self->relative_chain_start;
  if (self->parent_fd >= 0) {
    self->parent_fd = transfer_scratch_descriptor(
        self, self->parent_fd, &self->parent_guard_fd, &self->parent_device,
        &self->parent_inode);
    if (self->parent_fd >= 0) {
      self->parent_identity_known = 1;
    }
  }
  if (self->parent_fd < 0 ||
      record_identity(self->parent_fd, &self->parent_device,
                      &self->parent_inode, S_IFDIR) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (self->parent_device != self->anchor_device) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace destination crosses the allowed-root device");
    goto error;
  }
  if (verify_parent_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "workspace allowed-root binding changed before provision");
    goto error;
  }
  if (native_fstatat_retry(self->parent_guard_fd, self->destination_name,
                           &metadata, AT_SYMLINK_NOFOLLOW) == 0) {
    PyErr_SetString(PyExc_FileExistsError,
                    "workspace destination already exists");
    goto error;
  }
  if (errno != ENOENT) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (native_fstatat_retry(self->parent_guard_fd, self->stage_name, &metadata,
                           AT_SYMLINK_NOFOLLOW) == 0) {
    PyErr_SetString(PyExc_FileExistsError, "workspace stage already exists");
    goto error;
  }
  if (errno != ENOENT || check_deadline(deadline_ns) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    goto error;
  }
  if (verify_parent_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "workspace allowed-root binding changed before mutation");
    goto error;
  }
  /* This is the last instruction boundary before the first namespace
     mutation.  Reattest the pinned allowed-root guard here, after all path
     probes and immediately before mkdirat. */
  if (verify_allowed_root_policy(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "workspace allowed-root policy changed before mutation");
    goto error;
  }
  self->namespace_state = WORKSPACE_PROVISIONING;
  if (mkdirat(self->parent_guard_fd, self->stage_name, 0700) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->namespace_created = 1;
  if (identify_created_root(self) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->root_identity_known = 1;
  self->root_fd = open_directory_at(self->parent_guard_fd, self->stage_name);
  if (self->root_fd < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->root_guard_fd = duplicate_cloexec(self->root_fd);
  if (self->root_guard_fd < 0) {
    int error = errno;
    discard_uncommitted_pair(&self->root_fd, &self->root_guard_fd, error);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (record_identity(self->root_fd, &metadata.st_dev, &metadata.st_ino,
                      S_IFDIR) < 0) {
    int error = errno;
    discard_uncommitted_pair(&self->root_fd, &self->root_guard_fd, error);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (metadata.st_dev != self->root_device ||
      metadata.st_ino != self->root_inode) {
    errno = ESTALE;
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (fchmod(self->root_fd, (mode_t)root_mode) < 0 ||
      same_identity_at(self->parent_guard_fd, self->stage_name,
                       self->root_device, self->root_inode) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (self->root_device != self->parent_device) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace root crosses the destination-parent device");
    goto error;
  }
  for (index = 0; index < self->directory_count; ++index) {
    if (check_deadline(deadline_ns) < 0 || verify_parent_binding(self) < 0) {
      if (!PyErr_Occurred()) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "workspace allowed-root binding changed during provision");
      }
      goto error;
    }
    parent = find_parent_descriptor(self, self->directories[index].path);
    if (parent < 0) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace directory parent is absent from its plan");
      goto error;
    }
    name = path_basename(self->directories[index].path);
    if (verify_allowed_root_policy(self) < 0) {
      PyErr_SetString(
          PyExc_RuntimeError,
          "workspace allowed-root policy changed before directory mutation");
      goto error;
    }
    if (mkdirat(parent, name, 0700) < 0) {
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (native_fstatat_retry(parent, name, &metadata, AT_SYMLINK_NOFOLLOW) <
        0) {
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (!S_ISDIR(metadata.st_mode) || metadata.st_dev == 0 ||
        metadata.st_ino == 0) {
      errno = EINVAL;
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    self->directories[index].device = metadata.st_dev;
    self->directories[index].inode = metadata.st_ino;
    self->directories[index].identity_known = 1;
    self->directories[index].fd = open_directory_at(parent, name);
    if (self->directories[index].fd < 0) {
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    self->directories[index].guard_fd =
        duplicate_cloexec(self->directories[index].fd);
    if (self->directories[index].guard_fd < 0) {
      int error = errno;
      discard_uncommitted_pair(&self->directories[index].fd,
                               &self->directories[index].guard_fd, error);
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (record_identity(self->directories[index].fd, &opened_device,
                        &opened_inode, S_IFDIR) < 0) {
      int error = errno;
      discard_uncommitted_pair(&self->directories[index].fd,
                               &self->directories[index].guard_fd, error);
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (opened_device != self->directories[index].device ||
        opened_inode != self->directories[index].inode) {
      errno = ESTALE;
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (fchmod(self->directories[index].fd, self->directories[index].mode) <
            0 ||
        same_identity_at(parent, name, self->directories[index].device,
                         self->directories[index].inode) < 0) {
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (self->directories[index].device != self->root_device) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace directory crosses the root device");
      goto error;
    }
  }
  if (native_fsync_retry(self->root_guard_fd) < 0 ||
      native_fsync_retry(self->parent_guard_fd) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (verify_owner_authority(self) < 0) {
    PyErr_SetString(
        PyExc_RuntimeError,
        "workspace allowed-root binding changed before provision commit");
    goto error;
  }
  if (verify_allowed_root_policy(self) < 0) {
    PyErr_SetString(
        PyExc_RuntimeError,
        "workspace allowed-root policy changed before provision commit");
    goto error;
  }
  self->namespace_state = WORKSPACE_PROVISIONED;
  free(allowed_root);
  free(destination);
  free(parent_path);
  Py_RETURN_NONE;

error:
  if (!acquisition_started) {
    free_configuration(self);
  }
  free(allowed_root);
  free(destination);
  free(parent_path);
  free(destination_name);
  free(destination_path);
  free(stage_name);
  free(plan_digest);
  return NULL;
}

static PyObject *WorkspaceOwner_capture_destination(WorkspaceOwner *self,
                                                    PyObject *args) {
  PyObject *allowed_root_object;
  PyObject *destination_object;
  PyObject *deadline_object;
  char *allowed_root = NULL;
  char *destination = NULL;
  char *parent_path = NULL;
  char *destination_name = NULL;
  char *destination_path = NULL;
  long long deadline_ns;
  struct stat linked_metadata;
  dev_t opened_device;
  ino_t opened_inode;
  int acquisition_started = 0;

  if (require_linux() < 0 || require_owner_pid(self) < 0) {
    return NULL;
  }
#if !defined(SYS_fstat) || !defined(SYS_newfstatat) || !defined(SYS_kcmp) ||   \
    !defined(SYS_close)
  PyErr_SetString(PyExc_RuntimeError,
                  "native strict destination capture requires Linux "
                  "descriptor and OFD-comparison syscalls");
  return NULL;
#else
  if (probe_kcmp_support() < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native strict destination capture requires permitted "
                    "same-process kcmp OFD comparison");
    return NULL;
  }
#endif
  if (self->closed || self->provision_started ||
      self->destination_capture_started || self->namespace_created ||
      self->namespace_sync_pending ||
      self->namespace_state != WORKSPACE_EMPTY ||
      self->publish_permit_claimed || self->publish_permit_active ||
      self->replacement_permit_claimed || self->replacement_permit_active ||
      self->replacement_lock_active || self->anchor_fd >= 0 ||
      self->anchor_guard_fd >= 0 || self->parent_fd >= 0 ||
      self->parent_guard_fd >= 0 || self->replacement_lock_fd >= 0 ||
      self->replacement_lock_guard_fd >= 0 || self->root_fd >= 0 ||
      self->root_guard_fd >= 0 || self->captured_destination_fd >= 0 ||
      self->captured_destination_guard_fd >= 0 || self->directories != NULL ||
      self->directory_count != 0 || self->scratch_count != 0 ||
      self->absolute_chain_count != 0 || self->relative_chain_count != 0 ||
      self->destination_name != NULL || self->destination_path != NULL ||
      self->initial_stage_name != NULL || self->stage_name != NULL ||
      self->replacement_slot_name != NULL || self->plan_digest != NULL ||
      self->orphan_name != NULL || self->file_fd >= 0 ||
      self->file_guard_fd >= 0 || self->file_name != NULL ||
      self->file_active || self->file_failed || self->receipt_generation != 0 ||
      self->allowed_root_policy_known || self->anchor_identity_known ||
      self->parent_identity_known || self->replacement_lock_identity_known ||
      self->root_identity_known || self->captured_destination_identity_known) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner is not empty for destination "
                    "capture");
    return NULL;
  }
  if (!PyArg_UnpackTuple(args, "capture_destination", 3, 3,
                         &allowed_root_object, &destination_object,
                         &deadline_object)) {
    return NULL;
  }
  if (!PyLong_CheckExact(deadline_object)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace capture deadline must be an exact integer");
    return NULL;
  }
  deadline_ns = PyLong_AsLongLong(deadline_object);
  if (deadline_ns == -1 && PyErr_Occurred()) {
    return NULL;
  }
  if (deadline_ns <= 0) {
    PyErr_SetString(PyExc_ValueError, "workspace capture deadline is invalid");
    return NULL;
  }
  if (check_deadline(deadline_ns) < 0) {
    return NULL;
  }
  allowed_root = copy_exact_bytes(allowed_root_object, "allowed root",
                                  CODENIB_MAX_PATH_BYTES);
  destination = copy_exact_bytes(destination_object, "workspace destination",
                                 CODENIB_MAX_PATH_BYTES);
  if (allowed_root == NULL || destination == NULL) {
    goto error;
  }
  if (allowed_root[0] != '/') {
    PyErr_SetString(PyExc_ValueError,
                    "workspace allowed root must be an absolute path");
    goto error;
  }
  if (split_destination(destination, &parent_path, &destination_name) < 0 ||
      preflight_descriptor_budget(self, allowed_root, parent_path, 1) < 0) {
    goto error;
  }
  destination_path = join_destination_path(allowed_root, destination);
  if (destination_path == NULL || check_deadline(deadline_ns) < 0) {
    goto error;
  }

  self->destination_name = destination_name;
  destination_name = NULL;
  self->destination_path = destination_path;
  destination_path = NULL;
  acquisition_started = 1;
  self->provision_started = 1;
  self->destination_capture_started = 1;

  self->anchor_fd = open_absolute_directory(self, allowed_root);
  self->absolute_chain_count = self->scratch_count;
  if (self->anchor_fd >= 0) {
    self->anchor_fd = transfer_scratch_descriptor(
        self, self->anchor_fd, &self->anchor_guard_fd, &self->anchor_device,
        &self->anchor_inode);
    if (self->anchor_fd >= 0) {
      self->anchor_identity_known = 1;
    }
  }
  if (self->anchor_fd < 0 ||
      record_identity(self->anchor_fd, &self->anchor_device,
                      &self->anchor_inode, S_IFDIR) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->allowed_root_euid = geteuid();
  self->allowed_root_policy_known = 1;
  if (verify_allowed_root_policy(self) < 0) {
    if (errno == EPERM) {
      PyErr_SetString(PyExc_PermissionError,
                      "workspace allowed root must be owned by the current "
                      "effective user with exact mode 0700");
    } else {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    goto error;
  }
  if (check_deadline(deadline_ns) < 0) {
    goto error;
  }
  self->relative_chain_start = self->scratch_count;
  self->parent_fd = walk_relative_directory(self, self->anchor_fd, parent_path);
  self->relative_chain_count = self->scratch_count - self->relative_chain_start;
  if (self->parent_fd >= 0) {
    self->parent_fd = transfer_scratch_descriptor(
        self, self->parent_fd, &self->parent_guard_fd, &self->parent_device,
        &self->parent_inode);
    if (self->parent_fd >= 0) {
      self->parent_identity_known = 1;
    }
  }
  if (self->parent_fd < 0 ||
      record_identity(self->parent_fd, &self->parent_device,
                      &self->parent_inode, S_IFDIR) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (self->parent_device != self->anchor_device) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace destination crosses the allowed-root device");
    goto error;
  }
  if (verify_parent_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "workspace allowed-root binding changed during destination "
                    "capture");
    goto error;
  }
  self->replacement_lock_fd = open_directory_at(self->parent_guard_fd, ".");
  if (self->replacement_lock_fd < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->replacement_lock_guard_fd =
      duplicate_cloexec(self->replacement_lock_fd);
  if (self->replacement_lock_guard_fd < 0) {
    int error_number = errno;
    discard_uncommitted_pair(&self->replacement_lock_fd,
                             &self->replacement_lock_guard_fd, error_number);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (record_identity(self->replacement_lock_fd, &self->replacement_lock_device,
                      &self->replacement_lock_inode, S_IFDIR) < 0) {
    int error_number = errno;
    discard_uncommitted_pair(&self->replacement_lock_fd,
                             &self->replacement_lock_guard_fd, error_number);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->replacement_lock_identity_known = 1;
  if (verify_replacement_lock_authority(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "workspace replacement lock authority is invalid");
    goto error;
  }
  if (native_fstatat_retry(self->parent_guard_fd, self->destination_name,
                           &linked_metadata, AT_SYMLINK_NOFOLLOW) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (!S_ISDIR(linked_metadata.st_mode) || linked_metadata.st_dev == 0 ||
      linked_metadata.st_ino == 0) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace capture destination is not a directory");
    goto error;
  }
  if (linked_metadata.st_dev != self->parent_device) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace capture destination crosses the parent device");
    goto error;
  }
  self->captured_destination_device = linked_metadata.st_dev;
  self->captured_destination_inode = linked_metadata.st_ino;
  self->captured_destination_fd =
      open_directory_at(self->parent_guard_fd, self->destination_name);
  if (self->captured_destination_fd < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->captured_destination_guard_fd =
      duplicate_cloexec(self->captured_destination_fd);
  if (self->captured_destination_guard_fd < 0) {
    int error_number = errno;
    discard_uncommitted_pair(&self->captured_destination_fd,
                             &self->captured_destination_guard_fd,
                             error_number);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (record_identity(self->captured_destination_fd, &opened_device,
                      &opened_inode, S_IFDIR) < 0) {
    int error_number = errno;
    discard_uncommitted_pair(&self->captured_destination_fd,
                             &self->captured_destination_guard_fd,
                             error_number);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (opened_device != self->captured_destination_device ||
      opened_inode != self->captured_destination_inode ||
      opened_device != self->parent_device) {
    errno = ESTALE;
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->captured_destination_identity_known = 1;
  if (check_deadline(deadline_ns) < 0 || verify_parent_binding(self) < 0 ||
      verify_descriptor(self->captured_destination_fd,
                        self->captured_destination_guard_fd,
                        self->captured_destination_device,
                        self->captured_destination_inode) < 0 ||
      same_identity_at(self->parent_guard_fd, self->destination_name,
                       self->captured_destination_device,
                       self->captured_destination_inode) < 0 ||
      verify_allowed_root_policy(self) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
                      "workspace destination binding changed before capture "
                      "commit");
    }
    goto error;
  }
  self->namespace_created = 0;
  self->namespace_sync_pending = 0;
  self->namespace_state = WORKSPACE_DESTINATION_CAPTURED;
  free(allowed_root);
  free(destination);
  free(parent_path);
  Py_RETURN_NONE;

error:
  if (!acquisition_started) {
    free_configuration(self);
  }
  free(allowed_root);
  free(destination);
  free(parent_path);
  free(destination_name);
  free(destination_path);
  return NULL;
}

static int verify_incumbent_at_destination(WorkspaceOwner *self) {
  if (!self->replacement_lock_active ||
      verify_replacement_lock_authority(self) < 0 ||
      verify_parent_binding(self) < 0 ||
      verify_descriptor(self->captured_destination_fd,
                        self->captured_destination_guard_fd,
                        self->captured_destination_device,
                        self->captured_destination_inode) < 0 ||
      self->captured_destination_device != self->parent_device ||
      same_identity_at(self->parent_guard_fd, self->destination_name,
                       self->captured_destination_device,
                       self->captured_destination_inode) < 0) {
    return -1;
  }
  return 0;
}

static PyObject *
WorkspaceOwner_acquire_replacement_lease(WorkspaceOwner *self,
                                         PyObject *deadline_object) {
  PyObject *error_type = NULL;
  PyObject *error_value = NULL;
  PyObject *error_traceback = NULL;
  long long deadline_ns;

  if (require_linux() < 0 || require_owner_pid(self) < 0) {
    return NULL;
  }
  if (!PyLong_CheckExact(deadline_object)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace replacement lease deadline must be an exact "
                    "integer");
    return NULL;
  }
  deadline_ns = PyLong_AsLongLong(deadline_object);
  if (deadline_ns == -1 && PyErr_Occurred()) {
    return NULL;
  }
  if (deadline_ns <= 0) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace replacement lease deadline is invalid");
    return NULL;
  }
  if (check_deadline(deadline_ns) < 0) {
    return NULL;
  }
  if (self->namespace_state == WORKSPACE_DESTINATION_LEASED) {
    if (self->closed || !self->replacement_lock_active ||
        self->replacement_permit_claimed || self->replacement_permit_active ||
        self->namespace_created || self->namespace_sync_pending ||
        self->root_fd >= 0 || self->root_guard_fd >= 0 ||
        self->replacement_slot_name != NULL || self->directories != NULL ||
        verify_captured_destination_binding(self) < 0) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace owner is not replacement-lease-ready");
      return NULL;
    }
    if (check_deadline(deadline_ns) < 0) {
      return NULL;
    }
    Py_RETURN_NONE;
  }
  if (self->closed || self->namespace_state != WORKSPACE_DESTINATION_CAPTURED ||
      self->replacement_lock_active || self->replacement_permit_claimed ||
      self->replacement_permit_active || self->namespace_created ||
      self->namespace_sync_pending || self->root_fd >= 0 ||
      self->root_guard_fd >= 0 || self->replacement_slot_name != NULL ||
      self->directories != NULL ||
      verify_captured_destination_local_authority(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner is not replacement-lease-ready");
    return NULL;
  }
  if (native_flock_retry(self->replacement_lock_fd, LOCK_EX | LOCK_NB) < 0) {
    if (errno == EWOULDBLOCK || errno == EAGAIN) {
      return PyErr_SetFromErrno(PyExc_BlockingIOError);
    }
    return PyErr_SetFromErrno(PyExc_OSError);
  }

  /* From this assignment onward every return boundary is settlement-owned.
     The captured binding is deliberately revalidated only after the private,
     independently opened parent OFD has become the cooperative single-writer
     lease.  Namespace operations continue to use the original parent guard. */
  self->replacement_lock_active = 1;
  self->namespace_state = WORKSPACE_DESTINATION_LEASED;
  if (check_deadline(deadline_ns) < 0) {
    goto rollback_lease;
  }
  if (verify_captured_destination_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace captured destination changed before "
                    "replacement lease");
    goto rollback_lease;
  }
  if (check_deadline(deadline_ns) < 0) {
    goto rollback_lease;
  }
  Py_RETURN_NONE;

rollback_lease:
  PyErr_Fetch(&error_type, &error_value, &error_traceback);
  if (release_replacement_lock(self) < 0) {
    enter_replacement_recovery(self);
  } else {
    self->namespace_state = WORKSPACE_DESTINATION_CAPTURED;
  }
  PyErr_Restore(error_type, error_value, error_traceback);
  return NULL;
}

static PyObject *WorkspaceOwner_provision_replacement(WorkspaceOwner *self,
                                                      PyObject *args) {
  PyObject *slot_object;
  PyObject *digest_object;
  PyObject *root_mode_object;
  PyObject *directories_object;
  PyObject *deadline_object;
  char *slot_name = NULL;
  char *plan_digest = NULL;
  long root_mode;
  long long deadline_ns;
  struct stat metadata;
  dev_t opened_device;
  ino_t opened_inode;
  Py_ssize_t index;
  int parent;
  int mutation_attempted = 0;
  const char *name;

  if (require_linux() < 0 || require_owner_pid(self) < 0) {
    return NULL;
  }
#if !defined(SYS_renameat2) || !defined(SYS_fstat) ||                          \
    !defined(SYS_newfstatat) || !defined(SYS_kcmp) || !defined(SYS_close)
  PyErr_SetString(PyExc_RuntimeError,
                  "native strict workspace replacement requires Linux "
                  "descriptor, flock, and renameat2 syscalls");
  return NULL;
#endif
  if (self->closed || self->namespace_state != WORKSPACE_DESTINATION_LEASED ||
      !self->replacement_permit_claimed || !self->replacement_permit_active ||
      !self->replacement_lock_active || self->namespace_created ||
      self->root_fd >= 0 || self->root_guard_fd >= 0 ||
      self->replacement_slot_name != NULL || self->directories != NULL ||
      verify_captured_destination_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner is not replacement-ready");
    return NULL;
  }
  if (!PyArg_UnpackTuple(args, "provision_replacement", 5, 5, &slot_object,
                         &digest_object, &root_mode_object, &directories_object,
                         &deadline_object)) {
    return NULL;
  }
  if (!PyLong_CheckExact(deadline_object)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace replacement deadline must be an exact integer");
    return NULL;
  }
  deadline_ns = PyLong_AsLongLong(deadline_object);
  if (deadline_ns == -1 && PyErr_Occurred()) {
    return NULL;
  }
  if (deadline_ns <= 0) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace replacement deadline is invalid");
    return NULL;
  }
  if (check_deadline(deadline_ns) < 0) {
    return NULL;
  }
  slot_name = copy_exact_bytes(slot_object, "workspace replacement slot",
                               CODENIB_MAX_COMPONENT_BYTES);
  plan_digest = copy_exact_bytes(digest_object, "workspace plan digest", 128);
  if (slot_name == NULL || plan_digest == NULL) {
    goto error;
  }
  if (!valid_component(slot_name) || slot_name[0] != '.' ||
      strcmp(slot_name, self->destination_name) == 0 ||
      strlen(plan_digest) != 64) {
    PyErr_SetString(PyExc_ValueError,
                    "native workspace replacement arguments are invalid");
    goto error;
  }
  for (index = 0; index < 64; ++index) {
    if (!((plan_digest[index] >= '0' && plan_digest[index] <= '9') ||
          (plan_digest[index] >= 'a' && plan_digest[index] <= 'f'))) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace plan digest must be lowercase hexadecimal");
      goto error;
    }
  }
  if (!PyLong_CheckExact(root_mode_object)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace root mode must be an exact integer");
    goto error;
  }
  root_mode = PyLong_AsLong(root_mode_object);
  if (root_mode == -1 && PyErr_Occurred()) {
    goto error;
  }
  if (root_mode < 0 || root_mode > 0777) {
    PyErr_SetString(PyExc_ValueError, "workspace root mode is invalid");
    goto error;
  }
  if (parse_directories(self, directories_object, deadline_ns) < 0 ||
      validate_directory_plan(self, deadline_ns) < 0 ||
      preflight_additional_descriptor_budget((size_t)self->directory_count +
                                             1) < 0) {
    goto error;
  }
  self->replacement_slot_name = slot_name;
  slot_name = NULL;
  self->plan_digest = plan_digest;
  plan_digest = NULL;
  if (check_deadline(deadline_ns) < 0 ||
      verify_incumbent_at_destination(self) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
                      "workspace incumbent binding changed before provision");
    }
    goto error;
  }
  if (native_fstatat_retry(self->parent_guard_fd, self->replacement_slot_name,
                           &metadata, AT_SYMLINK_NOFOLLOW) == 0) {
    PyErr_SetString(PyExc_FileExistsError,
                    "workspace replacement slot already exists");
    goto error;
  }
  if (errno != ENOENT || check_deadline(deadline_ns) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    goto error;
  }
  if (verify_incumbent_at_destination(self) < 0 ||
      verify_allowed_root_policy(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "workspace replacement binding changed before mutation");
    goto error;
  }
  self->namespace_state = WORKSPACE_REPLACEMENT_PROVISIONING;
  self->namespace_sync_pending = 1;
  mutation_attempted = 1;
  if (mkdirat(self->parent_guard_fd, self->replacement_slot_name, 0700) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->namespace_created = 1;
  self->namespace_sync_pending = 1;
  if (identify_created_root(self) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->root_identity_known = 1;
  self->root_fd =
      open_directory_at(self->parent_guard_fd, self->replacement_slot_name);
  if (self->root_fd < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->root_guard_fd = duplicate_cloexec(self->root_fd);
  if (self->root_guard_fd < 0) {
    int error_number = errno;
    discard_uncommitted_pair(&self->root_fd, &self->root_guard_fd,
                             error_number);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (record_identity(self->root_fd, &opened_device, &opened_inode, S_IFDIR) <
      0) {
    int error_number = errno;
    discard_uncommitted_pair(&self->root_fd, &self->root_guard_fd,
                             error_number);
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (opened_device != self->root_device || opened_inode != self->root_inode) {
    errno = ESTALE;
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (native_fchmod_retry(self->root_guard_fd, (mode_t)root_mode) < 0 ||
      same_identity_at(self->parent_guard_fd, self->replacement_slot_name,
                       self->root_device, self->root_inode) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  if (self->root_device != self->parent_device ||
      self->root_device != self->captured_destination_device) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace replacement roots cross the parent device");
    goto error;
  }
  for (index = 0; index < self->directory_count; ++index) {
    if (check_deadline(deadline_ns) < 0 ||
        verify_incumbent_at_destination(self) < 0) {
      if (!PyErr_Occurred()) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "workspace replacement binding changed during provision");
      }
      goto error;
    }
    parent = find_parent_descriptor(self, self->directories[index].path);
    if (parent < 0) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace directory parent is absent from its plan");
      goto error;
    }
    name = path_basename(self->directories[index].path);
    if (verify_allowed_root_policy(self) < 0 ||
        mkdirat(parent, name, 0700) < 0 ||
        native_fstatat_retry(parent, name, &metadata, AT_SYMLINK_NOFOLLOW) <
            0) {
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (!S_ISDIR(metadata.st_mode) || metadata.st_dev == 0 ||
        metadata.st_ino == 0) {
      errno = EINVAL;
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    self->directories[index].device = metadata.st_dev;
    self->directories[index].inode = metadata.st_ino;
    self->directories[index].identity_known = 1;
    self->directories[index].fd = open_directory_at(parent, name);
    if (self->directories[index].fd < 0) {
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    self->directories[index].guard_fd =
        duplicate_cloexec(self->directories[index].fd);
    if (self->directories[index].guard_fd < 0) {
      int error_number = errno;
      discard_uncommitted_pair(&self->directories[index].fd,
                               &self->directories[index].guard_fd,
                               error_number);
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (record_identity(self->directories[index].fd, &opened_device,
                        &opened_inode, S_IFDIR) < 0) {
      int error_number = errno;
      discard_uncommitted_pair(&self->directories[index].fd,
                               &self->directories[index].guard_fd,
                               error_number);
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (opened_device != self->directories[index].device ||
        opened_inode != self->directories[index].inode) {
      errno = ESTALE;
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (native_fchmod_retry(self->directories[index].guard_fd,
                            self->directories[index].mode) < 0 ||
        same_identity_at(parent, name, self->directories[index].device,
                         self->directories[index].inode) < 0) {
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    if (self->directories[index].device != self->root_device) {
      PyErr_SetString(
          PyExc_ValueError,
          "workspace directory crosses the replacement root device");
      goto error;
    }
  }
  if (native_fsync_retry(self->root_guard_fd) < 0 ||
      native_fsync_retry(self->parent_guard_fd) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->namespace_sync_pending = 0;
  if (verify_replacement_binding(self) < 0 ||
      verify_allowed_root_policy(self) < 0) {
    PyErr_SetString(
        PyExc_RuntimeError,
        "workspace replacement binding changed before provision commit");
    goto error;
  }
  self->namespace_state = WORKSPACE_REPLACEMENT_PROVISIONED;
  Py_RETURN_NONE;

error:
  if (!mutation_attempted) {
    free_unprovisioned_replacement_configuration(self);
  }
  free(slot_name);
  free(plan_digest);
  return NULL;
}

static int verify_descriptor(int descriptor, int guard, dev_t device,
                             ino_t inode) {
  struct stat metadata;
  int flags;
  if (descriptor < 0 || guard < 0 ||
      compare_open_file_description(descriptor, guard) != OFD_SAME ||
      native_fstat_retry(descriptor, &metadata) < 0) {
    return -1;
  }
  flags = native_fcntl_getfd_retry(descriptor);
  if (!S_ISDIR(metadata.st_mode) || metadata.st_dev != device ||
      metadata.st_ino != inode || flags < 0 || (flags & FD_CLOEXEC) == 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_hidden_directory(int descriptor, dev_t device, ino_t inode) {
  struct stat metadata;
  int flags;

  if (descriptor < 0 || native_fstat_retry(descriptor, &metadata) < 0) {
    return -1;
  }
  flags = native_fcntl_getfd_retry(descriptor);
  if (!S_ISDIR(metadata.st_mode) || metadata.st_dev != device ||
      metadata.st_ino != inode || flags < 0 || (flags & FD_CLOEXEC) == 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_replacement_lock_authority(WorkspaceOwner *self) {
  int guard_flags;

  if (!self->parent_identity_known || !self->replacement_lock_identity_known ||
      self->replacement_lock_device != self->parent_device ||
      self->replacement_lock_inode != self->parent_inode ||
      verify_descriptor(
          self->replacement_lock_fd, self->replacement_lock_guard_fd,
          self->replacement_lock_device, self->replacement_lock_inode) < 0 ||
      compare_open_file_description(self->replacement_lock_fd,
                                    self->parent_guard_fd) != OFD_DIFFERENT ||
      compare_open_file_description(self->replacement_lock_guard_fd,
                                    self->parent_guard_fd) != OFD_DIFFERENT ||
      (guard_flags =
           native_fcntl_getfd_retry(self->replacement_lock_guard_fd)) < 0 ||
      (guard_flags & FD_CLOEXEC) == 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_absolute_chain(WorkspaceOwner *self) {
  Py_ssize_t index;
  int descriptor;
  int guard;
  int parent;
  struct stat metadata;

  if (self->absolute_chain_count <= 0 ||
      self->absolute_chain_count > self->scratch_count) {
    errno = ESTALE;
    return -1;
  }
  for (index = 0; index < self->absolute_chain_count; ++index) {
    WorkspaceDirectoryRecord *record = &self->scratch[index];
    if (index + 1 == self->absolute_chain_count) {
      descriptor = self->anchor_fd;
      guard = self->anchor_guard_fd;
    } else {
      descriptor = record->fd;
      guard = record->guard_fd;
    }
    if (!record->identity_known ||
        verify_descriptor(descriptor, guard, record->device, record->inode) <
            0) {
      return -1;
    }
    if (index == 0) {
      if (record->path == NULL || strcmp(record->path, "/") != 0 ||
          native_fstatat_retry(AT_FDCWD, "/", &metadata, AT_SYMLINK_NOFOLLOW) <
              0 ||
          !S_ISDIR(metadata.st_mode) || metadata.st_dev != record->device ||
          metadata.st_ino != record->inode) {
        errno = ESTALE;
        return -1;
      }
      continue;
    }
    parent = self->scratch[index - 1].guard_fd;
    if (parent < 0 || record->path == NULL ||
        same_identity_at(parent, record->path, record->device, record->inode) <
            0) {
      return -1;
    }
  }
  return 0;
}

static int verify_relative_chain(WorkspaceOwner *self) {
  Py_ssize_t offset;
  int descriptor;
  int guard;
  int parent;

  if (self->relative_chain_count <= 0 || self->relative_chain_start < 0 ||
      self->relative_chain_start + self->relative_chain_count >
          self->scratch_count) {
    errno = ESTALE;
    return -1;
  }
  for (offset = 0; offset < self->relative_chain_count; ++offset) {
    Py_ssize_t index = self->relative_chain_start + offset;
    WorkspaceDirectoryRecord *record = &self->scratch[index];
    if (offset + 1 == self->relative_chain_count) {
      descriptor = self->parent_fd;
      guard = self->parent_guard_fd;
    } else {
      descriptor = record->fd;
      guard = record->guard_fd;
    }
    if (!record->identity_known ||
        verify_descriptor(descriptor, guard, record->device, record->inode) <
            0) {
      return -1;
    }
    if (offset == 0) {
      if (record->path != NULL ||
          compare_open_file_description(guard, self->anchor_guard_fd) !=
              OFD_SAME ||
          record->device != self->anchor_device ||
          record->inode != self->anchor_inode) {
        errno = ESTALE;
        return -1;
      }
      continue;
    }
    parent = self->scratch[index - 1].guard_fd;
    if (parent < 0 || record->path == NULL ||
        same_identity_at(parent, record->path, record->device, record->inode) <
            0) {
      return -1;
    }
  }
  return 0;
}

static int verify_parent_binding(WorkspaceOwner *self) {
  if (verify_allowed_root_policy(self) < 0 || !self->anchor_identity_known ||
      !self->parent_identity_known ||
      verify_descriptor(self->anchor_fd, self->anchor_guard_fd,
                        self->anchor_device, self->anchor_inode) < 0 ||
      verify_descriptor(self->parent_fd, self->parent_guard_fd,
                        self->parent_device, self->parent_inode) < 0 ||
      verify_absolute_chain(self) < 0 || verify_relative_chain(self) < 0) {
    return -1;
  }
  return 0;
}

static int metadata_matches_directory(const struct stat *metadata, dev_t device,
                                      ino_t inode) {
  return S_ISDIR(metadata->st_mode) && metadata->st_dev == device &&
         metadata->st_ino == inode;
}

static enum workspace_replacement_mapping
classify_replacement_mapping(WorkspaceOwner *self) {
  struct stat destination_metadata;
  struct stat slot_metadata;
  int destination_result;
  int slot_result;

  if (self->parent_guard_fd < 0 || self->destination_name == NULL ||
      self->replacement_slot_name == NULL || !self->root_identity_known ||
      !self->captured_destination_identity_known) {
    return WORKSPACE_REPLACEMENT_MAPPING_UNKNOWN;
  }
  destination_result =
      native_fstatat_retry(self->parent_guard_fd, self->destination_name,
                           &destination_metadata, AT_SYMLINK_NOFOLLOW);
  slot_result =
      native_fstatat_retry(self->parent_guard_fd, self->replacement_slot_name,
                           &slot_metadata, AT_SYMLINK_NOFOLLOW);
  if (destination_result < 0 || slot_result < 0) {
    return WORKSPACE_REPLACEMENT_MAPPING_UNKNOWN;
  }
  if (metadata_matches_directory(&destination_metadata,
                                 self->captured_destination_device,
                                 self->captured_destination_inode) &&
      metadata_matches_directory(&slot_metadata, self->root_device,
                                 self->root_inode)) {
    return WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE;
  }
  if (metadata_matches_directory(&destination_metadata, self->root_device,
                                 self->root_inode) &&
      metadata_matches_directory(&slot_metadata,
                                 self->captured_destination_device,
                                 self->captured_destination_inode)) {
    return WORKSPACE_REPLACEMENT_MAPPING_EXCHANGED;
  }
  return WORKSPACE_REPLACEMENT_MAPPING_UNKNOWN;
}

static int replacement_state_expects_preexchange(WorkspaceOwner *self) {
  return self->namespace_state == WORKSPACE_REPLACEMENT_PROVISIONING ||
         self->namespace_state == WORKSPACE_REPLACEMENT_PROVISIONED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_ADOPTED;
}

static int replacement_state_expects_exchange(WorkspaceOwner *self) {
  return self->namespace_state == WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_RECEIPTED;
}

static int verify_replacement_binding(WorkspaceOwner *self) {
  enum workspace_replacement_mapping mapping;
  Py_ssize_t index;

  if (self->closed || !self->destination_capture_started ||
      !self->namespace_created || !self->replacement_permit_claimed ||
      !self->replacement_lock_active || self->publish_permit_claimed ||
      self->publish_permit_active || self->destination_name == NULL ||
      self->destination_path == NULL || self->replacement_slot_name == NULL ||
      self->plan_digest == NULL || !self->root_identity_known ||
      !self->captured_destination_identity_known || self->root_device == 0 ||
      self->root_inode == 0 || self->captured_destination_device == 0 ||
      self->captured_destination_inode == 0 ||
      self->root_device != self->parent_device ||
      self->captured_destination_device != self->parent_device ||
      verify_replacement_lock_authority(self) < 0 ||
      verify_parent_binding(self) < 0 ||
      verify_descriptor(self->root_fd, self->root_guard_fd, self->root_device,
                        self->root_inode) < 0 ||
      verify_descriptor(self->captured_destination_fd,
                        self->captured_destination_guard_fd,
                        self->captured_destination_device,
                        self->captured_destination_inode) < 0) {
    errno = ESTALE;
    return -1;
  }
  mapping = classify_replacement_mapping(self);
  if ((replacement_state_expects_preexchange(self) &&
       mapping != WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE) ||
      (replacement_state_expects_exchange(self) &&
       mapping != WORKSPACE_REPLACEMENT_MAPPING_EXCHANGED) ||
      (!replacement_state_expects_preexchange(self) &&
       !replacement_state_expects_exchange(self))) {
    errno = ESTALE;
    return -1;
  }
  for (index = 0; index < self->directory_count; ++index) {
    int parent;
    const char *name;
    if (!self->directories[index].identity_known ||
        verify_descriptor(self->directories[index].fd,
                          self->directories[index].guard_fd,
                          self->directories[index].device,
                          self->directories[index].inode) < 0) {
      return -1;
    }
    parent = find_parent_descriptor(self, self->directories[index].path);
    name = path_basename(self->directories[index].path);
    if (parent < 0 ||
        same_identity_at(parent, name, self->directories[index].device,
                         self->directories[index].inode) < 0) {
      return -1;
    }
  }
  return 0;
}

static int verify_captured_destination_structure(WorkspaceOwner *self) {
  if (self->closed || !self->provision_started ||
      !self->destination_capture_started || self->namespace_created ||
      self->publish_permit_claimed || self->publish_permit_active ||
      self->replacement_permit_active > self->replacement_permit_claimed ||
      self->destination_name == NULL || self->destination_path == NULL ||
      !self->captured_destination_identity_known ||
      self->captured_destination_device == 0 ||
      self->captured_destination_inode == 0 || self->root_fd >= 0 ||
      self->root_guard_fd >= 0 || self->root_identity_known ||
      self->directories != NULL || self->directory_count != 0 ||
      self->file_fd >= 0 || self->file_guard_fd >= 0 || self->file_active ||
      self->file_failed || self->file_name != NULL ||
      self->initial_stage_name != NULL || self->stage_name != NULL ||
      self->replacement_slot_name != NULL || self->plan_digest != NULL ||
      self->orphan_name != NULL || self->receipt_generation != 0 ||
      !self->anchor_identity_known || !self->parent_identity_known ||
      !self->replacement_lock_identity_known || self->anchor_device == 0 ||
      self->anchor_inode == 0 || self->parent_device == 0 ||
      self->parent_inode == 0 || self->replacement_lock_device == 0 ||
      self->replacement_lock_inode == 0 ||
      self->parent_device != self->anchor_device ||
      self->replacement_lock_device != self->parent_device ||
      self->replacement_lock_inode != self->parent_inode ||
      self->captured_destination_device != self->parent_device) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_captured_destination_local_authority(WorkspaceOwner *self) {
  if (verify_captured_destination_structure(self) < 0 ||
      verify_descriptor(self->parent_fd, self->parent_guard_fd,
                        self->parent_device, self->parent_inode) < 0 ||
      verify_replacement_lock_authority(self) < 0 ||
      verify_descriptor(self->captured_destination_fd,
                        self->captured_destination_guard_fd,
                        self->captured_destination_device,
                        self->captured_destination_inode) < 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_captured_destination_hidden_authority(WorkspaceOwner *self) {
  if (verify_captured_destination_structure(self) < 0 ||
      verify_hidden_directory(self->parent_guard_fd, self->parent_device,
                              self->parent_inode) < 0 ||
      verify_replacement_lock_authority(self) < 0 ||
      verify_hidden_directory(self->captured_destination_guard_fd,
                              self->captured_destination_device,
                              self->captured_destination_inode) < 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_captured_destination_authority(WorkspaceOwner *self) {
  if (verify_captured_destination_local_authority(self) < 0 ||
      verify_parent_binding(self) < 0 ||
      same_identity_at(self->parent_guard_fd, self->destination_name,
                       self->captured_destination_device,
                       self->captured_destination_inode) < 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_captured_destination_binding(WorkspaceOwner *self) {
  int expects_lease;

  if (self->namespace_state == WORKSPACE_DESTINATION_CAPTURED) {
    expects_lease = 0;
  } else if (self->namespace_state == WORKSPACE_DESTINATION_LEASED) {
    expects_lease = 1;
  } else {
    errno = ESTALE;
    return -1;
  }
  if (!!self->replacement_lock_active != expects_lease ||
      self->namespace_sync_pending ||
      verify_captured_destination_authority(self) < 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_owner_authority(WorkspaceOwner *self) {
  Py_ssize_t index;
  if (self->replacement_slot_name != NULL) {
    return verify_replacement_binding(self);
  }
  if (self->destination_capture_started) {
    return verify_captured_destination_binding(self);
  }
  if (verify_parent_binding(self) < 0 || !self->root_identity_known ||
      verify_descriptor(self->root_fd, self->root_guard_fd, self->root_device,
                        self->root_inode) < 0 ||
      !self->namespace_created || self->stage_name == NULL ||
      same_identity_at(self->parent_guard_fd, self->stage_name,
                       self->root_device, self->root_inode) < 0) {
    return -1;
  }
  for (index = 0; index < self->directory_count; ++index) {
    int parent;
    const char *name;
    if (!self->directories[index].identity_known ||
        verify_descriptor(self->directories[index].fd,
                          self->directories[index].guard_fd,
                          self->directories[index].device,
                          self->directories[index].inode) < 0) {
      return -1;
    }
    parent = find_parent_descriptor(self, self->directories[index].path);
    name = path_basename(self->directories[index].path);
    if (parent < 0 ||
        same_identity_at(parent, name, self->directories[index].device,
                         self->directories[index].inode) < 0) {
      return -1;
    }
  }
  return 0;
}

static PyObject *
WorkspaceOwner_verify_destination_binding(WorkspaceOwner *self,
                                          PyObject *Py_UNUSED(ignored)) {
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (verify_captured_destination_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace destination binding changed");
    return NULL;
  }
  Py_RETURN_NONE;
}

static PyObject *
WorkspaceOwner_verify_replacement_binding(WorkspaceOwner *self,
                                          PyObject *Py_UNUSED(ignored)) {
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if ((self->namespace_state != WORKSPACE_REPLACEMENT_PROVISIONED &&
       self->namespace_state != WORKSPACE_REPLACEMENT_ADOPTED &&
       self->namespace_state != WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED) ||
      verify_replacement_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace replacement binding changed");
    return NULL;
  }
  Py_RETURN_NONE;
}

static PyObject *WorkspaceOwner_verify(WorkspaceOwner *self,
                                       PyObject *Py_UNUSED(ignored)) {
  Py_ssize_t index;
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->closed ||
      self->namespace_state == WORKSPACE_REPLACEMENT_PROVISIONING ||
      verify_owner_authority(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace descriptor authority changed");
    return NULL;
  }
  for (index = 0; index < self->directory_count; ++index) {
    if (!self->directories[index].identity_known ||
        verify_descriptor(self->directories[index].fd,
                          self->directories[index].guard_fd,
                          self->directories[index].device,
                          self->directories[index].inode) < 0) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace directory authority changed");
      return NULL;
    }
  }
  for (index = 0; index < self->scratch_count; ++index) {
    if (self->scratch[index].fd >= 0 &&
        (!self->scratch[index].identity_known ||
         verify_descriptor(
             self->scratch[index].fd, self->scratch[index].guard_fd,
             self->scratch[index].device, self->scratch[index].inode) < 0)) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace anchor-chain authority changed");
      return NULL;
    }
  }
  Py_RETURN_NONE;
}

static int workspace_state_is_provisioned(WorkspaceOwner *self) {
  return self->namespace_state == WORKSPACE_PROVISIONED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_PROVISIONED;
}

static int workspace_state_is_adopted(WorkspaceOwner *self) {
  return self->namespace_state == WORKSPACE_ADOPTED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_ADOPTED;
}

static int workspace_state_is_replacement(WorkspaceOwner *self) {
  return self->replacement_permit_claimed ||
         self->replacement_slot_name != NULL || self->replacement_lock_active ||
         self->namespace_state == WORKSPACE_DESTINATION_LEASED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_PROVISIONING ||
         self->namespace_state == WORKSPACE_REPLACEMENT_PROVISIONED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_ADOPTED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_RECEIPTED ||
         self->namespace_state == WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED;
}

static PyObject *WorkspaceOwner_mark_adopted(WorkspaceOwner *self,
                                             PyObject *Py_UNUSED(ignored)) {
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->closed || !workspace_state_is_provisioned(self) ||
      verify_owner_authority(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner is not provisioned");
    return NULL;
  }
  self->namespace_state =
      self->namespace_state == WORKSPACE_REPLACEMENT_PROVISIONED
          ? WORKSPACE_REPLACEMENT_ADOPTED
          : WORKSPACE_ADOPTED;
  Py_RETURN_NONE;
}

static int exact_bytes_equal(PyObject *value, const char *expected,
                             const char *label) {
  char *observed;
  Py_ssize_t observed_size;
  size_t expected_size;

  if (!PyBytes_CheckExact(value)) {
    PyErr_Format(PyExc_TypeError, "%s must be exact bytes", label);
    return -1;
  }
  if (PyBytes_AsStringAndSize(value, &observed, &observed_size) < 0) {
    return -1;
  }
  expected_size = strlen(expected);
  if (observed_size < 0 || (size_t)observed_size != expected_size ||
      memchr(observed, '\0', (size_t)observed_size) != NULL ||
      memcmp(observed, expected, expected_size) != 0) {
    PyErr_SetString(PyExc_ValueError,
                    "native workspace adoption binding differs");
    return -1;
  }
  return 0;
}

static PyObject *WorkspaceOwner_verify_adoption_binding(WorkspaceOwner *self,
                                                        PyObject *args) {
  PyObject *destination;
  PyObject *stage;
  PyObject *plan_digest;
  const char *candidate_name;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (!PyArg_UnpackTuple(args, "verify_adoption_binding", 3, 3, &destination,
                         &stage, &plan_digest)) {
    return NULL;
  }
  candidate_name = candidate_namespace_name(self);
  if (self->closed || !workspace_state_is_provisioned(self) ||
      self->destination_path == NULL || candidate_name == NULL ||
      self->plan_digest == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner is not provisioned");
    return NULL;
  }
  if (exact_bytes_equal(destination, self->destination_path,
                        "workspace adoption destination") < 0 ||
      exact_bytes_equal(stage, candidate_name, "workspace adoption stage") <
          0 ||
      exact_bytes_equal(plan_digest, self->plan_digest,
                        "workspace adoption plan digest") < 0) {
    return NULL;
  }
  Py_RETURN_NONE;
}

static int exact_file_mode(PyObject *value, const char *label, mode_t *mode) {
  long observed;

  if (!PyLong_CheckExact(value)) {
    PyErr_Format(PyExc_TypeError, "%s must be an exact integer", label);
    return -1;
  }
  observed = PyLong_AsLong(value);
  if (observed == -1 && PyErr_Occurred()) {
    return -1;
  }
  if (observed < 0 || observed > 0777) {
    PyErr_Format(PyExc_ValueError, "%s is outside 0000..0777", label);
    return -1;
  }
  *mode = (mode_t)observed;
  return 0;
}

static int resolve_file_parent(WorkspaceOwner *self, PyObject *value,
                               Py_ssize_t *parent_index, int *parent_descriptor,
                               int *parent_guard_descriptor) {
  char *path;
  Py_ssize_t size;
  Py_ssize_t index;

  if (!PyBytes_CheckExact(value) ||
      PyBytes_AsStringAndSize(value, &path, &size) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_TypeError,
                      "workspace file parent must be exact bytes");
    }
    return -1;
  }
  if (size < 0 || size > CODENIB_MAX_PATH_BYTES ||
      memchr(path, '\0', (size_t)size) != NULL) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace file parent is unbounded or contains NUL");
    return -1;
  }
  if (size == 0) {
    *parent_index = -1;
    *parent_descriptor = self->root_fd;
    *parent_guard_descriptor = self->root_guard_fd;
    return 0;
  }
  if (!valid_relative_path(path)) {
    PyErr_SetString(PyExc_ValueError,
                    "workspace file parent must be normalized relative POSIX");
    return -1;
  }
  index = find_directory_index(self, path, (size_t)size, self->directory_count);
  if (index < 0) {
    PyErr_SetString(PyExc_KeyError,
                    "workspace file parent is absent from its plan");
    return -1;
  }
  *parent_index = index;
  *parent_descriptor = self->directories[index].fd;
  *parent_guard_descriptor = self->directories[index].guard_fd;
  return 0;
}

static int inflight_parent_descriptors(WorkspaceOwner *self,
                                       int *parent_descriptor,
                                       int *parent_guard_descriptor) {
  if (self->file_parent_index == -1) {
    *parent_descriptor = self->root_fd;
    *parent_guard_descriptor = self->root_guard_fd;
    return 0;
  }
  if (self->file_parent_index < 0 ||
      self->file_parent_index >= self->directory_count) {
    errno = ESTALE;
    return -1;
  }
  *parent_descriptor = self->directories[self->file_parent_index].fd;
  *parent_guard_descriptor =
      self->directories[self->file_parent_index].guard_fd;
  return 0;
}

static int verify_inflight_parent(WorkspaceOwner *self, int parent_descriptor,
                                  int parent_guard_descriptor) {
  if (self->file_parent_index == -1) {
    return verify_descriptor(parent_descriptor, parent_guard_descriptor,
                             self->root_device, self->root_inode);
  }
  if (self->file_parent_index < 0 ||
      self->file_parent_index >= self->directory_count) {
    errno = ESTALE;
    return -1;
  }
  return verify_descriptor(parent_descriptor, parent_guard_descriptor,
                           self->directories[self->file_parent_index].device,
                           self->directories[self->file_parent_index].inode);
}

static int same_file_identity_at(int parent, const char *name, dev_t device,
                                 ino_t inode, struct stat *metadata) {
  if (native_fstatat_retry(parent, name, metadata, AT_SYMLINK_NOFOLLOW) < 0) {
    return -1;
  }
  if (!S_ISREG(metadata->st_mode) || metadata->st_dev != device ||
      metadata->st_ino != inode || metadata->st_nlink != 1) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int verify_inflight_file(WorkspaceOwner *self, struct stat *metadata) {
  int parent_descriptor;
  int parent_guard_descriptor;
  int flags;
  struct stat linked_metadata;

  if (!self->file_active || self->file_failed || self->file_fd < 0 ||
      self->file_guard_fd < 0 || !self->file_identity_known ||
      self->file_name == NULL ||
      inflight_parent_descriptors(self, &parent_descriptor,
                                  &parent_guard_descriptor) < 0 ||
      parent_guard_descriptor < 0 ||
      verify_inflight_parent(self, parent_descriptor, parent_guard_descriptor) <
          0 ||
      native_fstat_retry(self->file_guard_fd, metadata) < 0) {
    errno = ESTALE;
    return -1;
  }
  (void)parent_descriptor;
  flags = native_fcntl_getfd_retry(self->file_guard_fd);
  if (compare_open_file_description(self->file_fd, self->file_guard_fd) !=
          OFD_SAME ||
      flags < 0 || (flags & FD_CLOEXEC) == 0 || !S_ISREG(metadata->st_mode) ||
      metadata->st_dev != self->file_device ||
      metadata->st_ino != self->file_inode || metadata->st_nlink != 1 ||
      metadata->st_size != self->file_offset ||
      same_file_identity_at(parent_guard_descriptor, self->file_name,
                            self->file_device, self->file_inode,
                            &linked_metadata) < 0 ||
      linked_metadata.st_size != metadata->st_size) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static PyObject *WorkspaceOwner_begin_file(WorkspaceOwner *self,
                                           PyObject *args) {
  PyObject *parent_object;
  PyObject *name_object;
  PyObject *mode_object;
  char *name = NULL;
  mode_t create_mode;
  Py_ssize_t parent_index;
  int parent_descriptor;
  int parent_guard_descriptor;
  struct stat metadata;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (!PyArg_UnpackTuple(args, "begin_file", 3, 3, &parent_object, &name_object,
                         &mode_object)) {
    return NULL;
  }
  if (self->closed || !workspace_state_is_adopted(self) || self->file_active ||
      self->file_failed || verify_owner_authority(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner cannot begin a file");
    return NULL;
  }
  name = copy_exact_bytes(name_object, "workspace file name",
                          CODENIB_MAX_COMPONENT_BYTES);
  if (name == NULL || !valid_component(name) ||
      exact_file_mode(mode_object, "workspace file create mode", &create_mode) <
          0 ||
      resolve_file_parent(self, parent_object, &parent_index,
                          &parent_descriptor, &parent_guard_descriptor) < 0) {
    if (name != NULL && !valid_component(name) && !PyErr_Occurred()) {
      PyErr_SetString(PyExc_ValueError,
                      "workspace file name must be one safe component");
    }
    free(name);
    return NULL;
  }
  (void)parent_descriptor;
  self->file_name = name;
  self->file_parent_index = parent_index;
  self->file_active = 1;
  self->file_offset = 0;
  self->file_fd = native_open_file_retry(parent_guard_descriptor,
                                         self->file_name, create_mode);
  if (self->file_fd < 0) {
    self->file_failed = 1;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  self->file_guard_fd = duplicate_cloexec(self->file_fd);
  if (self->file_guard_fd < 0) {
    int error = errno;
    discard_uncommitted_pair(&self->file_fd, &self->file_guard_fd, error);
    self->file_failed = 1;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  if (native_fstat_retry(self->file_guard_fd, &metadata) < 0) {
    int error = errno;
    discard_uncommitted_pair(&self->file_fd, &self->file_guard_fd, error);
    self->file_failed = 1;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  if (!S_ISREG(metadata.st_mode) || metadata.st_dev == 0 ||
      metadata.st_ino == 0 || metadata.st_nlink != 1 ||
      metadata.st_dev != self->root_device) {
    self->file_failed = 1;
    errno = ESTALE;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  self->file_device = metadata.st_dev;
  self->file_inode = metadata.st_ino;
  self->file_identity_known = 1;
  if (verify_inflight_file(self, &metadata) < 0) {
    self->file_failed = 1;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  Py_RETURN_NONE;
}

static PyObject *WorkspaceOwner_write_file(WorkspaceOwner *self,
                                           PyObject *payload) {
  char *data;
  Py_ssize_t size;
  Py_ssize_t offset = 0;
  struct stat metadata;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->destination_capture_started && !workspace_state_is_adopted(self)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "captured destination owner cannot write a file");
    return NULL;
  }
  if (!PyBytes_CheckExact(payload) ||
      PyBytes_AsStringAndSize(payload, &data, &size) < 0) {
    if (self->file_active) {
      self->file_failed = 1;
    }
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_TypeError,
                      "workspace file payload must be exact bytes");
    }
    return NULL;
  }
  if (size < 0 || size > CODENIB_MAX_FILE_WRITE_BYTES) {
    if (self->file_active) {
      self->file_failed = 1;
    }
    PyErr_SetString(PyExc_ValueError,
                    "workspace file payload exceeds the native chunk budget");
    return NULL;
  }
  if (self->closed || !workspace_state_is_adopted(self) ||
      verify_inflight_file(self, &metadata) < 0) {
    self->file_failed = 1;
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace file authority changed");
    return NULL;
  }
  if (size > 0 && self->file_offset > (off_t)(LLONG_MAX - size)) {
    self->file_failed = 1;
    PyErr_SetString(PyExc_OverflowError,
                    "native workspace file offset overflowed");
    return NULL;
  }
  while (offset < size) {
    ssize_t written = -1;
    int attempts;
    for (attempts = 0; attempts < 64; ++attempts) {
      written =
          pwrite(self->file_guard_fd, data + offset, (size_t)(size - offset),
                 self->file_offset + (off_t)offset);
      if (written >= 0 || errno != EINTR) {
        break;
      }
    }
    if (written <= 0) {
      if (written == 0) {
        errno = EIO;
      }
      self->file_failed = 1;
      return PyErr_SetFromErrno(PyExc_OSError);
    }
    offset += (Py_ssize_t)written;
  }
  self->file_offset += (off_t)size;
  Py_RETURN_NONE;
}

static PyObject *WorkspaceOwner_finish_file(WorkspaceOwner *self,
                                            PyObject *mode_object) {
  mode_t final_mode;
  struct stat metadata;
  struct stat linked_metadata;
  int parent_descriptor;
  int parent_guard_descriptor;
  int close_error;
  long long modified_ns;
  long long changed_ns;
  PyObject *result;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->destination_capture_started && !workspace_state_is_adopted(self)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "captured destination owner cannot finish a file");
    return NULL;
  }
  if (exact_file_mode(mode_object, "workspace file final mode", &final_mode) <
      0) {
    if (self->file_active) {
      self->file_failed = 1;
    }
    return NULL;
  }
  if (self->closed || !workspace_state_is_adopted(self)) {
    self->file_failed = 1;
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace file is not finishable");
    return NULL;
  }
  if (verify_inflight_file(self, &metadata) < 0 ||
      inflight_parent_descriptors(self, &parent_descriptor,
                                  &parent_guard_descriptor) < 0) {
    self->file_failed = 1;
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace file binding changed");
    return NULL;
  }
  (void)parent_descriptor;
  if (native_fchmod_retry(self->file_guard_fd, final_mode) < 0 ||
      native_fsync_retry(self->file_guard_fd) < 0 ||
      native_fstat_retry(self->file_guard_fd, &metadata) < 0) {
    self->file_failed = 1;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  if (!S_ISREG(metadata.st_mode) || metadata.st_dev != self->file_device ||
      metadata.st_ino != self->file_inode || metadata.st_nlink != 1 ||
      metadata.st_size != self->file_offset ||
      (metadata.st_mode & 0777) != final_mode) {
    self->file_failed = 1;
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace file metadata changed");
    return NULL;
  }
  if (same_file_identity_at(parent_guard_descriptor, self->file_name,
                            self->file_device, self->file_inode,
                            &linked_metadata) < 0 ||
      linked_metadata.st_size != metadata.st_size) {
    self->file_failed = 1;
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace file binding changed");
    return NULL;
  }
  if (native_fsync_retry(parent_guard_descriptor) < 0) {
    self->file_failed = 1;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
#if defined(__linux__)
  modified_ns = ((long long)metadata.st_mtim.tv_sec * 1000000000LL) +
                metadata.st_mtim.tv_nsec;
  changed_ns = ((long long)metadata.st_ctim.tv_sec * 1000000000LL) +
               metadata.st_ctim.tv_nsec;
#else
  modified_ns = (long long)metadata.st_mtime * 1000000000LL;
  changed_ns = (long long)metadata.st_ctime * 1000000000LL;
#endif
  close_error = close_inflight_file(self);
  if (close_error != 0) {
    self->file_failed = 1;
    errno = close_error;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  result = Py_BuildValue(
      "(KKKLLLKK)", (unsigned long long)metadata.st_dev,
      (unsigned long long)metadata.st_ino, (unsigned long long)metadata.st_mode,
      (long long)metadata.st_size, modified_ns, changed_ns,
      (unsigned long long)metadata.st_nlink, (unsigned long long)0);
  if (result == NULL) {
    self->file_failed = 1;
  }
  return result;
}

static PyObject *WorkspaceOwner_abort_file(WorkspaceOwner *self,
                                           PyObject *Py_UNUSED(ignored)) {
  int close_error;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->destination_capture_started && !workspace_state_is_adopted(self)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "captured destination owner cannot abort a file");
    return NULL;
  }
  if (!self->file_active && self->file_fd < 0 && self->file_guard_fd < 0) {
    Py_RETURN_NONE;
  }
  self->file_failed = 1;
  close_error = close_inflight_file(self);
  if (close_error != 0) {
    errno = close_error;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  Py_RETURN_NONE;
}

static PyObject *WorkspaceOwner_seal_directories(WorkspaceOwner *self,
                                                 PyObject *Py_UNUSED(ignored)) {
  Py_ssize_t index;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->closed || !workspace_state_is_adopted(self) || self->file_active ||
      self->file_failed || verify_owner_authority(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace directories are not seal-ready");
    return NULL;
  }
  for (index = self->directory_count; index > 0; --index) {
    if (native_fsync_retry(self->directories[index - 1].guard_fd) < 0) {
      return PyErr_SetFromErrno(PyExc_OSError);
    }
  }
  if (native_fsync_retry(self->root_guard_fd) < 0 ||
      verify_owner_authority(self) < 0) {
    if (errno == ESTALE) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace directory binding changed");
    } else {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    return NULL;
  }
  Py_RETURN_NONE;
}

static PyObject *
WorkspaceOwner_sync_parent_method(WorkspaceOwner *self,
                                  PyObject *Py_UNUSED(ignored)) {
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->destination_capture_started &&
      self->namespace_state != WORKSPACE_REPLACEMENT_PROVISIONED &&
      self->namespace_state != WORKSPACE_REPLACEMENT_ADOPTED) {
    PyErr_SetString(PyExc_RuntimeError,
                    "captured destination owner cannot sync its parent");
    return NULL;
  }
  if (self->closed ||
      (workspace_state_is_replacement(self)
           ? verify_owner_authority(self)
           : verify_parent_binding(self)) < 0 ||
      sync_parent(self) < 0) {
    if (errno == ESTALE) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace parent binding changed");
    } else {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    return NULL;
  }
  self->namespace_sync_pending = 0;
  Py_RETURN_NONE;
}

static int rename_noreplace(int parent, const char *source,
                            const char *destination) {
#if defined(__linux__) && defined(SYS_renameat2)
  return (int)syscall(SYS_renameat2, parent, source, parent, destination,
                      RENAME_NOREPLACE);
#else
  (void)parent;
  (void)source;
  (void)destination;
  errno = ENOSYS;
  return -1;
#endif
}

static int rename_exchange(int parent, const char *left, const char *right) {
#if defined(__linux__) && defined(SYS_renameat2)
  return (int)syscall(SYS_renameat2, parent, left, parent, right,
                      RENAME_EXCHANGE);
#else
  (void)parent;
  (void)left;
  (void)right;
  errno = ENOSYS;
  return -1;
#endif
}

static int rename_owned_root(WorkspaceOwner *self, const char *source,
                             const char *destination,
                             int require_lexical_binding) {
  char *next_name = NULL;
  char *next_orphan = NULL;
  enum workspace_namespace_state next_state;

  if (self->closed ||
      (require_lexical_binding
           ? verify_parent_binding(self)
           : verify_hidden_directory(self->parent_guard_fd, self->parent_device,
                                     self->parent_inode)) < 0 ||
      !valid_component(source) || !valid_component(destination) ||
      strcmp(source, destination) == 0 || self->stage_name == NULL ||
      strcmp(source, self->stage_name) != 0 ||
      self->namespace_state == WORKSPACE_EMPTY ||
      same_identity_at(self->parent_guard_fd, source, self->root_device,
                       self->root_inode) < 0) {
    errno = ESTALE;
    return -1;
  }
  next_name = strdup(destination);
  if (next_name == NULL) {
    errno = ENOMEM;
    return -1;
  }
  if (!require_lexical_binding) {
    next_state = WORKSPACE_QUARANTINED;
    next_orphan = strdup(destination);
    if (next_orphan == NULL) {
      free(next_name);
      errno = ENOMEM;
      return -1;
    }
  } else if (strcmp(destination, self->destination_name) == 0) {
    if (self->namespace_state != WORKSPACE_ADOPTED || self->file_active ||
        self->file_failed || strcmp(source, self->initial_stage_name) != 0) {
      free(next_name);
      errno = EINVAL;
      return -1;
    }
    next_state = WORKSPACE_PUBLISHED_UNRECEIPTED;
  } else {
    free(next_name);
    errno = EINVAL;
    return -1;
  }
  if (rename_noreplace(self->parent_guard_fd, source, destination) < 0) {
    int error = errno;
    free(next_name);
    free(next_orphan);
    errno = error;
    return -1;
  }
  free(self->stage_name);
  self->stage_name = next_name;
  free(self->orphan_name);
  self->orphan_name = next_orphan;
  self->namespace_state = next_state;
  self->namespace_sync_pending = 1;
  return 0;
}

static PyObject *
WorkspacePublishPermit_rename_child_noreplace(WorkspacePublishPermit *permit,
                                              PyObject *args) {
  WorkspaceOwner *self = permit->owner;
  PyObject *source_object;
  PyObject *destination_object;
  char *source = NULL;
  char *destination = NULL;
  int publishes_receipt;
  PyObject *receipt_token = NULL;

  if (self == NULL || getpid() != permit->owner_pid || permit->consumed ||
      !self->publish_permit_claimed || !self->publish_permit_active) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace publish permit is unavailable");
    return NULL;
  }
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (!PyArg_UnpackTuple(args, "rename_child_noreplace", 2, 2, &source_object,
                         &destination_object)) {
    return NULL;
  }
  source = copy_exact_bytes(source_object, "workspace rename source",
                            CODENIB_MAX_COMPONENT_BYTES);
  destination =
      copy_exact_bytes(destination_object, "workspace rename destination",
                       CODENIB_MAX_COMPONENT_BYTES);
  if (source == NULL || destination == NULL) {
    goto error;
  }
  publishes_receipt = self->initial_stage_name != NULL &&
                      self->destination_name != NULL &&
                      strcmp(source, self->initial_stage_name) == 0 &&
                      strcmp(destination, self->destination_name) == 0;
  if (publishes_receipt && self->receipt_generation == UINT64_MAX) {
    PyErr_SetString(PyExc_OverflowError,
                    "native workspace receipt generation exhausted");
    goto error;
  }
  if (rename_owned_root(self, source, destination, 1) < 0) {
    if (errno == ENOMEM) {
      PyErr_NoMemory();
    } else if (errno == ESTALE || errno == EINVAL) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace rename binding changed");
    } else {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    goto error;
  }
  permit->consumed = 1;
  self->publish_permit_active = 0;
  if (sync_parent(self) < 0) {
    PyErr_SetFromErrno(PyExc_OSError);
    goto error;
  }
  self->namespace_sync_pending = 0;
  if (publishes_receipt) {
    ++self->receipt_generation;
    receipt_token = new_workspace_receipt_token(self, self->receipt_generation,
                                                WORKSPACE_RECEIPT_PUBLISH);
    if (receipt_token == NULL) {
      goto error;
    }
  }
  free(source);
  free(destination);
  if (receipt_token != NULL) {
    return receipt_token;
  }
  Py_RETURN_NONE;

error:
  free(source);
  free(destination);
  return NULL;
}

static PyObject *
WorkspaceReplacementPermit_exchange(WorkspaceReplacementPermit *permit,
                                    PyObject *args) {
  WorkspaceOwner *self = permit->owner;
  PyObject *slot_object;
  PyObject *destination_object;
  PyObject *deadline_object;
  char *slot = NULL;
  char *destination = NULL;
  long long deadline_ns;
  uint64_t generation;
  PyObject *receipt_token = NULL;
  int exchange_result;
  int exchange_error;
  enum workspace_replacement_mapping mapping;

  if (self == NULL || permit->consumed || !self->replacement_permit_claimed ||
      !self->replacement_permit_active) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace replacement permit is unavailable");
    return NULL;
  }
  if (getpid() != permit->owner_pid) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner cannot cross a PID boundary");
    return NULL;
  }
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (!PyArg_UnpackTuple(args, "exchange_replacement", 3, 3, &slot_object,
                         &destination_object, &deadline_object)) {
    return NULL;
  }
  slot = copy_exact_bytes(slot_object, "workspace replacement slot",
                          CODENIB_MAX_COMPONENT_BYTES);
  destination =
      copy_exact_bytes(destination_object, "workspace replacement destination",
                       CODENIB_MAX_COMPONENT_BYTES);
  if (slot == NULL || destination == NULL) {
    goto error;
  }
  if (!valid_component(slot) || !valid_component(destination) ||
      self->replacement_slot_name == NULL || self->destination_name == NULL ||
      strcmp(slot, self->replacement_slot_name) != 0 ||
      strcmp(destination, self->destination_name) != 0 ||
      strcmp(slot, destination) == 0) {
    PyErr_SetString(PyExc_ValueError,
                    "native workspace replacement names differ");
    goto error;
  }
  if (!PyLong_CheckExact(deadline_object)) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace exchange deadline must be an exact integer");
    goto error;
  }
  deadline_ns = PyLong_AsLongLong(deadline_object);
  if (deadline_ns == -1 && PyErr_Occurred()) {
    goto error;
  }
  if (deadline_ns <= 0) {
    PyErr_SetString(PyExc_ValueError, "workspace exchange deadline is invalid");
    goto error;
  }
  if (self->closed || self->namespace_state != WORKSPACE_REPLACEMENT_ADOPTED ||
      self->file_active || self->file_failed ||
      !self->replacement_lock_active || verify_replacement_binding(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace replacement is not exchange-ready");
    goto error;
  }
  if (check_deadline(deadline_ns) < 0) {
    goto error;
  }
  if (self->receipt_generation == UINT64_MAX) {
    PyErr_SetString(PyExc_OverflowError,
                    "native workspace receipt generation exhausted");
    goto error;
  }
  generation = self->receipt_generation + 1;
  receipt_token = new_workspace_receipt_token(self, generation,
                                              WORKSPACE_RECEIPT_REPLACEMENT);
  if (receipt_token == NULL) {
    goto error;
  }
  if (check_deadline(deadline_ns) < 0) {
    goto error;
  }

  permit->consumed = 1;
  self->replacement_permit_active = 0;

  exchange_result =
      rename_exchange(self->parent_guard_fd, self->replacement_slot_name,
                      self->destination_name);
  exchange_error = exchange_result < 0 ? (errno == 0 ? EIO : errno) : 0;
  mapping = classify_replacement_mapping(self);
  if (mapping == WORKSPACE_REPLACEMENT_MAPPING_EXCHANGED) {
    self->namespace_state = WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED;
    self->namespace_sync_pending = 1;
    self->receipt_generation = generation;
    if (native_fsync_retry(self->parent_guard_fd) < 0) {
      enter_replacement_recovery(self);
      PyErr_SetFromErrno(PyExc_OSError);
      goto error;
    }
    self->namespace_sync_pending = 0;
    free(slot);
    free(destination);
    return receipt_token;
  }
  if (mapping == WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE) {
    if (exchange_result == 0) {
      self->namespace_sync_pending = 1;
      if (native_fsync_retry(self->parent_guard_fd) < 0) {
        enter_replacement_recovery(self);
        PyErr_SetFromErrno(PyExc_OSError);
        goto error;
      }
      self->namespace_sync_pending = 0;
    }
    if (verify_replacement_binding(self) < 0) {
      enter_replacement_recovery(self);
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace replacement binding changed after "
                      "its exchange attempt");
      goto error;
    }
    if (exchange_result < 0) {
      errno = exchange_error;
      PyErr_SetFromErrno(PyExc_OSError);
    } else {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace exchange reported success without "
                      "swapping its exact roots");
    }
    goto error;
  }
  enter_replacement_recovery(self);
  PyErr_SetString(PyExc_RuntimeError,
                  "native workspace exchange outcome is unknown");

error:
  free(slot);
  free(destination);
  Py_XDECREF(receipt_token);
  return NULL;
}

static PyObject *WorkspaceReceiptToken_commit(WorkspaceReceiptToken *token) {
  WorkspaceOwner *self = token->owner;

  if (self == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace receipt token is unavailable");
    return NULL;
  }
  if (getpid() != token->owner_pid) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner cannot cross a PID boundary");
    return NULL;
  }
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->closed) {
    PyErr_SetString(PyExc_RuntimeError,
                    "closed native workspace owner cannot commit a receipt");
    return NULL;
  }
  if (token->kind == WORKSPACE_RECEIPT_REPLACEMENT) {
    if (token->consumed &&
        self->namespace_state == WORKSPACE_REPLACEMENT_RECEIPTED &&
        token->generation == self->receipt_generation) {
      Py_RETURN_NONE;
    }
    if (token->consumed || token->generation != self->receipt_generation ||
        (self->namespace_state != WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED &&
         self->namespace_state != WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED) ||
        self->file_active || self->file_failed ||
        !self->replacement_lock_active) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace replacement is not receipt-ready");
      return NULL;
    }
    if (self->namespace_sync_pending &&
        native_fsync_retry(self->parent_guard_fd) < 0) {
      enter_replacement_recovery(self);
      return PyErr_SetFromErrno(PyExc_OSError);
    }
    self->namespace_sync_pending = 0;
    if (self->namespace_state == WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED) {
      self->namespace_state = WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED;
    }
    if (verify_replacement_binding(self) < 0) {
      enter_replacement_recovery(self);
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace replacement binding changed");
      return NULL;
    }
    if (release_replacement_lock(self) < 0) {
      enter_replacement_recovery(self);
      return PyErr_SetFromErrno(PyExc_OSError);
    }
    self->namespace_state = WORKSPACE_REPLACEMENT_RECEIPTED;
    token->consumed = 1;
    Py_RETURN_NONE;
  }
  if (token->kind != WORKSPACE_RECEIPT_PUBLISH) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace receipt kind is invalid");
    return NULL;
  }
  if (token->consumed && self->namespace_state == WORKSPACE_RECEIPTED &&
      token->generation == self->receipt_generation) {
    Py_RETURN_NONE;
  }
  if (token->consumed || token->generation != self->receipt_generation ||
      self->namespace_state != WORKSPACE_PUBLISHED_UNRECEIPTED ||
      self->file_active || self->file_failed) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace publication is not receipt-ready");
    return NULL;
  }
  if ((self->namespace_sync_pending && sync_parent(self) < 0) ||
      verify_owner_authority(self) < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace publication binding changed");
    return NULL;
  }
  self->namespace_sync_pending = 0;
  self->namespace_state = WORKSPACE_RECEIPTED;
  token->consumed = 1;
  Py_RETURN_NONE;
}

static int random_bytes(unsigned char *buffer, size_t size) {
#ifdef __linux__
  size_t offset = 0;
  ssize_t result;
  int interruptions = 0;
  while (offset < size) {
    result = getrandom(buffer + offset, size - offset, 0);
    if (result < 0 && errno == EINTR) {
      if (++interruptions >= 64) {
        errno = EINTR;
        return -1;
      }
      continue;
    }
    if (result == 0) {
      errno = EIO;
      return -1;
    }
    if (result < 0) {
      return -1;
    }
    offset += (size_t)result;
  }
  return 0;
#else
  (void)buffer;
  (void)size;
  errno = ENOSYS;
  return -1;
#endif
}

static int ensure_replacement_candidate_authority(WorkspaceOwner *self) {
  enum workspace_replacement_mapping mapping;
  const char *candidate_name;
  dev_t opened_device;
  ino_t opened_inode;

  if (!self->namespace_created || self->replacement_slot_name == NULL) {
    errno = EINVAL;
    return -1;
  }
  if (!self->root_identity_known) {
    /* Once mkdirat has returned, only the identity captured immediately in
       that provisioning frame may authorize cleanup.  Adopting the current
       slot after a later failure could bless a foreign same-name directory. */
    errno = ESTALE;
    return -1;
  }
  if (self->root_fd < 0 && self->root_guard_fd < 0) {
    mapping = classify_replacement_mapping(self);
    if (mapping == WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE) {
      candidate_name = self->replacement_slot_name;
    } else if (mapping == WORKSPACE_REPLACEMENT_MAPPING_EXCHANGED) {
      candidate_name = self->destination_name;
    } else {
      errno = ESTALE;
      return -1;
    }
    self->root_fd = open_directory_at(self->parent_guard_fd, candidate_name);
    if (self->root_fd < 0) {
      return -1;
    }
    self->root_guard_fd = duplicate_cloexec(self->root_fd);
    if (self->root_guard_fd < 0) {
      int error_number = errno;
      discard_uncommitted_pair(&self->root_fd, &self->root_guard_fd,
                               error_number);
      return -1;
    }
  }
  if (record_identity(self->root_fd, &opened_device, &opened_inode, S_IFDIR) <
          0 ||
      opened_device != self->root_device || opened_inode != self->root_inode ||
      opened_device != self->parent_device ||
      verify_descriptor(self->root_fd, self->root_guard_fd, self->root_device,
                        self->root_inode) < 0) {
    errno = ESTALE;
    return -1;
  }
  return 0;
}

static int
verify_failed_replacement_provision_hidden_authority(WorkspaceOwner *self) {
  Py_ssize_t index;

  if (self->closed || !self->provision_started ||
      !self->destination_capture_started || self->namespace_created ||
      (self->namespace_state != WORKSPACE_REPLACEMENT_PROVISIONING &&
       self->namespace_state != WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED) ||
      self->publish_permit_claimed || self->publish_permit_active ||
      !self->replacement_permit_claimed ||
      self->replacement_permit_active > self->replacement_permit_claimed ||
      !self->replacement_lock_active || self->destination_name == NULL ||
      self->destination_path == NULL || self->replacement_slot_name == NULL ||
      self->plan_digest == NULL || !self->captured_destination_identity_known ||
      self->captured_destination_device == 0 ||
      self->captured_destination_inode == 0 || self->root_fd >= 0 ||
      self->root_guard_fd >= 0 || self->root_identity_known ||
      self->file_fd >= 0 || self->file_guard_fd >= 0 || self->file_active ||
      self->file_failed || self->file_name != NULL ||
      self->initial_stage_name != NULL || self->stage_name != NULL ||
      self->orphan_name != NULL || self->receipt_generation != 0 ||
      !self->anchor_identity_known || !self->parent_identity_known ||
      !self->replacement_lock_identity_known || self->anchor_device == 0 ||
      self->anchor_inode == 0 || self->parent_device == 0 ||
      self->parent_inode == 0 || self->replacement_lock_device == 0 ||
      self->replacement_lock_inode == 0 ||
      self->parent_device != self->anchor_device ||
      self->replacement_lock_device != self->parent_device ||
      self->replacement_lock_inode != self->parent_inode ||
      self->captured_destination_device != self->parent_device ||
      (self->directory_count == 0 && self->directories != NULL) ||
      (self->directory_count > 0 && self->directories == NULL) ||
      verify_allowed_root_policy(self) < 0 ||
      verify_hidden_directory(self->parent_guard_fd, self->parent_device,
                              self->parent_inode) < 0 ||
      verify_replacement_lock_authority(self) < 0 ||
      verify_hidden_directory(self->captured_destination_guard_fd,
                              self->captured_destination_device,
                              self->captured_destination_inode) < 0 ||
      same_identity_at(self->parent_guard_fd, self->destination_name,
                       self->captured_destination_device,
                       self->captured_destination_inode) < 0) {
    errno = ESTALE;
    return -1;
  }
  for (index = 0; index < self->directory_count; ++index) {
    if (self->directories[index].path == NULL ||
        self->directories[index].fd >= 0 ||
        self->directories[index].guard_fd >= 0 ||
        self->directories[index].identity_known ||
        self->directories[index].exposed) {
      errno = ESTALE;
      return -1;
    }
  }
  return 0;
}

static int replacement_slot_is_definitely_absent(WorkspaceOwner *self) {
  struct stat metadata;

  if (native_fstatat_retry(self->parent_guard_fd, self->replacement_slot_name,
                           &metadata, AT_SYMLINK_NOFOLLOW) < 0 &&
      errno == ENOENT) {
    return 1;
  }
  return 0;
}

static int settle_replacement_namespace_internal(WorkspaceOwner *self) {
  enum workspace_replacement_mapping mapping;
  int reverse_result;
  int reverse_error;
  int failed_provision_without_candidate = 0;

  if (!workspace_state_is_replacement(self)) {
    return 0;
  }
  if (getpid() != self->owner_pid) {
    errno = EPERM;
    return -1;
  }
  if (self->namespace_state == WORKSPACE_REPLACEMENT_RECEIPTED) {
    if (self->replacement_lock_active) {
      errno = ESTALE;
      return -1;
    }
    return 0;
  }
  if (self->namespace_state == WORKSPACE_QUARANTINED) {
    if (self->replacement_lock_active) {
      errno = ESTALE;
      return -1;
    }
    return 0;
  }
  if (self->namespace_state == WORKSPACE_DESTINATION_CAPTURED &&
      !self->namespace_created && !self->replacement_lock_active) {
    self->replacement_permit_active = 0;
    self->replacement_permit_claimed = 0;
    return 0;
  }
  if (!self->replacement_lock_active) {
    enter_replacement_recovery(self);
    errno = ESTALE;
    return -1;
  }
  if (self->namespace_created) {
    if (verify_parent_binding(self) < 0 ||
        ensure_replacement_candidate_authority(self) < 0 ||
        verify_descriptor(self->captured_destination_fd,
                          self->captured_destination_guard_fd,
                          self->captured_destination_device,
                          self->captured_destination_inode) < 0) {
      enter_replacement_recovery(self);
      errno = ESTALE;
      return -1;
    }
    mapping = classify_replacement_mapping(self);
    if (mapping == WORKSPACE_REPLACEMENT_MAPPING_EXCHANGED) {
      reverse_result =
          rename_exchange(self->parent_guard_fd, self->replacement_slot_name,
                          self->destination_name);
      reverse_error = reverse_result < 0 ? (errno == 0 ? EIO : errno) : 0;
      self->namespace_sync_pending = 1;
      mapping = classify_replacement_mapping(self);
      if (mapping != WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE) {
        enter_replacement_recovery(self);
        errno = mapping == WORKSPACE_REPLACEMENT_MAPPING_EXCHANGED &&
                        reverse_error != 0
                    ? reverse_error
                    : ESTALE;
        return -1;
      }
    } else if (mapping != WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE) {
      enter_replacement_recovery(self);
      errno = ESTALE;
      return -1;
    }
  } else {
    if (self->replacement_slot_name != NULL) {
      if (verify_failed_replacement_provision_hidden_authority(self) < 0 ||
          !replacement_slot_is_definitely_absent(self)) {
        enter_replacement_recovery(self);
        errno = ESTALE;
        return -1;
      }
      self->namespace_sync_pending = 1;
      failed_provision_without_candidate = 1;
    } else if ((self->namespace_state != WORKSPACE_DESTINATION_LEASED &&
                self->namespace_state !=
                    WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED) ||
               verify_captured_destination_hidden_authority(self) < 0) {
      enter_replacement_recovery(self);
      errno = ESTALE;
      return -1;
    }
  }
  if (self->namespace_sync_pending) {
    if (native_fsync_retry(self->parent_guard_fd) < 0) {
      enter_replacement_recovery(self);
      return -1;
    }
    self->namespace_sync_pending = 0;
  }
  if ((self->namespace_created &&
       (verify_parent_binding(self) < 0 ||
        ensure_replacement_candidate_authority(self) < 0 ||
        verify_descriptor(self->captured_destination_fd,
                          self->captured_destination_guard_fd,
                          self->captured_destination_device,
                          self->captured_destination_inode) < 0 ||
        classify_replacement_mapping(self) !=
            WORKSPACE_REPLACEMENT_MAPPING_PREEXCHANGE)) ||
      (!self->namespace_created &&
       ((failed_provision_without_candidate &&
         (verify_failed_replacement_provision_hidden_authority(self) < 0 ||
          !replacement_slot_is_definitely_absent(self))) ||
        (!failed_provision_without_candidate &&
         verify_captured_destination_hidden_authority(self) < 0)))) {
    enter_replacement_recovery(self);
    errno = ESTALE;
    return -1;
  }
  if (self->replacement_lock_active && release_replacement_lock(self) < 0) {
    enter_replacement_recovery(self);
    return -1;
  }
  if (self->namespace_created) {
    self->namespace_state = WORKSPACE_QUARANTINED;
  } else {
    if (failed_provision_without_candidate) {
      free_unprovisioned_replacement_configuration(self);
    }
    self->namespace_state = WORKSPACE_DESTINATION_CAPTURED;
    self->replacement_permit_active = 0;
    self->replacement_permit_claimed = 0;
  }
  return 0;
}

static int quarantine_namespace_internal(WorkspaceOwner *self) {
  static const char hex[] = "0123456789abcdef";
  static const char prefix[] = ".codenib-workspace-orphan-";
  unsigned char entropy[16];
  char candidate[sizeof(prefix) - 1 + 32 + 1];
  size_t index;
  int attempt;
  int close_error;

  if (self->namespace_state == WORKSPACE_QUARANTINED) {
    if (self->namespace_sync_pending && sync_parent(self) < 0) {
      return -1;
    }
    self->namespace_sync_pending = 0;
    return 0;
  }
  if (!self->namespace_created || self->namespace_state == WORKSPACE_EMPTY) {
    return 0;
  }
  if (self->namespace_state == WORKSPACE_RECEIPTED) {
    errno = EPERM;
    return -1;
  }
  if (self->closed) {
    errno = EBADF;
    return -1;
  }
  close_error = close_inflight_file(self);
  if (close_error != 0) {
    errno = close_error;
    return -1;
  }
  if (!self->parent_identity_known ||
      verify_hidden_directory(self->parent_guard_fd, self->parent_device,
                              self->parent_inode) < 0) {
    return -1;
  }
  if ((self->root_device == 0 || self->root_inode == 0) &&
      identify_created_root(self) < 0) {
    return -1;
  }
  memcpy(candidate, prefix, sizeof(prefix) - 1);
  for (attempt = 0; attempt < CODENIB_ORPHAN_ATTEMPTS; ++attempt) {
    if (random_bytes(entropy, sizeof(entropy)) < 0) {
      return -1;
    }
    for (index = 0; index < sizeof(entropy); ++index) {
      candidate[sizeof(prefix) - 1 + (index * 2)] = hex[entropy[index] >> 4];
      candidate[sizeof(prefix) - 1 + (index * 2) + 1] =
          hex[entropy[index] & 0x0f];
    }
    candidate[sizeof(candidate) - 1] = '\0';
    if (rename_owned_root(self, self->stage_name, candidate, 0) == 0) {
      if (sync_parent(self) < 0) {
        return -1;
      }
      self->namespace_sync_pending = 0;
      return 0;
    }
    if (errno != EEXIST) {
      return -1;
    }
  }
  errno = EEXIST;
  return -1;
}

static PyObject *WorkspaceOwner_quarantine(WorkspaceOwner *self,
                                           PyObject *Py_UNUSED(ignored)) {
  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->destination_capture_started) {
    PyErr_SetString(PyExc_RuntimeError,
                    "captured destination owner cannot be quarantined");
    return NULL;
  }
  if (quarantine_namespace_internal(self) < 0) {
    if (errno == ENOMEM) {
      PyErr_NoMemory();
    } else if (errno == EPERM) {
      PyErr_SetString(PyExc_RuntimeError,
                      "receipted workspace cannot be quarantined");
    } else if (errno == EEXIST) {
      PyErr_SetString(PyExc_FileExistsError,
                      "native workspace quarantine names were exhausted");
    } else {
      PyErr_SetFromErrno(PyExc_OSError);
    }
    return NULL;
  }
  if (self->orphan_name == NULL) {
    Py_RETURN_NONE;
  }
  return PyUnicode_FromString(self->orphan_name);
}

static PyObject *WorkspaceOwner_close(WorkspaceOwner *self,
                                      PyObject *Py_UNUSED(ignored)) {
  int owner_changed = getpid() != self->owner_pid;
  int close_error;

  if (self->closed) {
    if (owner_changed) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace owner cannot cross a PID boundary");
      return NULL;
    }
    Py_RETURN_NONE;
  }
  if (!owner_changed && workspace_state_is_replacement(self)) {
    if (settle_replacement_namespace_internal(self) < 0) {
      return PyErr_SetFromErrno(PyExc_OSError);
    }
  } else if (!owner_changed && self->namespace_created &&
             self->namespace_state != WORKSPACE_RECEIPTED) {
    if (quarantine_namespace_internal(self) < 0) {
      if (errno == ENOMEM) {
        PyErr_NoMemory();
      } else {
        PyErr_SetFromErrno(PyExc_OSError);
      }
      return NULL;
    }
  }
  close_error = close_all(self);
  if (owner_changed) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner cannot cross a PID boundary");
    return NULL;
  }
  if (close_error != 0) {
    errno = close_error;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  Py_RETURN_NONE;
}

static PyObject *WorkspaceOwner_abort(WorkspaceOwner *self,
                                      PyObject *Py_UNUSED(ignored)) {
  int close_error;

  if (require_owner_pid(self) < 0) {
    return NULL;
  }
  if (self->closed) {
    Py_RETURN_NONE;
  }
  if (workspace_state_is_replacement(self)) {
    if (settle_replacement_namespace_internal(self) < 0) {
      return PyErr_SetFromErrno(PyExc_OSError);
    }
  } else if (self->namespace_created &&
             self->namespace_state != WORKSPACE_RECEIPTED) {
    if (quarantine_namespace_internal(self) < 0) {
      if (errno == ENOMEM) {
        PyErr_NoMemory();
      } else {
        PyErr_SetFromErrno(PyExc_OSError);
      }
      return NULL;
    }
  }
  close_error = close_all(self);
  if (close_error != 0) {
    errno = close_error;
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  Py_RETURN_NONE;
}

static PyObject *WorkspaceOwner_directory_descriptor(WorkspaceOwner *self,
                                                     PyObject *argument) {
  char *path = NULL;
  Py_ssize_t size;
  Py_ssize_t index;

  if (require_owner_pid(self) < 0 || self->closed ||
      (self->destination_capture_started &&
       !workspace_state_is_provisioned(self) &&
       !workspace_state_is_adopted(self)) ||
      verify_owner_authority(self) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError, "native workspace owner is closed");
    }
    return NULL;
  }
  if (!PyBytes_CheckExact(argument) ||
      PyBytes_AsStringAndSize(argument, &path, &size) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_TypeError,
                      "workspace directory path must be exact bytes");
    }
    return NULL;
  }
  if (size <= 0 || size > CODENIB_MAX_PATH_BYTES ||
      memchr(path, '\0', (size_t)size) != NULL) {
    PyErr_SetString(PyExc_ValueError, "workspace directory path is invalid");
    return NULL;
  }
  index = find_directory_index(self, path, (size_t)size, self->directory_count);
  if (index >= 0) {
    self->directories[index].exposed = 1;
    return PyLong_FromLong(self->directories[index].fd);
  }
  PyErr_SetString(PyExc_KeyError,
                  "workspace directory descriptor is not owned");
  return NULL;
}

static PyObject *WorkspaceOwner_get_parent_descriptor(WorkspaceOwner *self,
                                                      void *closure) {
  (void)closure;
  if (require_owner_pid(self) < 0 || self->closed ||
      (self->destination_capture_started &&
       self->namespace_state != WORKSPACE_DESTINATION_CAPTURED &&
       self->namespace_state != WORKSPACE_DESTINATION_LEASED &&
       self->namespace_state != WORKSPACE_REPLACEMENT_PROVISIONED &&
       self->namespace_state != WORKSPACE_REPLACEMENT_ADOPTED) ||
      self->parent_fd < 0 ||
      (self->destination_capture_started ? verify_owner_authority(self)
                                         : verify_parent_binding(self)) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace parent descriptor is unavailable");
    }
    return NULL;
  }
  self->parent_exposed = 1;
  return PyLong_FromLong(self->parent_fd);
}

static PyObject *WorkspaceOwner_get_root_descriptor(WorkspaceOwner *self,
                                                    void *closure) {
  (void)closure;
  if (require_owner_pid(self) < 0 || self->closed ||
      (workspace_state_is_replacement(self) &&
       !workspace_state_is_provisioned(self) &&
       !workspace_state_is_adopted(self)) ||
      self->root_fd < 0 || verify_owner_authority(self) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace root descriptor is unavailable");
    }
    return NULL;
  }
  self->root_exposed = 1;
  return PyLong_FromLong(self->root_fd);
}

static PyObject *WorkspaceOwner_get_destination_descriptor(WorkspaceOwner *self,
                                                           void *closure) {
  (void)closure;
  if (require_owner_pid(self) < 0 ||
      verify_captured_destination_binding(self) < 0) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
                      "native workspace destination descriptor is unavailable");
    }
    return NULL;
  }
  self->captured_destination_exposed = 1;
  return PyLong_FromLong(self->captured_destination_fd);
}

static PyObject *WorkspaceOwner_get_closed(WorkspaceOwner *self,
                                           void *closure) {
  (void)closure;
  return PyBool_FromLong(self->closed);
}

static PyObject *WorkspaceOwner_get_state(WorkspaceOwner *self, void *closure) {
  const char *state;
  (void)closure;
  if (self->closed) {
    state = "closed";
  } else {
    switch (self->namespace_state) {
    case WORKSPACE_EMPTY:
      state = "empty";
      break;
    case WORKSPACE_PROVISIONING:
      state = "provisioning";
      break;
    case WORKSPACE_PROVISIONED:
      state = "provisioned";
      break;
    case WORKSPACE_ADOPTED:
      state = "adopted";
      break;
    case WORKSPACE_PUBLISHED_UNRECEIPTED:
      state = "published-unreceipted";
      break;
    case WORKSPACE_RECEIPTED:
      state = "receipted";
      break;
    case WORKSPACE_QUARANTINED:
      state = "quarantined";
      break;
    case WORKSPACE_DESTINATION_CAPTURED:
      state = "destination-captured";
      break;
    case WORKSPACE_DESTINATION_LEASED:
      state = "destination-leased";
      break;
    case WORKSPACE_REPLACEMENT_PROVISIONING:
      state = "replacement-provisioning";
      break;
    case WORKSPACE_REPLACEMENT_PROVISIONED:
      state = "replacement-provisioned";
      break;
    case WORKSPACE_REPLACEMENT_ADOPTED:
      state = "replacement-adopted";
      break;
    case WORKSPACE_REPLACEMENT_EXCHANGED_UNRECEIPTED:
      state = "replacement-exchanged-unreceipted";
      break;
    case WORKSPACE_REPLACEMENT_RECEIPTED:
      state = "replacement-receipted";
      break;
    case WORKSPACE_REPLACEMENT_RECOVERY_REQUIRED:
      state = "replacement-recovery-required";
      break;
    default:
      state = "invalid";
      break;
    }
  }
  return PyUnicode_FromString(state);
}

static PyType_Slot WorkspaceOwner_slots[] = {
    {Py_tp_new, WorkspaceOwner_new},
    {Py_tp_dealloc, WorkspaceOwner_dealloc},
    {Py_tp_doc, "Opaque native owner for one strict workspace skeleton."},
    {0, NULL},
};

static PyType_Spec WorkspaceOwner_spec = {
    "codenib._workspace_owner_impl.WorkspaceOwner", sizeof(WorkspaceOwner), 0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,  WorkspaceOwner_slots,
};

static PyObject *WorkspaceOwnerTypeObject = NULL;

static PyObject *WorkspacePublishPermit_new(PyTypeObject *type, PyObject *args,
                                            PyObject *keywords) {
  (void)type;
  (void)args;
  (void)keywords;
  PyErr_SetString(PyExc_TypeError,
                  "native workspace publish permits cannot be constructed");
  return NULL;
}

static void WorkspacePublishPermit_dealloc(WorkspacePublishPermit *self) {
  freefunc release;
  WorkspaceOwner *owner = self->owner;

  self->owner = NULL;
  if (owner != NULL) {
    if (!self->consumed && getpid() == self->owner_pid) {
      owner->publish_permit_active = 0;
    }
    Py_DECREF((PyObject *)owner);
  }
  release = (freefunc)PyType_GetSlot(Py_TYPE(self), Py_tp_free);
  if (release != NULL) {
    release((PyObject *)self);
  }
}

static PyType_Slot WorkspacePublishPermit_slots[] = {
    {Py_tp_new, WorkspacePublishPermit_new},
    {Py_tp_dealloc, WorkspacePublishPermit_dealloc},
    {Py_tp_doc,
     "Private one-shot capability for one native workspace publication."},
    {0, NULL},
};

static PyType_Spec WorkspacePublishPermit_spec = {
    "codenib._workspace_owner_impl._WorkspacePublishPermit",
    sizeof(WorkspacePublishPermit),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,
    WorkspacePublishPermit_slots,
};

static PyObject *new_workspace_publish_permit(WorkspaceOwner *owner) {
  allocfunc allocate;
  WorkspacePublishPermit *permit;

  if (WorkspacePublishPermitTypeObject == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace publish permit type is unavailable");
    return NULL;
  }
  allocate = (allocfunc)PyType_GetSlot(
      (PyTypeObject *)WorkspacePublishPermitTypeObject, Py_tp_alloc);
  if (allocate == NULL) {
    return NULL;
  }
  permit = (WorkspacePublishPermit *)allocate(
      (PyTypeObject *)WorkspacePublishPermitTypeObject, 0);
  if (permit == NULL) {
    return NULL;
  }
  Py_INCREF((PyObject *)owner);
  permit->owner = owner;
  permit->owner_pid = owner->owner_pid;
  permit->consumed = 0;
  return (PyObject *)permit;
}

static WorkspacePublishPermit *
require_workspace_publish_permit_exact(PyObject *candidate) {
  if (WorkspacePublishPermitTypeObject == NULL ||
      Py_TYPE(candidate) != (PyTypeObject *)WorkspacePublishPermitTypeObject) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace publish capability has an invalid native type");
    return NULL;
  }
  return (WorkspacePublishPermit *)candidate;
}

static PyObject *WorkspaceReplacementPermit_new(PyTypeObject *type,
                                                PyObject *args,
                                                PyObject *keywords) {
  (void)type;
  (void)args;
  (void)keywords;
  PyErr_SetString(PyExc_TypeError,
                  "native workspace replacement permits cannot be constructed");
  return NULL;
}

static void
WorkspaceReplacementPermit_dealloc(WorkspaceReplacementPermit *self) {
  freefunc release;
  WorkspaceOwner *owner = self->owner;

  self->owner = NULL;
  if (owner != NULL) {
    if (!self->consumed && getpid() == self->owner_pid) {
      owner->replacement_permit_active = 0;
    }
    Py_DECREF((PyObject *)owner);
  }
  release = (freefunc)PyType_GetSlot(Py_TYPE(self), Py_tp_free);
  if (release != NULL) {
    release((PyObject *)self);
  }
}

static PyType_Slot WorkspaceReplacementPermit_slots[] = {
    {Py_tp_new, WorkspaceReplacementPermit_new},
    {Py_tp_dealloc, WorkspaceReplacementPermit_dealloc},
    {Py_tp_doc,
     "Private one-shot capability for one native workspace replacement."},
    {0, NULL},
};

static PyType_Spec WorkspaceReplacementPermit_spec = {
    "codenib._workspace_owner_impl._WorkspaceReplacementPermit",
    sizeof(WorkspaceReplacementPermit),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,
    WorkspaceReplacementPermit_slots,
};

static PyObject *new_workspace_replacement_permit(WorkspaceOwner *owner) {
  allocfunc allocate;
  WorkspaceReplacementPermit *permit;

  if (WorkspaceReplacementPermitTypeObject == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace replacement permit type is unavailable");
    return NULL;
  }
  allocate = (allocfunc)PyType_GetSlot(
      (PyTypeObject *)WorkspaceReplacementPermitTypeObject, Py_tp_alloc);
  if (allocate == NULL) {
    return NULL;
  }
  permit = (WorkspaceReplacementPermit *)allocate(
      (PyTypeObject *)WorkspaceReplacementPermitTypeObject, 0);
  if (permit == NULL) {
    return NULL;
  }
  Py_INCREF((PyObject *)owner);
  permit->owner = owner;
  permit->owner_pid = owner->owner_pid;
  permit->consumed = 0;
  return (PyObject *)permit;
}

static WorkspaceReplacementPermit *
require_workspace_replacement_permit_exact(PyObject *candidate) {
  if (WorkspaceReplacementPermitTypeObject == NULL ||
      Py_TYPE(candidate) !=
          (PyTypeObject *)WorkspaceReplacementPermitTypeObject) {
    PyErr_SetString(
        PyExc_TypeError,
        "workspace replacement capability has an invalid native type");
    return NULL;
  }
  return (WorkspaceReplacementPermit *)candidate;
}

static PyObject *WorkspaceReceiptToken_new(PyTypeObject *type, PyObject *args,
                                           PyObject *keywords) {
  (void)type;
  (void)args;
  (void)keywords;
  PyErr_SetString(PyExc_TypeError,
                  "native workspace receipt tokens cannot be constructed");
  return NULL;
}

static void WorkspaceReceiptToken_dealloc(WorkspaceReceiptToken *self) {
  freefunc release;
  WorkspaceOwner *owner = self->owner;

  self->owner = NULL;
  if (owner != NULL) {
    Py_DECREF((PyObject *)owner);
  }
  release = (freefunc)PyType_GetSlot(Py_TYPE(self), Py_tp_free);
  if (release != NULL) {
    release((PyObject *)self);
  }
}

static PyType_Slot WorkspaceReceiptToken_slots[] = {
    {Py_tp_new, WorkspaceReceiptToken_new},
    {Py_tp_dealloc, WorkspaceReceiptToken_dealloc},
    {Py_tp_doc,
     "Private single-owner capability for one native publication receipt."},
    {0, NULL},
};

static PyType_Spec WorkspaceReceiptToken_spec = {
    "codenib._workspace_owner_impl._WorkspaceReceiptToken",
    sizeof(WorkspaceReceiptToken),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,
    WorkspaceReceiptToken_slots,
};

static PyObject *new_workspace_receipt_token(WorkspaceOwner *owner,
                                             uint64_t generation,
                                             enum workspace_receipt_kind kind) {
  allocfunc allocate;
  WorkspaceReceiptToken *token;

  if (WorkspaceReceiptTokenTypeObject == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace receipt token type is unavailable");
    return NULL;
  }
  allocate = (allocfunc)PyType_GetSlot(
      (PyTypeObject *)WorkspaceReceiptTokenTypeObject, Py_tp_alloc);
  if (allocate == NULL) {
    return NULL;
  }
  token = (WorkspaceReceiptToken *)allocate(
      (PyTypeObject *)WorkspaceReceiptTokenTypeObject, 0);
  if (token == NULL) {
    return NULL;
  }
  Py_INCREF((PyObject *)owner);
  token->owner = owner;
  token->owner_pid = owner->owner_pid;
  token->generation = generation;
  token->kind = kind;
  token->consumed = 0;
  return (PyObject *)token;
}

static WorkspaceReceiptToken *
require_workspace_receipt_token_exact(PyObject *candidate) {
  if (WorkspaceReceiptTokenTypeObject == NULL ||
      Py_TYPE(candidate) != (PyTypeObject *)WorkspaceReceiptTokenTypeObject) {
    PyErr_SetString(PyExc_TypeError,
                    "workspace receipt capability has an invalid native type");
    return NULL;
  }
  return (WorkspaceReceiptToken *)candidate;
}

static WorkspaceOwner *require_workspace_owner_exact(PyObject *candidate) {
  if (WorkspaceOwnerTypeObject == NULL ||
      Py_TYPE(candidate) != (PyTypeObject *)WorkspaceOwnerTypeObject) {
    PyErr_SetString(PyExc_TypeError,
                    "provisioned workspace owner has an invalid native type");
    return NULL;
  }
  return (WorkspaceOwner *)candidate;
}

typedef PyObject *(*owner_varargs_function)(WorkspaceOwner *, PyObject *);
typedef PyObject *(*owner_argument_function)(WorkspaceOwner *, PyObject *);
typedef PyObject *(*owner_noargs_function)(WorkspaceOwner *, PyObject *);

static PyObject *dispatch_owner_varargs_exact(PyObject *args,
                                              Py_ssize_t expected_size,
                                              const char *label,
                                              owner_varargs_function function) {
  WorkspaceOwner *owner;
  PyObject *tail;
  PyObject *result;

  if (!PyTuple_CheckExact(args) || PyTuple_Size(args) != expected_size) {
    PyErr_Format(PyExc_TypeError, "%s requires exactly %zd arguments", label,
                 expected_size);
    return NULL;
  }
  owner = require_workspace_owner_exact(PyTuple_GetItem(args, 0));
  if (owner == NULL) {
    return NULL;
  }
  tail = PyTuple_GetSlice(args, 1, expected_size);
  if (tail == NULL) {
    return NULL;
  }
  result = function(owner, tail);
  Py_DECREF(tail);
  return result;
}

static PyObject *
dispatch_owner_argument_exact(PyObject *args, const char *label,
                              owner_argument_function function) {
  WorkspaceOwner *owner;
  PyObject *argument;

  if (!PyTuple_CheckExact(args) || PyTuple_Size(args) != 2) {
    PyErr_Format(PyExc_TypeError, "%s requires exactly 2 arguments", label);
    return NULL;
  }
  owner = require_workspace_owner_exact(PyTuple_GetItem(args, 0));
  argument = PyTuple_GetItem(args, 1);
  if (owner == NULL || argument == NULL) {
    return NULL;
  }
  return function(owner, argument);
}

static PyObject *dispatch_owner_noargs_exact(PyObject *candidate,
                                             owner_noargs_function function) {
  WorkspaceOwner *owner = require_workspace_owner_exact(candidate);
  if (owner == NULL) {
    return NULL;
  }
  return function(owner, NULL);
}

static PyObject *module_provision_owner_exact(PyObject *module,
                                              PyObject *args) {
  (void)module;
  return dispatch_owner_varargs_exact(args, 8, "provision_owner_exact",
                                      WorkspaceOwner_provision);
}

static PyObject *module_capture_owner_destination_exact(PyObject *module,
                                                        PyObject *args) {
  (void)module;
  return dispatch_owner_varargs_exact(args, 4,
                                      "capture_owner_destination_exact",
                                      WorkspaceOwner_capture_destination);
}

static PyObject *module_acquire_owner_replacement_lease_exact(PyObject *module,
                                                              PyObject *args) {
  (void)module;
  return dispatch_owner_argument_exact(
      args, "acquire_owner_replacement_lease_exact",
      WorkspaceOwner_acquire_replacement_lease);
}

static PyObject *module_provision_owner_replacement_exact(PyObject *module,
                                                          PyObject *args) {
  (void)module;
  return dispatch_owner_varargs_exact(args, 6,
                                      "provision_owner_replacement_exact",
                                      WorkspaceOwner_provision_replacement);
}

static PyObject *module_claim_owner_publish_permit_exact(PyObject *module,
                                                         PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner,
                                     WorkspaceOwner_claim_publish_permit);
}

static PyObject *module_claim_owner_replacement_permit_exact(PyObject *module,
                                                             PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner,
                                     WorkspaceOwner_claim_replacement_permit);
}

static PyObject *module_verify_owner_authority_exact(PyObject *module,
                                                     PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner, WorkspaceOwner_verify);
}

static PyObject *
module_verify_owner_destination_binding_exact(PyObject *module,
                                              PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner,
                                     WorkspaceOwner_verify_destination_binding);
}

static PyObject *
module_verify_owner_replacement_binding_exact(PyObject *module,
                                              PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner,
                                     WorkspaceOwner_verify_replacement_binding);
}

static PyObject *module_verify_owner_adoption_binding_exact(PyObject *module,
                                                            PyObject *args) {
  (void)module;
  return dispatch_owner_varargs_exact(args, 4,
                                      "verify_owner_adoption_binding_exact",
                                      WorkspaceOwner_verify_adoption_binding);
}

static PyObject *module_borrow_owner_parent_descriptor_exact(PyObject *module,
                                                             PyObject *owner) {
  WorkspaceOwner *exact_owner;
  (void)module;
  exact_owner = require_workspace_owner_exact(owner);
  if (exact_owner == NULL) {
    return NULL;
  }
  return WorkspaceOwner_get_parent_descriptor(exact_owner, NULL);
}

static PyObject *module_borrow_owner_root_descriptor_exact(PyObject *module,
                                                           PyObject *owner) {
  WorkspaceOwner *exact_owner;
  (void)module;
  exact_owner = require_workspace_owner_exact(owner);
  if (exact_owner == NULL) {
    return NULL;
  }
  return WorkspaceOwner_get_root_descriptor(exact_owner, NULL);
}

static PyObject *
module_borrow_owner_destination_descriptor_exact(PyObject *module,
                                                 PyObject *owner) {
  WorkspaceOwner *exact_owner;
  (void)module;
  exact_owner = require_workspace_owner_exact(owner);
  if (exact_owner == NULL) {
    return NULL;
  }
  return WorkspaceOwner_get_destination_descriptor(exact_owner, NULL);
}

static PyObject *
module_borrow_owner_directory_descriptor_exact(PyObject *module,
                                               PyObject *args) {
  (void)module;
  return dispatch_owner_argument_exact(
      args, "borrow_owner_directory_descriptor_exact",
      WorkspaceOwner_directory_descriptor);
}

static PyObject *module_begin_owner_file_exact(PyObject *module,
                                               PyObject *args) {
  (void)module;
  return dispatch_owner_varargs_exact(args, 4, "begin_owner_file_exact",
                                      WorkspaceOwner_begin_file);
}

static PyObject *module_write_owner_file_exact(PyObject *module,
                                               PyObject *args) {
  (void)module;
  return dispatch_owner_argument_exact(args, "write_owner_file_exact",
                                       WorkspaceOwner_write_file);
}

static PyObject *module_finish_owner_file_exact(PyObject *module,
                                                PyObject *args) {
  (void)module;
  return dispatch_owner_argument_exact(args, "finish_owner_file_exact",
                                       WorkspaceOwner_finish_file);
}

static PyObject *module_abort_owner_file_exact(PyObject *module,
                                               PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner, WorkspaceOwner_abort_file);
}

static PyObject *module_seal_owner_directories_exact(PyObject *module,
                                                     PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner, WorkspaceOwner_seal_directories);
}

static PyObject *module_sync_owner_parent_exact(PyObject *module,
                                                PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner, WorkspaceOwner_sync_parent_method);
}

static PyObject *module_mark_owner_adopted_exact(PyObject *module,
                                                 PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner, WorkspaceOwner_mark_adopted);
}

static PyObject *module_rename_owner_child_noreplace_exact(PyObject *module,
                                                           PyObject *args) {
  WorkspacePublishPermit *permit;
  PyObject *tail;
  PyObject *result;
  (void)module;
  if (!PyTuple_CheckExact(args) || PyTuple_Size(args) != 3) {
    PyErr_SetString(
        PyExc_TypeError,
        "rename_owner_child_noreplace_exact requires exactly 3 arguments");
    return NULL;
  }
  permit = require_workspace_publish_permit_exact(PyTuple_GetItem(args, 0));
  if (permit == NULL) {
    return NULL;
  }
  tail = PyTuple_GetSlice(args, 1, 3);
  if (tail == NULL) {
    return NULL;
  }
  result = WorkspacePublishPermit_rename_child_noreplace(permit, tail);
  Py_DECREF(tail);
  return result;
}

static PyObject *module_exchange_owner_replacement_exact(PyObject *module,
                                                         PyObject *args) {
  WorkspaceReplacementPermit *permit;
  PyObject *tail;
  PyObject *result;
  (void)module;
  if (!PyTuple_CheckExact(args) || PyTuple_Size(args) != 4) {
    PyErr_SetString(
        PyExc_TypeError,
        "exchange_owner_replacement_exact requires exactly 4 arguments");
    return NULL;
  }
  permit = require_workspace_replacement_permit_exact(PyTuple_GetItem(args, 0));
  if (permit == NULL) {
    return NULL;
  }
  tail = PyTuple_GetSlice(args, 1, 4);
  if (tail == NULL) {
    return NULL;
  }
  result = WorkspaceReplacementPermit_exchange(permit, tail);
  Py_DECREF(tail);
  return result;
}

static PyObject *module_commit_owner_receipt_exact(PyObject *module,
                                                   PyObject *candidate) {
  WorkspaceReceiptToken *token;
  (void)module;
  token = require_workspace_receipt_token_exact(candidate);
  if (token == NULL) {
    return NULL;
  }
  return WorkspaceReceiptToken_commit(token);
}

static PyObject *module_abort_owner_exact(PyObject *module, PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner, WorkspaceOwner_abort);
}

static PyObject *module_quarantine_owner_exact(PyObject *module,
                                               PyObject *owner) {
  (void)module;
  return dispatch_owner_noargs_exact(owner, WorkspaceOwner_quarantine);
}

static PyObject *module_owner_state_exact(PyObject *module, PyObject *owner) {
  WorkspaceOwner *exact_owner;
  (void)module;
  exact_owner = require_workspace_owner_exact(owner);
  if (exact_owner == NULL) {
    return NULL;
  }
  return WorkspaceOwner_get_state(exact_owner, NULL);
}

static PyObject *module_owner_closed_exact(PyObject *module, PyObject *owner) {
  WorkspaceOwner *exact_owner;
  (void)module;
  exact_owner = require_workspace_owner_exact(owner);
  if (exact_owner == NULL) {
    return NULL;
  }
  return WorkspaceOwner_get_closed(exact_owner, NULL);
}

static PyObject *module_close_owner_exact(PyObject *module, PyObject *owner) {
  WorkspaceOwner *exact_owner;
  (void)module;
  exact_owner = require_workspace_owner_exact(owner);
  if (exact_owner == NULL) {
    return NULL;
  }
  return WorkspaceOwner_close(exact_owner, NULL);
}

static PyObject *module_create_owner_exact(PyObject *module,
                                           PyObject *Py_UNUSED(ignored)) {
  (void)module;
  if (WorkspaceOwnerTypeObject == NULL ||
      WorkspacePublishPermitTypeObject == NULL ||
      WorkspaceReplacementPermitTypeObject == NULL ||
      WorkspaceReceiptTokenTypeObject == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner type is unavailable");
    return NULL;
  }
  return PyObject_CallObject(WorkspaceOwnerTypeObject, NULL);
}

static PyObject *module_require_owner_exact(PyObject *module, PyObject *owner) {
  (void)module;
  if (require_workspace_owner_exact(owner) == NULL) {
    return NULL;
  }
  Py_INCREF(owner);
  return owner;
}

#define CODENIB_WORKSPACE_OWNER_PROTOCOL_VERSION 5

static PyObject *module_require_support(PyObject *module,
                                        PyObject *Py_UNUSED(ignored)) {
  (void)module;
  if (WorkspaceOwnerTypeObject == NULL ||
      WorkspacePublishPermitTypeObject == NULL ||
      WorkspaceReplacementPermitTypeObject == NULL ||
      WorkspaceReceiptTokenTypeObject == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native workspace owner ABI is incomplete");
    return NULL;
  }
  if (require_linux() < 0) {
    return NULL;
  }
#if !defined(SYS_renameat2) || !defined(SYS_fstat) ||                          \
    !defined(SYS_newfstatat) || !defined(SYS_kcmp) || !defined(SYS_close)
  PyErr_SetString(PyExc_RuntimeError,
                  "native strict workspace provisioning requires Linux "
                  "descriptor and OFD-comparison syscalls");
  return NULL;
#endif
  if (probe_kcmp_support() < 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "native strict workspace provisioning requires permitted "
                    "same-process kcmp OFD comparison");
    return NULL;
  }
  Py_RETURN_NONE;
}

static PyMethodDef workspace_module_methods[] = {
    {"require_support", (PyCFunction)module_require_support, METH_NOARGS,
     "Require native Linux workspace ownership support."},
    {"close_owner_exact", (PyCFunction)module_close_owner_exact, METH_O,
     "Close an exact native owner without instance method dispatch."},
    {"create_owner_exact", (PyCFunction)module_create_owner_exact, METH_NOARGS,
     "Create the internally pinned exact native owner type."},
    {"require_owner_exact", (PyCFunction)module_require_owner_exact, METH_O,
     "Authenticate against the internally pinned native owner type."},
    {"provision_owner_exact", (PyCFunction)module_provision_owner_exact,
     METH_VARARGS, "Provision through exact native-owner dispatch."},
    {"capture_owner_destination_exact",
     (PyCFunction)module_capture_owner_destination_exact, METH_VARARGS,
     "Capture one existing destination through exact native-owner dispatch."},
    {"acquire_owner_replacement_lease_exact",
     (PyCFunction)module_acquire_owner_replacement_lease_exact, METH_VARARGS,
     "Lease and revalidate one exact captured destination parent."},
    {"provision_owner_replacement_exact",
     (PyCFunction)module_provision_owner_replacement_exact, METH_VARARGS,
     "Provision one candidate beside an exact captured destination."},
    {"claim_owner_publish_permit_exact",
     (PyCFunction)module_claim_owner_publish_permit_exact, METH_O,
     "Claim the exact owner's private one-shot publication capability."},
    {"claim_owner_replacement_permit_exact",
     (PyCFunction)module_claim_owner_replacement_permit_exact, METH_O,
     "Claim the exact owner's private one-shot replacement capability."},
    {"verify_owner_authority_exact",
     (PyCFunction)module_verify_owner_authority_exact, METH_O,
     "Verify authority through exact native-owner dispatch."},
    {"verify_owner_destination_binding_exact",
     (PyCFunction)module_verify_owner_destination_binding_exact, METH_O,
     "Verify one exact captured destination binding."},
    {"verify_owner_replacement_binding_exact",
     (PyCFunction)module_verify_owner_replacement_binding_exact, METH_O,
     "Verify one exact incumbent and candidate replacement binding."},
    {"verify_owner_adoption_binding_exact",
     (PyCFunction)module_verify_owner_adoption_binding_exact, METH_VARARGS,
     "Verify adoption binding through exact native-owner dispatch."},
    {"borrow_owner_parent_descriptor_exact",
     (PyCFunction)module_borrow_owner_parent_descriptor_exact, METH_O,
     "Borrow the exact owner's destination-parent descriptor."},
    {"borrow_owner_root_descriptor_exact",
     (PyCFunction)module_borrow_owner_root_descriptor_exact, METH_O,
     "Borrow the exact owner's workspace-root descriptor."},
    {"borrow_owner_destination_descriptor_exact",
     (PyCFunction)module_borrow_owner_destination_descriptor_exact, METH_O,
     "Borrow the exact owner's captured-destination descriptor."},
    {"borrow_owner_directory_descriptor_exact",
     (PyCFunction)module_borrow_owner_directory_descriptor_exact, METH_VARARGS,
     "Borrow one planned directory through exact native-owner dispatch."},
    {"begin_owner_file_exact", (PyCFunction)module_begin_owner_file_exact,
     METH_VARARGS, "Begin a file through exact native-owner dispatch."},
    {"write_owner_file_exact", (PyCFunction)module_write_owner_file_exact,
     METH_VARARGS, "Write a file through exact native-owner dispatch."},
    {"finish_owner_file_exact", (PyCFunction)module_finish_owner_file_exact,
     METH_VARARGS, "Finish a file through exact native-owner dispatch."},
    {"abort_owner_file_exact", (PyCFunction)module_abort_owner_file_exact,
     METH_O, "Abort a file through exact native-owner dispatch."},
    {"seal_owner_directories_exact",
     (PyCFunction)module_seal_owner_directories_exact, METH_O,
     "Seal directories through exact native-owner dispatch."},
    {"sync_owner_parent_exact", (PyCFunction)module_sync_owner_parent_exact,
     METH_O, "Sync the parent through exact native-owner dispatch."},
    {"mark_owner_adopted_exact", (PyCFunction)module_mark_owner_adopted_exact,
     METH_O, "Mark adoption through exact native-owner dispatch."},
    {"rename_owner_child_noreplace_exact",
     (PyCFunction)module_rename_owner_child_noreplace_exact, METH_VARARGS,
     "Publish through one exact native publication capability."},
    {"exchange_owner_replacement_exact",
     (PyCFunction)module_exchange_owner_replacement_exact, METH_VARARGS,
     "Exchange exact incumbent and candidate roots under one commit lease."},
    {"commit_owner_receipt_exact",
     (PyCFunction)module_commit_owner_receipt_exact, METH_O,
     "Commit one exact native receipt capability."},
    {"abort_owner_exact", (PyCFunction)module_abort_owner_exact, METH_O,
     "Abort through exact native-owner dispatch."},
    {"quarantine_owner_exact", (PyCFunction)module_quarantine_owner_exact,
     METH_O, "Quarantine through exact native-owner dispatch."},
    {"owner_state_exact", (PyCFunction)module_owner_state_exact, METH_O,
     "Return state through exact native-owner dispatch."},
    {"owner_closed_exact", (PyCFunction)module_owner_closed_exact, METH_O,
     "Return closed state through exact native-owner dispatch."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef workspace_owner_module = {
    PyModuleDef_HEAD_INIT,
    "_workspace_owner_impl",
    "Native ownership for strict local workspaces.",
    -1,
    workspace_module_methods,
    NULL,
    NULL,
    NULL,
    NULL,
};

PyMODINIT_FUNC PyInit__workspace_owner_impl(void);

PyMODINIT_FUNC PyInit__workspace_owner_impl(void) {
  PyObject *module;
  PyObject *owner_type;
  PyObject *publish_permit_type;
  PyObject *replacement_permit_type;
  PyObject *receipt_token_type;

  module = PyModule_Create(&workspace_owner_module);
  if (module == NULL) {
    return NULL;
  }
  if (PyModule_AddIntConstant(module, "workspace_owner_protocol_version",
                              CODENIB_WORKSPACE_OWNER_PROTOCOL_VERSION) < 0) {
    Py_DECREF(module);
    return NULL;
  }
  owner_type = PyType_FromSpec(&WorkspaceOwner_spec);
  if (owner_type == NULL) {
    Py_DECREF(module);
    return NULL;
  }
  publish_permit_type = PyType_FromSpec(&WorkspacePublishPermit_spec);
  if (publish_permit_type == NULL) {
    Py_DECREF(owner_type);
    Py_DECREF(module);
    return NULL;
  }
  replacement_permit_type = PyType_FromSpec(&WorkspaceReplacementPermit_spec);
  if (replacement_permit_type == NULL) {
    Py_DECREF(publish_permit_type);
    Py_DECREF(owner_type);
    Py_DECREF(module);
    return NULL;
  }
  receipt_token_type = PyType_FromSpec(&WorkspaceReceiptToken_spec);
  if (receipt_token_type == NULL) {
    Py_DECREF(replacement_permit_type);
    Py_DECREF(publish_permit_type);
    Py_DECREF(owner_type);
    Py_DECREF(module);
    return NULL;
  }
  /* Keep the exact type private: every operation enters through a pinned
     module-level wrapper, never through mutable attribute dispatch. */
  WorkspaceOwnerTypeObject = owner_type;
  WorkspacePublishPermitTypeObject = publish_permit_type;
  WorkspaceReplacementPermitTypeObject = replacement_permit_type;
  WorkspaceReceiptTokenTypeObject = receipt_token_type;
  return module;
}
