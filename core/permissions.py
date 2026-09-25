from rest_framework.permissions import BasePermission, SAFE_METHODS


class PasswordChangeRequired(BasePermission):
    """Restrict temporary-password users until they choose a personal password."""

    message = 'You must change your temporary password before using this endpoint.'
    allowed_actions = {'me', 'change_password', 'logout'}

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not getattr(user, 'must_change_password', False):
            return True
        return (
            getattr(view, 'allow_forced_password_change', False)
            or getattr(view, 'action', None) in self.allowed_actions
        )


class RolePermission(BasePermission):
    """ViewSets declare read_roles/write_roles; superusers always pass."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if not PasswordChangeRequired().has_permission(request, view):
            return False
        if user.is_superuser:
            return True
        action_roles = getattr(view, 'action_roles', {})
        roles = action_roles.get(getattr(view, 'action', None))
        if roles is None:
            roles = getattr(view, 'read_roles', ()) if request.method in SAFE_METHODS else getattr(view, 'write_roles', ())
        return not roles or user.has_role(*roles)


class IsAdministrator(BasePermission):
    def has_permission(self, request, view):
        is_administrator = bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and request.user.has_role('administrator')
        )
        return is_administrator and PasswordChangeRequired().has_permission(request, view)
