from django.utils import timezone
from rest_framework import serializers

from .models import (
    Attendance, Employee, EmploymentAssignment, EmploymentDocument, Holiday,
    LeaveRequest, PayrollComponent, PayrollRecord, SalaryHistory,
    ShiftAssignment, WorkShift,
)


class EmployeeSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)

    class Meta:
        model = Employee
        fields = '__all__'
        read_only_fields = ('id', 'employee_number', 'created_at', 'updated_at')


class AttendanceSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    worked_duration = serializers.DurationField(read_only=True)

    class Meta:
        model = Attendance
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class PayrollComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollComponent
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class PayrollRecordSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    gross_pay = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    net_pay = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    components = PayrollComponentSerializer(many=True, read_only=True)

    class Meta:
        model = PayrollRecord
        fields = '__all__'
        read_only_fields = ('id', 'base_salary', 'paid_at', 'created_at', 'updated_at')

    def validate(self, attrs):
        employee = attrs.get('employee') or getattr(self.instance, 'employee', None)
        if employee is not None:
            attrs['base_salary'] = employee.salary
        return attrs


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    total_days = serializers.IntegerField(read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.get_full_name', read_only=True)

    class Meta:
        model = LeaveRequest
        fields = '__all__'
        read_only_fields = ('id', 'reviewed_by', 'reviewed_at', 'created_at', 'updated_at')

    def update(self, instance, validated_data):
        new_status = validated_data.get('status', instance.status)
        if new_status in (LeaveRequest.Status.APPROVED, LeaveRequest.Status.REJECTED) and instance.status != new_status:
            validated_data['reviewed_by'] = self.context['request'].user
            validated_data['reviewed_at'] = timezone.now()
        return super().update(instance, validated_data)


class WorkShiftSerializer(serializers.ModelSerializer):
    crosses_midnight = serializers.BooleanField(read_only=True)
    scheduled_duration = serializers.DurationField(read_only=True)

    class Meta:
        model = WorkShift
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class ShiftAssignmentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    shift_name = serializers.CharField(source='shift.name', read_only=True)

    class Meta:
        model = ShiftAssignment
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class HolidaySerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source='department.name', read_only=True)

    class Meta:
        model = Holiday
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class SalaryHistorySerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)

    class Meta:
        model = SalaryHistory
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

class EmploymentDocumentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)

    class Meta:
        model = EmploymentDocument
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class EmploymentAssignmentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True)

    class Meta:
        model = EmploymentAssignment
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
