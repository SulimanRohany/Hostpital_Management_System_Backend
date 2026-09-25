from rest_framework.routers import DefaultRouter

from .views import (
    AttendanceViewSet, EmployeeViewSet, EmploymentAssignmentViewSet,
    EmploymentDocumentViewSet, HolidayViewSet, LeaveRequestViewSet,
    PayrollComponentViewSet, PayrollRecordViewSet, SalaryHistoryViewSet,
    ShiftAssignmentViewSet, WorkShiftViewSet,
)

router = DefaultRouter()
router.register('employees', EmployeeViewSet)
router.register('attendance', AttendanceViewSet)
router.register('payroll', PayrollRecordViewSet)
router.register('leave-requests', LeaveRequestViewSet)
router.register('work-shifts', WorkShiftViewSet)
router.register('shift-assignments', ShiftAssignmentViewSet)
router.register('holidays', HolidayViewSet)
router.register('salary-history', SalaryHistoryViewSet)
router.register('payroll-components', PayrollComponentViewSet)
router.register('employment-documents', EmploymentDocumentViewSet)
router.register('employment-assignments', EmploymentAssignmentViewSet)

urlpatterns = router.urls
