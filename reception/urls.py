from rest_framework.routers import DefaultRouter

from .views import VisitViewSet

router = DefaultRouter()
router.register('receptions', VisitViewSet, basename='reception')

urlpatterns = router.urls
