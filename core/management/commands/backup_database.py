import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.utils import timezone


class Command(BaseCommand):
    help = 'Create a dated, atomic backup of the default database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Replace an existing backup for today.',
        )

    def handle(self, *args, **options):
        backup_dir = Path(settings.DATABASE_BACKUP_DIR)
        if not backup_dir.is_absolute():
            backup_dir = Path(settings.BASE_DIR) / backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)

        connection = connections['default']
        today = timezone.localdate().isoformat()
        destination = backup_dir / f'database-{today}.sqlite3'

        if destination.exists() and not options['force']:
            self.stdout.write(self.style.WARNING(f'Backup already exists: {destination}'))
            return

        temporary = destination.with_name(f'.{destination.name}.tmp')
        try:
            temporary.unlink(missing_ok=True)
            self._backup_sqlite(connection, temporary)
            os.replace(temporary, destination)
        except CommandError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, sqlite3.Error) as exc:
            temporary.unlink(missing_ok=True)
            raise CommandError(f'Database backup failed: {exc}') from exc

        self._remove_expired_backups(backup_dir)
        self.stdout.write(self.style.SUCCESS(f'Database backup created: {destination}'))

    @staticmethod
    def _backup_sqlite(connection, destination):
        connection.ensure_connection()
        source = connection.connection
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()

    def _remove_expired_backups(self, backup_dir):
        retention_days = settings.DATABASE_BACKUP_RETENTION_DAYS
        if retention_days <= 0:
            return
        cutoff = timezone.localdate() - timedelta(days=retention_days)
        for path in backup_dir.glob('database-*.sqlite3'):
            try:
                backup_date = datetime.strptime(
                    path.name.removeprefix('database-')[:10], '%Y-%m-%d'
                ).date()
            except ValueError:
                continue
            if backup_date < cutoff:
                path.unlink()
