from rest_framework.routers import DefaultRouter

from core.views import AuditLogViewSet
from .views import UserViewSet

router = DefaultRouter()
router.register('users', UserViewSet)
router.register('audit-logs', AuditLogViewSet, basename='audit-log')

urlpatterns = router.urls
