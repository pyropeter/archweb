from django.db import models
from django.contrib.auth.models import User

class News(models.Model):
    id = models.AutoField(primary_key=True)
    slug = models.SlugField(max_length=255, unique=True)
    author = models.ForeignKey(User, related_name='news_author')
    postdate = models.DateTimeField("post date", auto_now_add=True, db_index=True)
    last_modified = models.DateTimeField(editable=False,
            auto_now=True, db_index=True)
    title = models.CharField(max_length=255)
    content = models.TextField()

    def get_absolute_url(self):
        return '/news/%s/' % self.slug

    def __unicode__(self):
        return self.title

    class Meta:
        db_table = 'news'
        verbose_name_plural = 'news'
        get_latest_by = 'postdate'
        ordering = ['-postdate']

# connect signals needed to keep cache in line with reality
from main.utils import refresh_news_latest
from django.db.models.signals import post_save
post_save.connect(refresh_news_latest, sender=News,
        dispatch_uid="news.models")

# vim: set ts=4 sw=4 et:
