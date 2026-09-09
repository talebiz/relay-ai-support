"""
Auth-related views.

Login/refresh/logout are handled by djangorestframework-simplejwt's own
views (wired in core/urls.py) - there is no need to reinvent token
issuance. The only custom view here is `me`, which lets the React app
ask "who is currently logged in" without decoding the JWT client-side.
"""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import MeSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user).data)
