from django.shortcuts import render
from django.db import connection

def test_view(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT version();")
        db_version = cursor.fetchone()[0]
    
    context = {
        'db_version': db_version
    }
    return render(request, 'test.html', context)


from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response


class ProductPolicyView(APIView):
    def get(self, request):
        names = ['PLAN_MAX_GOALS_PER_PROPOSAL', 'PLAN_MAX_TASKS_PER_PROPOSAL', 'PLAN_NEAR_TERM_DAYS',
                 'MIN_SESSION_MINUTES', 'MAX_SESSION_MINUTES', 'SCHEDULE_MAX_RANGE_DAYS', 'PROPOSAL_TTL_SECONDS']
        return Response({name.lower(): getattr(settings, name) for name in names})
