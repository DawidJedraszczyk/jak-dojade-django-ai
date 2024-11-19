from django.views.generic import TemplateView
from django.http import JsonResponse
from geopy.geocoders import Nominatim
from .modules.algorithm_parts.utils import *
from .modules.algorithm_parts.AstarPlanner import *
from django.views import View
import redis
from ebus.settings import REDIS_HOST, REDIS_PORT
from django.conf import settings
import json

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
geolocator = Nominatim(user_agent="ebus")


def load_cities_data():
    with open(settings.CITIES_JSON_PATH, 'r', encoding='utf-8') as file:
        cities_data = json.load(file)
    return cities_data

cities = load_cities_data()

def get_city(request_path):
    for city, data in cities.items():
        if city.lower() in request_path.lower():
            return city.lower()

def get_coords(request_path):
    for city, data in cities.items():
        if city.lower() in request_path.lower():
            coordinates = data.get("center_coordinates")
            return [coordinates["lng"], coordinates["lat"]]

    return None

class BaseView(TemplateView):
    '''
    Simple view which map and searching engine. In this view we can search for bus route and find the best bus
    that we want to use.
    Also in future #TODO there will be a schedule
    '''

    template_name = 'base_view/index.html'

    def get(self, request, *args, **kwargs):

        return super().get(request, *args, **kwargs)

    def get_context_data(self,*args, **kwargs):
        context = super().get_context_data(**kwargs)
        context['city'] = get_city(self.request.path)
        context['center_coordinates'] = get_coords(self.request.path)
        return context


class FindRouteView(View):

    def post(self, request, *args, **kwargs):
        city = request.POST.get('city')
        print(city)
        start_time = time_to_seconds(request.POST.get('time')+":00")

        start = geolocator.geocode(request.POST.get('start_location') + ',' + city +', Polska')
        print(start)
        destination = geolocator.geocode(request.POST.get('goal_location') + ',' + city + ', Polska')
        print(destination)


        planner_straight = AStarPlanner(start_time, (start.latitude, start.longitude), (destination.latitude, destination.longitude), 'manhattan', datetime.date.today())

        for _ in range(20):
            planner_straight.find_next_plan()
        print(planner_straight.found_plans)
        html = planner_straight.plans_to_html()

        coords = {}
        for solution_id in range(len(planner_straight.found_plans)):
            coords[solution_id] = planner_straight.prepare_coords(solution_id)

        details = {}
        for solution_id in range(len(planner_straight.found_plans)):
            details[solution_id] = planner_straight.prepare_departure_details(solution_id, start,destination)


        response_data = {
            'html': html,
            'coords': coords,
            'details': details
        }
        return JsonResponse(response_data)
