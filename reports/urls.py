from django.urls import path

from .views import (
    DashboardAPIView, DatabaseBackupAPIView, FinancialReportAPIView, IncomeTrendAPIView,
    LaboratoryReportAPIView, PharmacyReportAPIView, ReceptionReportAPIView, StockReportAPIView,
)

urlpatterns = [
    path('dashboard/', DashboardAPIView.as_view(), name='dashboard'),
    path('reports/income-trend/', IncomeTrendAPIView.as_view(), name='income-trend'),
    path('reports/reception/', ReceptionReportAPIView.as_view(), name='reception-report'),
    path('reports/pharmacy/', PharmacyReportAPIView.as_view(), name='pharmacy-report'),
    path('reports/financial/', FinancialReportAPIView.as_view(), name='financial-report'),
    path('reports/laboratory/', LaboratoryReportAPIView.as_view(), name='laboratory-report'),
    path('reports/stock/', StockReportAPIView.as_view(), name='stock-report'),
    path('database-backup/', DatabaseBackupAPIView.as_view(), name='database-backup'),
]
