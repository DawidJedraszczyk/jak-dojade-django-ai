from django.contrib import admin
from django.urls import path, include
from .views import *

app_name='route_search'
urlpatterns = [
    path('<str:city>/', BaseView.as_view(), name='BaseView'),
    path('algorithm/find-route/', FindRouteView.as_view(), name='FindRoute'),
]
