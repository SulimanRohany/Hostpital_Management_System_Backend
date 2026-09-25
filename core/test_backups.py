import sqlite3
from datetime import timedelta
from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone


class BackupDatabaseCommandTests(TestCase):
    backup_dir = settings.BASE_DIR / 'test-backups'

    def setUp(self):
        for path in self.backup_dir.glob('*'):
            if path.name != '.gitkeep':
                path.unlink()

    def test_creates_readable_dated_sqlite_backup(self):
        with override_settings(
            DATABASE_BACKUP_DIR=self.backup_dir,
            DATABASE_BACKUP_RETENTION_DAYS=30,
        ):
            call_command('backup_database')

        backup = self.backup_dir / f'database-{timezone.localdate().isoformat()}.sqlite3'
        self.assertTrue(backup.exists())
        database = sqlite3.connect(backup)
        try:
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            database.close()
        self.assertIn('django_migrations', tables)

    def test_keeps_existing_same_day_backup_without_force(self):
        backup = self.backup_dir / f'database-{timezone.localdate().isoformat()}.sqlite3'
        backup.write_bytes(b'existing')
        with override_settings(
            DATABASE_BACKUP_DIR=self.backup_dir,
            DATABASE_BACKUP_RETENTION_DAYS=30,
        ):
            call_command('backup_database')
        self.assertEqual(backup.read_bytes(), b'existing')

    def test_removes_only_expired_dated_backups(self):
        expired = self.backup_dir / f'database-{(timezone.localdate() - timedelta(days=8)).isoformat()}.sqlite3'
        recent = self.backup_dir / f'database-{(timezone.localdate() - timedelta(days=2)).isoformat()}.sqlite3'
        unrelated = self.backup_dir / 'notes.txt'
        expired.write_bytes(b'old')
        recent.write_bytes(b'recent')
        unrelated.write_text('keep me')

        with override_settings(
            DATABASE_BACKUP_DIR=self.backup_dir,
            DATABASE_BACKUP_RETENTION_DAYS=7,
        ):
            call_command('backup_database')

        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())
