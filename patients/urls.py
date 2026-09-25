from rest_framework.routers import DefaultRouter

from .views import PatientNoteViewSet, PatientViewSet

router = DefaultRouter()
router.register('patients', PatientViewSet)
router.register('patient-notes', PatientNoteViewSet)

urlpatterns = router.urls
