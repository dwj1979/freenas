# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-03-07 12:21
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('system', '0011_auto_20180219_1615'),
    ]

    operations = [
        migrations.CreateModel(
            name='Filesystem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('identifier', models.CharField(max_length=255, unique=True)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
