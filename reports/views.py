import logging
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import connection
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.http import FileResponse
from django.utils import timezone
from rest_framework import response, status
from rest_framework.views import APIView

from core.models import AuditLog
from core.permissions import IsAdministrator, RolePermission
from finance.models import Expense, Turnover, Wallet, WalletTransaction
from laboratory.models import LabOrder
from patients.models import Patient
from pharmacy.models import Medicine, MedicineBatch, Purchase, Sale, SaleLine, Supplier
from reception.models import Visit
from .serializers import DateRangeSerializer, StockReportFilterSerializer


MONEY = DecimalField(max_digits=16, decimal_places=2)
QUANTITY = DecimalField(max_digits=16, decimal_places=3)
ZERO = Decimal('0.00')
logger = logging.getLogger(__name__)


def amount(queryset, field):
    return queryset.aggregate(value=Coalesce(Sum(field), ZERO, output_field=MONEY))['value']


def validated_range(request):
    serializer = DateRangeSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data.get('start'), serializer.validated_data.get('end')


def apply_date_range(queryset, field, start, end):
    """Apply an inclusive local-date range to a DateField or DateTimeField."""
    model_field = queryset.model._meta.get_field(field)
    lookup = f'{field}__date' if model_field.get_internal_type() == 'DateTimeField' else field
    filters = {}
    if start:
        filters[f'{lookup}__gte'] = start
    if end:
        filters[f'{lookup}__lte'] = end
    return queryset.filter(**filters)


def sale_profit(queryset):
    sale_ids = queryset.order_by().values('pk')
    margin = ExpressionWrapper(
        F('quantity') * (F('unit_price') - F('unit_cost')),
        output_field=MONEY,
    )
    gross_margin = amount(SaleLine.objects.filter(sale_id__in=sale_ids), margin)
    return gross_margin - amount(queryset, 'discount_amount')


def month_start_months_ago(value, months):
    month_index = value.year * 12 + value.month - 1 - months
    return value.replace(year=month_index // 12, month=month_index % 12 + 1, day=1)


def supplier_due_rows():
    rows = []
    for supplier in Supplier.objects.all():
        due = supplier.amount_due
        if due != ZERO:
            rows.append({'id': str(supplier.id), 'name': supplier.name, 'amount_due': due})
    return rows


class DashboardAPIView(APIView):
    permission_classes = (RolePermission,)

    def get(self, request):
        today = timezone.localdate()
        visits_today = Visit.objects.exclude(status=Visit.Status.CANCELLED).filter(visit_date__date=today)
        sales_today = Sale.objects.filter(status=Sale.Status.POSTED, sale_date__date=today)
        posted_sales = Sale.objects.filter(status=Sale.Status.POSTED)
        posted_purchases = Purchase.objects.filter(status=Purchase.Status.POSTED)
        active_batches = MedicineBatch.objects.filter(
            is_active=True, expiry_date__gt=today, quantity_available__gt=0,
        )
        stock_expression = ExpressionWrapper(F('quantity_available') * F('purchase_price'), output_field=MONEY)
        wallet_map = {wallet.kind: wallet.balance for wallet in Wallet.objects.filter(is_active=True)}
        supplier_due = sum((supplier.amount_due for supplier in Supplier.objects.all()), ZERO)
        return response.Response({
            'as_of': timezone.now(),
            'today': {
                'patients': visits_today.values('patient_id').distinct().count(),
                'reception_income': amount(visits_today, 'paid_amount'),
                'pharmacy_sales': amount(sales_today, 'total_amount'),
                'pharmacy_profit': sale_profit(sales_today),
            },
            'overall': {
                'total_patients': Patient.objects.filter(is_active=True).count(),
                'reception_income': amount(Visit.objects.exclude(status=Visit.Status.CANCELLED), 'paid_amount'),
                'pharmacy_sales': amount(posted_sales, 'total_amount'),
                'pharmacy_purchases': amount(posted_purchases, 'total_amount'),
                'pharmacy_profit': sale_profit(posted_sales),
                'pharmacy_stock_value': active_batches.aggregate(value=Coalesce(Sum(stock_expression), ZERO, output_field=MONEY))['value'],
                'total_expenses': amount(Expense.objects.filter(is_void=False), 'amount'),
                'total_turnover': amount(Turnover.objects.all(), 'amount'),
                'suppliers_due': supplier_due,
            },
            'wallets': {
                'reception': wallet_map.get(Wallet.Kind.RECEPTION, ZERO),
                'pharmacy': wallet_map.get(Wallet.Kind.PHARMACY, ZERO),
                'manager': wallet_map.get(Wallet.Kind.MANAGER, ZERO),
            },
            'today_receptions': [
                {
                    'id': str(visit.id), 'visit_number': visit.visit_number, 'date': visit.visit_date,
                    'patient': visit.patient.full_name, 'father_name': visit.patient.father_name,
                    'age': visit.patient.age, 'gender': visit.patient.gender,
                    'department': visit.department.name, 'total': visit.total_amount,
                    'discount': visit.discount_amount, 'paid': visit.paid_amount,
                }
                for visit in visits_today.select_related('patient', 'department').order_by('-visit_date')[:20]
            ],
        })


class IncomeTrendAPIView(APIView):
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'reception', 'pharmacy', 'finance', 'manager')

    def get(self, request):
        current_month = timezone.localdate().replace(day=1)
        start = month_start_months_ago(current_month, 11)
        reception = (
            Visit.objects.exclude(status=Visit.Status.CANCELLED).filter(visit_date__date__gte=start)
            .annotate(month=TruncMonth('visit_date')).values('month')
            .annotate(total=Coalesce(Sum('paid_amount'), ZERO, output_field=MONEY)).order_by('month')
        )
        pharmacy = (
            Sale.objects.filter(status=Sale.Status.POSTED, sale_date__date__gte=start)
            .annotate(month=TruncMonth('sale_date')).values('month')
            .annotate(total=Coalesce(Sum('total_amount'), ZERO, output_field=MONEY)).order_by('month')
        )
        return response.Response({'reception': list(reception), 'pharmacy': list(pharmacy)})


class ReceptionReportAPIView(APIView):
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'reception', 'finance', 'manager')

    def get(self, request):
        start, end = validated_range(request)
        qs = apply_date_range(Visit.objects.exclude(status=Visit.Status.CANCELLED), 'visit_date', start, end)
        by_department = qs.values('department__id', 'department__name').annotate(
            visits=Count('id'), total=Sum('total_amount'), discounts=Sum('discount_amount'), paid=Sum('paid_amount')
        ).order_by('department__name')
        return response.Response({
            'period': {'start': start, 'end': end},
            'summary': {
                'visits': qs.count(), 'patients': qs.values('patient_id').distinct().count(),
                'total': amount(qs, 'total_amount'), 'discounts': amount(qs, 'discount_amount'),
                'net': amount(qs, 'total_amount') - amount(qs, 'discount_amount'),
                'paid': amount(qs, 'paid_amount'),
                'outstanding': amount(qs, 'total_amount') - amount(qs, 'discount_amount') - amount(qs, 'paid_amount'),
            },
            'by_department': list(by_department),
        })


class PharmacyReportAPIView(APIView):
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'pharmacy', 'finance', 'manager')

    def get(self, request):
        start, end = validated_range(request)
        sales = Sale.objects.filter(status=Sale.Status.POSTED)
        purchases = Purchase.objects.filter(status=Purchase.Status.POSTED)
        sales = apply_date_range(sales, 'sale_date', start, end)
        purchases = apply_date_range(purchases, 'purchase_date', start, end)
        purchase_due = amount(purchases, 'total_amount') - amount(purchases, 'paid_amount')
        return response.Response({
            'period': {'start': start, 'end': end},
            'sales_count': sales.count(), 'sales_total': amount(sales, 'total_amount'),
            'sales_paid': amount(sales, 'paid_amount'),
            'sales_discounts': amount(sales, 'discount_amount'),
            'profit': sale_profit(sales),
            'purchases_count': purchases.count(), 'purchases_total': amount(purchases, 'total_amount'),
            'purchases_paid': amount(purchases, 'paid_amount'),
            'purchases_due_at_posting': purchase_due,
            'purchases_by_supplier': list(purchases.values('supplier_id', 'supplier__name').annotate(
                count=Count('id'), total=Coalesce(Sum('total_amount'), ZERO, output_field=MONEY),
                paid=Coalesce(Sum('paid_amount'), ZERO, output_field=MONEY),
            ).order_by('supplier__name')),
        })


class FinancialReportAPIView(APIView):
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'finance', 'manager')

    def get(self, request):
        start, end = validated_range(request)
        expenses = apply_date_range(Expense.objects.filter(is_void=False), 'expense_date', start, end)
        entries = apply_date_range(WalletTransaction.objects.all(), 'transaction_date', start, end)
        turnovers = apply_date_range(Turnover.objects.all(), 'created_at', start, end)
        expenses_by_category = expenses.values('category__name').annotate(total=Sum('amount'), count=Count('id')).order_by('-total')
        return response.Response({
            'period': {'start': start, 'end': end},
            'expenses_total': amount(expenses, 'amount'),
            'expenses_by_category': list(expenses_by_category),
            'wallets': list(Wallet.objects.filter(is_active=True).values('id', 'code', 'name', 'kind', 'balance')),
            'credits': amount(entries.filter(entry_type='credit'), 'amount'),
            'debits': amount(entries.filter(entry_type='debit'), 'amount'),
            'turnovers': {
                'count': turnovers.count(), 'total': amount(turnovers, 'amount'),
                'by_status': list(turnovers.values('status').annotate(
                    count=Count('id'), total=Coalesce(Sum('amount'), ZERO, output_field=MONEY),
                ).order_by('status')),
            },
            'supplier_due': supplier_due_rows(),
        })


class LaboratoryReportAPIView(APIView):
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'laboratory', 'manager')

    def get(self, request):
        start, end = validated_range(request)
        qs = apply_date_range(LabOrder.objects.all(), 'ordered_at', start, end)
        return response.Response({
            'period': {'start': start, 'end': end},
            'total_orders': qs.count(),
            'by_status': list(qs.values('status').annotate(count=Count('id')).order_by('status')),
            'test_volume': list(qs.values('items__test__name').annotate(count=Count('items')).order_by('-count')),
        })


class StockReportAPIView(APIView):
    permission_classes = (RolePermission,)
    read_roles = ('administrator', 'pharmacy', 'finance', 'manager')

    def get(self, request):
        serializer = StockReportFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        today = timezone.localdate()
        cutoff = today + timedelta(days=serializer.validated_data['expiry_days'])
        medicines = Medicine.objects.all()
        batches = MedicineBatch.objects.filter(quantity_available__gt=0)
        if not serializer.validated_data['include_inactive']:
            medicines = medicines.filter(is_active=True)
            batches = batches.filter(is_active=True)
        usable = batches.filter(expiry_date__gt=today)
        expired = batches.filter(expiry_date__lte=today)
        near_expiry = usable.filter(expiry_date__lte=cutoff)
        stock_value = ExpressionWrapper(F('quantity_available') * F('purchase_price'), output_field=MONEY)
        low_stock = medicines.annotate(
            available=Coalesce(Sum(
                'batches__quantity_available',
                filter=Q(batches__is_active=True, batches__expiry_date__gt=today),
            ), Decimal('0.000')),
        ).filter(available__lte=F('reorder_level')).values(
            'id', 'code', 'name', 'strength', 'reorder_level', 'available',
        ).order_by('name')
        return response.Response({
            'as_of': today,
            'expiry_days': serializer.validated_data['expiry_days'],
            'summary': {
                'usable_batches': usable.count(),
                'usable_quantity': usable.aggregate(
                    value=Coalesce(Sum('quantity_available'), Decimal('0.000'), output_field=QUANTITY)
                )['value'],
                'stock_value': usable.aggregate(value=Coalesce(Sum(stock_value), ZERO, output_field=MONEY))['value'],
                'low_stock_medicines': low_stock.count(),
                'near_expiry_batches': near_expiry.count(),
                'expired_batches': expired.count(),
            },
            'low_stock': list(low_stock),
            'near_expiry': list(near_expiry.values(
                'id', 'medicine_id', 'medicine__code', 'medicine__name', 'batch_number',
                'expiry_date', 'quantity_available', 'purchase_price', 'sale_price',
            ).order_by('expiry_date', 'medicine__name')),
            'expired': list(expired.values(
                'id', 'medicine_id', 'medicine__code', 'medicine__name', 'batch_number',
                'expiry_date', 'quantity_available',
            ).order_by('expiry_date', 'medicine__name')),
        })


class DatabaseBackupAPIView(APIView):
    permission_classes = (IsAdministrator,)

    def post(self, request):
        if connection.vendor != 'postgresql':
            return response.Response(
                {'detail': 'Online backup is only available for PostgreSQL.'},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        configured_executable = settings.PG_DUMP_PATH
        executable = shutil.which(configured_executable)
        if not executable and os.path.isfile(configured_executable):
            executable = configured_executable
        if not executable:
            return response.Response(
                {'detail': 'The configured PostgreSQL backup tool could not be found.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        config = connection.settings_dict
        temp = tempfile.NamedTemporaryFile(prefix='health-plus-', suffix='.dump', delete=False)
        temp.close()
        command = [executable, '--format=custom', '--file', temp.name]
        if config.get('HOST'):
            command.extend(['--host', str(config['HOST'])])
        if config.get('PORT'):
            command.extend(['--port', str(config['PORT'])])
        if config.get('USER'):
            command.extend(['--username', str(config['USER'])])
        command.append(str(config['NAME']))

        environment = os.environ.copy()
        if config.get('PASSWORD'):
            environment['PGPASSWORD'] = str(config['PASSWORD'])
        try:
            subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            logger.exception('PostgreSQL database backup failed')
            if os.path.exists(temp.name):
                os.unlink(temp.name)
            return response.Response(
                {'detail': 'PostgreSQL backup failed. Check the server logs and database connection settings.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        AuditLog.objects.create(actor=request.user, action=AuditLog.Action.BACKUP, object_repr='Database backup')
        filename = f'health-plus-backup-{timezone.now():%Y%m%d-%H%M%S}.dump'
        file_handle = open(temp.name, 'rb')
        result = FileResponse(file_handle, as_attachment=True, filename=filename, content_type='application/octet-stream')
        result._resource_closers.append(lambda: os.unlink(temp.name) if os.path.exists(temp.name) else None)
        return result
