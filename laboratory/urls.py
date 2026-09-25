from rest_framework.routers import DefaultRouter

from .views import LabOrderViewSet, LabTestViewSet

router = DefaultRouter()
router.register('lab-tests', LabTestViewSet)
router.register('lab-orders', LabOrderViewSet)

urlpatterns = router.urls
