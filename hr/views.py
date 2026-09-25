from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, viewsets

from core.mixins import AuditModelViewSetMixin
from core.permissions import RolePermission
from .models import (
    Attendance, Employee, EmploymentAssignment, EmploymentDocument, Holiday,
    LeaveRequest, PayrollComponent, PayrollRecord, SalaryHistory,
    ShiftAssignment, WorkShift,
)
from .serializers import (
    AttendanceSerializer, EmployeeSerializer, EmploymentAssignmentSerializer,
    EmploymentDocumentSerializer, HolidaySerializer, LeaveRequestSerializer,
    PayrollComponentSerializer, PayrollRecordSerializer, SalaryHistorySerializer,
    ShiftAssignmentSerializer, WorkShiftSerializer,
)


HR_ROLES = ('administrator', 'hr', 'manager')


class HRModelViewSet(AuditModelViewSetMixin, viewsets.ModelViewSet):
    permission_classes = (RolePermission,)
    read_roles = HR_ROLES
    write_roles = ('administrator', 'hr')

    def perform_create(self, serializer):
        try:
            return super().perform_create(serializer)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(getattr(exc, 'message_dict', exc.messages)) from exc

    def perform_update(self, serializer):
        try:
            return super().perform_update(serializer)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(getattr(exc, 'message_dict', exc.messages)) from exc


class EmployeeViewSet(HRModelViewSet):
    queryset = Employee.objects.select_related('department', 'user')
    serializer_class = EmployeeSerializer
    search_fields = ('employee_number', 'first_name', 'last_name', 'father_name', 'job_title', 'phone', 'national_id')
    ordering_fields = ('first_name', 'last_name', 'hire_date', 'salary', 'status')


class AttendanceViewSet(HRModelViewSet):
    queryset = Attendance.objects.select_related('employee')
    serializer_class = AttendanceSerializer
    search_fields = ('employee__employee_number', 'employee__first_name', 'employee__last_name')
    ordering_fields = ('date', 'status', 'created_at')

    def get_queryset(self):
        qs = super().get_queryset()
        if employee := self.request.query_params.get('employee'):
            qs = qs.filter(employee_id=employee)
        if date := self.request.query_params.get('date'):
            qs = qs.filter(date=date)
        return qs


class PayrollRecordViewSet(HRModelViewSet):
    queryset = PayrollRecord.objects.select_related('employee').prefetch_related('components')
    serializer_class = PayrollRecordSerializer
    search_fields = ('employee__employee_number', 'employee__first_name', 'employee__last_name')
    ordering_fields = ('period_start', 'period_end', 'base_salary', 'paid_at', 'status')


class LeaveRequestViewSet(HRModelViewSet):
    queryset = LeaveRequest.objects.select_related('employee', 'reviewed_by')
    serializer_class = LeaveRequestSerializer
    filterset_fields = ('employee', 'status', 'leave_type')
    ordering_fields = ('start_date', 'end_date', 'status')


class WorkShiftViewSet(HRModelViewSet):
    queryset = WorkShift.objects.all()
    serializer_class = WorkShiftSerializer
    search_fields = ('name',)


class ShiftAssignmentViewSet(HRModelViewSet):
    queryset = ShiftAssignment.objects.select_related('employee', 'shift')
    serializer_class = ShiftAssignmentSerializer
    filterset_fields = ('employee', 'shift')


class HolidayViewSet(HRModelViewSet):
    queryset = Holiday.objects.select_related('department')
    serializer_class = HolidaySerializer
    filterset_fields = ('department', 'date', 'is_paid')


class SalaryHistoryViewSet(HRModelViewSet):
    queryset = SalaryHistory.objects.select_related('employee')
    serializer_class = SalaryHistorySerializer
    filterset_fields = ('employee',)


class PayrollComponentViewSet(HRModelViewSet):
    queryset = PayrollComponent.objects.select_related('payroll', 'payroll__employee')
    serializer_class = PayrollComponentSerializer
    filterset_fields = ('payroll', 'kind')


class EmploymentDocumentViewSet(HRModelViewSet):
    queryset = EmploymentDocument.objects.select_related('employee')
    serializer_class = EmploymentDocumentSerializer
    filterset_fields = ('employee', 'document_type')


class EmploymentAssignmentViewSet(HRModelViewSet):
    queryset = EmploymentAssignment.objects.select_related('employee', 'department')
    serializer_class = EmploymentAssignmentSerializer
    filterset_fields = ('employee', 'department')
