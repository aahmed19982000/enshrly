from django.shortcuts import render, redirect
from django.contrib import messages
from django.views.generic import TemplateView
from .models import ContactMessage


class AboutView(TemplateView):
    template_name = 'pages/about.html'


class PrivacyView(TemplateView):
    template_name = 'pages/privacy.html'


class TermsView(TemplateView):
    template_name = 'pages/terms.html'


class RefundView(TemplateView):
    template_name = 'pages/refund.html'


def contact_view(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        contact_info = request.POST.get('contact_info', '').strip()
        message_text = request.POST.get('message', '').strip()

        if name and contact_info and message_text:
            ContactMessage.objects.create(name=name, contact_info=contact_info, message=message_text)
            messages.success(request, 'تم إرسال رسالتك بنجاح، سنتواصل معك قريباً.')
            return redirect('pages:contact')
        else:
            messages.error(request, 'يرجى تعبئة جميع الحقول.')

    return render(request, 'pages/contact.html')
