import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class SingleInstanceLock:
    """Cross-platform process lock released automatically when the process exits."""

    def __init__(self, path):
        self.path = path
        self._file = None
        self._locked = False

    def acquire(self):
        try:
            self._file = open(self.path, "a+b")
            if os.name == "nt":
                if os.fstat(self._file.fileno()).st_size == 0:
                    self._file.write(b"0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(
                    self._file.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
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
                if os.name == "nt":
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._locked = False

    def __del__(self):
        self.release()
