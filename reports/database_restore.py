import os
import sqlite3
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone


class DatabaseRestoreError(Exception):
    """Raised when an uploaded file cannot safely replace the live database."""


_restore_lock = threading.Lock()


def _fetchall(connection_object, sql):
    cursor = connection_object.execute(sql)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _fetchone(connection_object, sql):
    cursor = connection_object.execute(sql)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _schema(connection_object):
    return _fetchall(connection_object,
        """
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    )


def _migrations(connection_object):
    return set(_fetchall(connection_object,
        'SELECT app, name FROM django_migrations'
    ))


def _validate_backup(candidate, live):
    if _fetchone(candidate, 'PRAGMA quick_check') != ('ok',):
        raise DatabaseRestoreError('The selected file is damaged and cannot be restored.')

    tables = {
        row[0]
        for row in _fetchall(candidate, "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    required = {'django_migrations', 'accounts_user', 'core_auditlog'}
    if not required.issubset(tables):
        raise DatabaseRestoreError('The selected file is not a Hospital System database backup.')

    if _migrations(candidate) != _migrations(live) or _schema(candidate) != _schema(live):
        raise DatabaseRestoreError(
            'This backup was created by an incompatible version of the Hospital System.'
        )


def _backup_directory():
    directory = Path(settings.DATABASE_BACKUP_DIR)
    if not directory.is_absolute():
        directory = Path(settings.BASE_DIR) / directory
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def restore_uploaded_database(upload):
    """Validate and restore a SQLite upload, returning the recovery-copy path."""
    maximum = settings.DATABASE_RESTORE_MAX_BYTES
    if upload.size > maximum:
        raise DatabaseRestoreError(
            f'The backup is too large. The maximum restore size is {maximum // (1024 * 1024)} MB.'
        )

    temp = tempfile.NamedTemporaryFile(prefix='health-plus-restore-', suffix='.sqlite3', delete=False)
    temp_path = Path(temp.name)
    try:
        with temp:
            for chunk in upload.chunks():
                temp.write(chunk)

        with temp_path.open('rb') as candidate_file:
            signature = candidate_file.read(16)
        if temp_path.stat().st_size < 100 or signature != b'SQLite format 3\x00':
            raise DatabaseRestoreError('The selected file is not a valid SQLite database backup.')

        if not _restore_lock.acquire(blocking=False):
            raise DatabaseRestoreError('Another database restore is already in progress.')

        try:
            connection.ensure_connection()
            live = connection.connection
            candidate = sqlite3.connect(temp_path)
            candidate.execute('PRAGMA query_only = ON')
            try:
                _validate_backup(candidate, live)

                timestamp = timezone.now().strftime('%Y%m%d-%H%M%S-%f')
                recovery_path = _backup_directory() / f'pre-restore-{timestamp}.sqlite3'
                partial_path = recovery_path.with_suffix('.sqlite3.partial')
                recovery = sqlite3.connect(partial_path)
                try:
                    live.backup(recovery)
                finally:
                    recovery.close()
                os.replace(partial_path, recovery_path)

                try:
                    candidate.backup(live)
                except (OSError, sqlite3.Error):
                    recovery = sqlite3.connect(recovery_path)
                    try:
                        recovery.backup(live)
                    finally:
                        recovery.close()
                    raise
            finally:
                candidate.close()
        finally:
            _restore_lock.release()
    except DatabaseRestoreError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseRestoreError('The database restore failed. The existing data was preserved.') from exc
    finally:
        temp_path.unlink(missing_ok=True)

    return recovery_path
