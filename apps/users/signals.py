"""
apps/users/signals.py

Auto-vytvoří UserProfile pro každého nového uživatele.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

User = get_user_model()


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        from apps.users.models import UserProfile
        UserProfile.objects.get_or_create(user=instance)
