from rest_framework.routers import DefaultRouter

from .views import DepartmentViewSet, ServiceViewSet

router = DefaultRouter()
router.register('departments', DepartmentViewSet)
router.register('services', ServiceViewSet)

urlpatterns = router.urls
