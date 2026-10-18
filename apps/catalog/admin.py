from django.contrib import admin

from .models import Coach, Fare, Station, Train, TrainRun


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "city")
    search_fields = ("code", "name", "city")


class CoachInline(admin.TabularInline):
    model = Coach
    extra = 0


class FareInline(admin.TabularInline):
    model = Fare
    extra = 0


@admin.register(Train)
class TrainAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "origin", "destination")
    list_select_related = ("origin", "destination")
    search_fields = ("number", "name")
    inlines = [CoachInline, FareInline]


@admin.register(TrainRun)
class TrainRunAdmin(admin.ModelAdmin):
    list_display = ("train", "departure_at", "arrival_at", "booking_opens_at", "status")
    list_select_related = ("train",)
    list_filter = ("status",)
    date_hierarchy = "departure_at"
