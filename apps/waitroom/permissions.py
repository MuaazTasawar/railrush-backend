from rest_framework.permissions import BasePermission

from . import passes, services


class HasWaitingRoomPass(BasePermission):
    """For seat-hold and booking endpoints: the caller must hold a valid pass for this run.

    The URL must carry a run_id keyword argument, the pass travels in the X-Waitroom-Pass header,
    and the caller must also be signed in (the pass is bound to that user).
    """

    message = "A valid waiting-room pass for this run is required."

    def has_permission(self, request, view):
        user = request.user
        run_id = view.kwargs.get("run_id")
        token = request.META.get("HTTP_X_WAITROOM_PASS", "")
        if not (run_id and token and user and user.is_authenticated):
            return False
        return passes.verify_pass(token, user.id, run_id, services.now())
