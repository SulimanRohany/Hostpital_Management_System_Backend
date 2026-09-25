from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from departments.models import Department
from .models import (
    Attendance, Employee, EmploymentAssignment, LeaveRequest,
    PayrollComponent, PayrollRecord, SalaryHistory, WorkShift,
)


class HRDomainModelTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(code='HR-T', name='HR Test')
        self.user = get_user_model().objects.create_user(
            username='hr-employee', password='test-password', department=self.department
        )
        self.employee = Employee.objects.create(
            user=self.user,
            department=self.department,
            first_name='Test',
            last_name='Employee',
            job_title='Nurse',
            hire_date=date(2025, 1, 1),
            salary=Decimal('1000.00'),
        )

    def assert_invalid(self, model, field=None):
        with self.assertRaises(ValidationError) as context:
            model.save()
        if field:
            self.assertIn(field, context.exception.message_dict)

    def test_terminated_employee_requires_end_date_and_deactivates_user(self):
        self.employee.status = Employee.Status.TERMINATED
        self.assert_invalid(self.employee, 'end_date')
        self.employee.end_date = timezone.localdate()
        self.employee.save()
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_attendance_enforces_status_times_and_supports_overnight(self):
        today = timezone.localdate()
        self.assert_invalid(Attendance(employee=self.employee, date=today, status=Attendance.Status.PRESENT), 'check_in')
        self.assert_invalid(
            Attendance(employee=self.employee, date=today, status=Attendance.Status.ABSENT, check_in=time(8)),
            'status',
        )
        record = Attendance.objects.create(
            employee=self.employee,
            date=today,
            status=Attendance.Status.PRESENT,
            check_in=time(20),
            check_out=time(4),
            check_out_date=today + timedelta(days=1),
        )
        self.assertEqual(record.worked_duration, timedelta(hours=8))

    def test_payroll_prevents_negative_net_overlap_and_paid_edits(self):
        payroll = PayrollRecord.objects.create(
            employee=self.employee,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            base_salary=Decimal('1000'),
            allowances=Decimal('100'),
            deductions=Decimal('100'),
        )
        self.assertEqual(payroll.gross_pay, Decimal('1100'))
        self.assertEqual(payroll.net_pay, Decimal('1000'))
        self.assert_invalid(
            PayrollRecord(
                employee=self.employee,
                period_start=date(2025, 1, 15),
                period_end=date(2025, 2, 15),
                base_salary=Decimal('1000'),
            ),
            'period_start',
        )
        payroll.status = PayrollRecord.Status.PAID
        payroll.save()
        self.assertIsNotNone(payroll.paid_at)
        payroll.base_salary = Decimal('1200')
        self.assert_invalid(payroll, 'status')
        self.assert_invalid(
            PayrollComponent(payroll=payroll, kind=PayrollComponent.Kind.BONUS, description='Late bonus', amount=10),
            'payroll',
        )

    def test_leave_requires_review_metadata_and_prevents_overlap(self):
        LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=LeaveRequest.LeaveType.ANNUAL,
            start_date=date(2025, 3, 1),
            end_date=date(2025, 3, 3),
        )
        self.assert_invalid(
            LeaveRequest(
                employee=self.employee,
                leave_type=LeaveRequest.LeaveType.SICK,
                start_date=date(2025, 3, 3),
                end_date=date(2025, 3, 4),
            ),
            'start_date',
        )
        self.assert_invalid(
            LeaveRequest(
                employee=self.employee,
                leave_type=LeaveRequest.LeaveType.SICK,
                start_date=date(2025, 4, 1),
                end_date=date(2025, 4, 1),
                status=LeaveRequest.Status.APPROVED,
            ),
            'reviewed_by',
        )

    def test_shift_salary_and_assignment_rules(self):
        shift = WorkShift.objects.create(name='Night', start_time=time(20), end_time=time(4), break_minutes=30)
        self.assertTrue(shift.crosses_midnight)
        self.assertEqual(shift.scheduled_duration, timedelta(hours=7, minutes=30))
        self.assertEqual(shift.working_days, [0, 1, 2, 3, 4])
        salary = SalaryHistory.objects.create(
            employee=self.employee, amount=Decimal('1200'), effective_from=date(2025, 2, 1)
        )
        self.assertEqual(salary.amount, Decimal('1200'))
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.salary, Decimal('1200'))
        EmploymentAssignment.objects.create(
            employee=self.employee,
            department=self.department,
            job_title='Nurse',
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 30),
        )
        self.assert_invalid(
            EmploymentAssignment(
                employee=self.employee,
                department=self.department,
                job_title='Senior Nurse',
                start_date=date(2025, 6, 1),
            ),
            'start_date',
        )


class HRAPITests(APITestCase):
    def setUp(self):
        self.department = Department.objects.create(code='HR-A', name='HR API')
        self.user = get_user_model().objects.create_user(
            username='hr-api', password='test-password', role='hr', must_change_password=False,
        )
        self.client.force_authenticate(self.user)

    def test_employee_create_accepts_dialog_payload_with_optional_blanks(self):
        response = self.client.post('/api/v1/employees/', {
            'department': str(self.department.pk),
            'user': None,
            'first_name': 'Karim',
            'last_name': 'Ahmad',
            'father_name': '',
            'job_title': 'Nurse',
            'phone': '078923482',
            'email': 'karim@gmail.com',
            'address': 'Kabul, Afghanistan',
            'national_id': '1400-234923-234',
            'hire_date': '2026-08-06',
            'end_date': None,
            'salary': '8000',
            'status': 'active',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(Employee.objects.filter(national_id='1400-234923-234').exists())

    def test_payroll_uses_employee_salary_and_ignores_manual_base_salary(self):
        employee = Employee.objects.create(
            department=self.department,
            first_name='Sami',
            last_name='Rahimi',
            job_title='Doctor',
            hire_date='2026-01-01',
            salary=Decimal('7500.00'),
        )

        response = self.client.post('/api/v1/payroll/', {
            'employee': str(employee.pk),
            'period_start': '2026-09-01',
            'period_end': '2026-09-30',
            'base_salary': '9999.99',
            'allowances': '200.00',
            'deductions': '150.00',
            'status': 'draft',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        payroll = PayrollRecord.objects.get(pk=response.data['id'])
        self.assertEqual(payroll.base_salary, employee.salary)
        self.assertEqual(payroll.gross_pay, Decimal('7700.00'))
        self.assertEqual(payroll.net_pay, Decimal('7550.00'))
