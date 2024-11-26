# Generated by Django 5.0.2 on 2024-11-17 12:33

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='TripUpdate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('trip_id', models.CharField(max_length=255)),
                ('route_id', models.CharField(max_length=50)),
                ('vehicle_id', models.CharField(max_length=50)),
                ('stop_sequence', models.DecimalField(decimal_places=0, max_digits=3)),
                ('arrival_time', models.DateTimeField()),
                ('delay', models.DecimalField(decimal_places=0, max_digits=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='VehiclePosition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('trip_id', models.CharField(max_length=50)),
                ('route_id', models.CharField(max_length=50)),
                ('vehicle_id', models.CharField(max_length=50)),
                ('latitude', models.FloatField()),
                ('longitude', models.FloatField()),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]