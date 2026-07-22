from modeltranslation.translator import register, TranslationOptions
from .models import Category, Article

@register(Category)
class CategoryTranslationOptions(TranslationOptions):
    fields = ('name', 'meta_title', 'meta_description', 'meta_keywords')


@register(Article)
class ArticleTranslationOptions(TranslationOptions):
    fields = ('title', 'body', 'excerpt', 'meta_title', 'meta_desc')
