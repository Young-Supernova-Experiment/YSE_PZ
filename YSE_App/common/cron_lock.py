"""File lock backend for django_cron that actually refuses a second holder (#472).

django_cron 0.5.1's own FileLock expects ``django.core.files.locks.lock`` to raise
when a non-blocking lock is already held. Since Django 3.0 that function returns
False instead, so the stock backend reports "locked" to every caller and never
skips an overlapping run. This subclass checks the return value.
"""

import os

from django.core.files import locks
from django_cron.backends.lock.file import FileLock as _DjangoCronFileLock


class FileLock(_DjangoCronFileLock):
    def lock(self):
        lock_name = self.get_lock_name()
        # Loop to avoid the unlink race described in django_cron's FileLock.
        while True:
            try:
                f = open(lock_name, 'ab+', 0)
            except OSError:
                # An unwritable lock directory is treated as "locked" (job skipped).
                return False
            if not locks.lock(f, locks.LOCK_EX | locks.LOCK_NB):
                f.close()
                return False
            if os.path.isfile(lock_name):
                st1 = os.fstat(f.fileno())
                st2 = os.stat(lock_name)
                if st1.st_ino == st2.st_ino:
                    f.seek(0)
                    f.truncate()
                    f.write(str(os.getpid()).encode('utf-8'))
                    self.lockfile = f
                    return True
            # The holder released (unlinked) the file between open and lock: retry.
            f.close()
