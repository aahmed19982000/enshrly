from django.views.generic import ListView, DetailView
from django.utils import timezone
from .models import BlogPost


class BlogListView(ListView):
    model = BlogPost
    template_name = 'blog/list.html'
    context_object_name = 'posts'
    paginate_by = 9

    def get_queryset(self):
        return BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())


class BlogDetailView(DetailView):
    model = BlogPost
    template_name = 'blog/detail.html'
    context_object_name = 'post'

    def get_queryset(self):
        return BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())
