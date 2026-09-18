import os

import msvcrt


class SingleInstanceLock:
    """Small Windows process lock; the OS releases it if the process crashes."""

    def __init__(self, path):
        self.path = path
        self._file = None
        self._locked = False

    def acquire(self):
        try:
            self._file = open(self.path, "a+b")
            if os.fstat(self._file.fileno()).st_size == 0:
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            self._locked = True
        except OSError:
            if self._file is not None:
                self._file.close()
            self._file = None
            return False

        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(os.getpid()).encode("ascii"))
        self._file.flush()
        self._file.seek(0)
        return True

    def release(self):
        if self._file is None:
            return
        try:
            if self._locked:
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None
            self._locked = False

    def __del__(self):
        self.release()
